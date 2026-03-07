import base64
import os
import shlex
import time
import click
import paramiko
import requests

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR

_BASE = "http://forgejo-http.forgejo.svc.cluster.local:3000"


def resolve_forgejo_version(version: str) -> str:
    """Resolve 'latest' to the actual latest Forgejo release tag via Codeberg API."""
    if version != 'latest':
        return version
    r = requests.get(
        'https://codeberg.org/api/v1/repos/forgejo/forgejo/releases?limit=1&pre-release=false',
        timeout=10,
    )
    r.raise_for_status()
    tag = r.json()[0]['tag_name'].lstrip('v')  # e.g. "14.0.2"
    click.echo(f"  Resolved Forgejo 'latest' -> {tag}")
    return tag


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


def wait_for_forgejo(client: paramiko.SSHClient, timeout: int = 300, interval: int = 10) -> None:
    """Poll Forgejo's version endpoint via cluster_curl until it responds."""
    url = f"{_BASE}/api/v1/version"
    click.echo("  Waiting for Forgejo...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = ssh_utils.cluster_curl(client, url)
            if r.status_code == 200:
                click.echo("  Forgejo is up.")
                return
        except RuntimeError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Forgejo did not become healthy within {timeout}s")


def create_admin(client: paramiko.SSHClient, username: str, password: str, email: str) -> None:
    """Create the Forgejo admin user via kubectl exec, idempotent.
    Always syncs the password to match the config, even on re-runs.
    """
    click.echo(f"  Creating Forgejo admin user '{username}'...")
    cmd = (
        f"forgejo admin user create "
        f"--admin --username {shlex.quote(username)} --password {shlex.quote(password)} "
        f"--email {shlex.quote(email)} --must-change-password=false\n"
    )
    try:
        ssh_utils.run_with_stdin(
            client,
            "k3s kubectl exec -n forgejo deploy/forgejo -i -- sh",
            cmd.encode(),
        )
    except RuntimeError as e:
        msg = str(e).lower()
        if "already exists" in msg:
            click.echo(f"  Admin user '{username}' already exists, syncing password...")
            sync_cmd = (
                f"forgejo admin user change-password "
                f"--username {shlex.quote(username)} --password {shlex.quote(password)} "
                f"--must-change-password=false\n"
            )
            ssh_utils.run_with_stdin(
                client,
                "k3s kubectl exec -n forgejo deploy/forgejo -i -- sh",
                sync_cmd.encode(),
            )
        elif "name is reserved" in msg:
            raise RuntimeError(
                f"'{username}' is a reserved username in Forgejo. "
                "Choose a different admin_username in your config (e.g. 'siteadmin')."
            ) from None
        else:
            raise


def create_runner_token(client: paramiko.SSHClient, username: str, api_token: str) -> str:
    """Fetch the Forgejo Actions instance-level runner registration token."""
    click.echo("  Fetching Forgejo Actions runner registration token...")
    r = ssh_utils.cluster_curl(
        client,
        f"{_BASE}/api/v1/admin/runners/registration-token",
        headers={"Authorization": f"token {api_token}"},
    )
    if not r.ok:
        raise RuntimeError(f"Failed to get runner token ({r.status_code}): {r.text}")
    click.echo("  Runner registration token obtained.")
    return r.json()["token"]


def create_platform_org(client: paramiko.SSHClient, api_token: str) -> None:
    """Create the platform-team Forgejo organisation (idempotent)."""
    click.echo("  Creating platform-team Forgejo organisation...")
    r = ssh_utils.cluster_curl(
        client,
        f"{_BASE}/api/v1/orgs",
        method='POST',
        headers={"Authorization": f"token {api_token}", "Content-Type": "application/json"},
        json_body={"username": "platform-team", "visibility": "private"},
    )
    if r.status_code == 422 and "already exist" in r.text.lower():
        click.echo("  platform-team org already exists, skipping.")
        return
    if not r.ok:
        raise RuntimeError(f"Failed to create platform-team org ({r.status_code}): {r.text}")
    click.echo("  platform-team org created.")


def create_api_token(client: paramiko.SSHClient, username: str, password: str) -> str:
    """Create a Forgejo API token, deleting any stale one with the same name first."""
    click.echo("  Creating Forgejo API token...")
    base = f"{_BASE}/api/v1"

    # Delete existing token with the same name if present
    r = ssh_utils.cluster_curl(
        client,
        f"{base}/users/{username}/tokens",
        auth=(username, password),
    )
    r.raise_for_status()
    for token in r.json():
        if token["name"] == "bootstrapper":
            ssh_utils.cluster_curl(
                client,
                f"{base}/users/{username}/tokens/{token['id']}",
                method='DELETE',
                auth=(username, password),
            )
            break

    r = ssh_utils.cluster_curl(
        client,
        f"{base}/users/{username}/tokens",
        method='POST',
        auth=(username, password),
        json_body={
            "name": "bootstrapper",
            "scopes": ["write:admin", "write:user", "write:repository", "write:issue", "write:organization"],
        },
    )
    if not r.ok:
        raise RuntimeError(f"Token creation failed ({r.status_code}): {r.text}")
    return r.json()["sha1"]


def deploy_runner(
    client: paramiko.SSHClient,
    runner_token: str,
    forgejo_url: str,
    forgejo_domain: str,
) -> None:
    """Deploy the Forgejo Actions runner as a k8s Deployment in kube-system."""
    click.echo("  Deploying Forgejo Actions runner to k3s...")
    manifest = manifests.render(
        'k8s/runner.yaml.j2',
        runner_token=runner_token,
        forgejo_url=forgejo_url,
        forgejo_domain=forgejo_domain,
    )

    remote_path = f"{DEPLOY_DIR}/runner.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, manifest, remote_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {remote_path}")
    click.echo("  Forgejo Actions runner deployed.")


