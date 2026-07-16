"""Layer 2 of the App-of-Apps model: render and seed the platform-gitops repo.

Phase 1 of docs/app-of-apps-design.md: every Bucket-A service the CLI installs
imperatively gets an Argo CD Application whose Helm values are rendered from
the *same* Jinja templates the CLI uses, so Argo CD adoption is a no-op diff.

The rendered repo is pushed to `platform-team/platform-gitops` on the
platform's own Forgejo (private — the values currently embed generated
secrets; replaced by pinned k8s Secrets in Phase 3). The root Application is
applied directly with kubectl; Argo CD reconciles everything below it.
"""
import base64
import json
import re
import time

import click
import paramiko
import yaml

from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR
from bootstrapper.services import analytics as analytics_module
from bootstrapper.services import authentik as authentik_module
from bootstrapper.services.forgejo import CI_RUNNER_IMAGE
from bootstrapper.services.headlamp import _role_for

_FORGEJO_BASE = "http://forgejo-http.forgejo.svc.cluster.local:3000"
GITOPS_REPO = "platform-gitops"
GITOPS_ORG = "platform-team"

# Chart sources, pinned to the versions the baseline (Phase 0) installed.
# Bump deliberately (invariant A.13), never float.
CHART_VERSIONS = {
    "cert-manager": ("https://charts.jetstack.io", "cert-manager", "v1.21.0"),
    # OCI registry: no scheme; needs the forgejo-helm-oci repository Secret.
    "forgejo": ("code.forgejo.org/forgejo-helm", "forgejo", "17.1.3"),
    "authentik": ("https://charts.goauthentik.io", "authentik", "2026.5.5"),
    "argocd": ("https://argoproj.github.io/argo-helm", "argo-cd", "10.1.4"),
    "headlamp": ("https://kubernetes-sigs.github.io/headlamp/", "headlamp", "0.43.0"),
    "umami": ("https://charts.christianhuth.de", "umami", "7.10.11"),
}

# Live argocd-cm/argocd-rbac-cm carry state Argo CD must not own (yet):
# argocd-rbac-cm is rewritten by the provision-team workflow on every team
# change, and argocd-secret holds runtime-generated keys plus the CLI-applied
# OIDC client secret. Managed in Phase 3/4; ignored until then.
_ARGOCD_IGNORE = (
    "- kind: ConfigMap\n"
    "  name: argocd-rbac-cm\n"
    "  namespace: argocd\n"
    "  jsonPointers:\n"
    "    - /data\n"
    "- kind: Secret\n"
    "  name: argocd-secret\n"
    "  namespace: argocd\n"
    "  jsonPointers:\n"
    "    - /data\n"
)

# The Bitnami postgres subchart re-generates postgres-password (the disabled
# superuser) on every `helm template` — nondeterministic renders would flap
# OutOfSync forever. The live value, created at install, is the real one.
_AUTHENTIK_IGNORE = (
    "- kind: Secret\n"
    "  name: authentik-postgresql\n"
    "  namespace: authentik\n"
    "  jsonPointers:\n"
    "    - /data/postgres-password\n"
)


def _remote_yaml(ssh: paramiko.SSHClient, path: str) -> dict:
    """Read and parse a YAML file the provision run left on the server."""
    out = ssh_utils.run(ssh, f"cat {path}")
    return yaml.safe_load(out)


