import json
import time
import click
import paramiko

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import ssh as ssh_utils

KUBECONFIG = '/etc/rancher/k3s/k3s.yaml'
MANIFESTS_DIR = '/var/lib/rancher/k3s/server/manifests'
DEPLOY_DIR = '/opt/bootstrapper'


def install_k3s(client: paramiko.SSHClient) -> None:
    """Install k3s with Traefik configured to terminate TLS on host ports 80/443.

    The Traefik HelmChartConfig is uploaded to the k3s manifests directory before
    installation so the Helm controller applies it on first boot.
    HTTP traffic is automatically redirected to HTTPS.
    """
    click.echo("  Installing k3s...")

    # Upload Traefik config before k3s starts so Helm controller picks it up immediately.
    # Traefik uses host ports 80/443 by default in k3s — we just add HTTP→HTTPS redirect.
    traefik_config = """\
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ports:
      web:
        redirectTo:
          port: websecure
"""
    ssh_utils.run(client, f"mkdir -p {MANIFESTS_DIR}")
    ssh_utils.upload(client, traefik_config, f"{MANIFESTS_DIR}/traefik-config.yaml")

    # Ensure no stale config (e.g. OIDC flags from a previous failed run)
    ssh_utils.run(client, "mkdir -p /etc/rancher/k3s && truncate -s 0 /etc/rancher/k3s/config.yaml")

    ssh_utils.run(client, "curl -sfL https://get.k3s.io | sh -")
    click.echo("  k3s installed. Waiting for node to become Ready...")

    _wait_for_k3s(client)
    click.echo("  k3s is Ready.")


def _wait_for_k3s(client: paramiko.SSHClient, timeout: int = 120, interval: int = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = ssh_utils.run(client, "k3s kubectl get nodes --no-headers 2>/dev/null")
            if "Ready" in out:
                return
        except RuntimeError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"k3s node did not become Ready within {timeout}s")


_TLS_SECRETS = [
    ("forgejo", "forgejo-tls"),
    ("authentik", "authentik-tls"),
    ("argocd", "argocd-server-tls"),
]


def save_tls_secrets(client: paramiko.SSHClient) -> dict:
    """Extract TLS secrets from the cluster for backup in the state file.

    Returns a dict of {secret_name: {namespace, crt, key}} for any secrets that
    currently exist and contain valid cert data. Silently skips missing ones.
    """
    saved = {}
    for ns, name in _TLS_SECRETS:
        out = ssh_utils.run(
            client,
            f"k3s kubectl get secret {name} -n {ns} -o jsonpath='{{.data}}' 2>/dev/null || true",
        )
        out = out.strip()
        if not out:
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        if "tls.crt" in data and "tls.key" in data:
            saved[name] = {"namespace": ns, "crt": data["tls.crt"], "key": data["tls.key"]}
    if saved:
        click.echo(f"  Saved {len(saved)} TLS secret(s) to state: {', '.join(saved)}")
    return saved


def restore_tls_secrets(client: paramiko.SSHClient, saved: dict) -> None:
    """Pre-populate TLS secrets before Helm installs so cert-manager skips ACME.

    cert-manager checks whether the referenced Secret already contains a valid,
    non-expiring certificate. If it does, it marks the Certificate Ready without
    requesting a new one from Let's Encrypt.
    """
    if not saved:
        return
    click.echo(f"  Restoring {len(saved)} saved TLS secret(s) — ACME skipped for these domains.")
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    for name, data in saved.items():
        ns = data["namespace"]
        ssh_utils.run(
            client,
            f"k3s kubectl create namespace {ns} --dry-run=client -o yaml | k3s kubectl apply -f -",
        )
        secret_yaml = (
            f"apiVersion: v1\nkind: Secret\ntype: kubernetes.io/tls\n"
            f"metadata:\n  name: {name}\n  namespace: {ns}\n"
            f"data:\n  tls.crt: {data['crt']}\n  tls.key: {data['key']}\n"
        )
        path = f"{DEPLOY_DIR}/{name}.yaml"
        ssh_utils.upload(client, secret_yaml, path)
        ssh_utils.run(client, f"k3s kubectl apply -f {path}")
        click.echo(f"    {name} ({ns}) restored.")