def seed_platform_config(ssh: paramiko.SSHClient, api_token: str, forgejo_domain: str) -> None:
    """Create platform-team/platform-config repo in Forgejo and seed it with template files."""
    base = f"{_BASE}/api/v1"
    headers = {"Authorization": f"token {api_token}", "Content-Type": "application/json"}

    # Create repo (idempotent)
    r = ssh_utils.cluster_curl(
        ssh,
        f"{base}/orgs/platform-team/repos",
        method='POST',
        headers=headers,
        json_body={
            "name": "platform-config",
            "description": "GitOps control plane for the developer platform",
            "private": True,
            "auto_init": True,
            "default_branch": "main",
        },
    )
    if r.status_code not in (201, 409):
        raise RuntimeError(f"Failed to create platform-config repo ({r.status_code}): {r.text}")
    if r.status_code == 409:
        click.echo("  platform-config repo already exists, skipping file seeding.")
        return
    click.echo("  platform-config repo created.")

    # Seed files from templates/platform-config/
    templates_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates', 'platform-config'))

    for root, dirs, files in os.walk(templates_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for filename in files:
            local_path = os.path.join(root, filename)
            rel_path = os.path.relpath(local_path, templates_dir).replace('\\', '/')

            with open(local_path, 'r', encoding='utf-8') as f:
                content = f.read()

            encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
            r = ssh_utils.cluster_curl(
                ssh,
                f"{base}/repos/platform-team/platform-config/contents/{rel_path}",
                method='POST',
                headers=headers,
                json_body={
                    "message": f"Bootstrap: add {rel_path}",
                    "content": encoded,
                    "branch": "main",
                },
            )
            if not r.ok:
                if r.status_code == 422 and "already exists" in r.text:
                    pass  # file was seeded in a previous run, skip silently
                else:
                    click.echo(f"  Warning: could not seed {rel_path}: {r.status_code} {r.text}")
            else:
                click.echo(f"  Seeded {rel_path}")