def resolve_inputs(ssh: paramiko.SSHClient, cfg: dict, state: dict) -> dict:
    """Collect the render inputs that are not directly in config/state.

    Newer provision runs persist these in state; for a box provisioned before
    that, fall back to the values files the CLI uploaded to /opt/bootstrapper.
    Inputs that exist nowhere yet (mid-provision) resolve to None; apps that
    need them are simply not rendered until they are available.
    """
    gen = state.get('generated_secrets', {})
    inputs = {}

    inputs["forgejo_version"] = state.get("forgejo_version") or (
        _remote_yaml(ssh, f"{DEPLOY_DIR}/helm-values-forgejo.yaml")["image"]["tag"]
    )
    # Pinned OIDC clients (Phase 3+): ids are constants, secrets are generated
    # once into state and set on the Authentik providers by blueprint.
    if 'k8s_oidc_client_secret' in gen:
        inputs["k8s_client_id"] = authentik_module.OIDC_CLIENT_IDS['kubernetes']
        inputs["k8s_client_secret"] = gen['k8s_oidc_client_secret']
        inputs["argocd_client_id"] = authentik_module.OIDC_CLIENT_IDS['argocd']
        return inputs

    # Pre-blueprint boxes: fall back to state, then to the values files the
    # old provision flow uploaded to /opt/bootstrapper.
    inputs["k8s_client_id"] = state.get("k8s_client_id")
    inputs["k8s_client_secret"] = state.get("k8s_client_secret")
    if cfg.get("headlamp_domain") and not inputs["k8s_client_secret"]:
        try:
            oidc = _remote_yaml(ssh, f"{DEPLOY_DIR}/helm-values-headlamp.yaml")["config"]["oidc"]
            inputs["k8s_client_id"] = oidc["clientID"]
            inputs["k8s_client_secret"] = oidc["clientSecret"]
        except RuntimeError:
            pass
    inputs["argocd_client_id"] = state.get("argocd_client_id")
    if not inputs["argocd_client_id"]:
        try:
            patch = json.loads(ssh_utils.run(ssh, f"cat {DEPLOY_DIR}/argocd-cm-patch.json"))
            m = re.search(r"clientID: (\S+)", patch["data"]["oidc.config"])
            inputs["argocd_client_id"] = m.group(1)
        except RuntimeError:
            pass
    return inputs


def _helm_app(name: str, namespace: str, values: str, *,
              prune: bool = True,
              extra_sync_options: list | None = None,
              ignore_differences: str | None = None) -> str:
    chart_repo, chart, revision = CHART_VERSIONS[name]
    return manifests.render(
        'gitops/app-helm.yaml.j2',
        name=name, release_name=name, namespace=namespace,
        chart_repo=chart_repo, chart=chart, revision=revision,
        values=values, prune=prune,
        extra_sync_options=extra_sync_options or [],
        ignore_differences=ignore_differences,
    )


def _manifest_app(name: str, namespace: str, repo_url: str) -> str:
    return manifests.render(
        'gitops/app-manifests.yaml.j2',
        name=name, namespace=namespace, repo_url=repo_url,
        path=f"manifests/{name}",
    )


# Apps that can be applied before Forgejo (and thus the gitops repo) exists:
# chart-based, inline values, no late-bound wiring inputs.
BOOTSTRAP_APPS = ("cert-manager", "forgejo", "authentik")


