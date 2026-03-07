import shlex
import time
import click
import paramiko

from bootstrapper.deploy import ssh as ssh_utils

_BASE = "http://forgejo-http.forgejo.svc.cluster.local:3000"


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