def install_cert_manager(client: paramiko.SSHClient, admin_email: str) -> str:
    """Install cert-manager and a Let's Encrypt prod ClusterIssuer.

    Returns the ClusterIssuer name to use in Helm ingress annotations.
    """
    click.echo("  Installing cert-manager...")
    helm_module.add_repo(client, "jetstack", "https://charts.jetstack.io")
    helm_module.upgrade_install(
        client, "cert-manager", "jetstack/cert-manager", "cert-manager",
        {"crds": {"enabled": True}},
    )

    issuer_name = "letsencrypt-prod"
    click.echo("  Creating Let's Encrypt ClusterIssuer...")
    cluster_issuer_yaml = f"""\
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: {issuer_name}
spec:
  acme:
    email: {admin_email}
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: {issuer_name}-key
    solvers:
      - http01:
          ingress:
            ingressClassName: traefik
"""
    issuer_path = f"{DEPLOY_DIR}/cluster-issuer.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, cluster_issuer_yaml, issuer_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {issuer_path}")
    click.echo("  cert-manager ready.")
    return issuer_name


def install_forgejo(
    client: paramiko.SSHClient,
    domain: str,
    admin_username: str,
    admin_password: str,
    admin_email: str,
    version: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Install Forgejo via Helm with Traefik Ingress and cert-manager TLS."""
    click.echo("  Installing Forgejo via Helm...")
    # OCI chart — no helm repo add needed
    values = {
        "gitea": {  # official Forgejo chart uses 'gitea' key for backward compat
            "admin": {
                "username": admin_username,
                "password": admin_password,
                "email": admin_email,
            },
            "config": {
                "server": {
                    "DOMAIN": domain,
                    "ROOT_URL": f"https://{domain}",
                    "SSH_DOMAIN": domain,
                    "SSH_PORT": "2222",
                    "SSH_LISTEN_PORT": "22",
                },
                "security": {
                    "INSTALL_LOCK": "true",
                },
            },
        },
        "image": {
            "tag": version,
        },
        "ingress": {
            "enabled": True,
            "className": "traefik",
            "annotations": {
                "cert-manager.io/cluster-issuer": cluster_issuer,
                "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                "traefik.ingress.kubernetes.io/router.tls": "true",
            },
            "hosts": [
                {
                    "host": domain,
                    "paths": [{"path": "/", "pathType": "Prefix"}],
                }
            ],
            "tls": [{"secretName": "forgejo-tls", "hosts": [domain]}],
        },
        "service": {
            "ssh": {
                "type": "LoadBalancer",
                "port": 2222,
            },
        },
        "persistence": {"enabled": True, "size": "10Gi"},
    }

    helm_module.upgrade_install(client, "forgejo", "oci://code.forgejo.org/forgejo-helm/forgejo", "forgejo", values)
    click.echo("  Forgejo installed.")


def install_authentik(
    client: paramiko.SSHClient,
    domain: str,
    secret_key: str,
    bootstrap_token: str,
    admin_password: str,
    admin_email: str,
    db_password: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Install Authentik via Helm with Traefik Ingress, cert-manager TLS, and embedded DB/Redis."""
    click.echo("  Installing Authentik via Helm...")
    helm_module.add_repo(client, "authentik", "https://charts.goauthentik.io")

    values = {
        "global": {
            "env": [{"name": "AUTHENTIK_HOST", "value": f"https://{domain}"}],
        },
        "authentik": {
            "secret_key": secret_key,
            "bootstrap_password": admin_password,
            "bootstrap_email": admin_email,
            "bootstrap_token": bootstrap_token,
            "postgresql": {
                "password": db_password,
            },
        },
        "server": {
            "ingress": {
                "enabled": True,
                "ingressClassName": "traefik",
                "annotations": {
                    "cert-manager.io/cluster-issuer": cluster_issuer,
                    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                },
                "hosts": [domain],
                "tls": [{"secretName": "authentik-tls", "hosts": [domain]}],
            },
        },
        "postgresql": {
            "enabled": True,
            "auth": {
                "password": db_password,
                "database": "authentik",
                "username": "authentik",
            },
        },
        "redis": {
            "enabled": True,
        },
    }

    helm_module.upgrade_install(client, "authentik", "authentik/authentik", "authentik", values)
    click.echo("  Authentik installed.")


def configure_k3s_oidc(
    client: paramiko.SSHClient,
    bootstrap_token: str,
) -> str:
    """Create Authentik OIDC provider for k8s.

    kube-apiserver requires HTTPS for the OIDC issuer URL, so we cannot
    configure k3s during bootstrap (DNS/TLS not live yet). The provider is
    created here so the client_id is stable. After DNS propagates:

      echo 'kube-apiserver-arg:
        - oidc-issuer-url=https://<authentik_domain>/application/o/kubernetes/
        - oidc-client-id=kubernetes
        - oidc-username-claim=email
        - oidc-groups-claim=groups' >> /etc/rancher/k3s/config.yaml
      /usr/local/bin/k3s-killall.sh && systemctl start k3s

    Returns the client_id of the kubernetes OIDC provider.
    """
    click.echo("  Creating Authentik OIDC provider for Kubernetes...")
    client_id = _create_authentik_k8s_provider(client, bootstrap_token)
    click.echo("  Authentik k8s OIDC provider created (k3s wired after DNS/TLS is live).")
    return client_id


def _create_authentik_k8s_provider(
    ssh: paramiko.SSHClient,
    bootstrap_token: str,
) -> str:
    """Create an Authentik OAuth2 provider + application named 'kubernetes'. Returns client_id."""
    from bootstrapper.services.authentik import (
        _get_default_authorization_flow,
        _get_default_invalidation_flow,
        _get_default_signing_key,
        _get_scope_mappings,
        _get_or_create_groups_scope_mapping,
    )

    base = "http://authentik-server.authentik.svc.cluster.local/api/v3"
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/?name=kubernetes", headers=headers)
    r.raise_for_status()
    results = r.json().get("results", [])

    scope_pks = _get_scope_mappings(ssh, base, headers, ["openid", "email", "profile"])
    scope_pks.append(_get_or_create_groups_scope_mapping(ssh, base, headers))

    payload = {
        "name": "kubernetes",
        "authorization_flow": _get_default_authorization_flow(ssh, base, headers),
        "invalidation_flow": _get_default_invalidation_flow(ssh, base, headers),
        "signing_key": _get_default_signing_key(ssh, base, headers),
        "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True,
        "property_mappings": scope_pks,
        "redirect_uris": [{"matching_mode": "strict", "url": "http://localhost:8000"}],
    }

    if results:
        pk = results[0]["pk"]
        r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/{pk}/", method='PATCH', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"k8s provider update failed ({r.status_code}): {r.text}")
        provider = r.json()
        click.echo("  Updated existing Authentik k8s OIDC provider.")
    else:
        r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/", method='POST', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"k8s provider creation failed ({r.status_code}): {r.text}")
        provider = r.json()
        click.echo("  Created Authentik k8s OIDC provider.")

    provider_pk = provider["pk"]
    client_id = provider["client_id"]

    r = ssh_utils.cluster_curl(ssh, f"{base}/core/applications/?slug=kubernetes", headers=headers)
    r.raise_for_status()
    if not r.json().get("results"):
        r = ssh_utils.cluster_curl(
            ssh, f"{base}/core/applications/", method='POST', headers=headers,
            json_body={"name": "Kubernetes", "slug": "kubernetes", "provider": provider_pk},
        )
        if not r.ok:
            raise RuntimeError(f"k8s application creation failed ({r.status_code}): {r.text}")
        click.echo("  Created Authentik k8s application.")

    return client_id


def install_argocd(
    client: paramiko.SSHClient,
    argocd_domain: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Install Argo CD via Helm with Traefik Ingress and cert-manager TLS.

    Runs in --insecure mode (no internal TLS); Traefik terminates HTTPS.
    """
    click.echo("  Installing Argo CD via Helm...")
    helm_module.add_repo(client, "argo", "https://argoproj.github.io/argo-helm")

    values = {
        "server": {
            "extraArgs": ["--insecure"],
            "ingress": {
                "enabled": True,
                "ingressClassName": "traefik",
                "annotations": {
                    "cert-manager.io/cluster-issuer": cluster_issuer,
                    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                },
                "hostname": argocd_domain,
                "tls": True,
                "servicePort": 80,
            },
        },
    }

    helm_module.upgrade_install(client, "argocd", "argo/argo-cd", "argocd", values)
    click.echo("  Argo CD installed.")


def configure_argocd_sso(
    client: paramiko.SSHClient,
    bootstrap_token: str,
    authentik_domain: str,
    argocd_domain: str,
    forgejo_domain: str,
    forgejo_admin_username: str,
    forgejo_api_token: str,
) -> None:
    """Configure Argo CD SSO via Authentik OIDC.

    Creates an Authentik OAuth2 provider + application for Argo CD, then
    patches argocd-cm with the OIDC config and argocd-rbac-cm with the
    initial team RBAC policy. Also seeds the platform-forgejo-token and
    forgejo-repo-creds Secrets.
    """
    click.echo("  Configuring Argo CD SSO with Authentik...")
    client_id, client_secret = _create_authentik_argocd_provider(
        client, bootstrap_token, argocd_domain,
    )
    _apply_argocd_sso_secrets(client, client_secret, forgejo_domain, forgejo_admin_username, forgejo_api_token)
    _patch_argocd_cm(client, authentik_domain, argocd_domain, client_id)
    _patch_argocd_rbac_cm(client)
    click.echo("  Argo CD SSO configured.")


def _create_authentik_argocd_provider(
    ssh: paramiko.SSHClient,
    bootstrap_token: str,
    argocd_domain: str,
) -> tuple[str, str]:
    """Create or update the Authentik OAuth2 provider + application for Argo CD.

    Returns (client_id, client_secret).
    """
    from bootstrapper.services.authentik import (
        _get_default_authorization_flow,
        _get_default_invalidation_flow,
        _get_default_signing_key,
        _get_scope_mappings,
        _get_or_create_groups_scope_mapping,
    )

    base = "http://authentik-server.authentik.svc.cluster.local/api/v3"
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    scope_pks = _get_scope_mappings(ssh, base, headers, ["openid", "email", "profile"])
    scope_pks.append(_get_or_create_groups_scope_mapping(ssh, base, headers))

    payload = {
        "name": "argocd",
        "authorization_flow": _get_default_authorization_flow(ssh, base, headers),
        "invalidation_flow": _get_default_invalidation_flow(ssh, base, headers),
        "signing_key": _get_default_signing_key(ssh, base, headers),
        "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True,
        "property_mappings": scope_pks,
        "redirect_uris": [
            {"matching_mode": "strict", "url": f"https://{argocd_domain}/auth/callback"},
        ],
    }

    r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/?name=argocd", headers=headers)
    r.raise_for_status()
    results = r.json().get("results", [])

    if results:
        pk = results[0]["pk"]
        r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/{pk}/", method='PATCH', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"Argo CD provider update failed ({r.status_code}): {r.text}")
        provider = r.json()
        click.echo("  Updated existing Authentik provider for Argo CD.")
    else:
        r = ssh_utils.cluster_curl(ssh, f"{base}/providers/oauth2/", method='POST', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"Argo CD provider creation failed ({r.status_code}): {r.text}")
        provider = r.json()
        click.echo("  Created Authentik provider for Argo CD.")

    provider_pk = provider["pk"]
    client_id = provider["client_id"]
    client_secret = provider["client_secret"]

    r = ssh_utils.cluster_curl(ssh, f"{base}/core/applications/?slug=argocd", headers=headers)
    r.raise_for_status()
    if not r.json().get("results"):
        r = ssh_utils.cluster_curl(
            ssh, f"{base}/core/applications/", method='POST', headers=headers,
            json_body={"name": "Argo CD", "slug": "argocd", "provider": provider_pk},
        )
        if not r.ok:
            raise RuntimeError(f"Argo CD application creation failed ({r.status_code}): {r.text}")
        click.echo("  Created Authentik application for Argo CD.")

    return client_id, client_secret


def _apply_argocd_sso_secrets(
    client: paramiko.SSHClient,
    oidc_client_secret: str,
    forgejo_domain: str,
    forgejo_admin_username: str,
    forgejo_api_token: str,
) -> None:
    """Apply the three Secrets Argo CD needs for SSO and SCM discovery."""
    # The $oidc.authentik.clientSecret reference in argocd-cm resolves from argocd-secret,
    # not from a custom secret. Patch argocd-secret directly so Argo CD can read it.
    manifest = f"""\
apiVersion: v1
kind: Secret
metadata:
  name: argocd-secret
  namespace: argocd
stringData:
  oidc.authentik.clientSecret: {oidc_client_secret}
---
apiVersion: v1
kind: Secret
metadata:
  name: platform-forgejo-token
  namespace: argocd
stringData:
  token: {forgejo_api_token}
---
apiVersion: v1
kind: Secret
metadata:
  name: forgejo-repo-creds
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repo-creds
stringData:
  type: git
  url: https://{forgejo_domain}/
  username: {forgejo_admin_username}
  password: {forgejo_api_token}
"""
    remote_path = f"{DEPLOY_DIR}/argocd-sso-secrets.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, manifest, remote_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {remote_path}")
    click.echo("  Applied Argo CD SSO secrets.")


def _patch_argocd_cm(client: paramiko.SSHClient, authentik_domain: str, argocd_domain: str, client_id: str) -> None:
    """Patch argocd-cm with the external URL and OIDC config block.

    The 'url' field is required when running in --insecure mode behind a TLS-terminating
    proxy (Traefik). Without it Argo CD cannot determine its own scheme and constructs
    the OIDC callback as http://, which Authentik then rejects as a redirect URI mismatch.
    """
    oidc_config = (
        f"name: Authentik\n"
        f"issuer: https://{authentik_domain}/application/o/argocd/\n"
        f"clientID: {client_id}\n"
        f"clientSecret: $oidc.authentik.clientSecret\n"
        f"requestedScopes:\n"
        f"  - openid\n"
        f"  - profile\n"
        f"  - email\n"
        f"  - groups\n"
    )
    patch = json.dumps({"data": {"url": f"https://{argocd_domain}", "oidc.config": oidc_config}})
    patch_path = f"{DEPLOY_DIR}/argocd-cm-patch.json"
    ssh_utils.upload(client, patch, patch_path)
    ssh_utils.run(
        client,
        f"k3s kubectl patch configmap argocd-cm -n argocd --type merge --patch-file {patch_path}",
    )
    click.echo("  Patched argocd-cm with OIDC config.")


def _patch_argocd_rbac_cm(client: paramiko.SSHClient) -> None:
    """Patch argocd-rbac-cm with the initial team RBAC policy."""
    patch = json.dumps({
        "data": {
            "policy.default": "role:readonly",
            "policy.csv": "g, platform-admins, role:admin\n",
        }
    })
    patch_path = f"{DEPLOY_DIR}/argocd-rbac-patch.json"
    ssh_utils.upload(client, patch, patch_path)
    ssh_utils.run(
        client,
        f"k3s kubectl patch configmap argocd-rbac-cm -n argocd --type merge --patch-file {patch_path}",
    )
    click.echo("  Patched argocd-rbac-cm with initial RBAC policy.")


def deploy_runner(
    client: paramiko.SSHClient,
    runner_token: str,
    forgejo_url: str,
    forgejo_domain: str,
) -> None:
    """Deploy the Forgejo Actions runner as a k8s Deployment in kube-system."""
    from jinja2 import Environment, PackageLoader

    click.echo("  Deploying Forgejo Actions runner to k3s...")
    env = Environment(loader=PackageLoader('bootstrapper', 'templates'))
    manifest = env.get_template('k8s/runner.yaml.j2').render(
        runner_token=runner_token,
        forgejo_url=forgejo_url,
        forgejo_domain=forgejo_domain,
    )

    remote_path = f"{DEPLOY_DIR}/runner.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, manifest, remote_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {remote_path}")
    click.echo("  Forgejo Actions runner deployed.")