def build_files(cfg: dict, state: dict, inputs: dict, only: set | None = None) -> dict:
    """Render the platform-gitops tree as {relative_path: content}.

    `only` limits rendering to the named apps (mid-provision, before all
    wiring inputs exist). With only=None, everything configured is rendered
    and missing inputs are an error — the seeded repo must be complete.
    """
    forgejo_cfg = cfg['forgejo']
    authentik_cfg = cfg['authentik']
    gen = state['generated_secrets']
    cluster_issuer = 'selfsigned' if cfg['provider'] == 'local' else 'letsencrypt-prod'
    repo_url = f"https://{forgejo_cfg['domain']}/{GITOPS_ORG}/{GITOPS_REPO}.git"

    def want(name):
        return only is None or name in only

    files = {}

    # ---- Helm-backed Applications (values identical to the CLI installs) ----
    if want('cert-manager'):
        files['apps/cert-manager.yaml'] = _helm_app(
            'cert-manager', 'cert-manager',
            yaml.dump({"crds": {"enabled": True}}, default_flow_style=False),
            extra_sync_options=["ServerSideApply=true"],  # CRDs exceed CSA limits
        )
    if want('forgejo'):
        files['apps/forgejo.yaml'] = _helm_app(
            'forgejo', 'forgejo',
            manifests.render(
                'helm/forgejo-values.yaml.j2',
                domain=forgejo_cfg['domain'],
                admin_username=forgejo_cfg['admin_username'],
                admin_password=forgejo_cfg['admin_password'],
                admin_email=forgejo_cfg['email'],
                version=inputs['forgejo_version'],
                cluster_issuer=cluster_issuer,
            ),
        )
    if want('authentik'):
        files['apps/authentik.yaml'] = _helm_app(
            'authentik', 'authentik',
            manifests.render(
                'helm/authentik-values.yaml.j2',
                domain=authentik_cfg['domain'],
                secret_key=gen['authentik_secret_key'],
                admin_password=authentik_cfg['admin_password'],
                admin_email=authentik_cfg['email'],
                bootstrap_token=gen['authentik_bootstrap_token'],
                db_password=gen['authentik_db_password'],
                cluster_issuer=cluster_issuer,
            ),
            extra_sync_options=["RespectIgnoreDifferences=true"],
            ignore_differences=_AUTHENTIK_IGNORE,
        )
    if want('argocd'):
        argocd_values = manifests.render(
            'helm/argocd-values.yaml.j2',
            argocd_domain=cfg['argocd_domain'],
            cluster_issuer=cluster_issuer,
        )
        # The CLI patches url + oidc.config into argocd-cm post-install; in git
        # they are ordinary chart values (must stay identical to _patch_argocd_cm).
        argocd_values += (
            f"\nconfigs:\n"
            f"  cm:\n"
            f"    url: https://{cfg['argocd_domain']}\n"
            f"    oidc.config: |\n"
            f"      name: Authentik\n"
            f"      issuer: https://{authentik_cfg['domain']}/application/o/argocd/\n"
            f"      clientID: {inputs['argocd_client_id']}\n"
            f"      clientSecret: $oidc.authentik.clientSecret\n"
            f"      requestedScopes:\n"
            f"        - openid\n"
            f"        - profile\n"
            f"        - email\n"
            f"        - groups\n"
        )
        files['apps/argocd.yaml'] = _helm_app(
            'argocd', 'argocd', argocd_values,
            # prune stays off: helm-hook leftovers (redis-secret-init) show as
            # requiresPruning; pruning the app managing Argo CD itself is not
            # worth that risk. See design doc Appendix C.
            prune=False,
            extra_sync_options=["RespectIgnoreDifferences=true"],
            ignore_differences=_ARGOCD_IGNORE,
        )
    if want('headlamp') and cfg.get('headlamp_domain'):
        files['apps/headlamp.yaml'] = _helm_app(
            'headlamp', 'headlamp',
            manifests.render(
                'helm/headlamp-values.yaml.j2',
                headlamp_domain=cfg['headlamp_domain'],
                authentik_domain=authentik_cfg['domain'],
                client_id=inputs['k8s_client_id'],
                client_secret=inputs['k8s_client_secret'],
                cluster_issuer=cluster_issuer,
            ),
        )
    if want('umami') and cfg.get('analytics_domain'):
        files['apps/umami.yaml'] = _helm_app(
            'umami', 'analytics',
            manifests.render(
                'helm/umami-values.yaml.j2',
                analytics_domain=cfg['analytics_domain'],
                db_host=analytics_module.DB_HOST,
                db_name=analytics_module.DB_NAME,
                db_user=analytics_module.DB_USER,
                db_password=gen['umami_db_password'],
                app_secret=gen['umami_app_secret'],
                cluster_issuer=cluster_issuer,
            ),
        )

    # ---- Plain-manifest Applications ----
    if want('cluster-issuer'):
        files['manifests/cluster-issuer/cluster-issuer.yaml'] = manifests.render(
            'k8s/cluster-issuer.yaml.j2',
            issuer_name=cluster_issuer, email=authentik_cfg['email'],
        )
        files['apps/cluster-issuer.yaml'] = _manifest_app(
            'cluster-issuer', 'cert-manager', repo_url)

    if want('authentik-blueprint'):
        files['manifests/authentik-blueprint/blueprint.yaml'] = (
            authentik_module.render_blueprint(cfg))
        files['apps/authentik-blueprint.yaml'] = _manifest_app(
            'authentik-blueprint', 'authentik', repo_url)

    if want('headlamp-rbac'):
        groups = authentik_cfg.get('groups', [])
        admin_group = authentik_cfg.get('admin_group', 'forgejo-admins')
        bindings = [
            {"name": re.sub(r'[^a-z0-9-]', '-', g.lower()),
             "group": g, "role": _role_for(g, admin_group)}
            for g in groups
        ]
        files['manifests/headlamp-rbac/oidc-rbac.yaml'] = manifests.render(
            'k8s/oidc-rbac.yaml.j2', bindings=bindings)
        files['apps/headlamp-rbac.yaml'] = _manifest_app(
            'headlamp-rbac', 'headlamp', repo_url)

    if want('portal-redirect') and cfg.get('portal_domain'):
        files['manifests/portal-redirect/portal-redirect.yaml'] = manifests.render(
            'k8s/portal-redirect.yaml.j2',
            portal_domain=cfg['portal_domain'],
            authentik_domain=authentik_cfg['domain'],
            cluster_issuer=cluster_issuer,
        )
        files['apps/portal-redirect.yaml'] = _manifest_app(
            'portal-redirect', 'authentik', repo_url)

    if want('runner'):
        files['manifests/runner/runner.yaml'] = manifests.render(
            'k8s/runner.yaml.j2',
            runner_token=state['runner_token'],
            forgejo_url=f"https://{forgejo_cfg['domain']}",
            forgejo_domain=forgejo_cfg['domain'],
            ci_runner_image=CI_RUNNER_IMAGE,
        )
        files['apps/runner.yaml'] = _manifest_app('runner', 'kube-system', repo_url)

    if only is None:
        files['README.md'] = manifests.render(
            'gitops/README.md.j2',
            chart_table=[
                {"name": n, "chart": f"{repo}/{chart}", "revision": rev}
                for n, (repo, chart, rev) in CHART_VERSIONS.items()
            ],
        )
    return files


