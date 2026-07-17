import json
import time
import click
import paramiko

from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR, upgrade_install, add_repo

MANIFESTS_DIR = '/var/lib/rancher/k3s/server/manifests'


def install_k3s(client: paramiko.SSHClient) -> None:
    """Install k3s with Traefik configured to terminate TLS on host ports 80/443.

    The Traefik HelmChartConfig is uploaded to the k3s manifests directory before
    installation so the Helm controller applies it on first boot.
    HTTP traffic is automatically redirected to HTTPS.
    """
    # Always (re-)upload the Traefik config so it reflects the current template.
    ssh_utils.run(client, f"mkdir -p {MANIFESTS_DIR}")
    ssh_utils.upload(
        client,
        manifests.render('k8s/traefik-config.yaml.j2'),
        f"{MANIFESTS_DIR}/traefik-config.yaml",
    )

    try:
        ssh_utils.run(client, "k3s --version")
        click.echo("  k3s already installed, ensuring service is running...")
        ssh_utils.run(client, "systemctl start k3s")
        _wait_for_k3s(client)
        return
    except RuntimeError:
        pass

    click.echo("  Installing k3s...")
    # Ensure no stale config (e.g. OIDC flags from a previous failed run)
    ssh_utils.run(client, "mkdir -p /etc/rancher/k3s && truncate -s 0 /etc/rancher/k3s/config.yaml")
    ssh_utils.run(client, "curl -sfL https://get.k3s.io | sh -")
    ssh_utils.run(client, "systemctl start k3s")
    click.echo("  k3s installed. Waiting for node to become Ready...")

    _wait_for_k3s(client)
    click.echo("  k3s is Ready.")


def _wait_for_k3s(client: paramiko.SSHClient, timeout: int = 300, interval: int = 5) -> None:
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


def _wait_for_k3s_stable(
    client: paramiko.SSHClient, timeout: int = 300, interval: int = 5, stable_checks: int = 6,
) -> None:
    """Wait until the apiserver answers /readyz repeatedly in a row.

    After k3s-killall.sh + systemctl start, k3s can come up, exit once, and be
    auto-restarted by systemd ~20s later. Node status also reads stale-Ready in
    that window, so a single successful poll is not proof of stability; require
    several consecutive OKs before letting callers exec into pods.
    """
    deadline = time.time() + timeout
    consecutive = 0
    while time.time() < deadline:
        try:
            out = ssh_utils.run(client, "k3s kubectl get --raw /readyz 2>/dev/null")
            consecutive = consecutive + 1 if out.strip() == 'ok' else 0
        except RuntimeError:
            consecutive = 0
        if consecutive >= stable_checks:
            return
        time.sleep(interval)
    raise TimeoutError(f"k3s apiserver did not stay ready within {timeout}s")


def wire_oidc(client: paramiko.SSHClient, authentik_domain: str) -> None:
    """Append OIDC flags to k3s config and restart k3s (idempotent).

    Must be run after DNS has propagated and TLS certificates are live,
    because kube-apiserver validates the OIDC issuer URL over HTTPS.
    """
    already = ssh_utils.run(
        client,
        "grep -q 'oidc-issuer-url' /etc/rancher/k3s/config.yaml && echo yes || echo no",
    ).strip()
    if already == 'yes':
        click.echo("  k3s OIDC already configured, skipping.")
        return

    issuer_url = f"https://{authentik_domain}/application/o/kubernetes/"
    # oidc-client-id MUST equal the Authentik kubernetes provider's client_id: the
    # apiserver rejects any token whose `aud` doesn't match it. configure_k3s_oidc
    # pins that provider's client_id to the literal "kubernetes" precisely so this
    # hardcoded value is correct and stable across rebuilds — keep the two in sync.
    ssh_utils.run(client, (
        f"printf 'kube-apiserver-arg:\\n"
        f"  - oidc-issuer-url={issuer_url}\\n"
        f"  - oidc-client-id=kubernetes\\n"
        f"  - oidc-username-claim=email\\n"
        f"  - oidc-groups-claim=groups\\n'"
        f" >> /etc/rancher/k3s/config.yaml"
    ))
    click.echo("  Restarting k3s...")
    ssh_utils.run(client, "/usr/local/bin/k3s-killall.sh && systemctl start k3s")
    click.echo("  Waiting for k3s to become ready...")
    _wait_for_k3s_stable(client)
    # k3s-killall.sh tears down every pod; the API server returns before workloads
    # are back. Wait for Forgejo specifically, since the caller exec's into it next.
    click.echo("  Waiting for Forgejo to restart...")
    ssh_utils.run(client, "k3s kubectl -n forgejo rollout status deploy/forgejo --timeout=180s")
    click.echo("  k3s OIDC wired.")


