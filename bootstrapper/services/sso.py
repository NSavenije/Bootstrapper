import shlex

import click
import paramiko

from bootstrapper.deploy import ssh as ssh_utils


def configure_forgejo_oauth_source(
    ssh: paramiko.SSHClient,
    authentik_domain: str,
    client_id: str,
    client_secret: str,
    admin_group: str = "forgejo-admins",
    *,
    public: bool = False,
) -> None:
    """Register Authentik as an OAuth2 source in Forgejo via kubectl exec CLI.

    During bootstrap (public=False) the internal cluster URL is used so this works
    before DNS/TLS is live. Call again with public=True from wire-k3s-oidc once
    DNS/TLS is up — the public discovery document returns the correct public
    authorization_endpoint, which is what the browser ultimately redirects to.
    """
    click.echo("  Configuring Forgejo OAuth source -> Authentik...")

    if public:
        discover_url = f"https://{authentik_domain}/application/o/forgejo/.well-known/openid-configuration"
    else:
        discover_url = "http://authentik-server.authentik.svc.cluster.local/application/o/forgejo/.well-known/openid-configuration"

    oauth_flags = (
        f" --provider openidConnect"
        f" --key {shlex.quote(client_id)}"
        f" --secret {shlex.quote(client_secret)}"
        f" --auto-discover-url {shlex.quote(discover_url)}"
        f" --scopes {shlex.quote('openid email profile groups')}"
        f" --group-claim-name groups"
        f" --admin-group {shlex.quote(admin_group)}"
    )

    # List existing auth sources to check for 'authentik'
    try:
        listing = ssh_utils.run_with_stdin(
            ssh,
            "k3s kubectl exec -n forgejo deploy/forgejo -i -- sh",
            b"forgejo admin auth list\n",
        )
    except RuntimeError:
        listing = ""

    existing_id = None
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "authentik":
            existing_id = parts[0]
            break

    if existing_id:
        inner_cmd = f"forgejo admin auth update-oauth --id {existing_id}" + oauth_flags + "\n"
        ssh_utils.run_with_stdin(
            ssh,
            "k3s kubectl exec -n forgejo deploy/forgejo -i -- sh",
            inner_cmd.encode(),
        )
        click.echo(f"  Updated existing 'authentik' auth source (id={existing_id}).")
    else:
        inner_cmd = "forgejo admin auth add-oauth --name authentik" + oauth_flags + "\n"
        ssh_utils.run_with_stdin(
            ssh,
            "k3s kubectl exec -n forgejo deploy/forgejo -i -- sh",
            inner_cmd.encode(),
        )
    click.echo("  SSO configured: Forgejo will offer 'Sign in with Authentik'.")