def apply_apps(ssh: paramiko.SSHClient, cfg: dict, state: dict, inputs: dict,
               names: set) -> None:
    """kubectl-apply the named child Applications directly (pre-git bootstrap)."""
    files = build_files(cfg, state, inputs, only=names)
    ssh_utils.run(ssh, f"mkdir -p {DEPLOY_DIR}/gitops-apps")
    for path, content in files.items():
        if not path.startswith('apps/'):
            continue
        fname = path.split('/', 1)[1]
        ssh_utils.upload(ssh, content, f"{DEPLOY_DIR}/gitops-apps/{fname}")
        ssh_utils.run(ssh, f"k3s kubectl apply -f {DEPLOY_DIR}/gitops-apps/{fname}")
        click.echo(f"  Applied Application {fname[:-5]}.")


def wait_for_app(ssh: paramiko.SSHClient, name: str, timeout: int = 900,
                 interval: int = 10) -> None:
    """Wait until an Argo CD Application reports Synced + Healthy."""
    click.echo(f"  Waiting for application {name} to be Synced/Healthy...")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            out = ssh_utils.run(
                ssh,
                f"k3s kubectl get application {name} -n argocd -o "
                f"jsonpath='{{.status.sync.status}}/{{.status.health.status}}' 2>/dev/null || true",
            ).strip()
        except RuntimeError:
            out = ""
        if out == "Synced/Healthy":
            click.echo(f"  {name}: Synced/Healthy.")
            return
        if out != last:
            click.echo(f"    {name}: {out or 'pending'}")
            last = out
        time.sleep(interval)
    raise TimeoutError(f"Application {name} not Synced/Healthy within {timeout}s (last: {last})")


