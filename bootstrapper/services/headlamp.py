"""Headlamp — Kubernetes web UI wired to Authentik SSO with per-user RBAC."""
import re

import click
import paramiko

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR

HEADLAMP_REPO = "https://kubernetes-sigs.github.io/headlamp/"


def install_headlamp(
    client: paramiko.SSHClient,
    headlamp_domain: str,
    authentik_domain: str,
    client_id: str,
    client_secret: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Install Headlamp via Helm with Traefik Ingress, cert-manager TLS, and OIDC."""
    click.echo("  Installing Headlamp via Helm...")
    helm_module.add_repo(client, "headlamp", HEADLAMP_REPO)
    helm_module.upgrade_install(
        client, "headlamp", "headlamp/headlamp", "headlamp",
        manifests.render(
            'helm/headlamp-values.yaml.j2',
            headlamp_domain=headlamp_domain,
            authentik_domain=authentik_domain,
            client_id=client_id,
            client_secret=client_secret,
            cluster_issuer=cluster_issuer,
        ),
    )
    click.echo("  Headlamp installed.")


def _role_for(group: str, admin_group: str) -> str:
    """Map a group name to a built-in ClusterRole by convention."""
    if group == admin_group:
        return "cluster-admin"
    if group.endswith("-devs"):
        return "edit"
    if group.endswith("-readers"):
        return "view"
    return "view"


def configure_oidc_rbac(
    client: paramiko.SSHClient,
    group_names: list[str],
    admin_group: str = "forgejo-admins",
) -> list[tuple[str, str]]:
    """Bind Authentik groups to cluster roles so OIDC logins actually see resources.

    Convention: the admin group -> cluster-admin, *-devs -> edit, *-readers -> view,
    anything else -> view. Returns the list of (group, role) applied.
    """
    bindings = []
    applied = []
    for group in group_names:
        role = _role_for(group, admin_group)
        # ClusterRoleBinding name must be RFC1123 (lowercase alnum + '-').
        name = re.sub(r'[^a-z0-9-]', '-', group.lower())
        bindings.append({"name": name, "group": group, "role": role})
        applied.append((group, role))

    manifest = manifests.render('k8s/oidc-rbac.yaml.j2', bindings=bindings)
    remote_path = f"{DEPLOY_DIR}/oidc-rbac.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, manifest, remote_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {remote_path}")
    click.echo(f"  Applied OIDC RBAC for {len(applied)} group(s).")
    return applied
