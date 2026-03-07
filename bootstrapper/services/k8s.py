import json
import time
import click
import paramiko

from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR, upgrade_install, add_repo

MANIFESTS_DIR = '/var/lib/rancher/k3s/server/manifests'


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
    add_repo(client, "jetstack", "https://charts.jetstack.io")
    upgrade_install(
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