def _upsert_file(ssh, headers, path: str, content: str) -> str:
    """Create or update one file via the Forgejo contents API; returns action."""
    base = f"{_FORGEJO_BASE}/api/v1/repos/{GITOPS_ORG}/{GITOPS_REPO}/contents/{path}"
    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')

    r = ssh_utils.cluster_curl(ssh, f"{base}?ref=main", headers=headers)
    if r.status_code == 404:
        r = ssh_utils.cluster_curl(
            ssh, base, method='POST', headers=headers,
            json_body={"message": f"gitops: add {path}", "content": encoded},
        )
        r.raise_for_status()
        return "created"
    r.raise_for_status()
    existing = r.json()
    if existing.get("content", "").replace("\n", "") == encoded:
        return "unchanged"
    r = ssh_utils.cluster_curl(
        ssh, base, method='PUT', headers=headers,
        json_body={"message": f"gitops: update {path}",
                   "content": encoded, "sha": existing["sha"]},
    )
    r.raise_for_status()
    return "updated"


def apply_oci_repo_secret(ssh: paramiko.SSHClient) -> None:
    """Register the Forgejo Helm OCI registry with Argo CD (idempotent)."""
    ssh_utils.run(ssh, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(ssh, manifests.render('k8s/argocd-oci-repo.yaml.j2'),
                     f"{DEPLOY_DIR}/argocd-oci-repo.yaml")
    ssh_utils.run(ssh, f"k3s kubectl apply -f {DEPLOY_DIR}/argocd-oci-repo.yaml")


def seed_gitops(ssh: paramiko.SSHClient, cfg: dict, state: dict) -> None:
    """Create/refresh the platform-gitops repo and apply the root Application."""
    api_token = state['forgejo_api_token']
    headers = {"Authorization": f"token {api_token}", "Content-Type": "application/json"}
    forgejo_domain = cfg['forgejo']['domain']
    repo_url = f"https://{forgejo_domain}/{GITOPS_ORG}/{GITOPS_REPO}.git"

    click.echo("  Resolving render inputs...")
    inputs = resolve_inputs(ssh, cfg, state)

    click.echo(f"  Ensuring {GITOPS_ORG}/{GITOPS_REPO} repo exists...")
    r = ssh_utils.cluster_curl(
        ssh, f"{_FORGEJO_BASE}/api/v1/orgs/{GITOPS_ORG}/repos",
        method='POST', headers=headers,
        json_body={
            "name": GITOPS_REPO,
            "description": "Desired state of the platform itself (App-of-Apps Layer 2)",
            "private": True,
            "auto_init": True,
            "default_branch": "main",
        },
    )
    if r.status_code not in (201, 409):
        raise RuntimeError(f"Failed to create {GITOPS_REPO} repo ({r.status_code}): {r.text}")

    files = build_files(cfg, state, inputs)
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for path, content in sorted(files.items()):
        counts[_upsert_file(ssh, headers, path, content)] += 1
    click.echo(f"  Seeded {len(files)} file(s): "
               f"{counts['created']} created, {counts['updated']} updated, "
               f"{counts['unchanged']} unchanged.")

    apply_oci_repo_secret(ssh)

    root = manifests.render('gitops/root-app.yaml.j2', repo_url=repo_url)
    ssh_utils.upload(ssh, root, f"{DEPLOY_DIR}/platform-root-app.yaml")
    ssh_utils.run(ssh, f"k3s kubectl apply -f {DEPLOY_DIR}/platform-root-app.yaml")
    click.echo("  Root Application applied (platform-root).")
