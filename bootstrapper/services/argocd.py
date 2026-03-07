import json
import click
import paramiko

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils
from bootstrapper.deploy.helm import DEPLOY_DIR
from bootstrapper.services import authentik as authentik_module


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
    helm_module.upgrade_install(
        client, "argocd", "argo/argo-cd", "argocd",
        manifests.render(
            'helm/argocd-values.yaml.j2',
            argocd_domain=argocd_domain,
            cluster_issuer=cluster_issuer,
        ),
    )
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
    client_id, client_secret = authentik_module.create_oauth_provider(
        client, bootstrap_token,
        name="argocd", slug="argocd", app_name="Argo CD",
        redirect_uris=[{"matching_mode": "strict", "url": f"https://{argocd_domain}/auth/callback"}],
    )
    _apply_argocd_sso_secrets(client, client_secret, forgejo_domain, forgejo_admin_username, forgejo_api_token)
    _patch_argocd_cm(client, authentik_domain, argocd_domain, client_id)
    _patch_argocd_rbac_cm(client)
    click.echo("  Argo CD SSO configured.")


def _apply_argocd_sso_secrets(
    client: paramiko.SSHClient,
    oidc_client_secret: str,
    forgejo_domain: str,
    forgejo_admin_username: str,
    forgejo_api_token: str,
) -> None:
    """Apply the three Secrets Argo CD needs for SSO and SCM discovery."""
    remote_path = f"{DEPLOY_DIR}/argocd-sso-secrets.yaml"
    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(
        client,
        manifests.render(
            'k8s/argocd-sso-secrets.yaml.j2',
            oidc_client_secret=oidc_client_secret,
            forgejo_api_token=forgejo_api_token,
            forgejo_domain=forgejo_domain,
            forgejo_admin_username=forgejo_admin_username,
        ),
        remote_path,
    )
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