_TLS_SECRETS = [
    ("forgejo", "forgejo-tls"),
    ("authentik", "authentik-tls"),
    ("argocd", "argocd-server-tls"),
    # Every issued cert must survive rebuilds — Let's Encrypt allows only 5
    # duplicate certificates per exact name per week, and repeated dev-box
    # rebuilds burn through that fast for any name missing from this list.
    ("headlamp", "headlamp-tls"),
    ("authentik", "portal-tls"),
    ("analytics", "umami-tls"),
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
        path = f"{DEPLOY_DIR}/{name}.yaml"
        ssh_utils.upload(
            client,
            manifests.render('k8s/tls-secret.yaml.j2', name=name, namespace=ns, tls_crt=data['crt'], tls_key=data['key']),
            path,
        )
        ssh_utils.run(client, f"k3s kubectl apply -f {path}")
        click.echo(f"    {name} ({ns}) restored.")


def apply_job_manifest(client: paramiko.SSHClient, rendered: str, job_name: str,
                       namespace: str, timeout: int = 900) -> None:
    """Apply a manifest containing a Job and wait for the Job to succeed.

    Job pod templates are immutable, so the previous run is deleted first
    (Argo CD's BeforeHookCreation hook policy does the same on syncs). On
    failure or timeout the job's pod logs are surfaced.
    """
    path = f"{DEPLOY_DIR}/{job_name}.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, rendered, path)
    ssh_utils.run(client, f"k3s kubectl delete job {job_name} -n {namespace} --ignore-not-found")
    ssh_utils.run(client, f"k3s kubectl apply -f {path}")
    click.echo(f"  Waiting for job {job_name} to complete...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = ssh_utils.run(
            client,
            f"k3s kubectl get job {job_name} -n {namespace} "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null || true",
        ).strip()
        succeeded, _, failed = status.partition('/')
        if succeeded.isdigit() and int(succeeded) >= 1:
            click.echo(f"  Job {job_name} completed.")
            return
        if failed.isdigit() and int(failed) >= 5:
            break
        time.sleep(10)
    logs = ssh_utils.run(
        client,
        f"k3s kubectl logs -n {namespace} job/{job_name} --tail=40 2>&1 || true",
    )
    raise RuntimeError(f"Job {job_name} did not succeed within {timeout}s. Logs:\n{logs}")


def apply_cluster_issuer(client: paramiko.SSHClient, provider: str, email: str) -> str:
    """Apply the ClusterIssuer manifest directly; cert-manager must be running.

    Used by the Argo-CD-era provision flow, where cert-manager itself is
    deployed by an Application: the issuer needs the CRDs + webhook first, so
    it is applied right after the cert-manager app reports Healthy. The same
    manifest is also owned by the cluster-issuer Application once the gitops
    repo is seeded (identical content — adoption is a no-op).
    """
    if provider == 'local':
        issuer_name = 'selfsigned'
        rendered = manifests.render('k8s/selfsigned-issuer.yaml.j2')
    else:
        issuer_name = 'letsencrypt-prod'
        rendered = manifests.render('k8s/cluster-issuer.yaml.j2',
                                    issuer_name=issuer_name, email=email)
    path = f"{DEPLOY_DIR}/cluster-issuer.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, rendered, path)
    ssh_utils.run(client, f"k3s kubectl apply -f {path}")
    click.echo(f"  ClusterIssuer {issuer_name} applied.")
    return issuer_name


def install_cert_manager_selfsigned(client: paramiko.SSHClient) -> str:
    """Install cert-manager with a self-signed ClusterIssuer for local/offline use.

    Returns the ClusterIssuer name to use in Helm ingress annotations.
    """
    click.echo("  Installing cert-manager (self-signed issuer for local)...")
    add_repo(client, "jetstack", "https://charts.jetstack.io")
    upgrade_install(
        client, "cert-manager", "jetstack/cert-manager", "cert-manager",
        {"crds": {"enabled": True}},
    )

    issuer_name = "selfsigned"
    issuer_path = f"{DEPLOY_DIR}/selfsigned-issuer.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(
        client,
        manifests.render('k8s/selfsigned-issuer.yaml.j2'),
        issuer_path,
    )
    ssh_utils.run(client, f"k3s kubectl apply -f {issuer_path}")
    click.echo("  cert-manager ready (self-signed).")
    return issuer_name


def install_cert_manager(client: paramiko.SSHClient, admin_email: str) -> str:
    """Install cert-manager and a Let's Encrypt prod ClusterIssuer.

    Returns the ClusterIssuer name to use in Helm ingress annotations.
    """
    click.echo("  Installing cert-manager...")
    add_repo(client, "jetstack", "https://charts.jetstack.io")
    upgrade_install(
        client, "cert-manager", "jetstack/cert-manager", "cert-manager",
        {"crds": {"enabled": True}},
    )

    issuer_name = "letsencrypt-prod"
    click.echo("  Creating Let's Encrypt ClusterIssuer...")
    issuer_path = f"{DEPLOY_DIR}/cluster-issuer.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(
        client,
        manifests.render('k8s/cluster-issuer.yaml.j2', issuer_name=issuer_name, email=admin_email),
        issuer_path,
    )
    ssh_utils.run(client, f"k3s kubectl apply -f {issuer_path}")
    click.echo("  cert-manager ready.")
    return issuer_name
