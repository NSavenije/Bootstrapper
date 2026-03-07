import time
import click
import paramiko

from bootstrapper.deploy import ssh as ssh_utils

DEFAULT_GROUPS = [
    "forgejo-admins",
    "platform-devs",
    "website-devs",
    "platform-readers",
    "website-readers",
]

_BASE = "http://authentik-server.authentik.svc.cluster.local/api/v3"


def wait_for_authentik(client: paramiko.SSHClient, bootstrap_token: str, timeout: int = 300, interval: int = 10) -> None:
    """Wait until Authentik is fully bootstrapped.

    Two-phase wait:
    1. Poll /api/v3/root/config/ until the server accepts connections.
    2. Poll /api/v3/core/users/?username=akadmin until the worker has finished
       running 'ak bootstrap' and created the admin user with the configured
       password.  Only after this is akadmin actually usable.
    """
    headers = {"Authorization": f"Bearer {bootstrap_token}"}

    click.echo(f"  Waiting for Authentik server...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = ssh_utils.cluster_curl(client, f"{_BASE}/root/config/")
            if r.status_code == 200:
                break
        except RuntimeError:
            pass
        time.sleep(interval)
    else:
        raise TimeoutError(f"Authentik did not become healthy within {timeout}s")
    click.echo("  Authentik server is up. Waiting for worker bootstrap (akadmin)...")

    while time.time() < deadline:
        try:
            r = ssh_utils.cluster_curl(
                client,
                f"{_BASE}/core/users/?username=akadmin&page_size=1",
                headers=headers,
            )
            if r.ok and r.json().get("results"):
                click.echo("  Authentik bootstrap complete.")
                return
        except RuntimeError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Authentik worker bootstrap did not complete within {timeout}s")


def sync_akadmin(client: paramiko.SSHClient, bootstrap_token: str, admin_password: str, admin_email: str) -> None:
    """Explicitly set akadmin's password and email via the API.

    Authentik creates akadmin during Django startup with set_unusable_password().
    The bootstrap blueprint (state: created) skips the user because it already
    exists by the time Celery runs it, so AUTHENTIK_BOOTSTRAP_PASSWORD has no
    effect. We fix this by calling set_password directly after provisioning.
    """
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    r = ssh_utils.cluster_curl(client, f"{_BASE}/core/users/?username=akadmin&page_size=1", headers=headers)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise RuntimeError("akadmin user not found in Authentik")
    pk = results[0]["pk"]

    r = ssh_utils.cluster_curl(client, f"{_BASE}/core/users/{pk}/set_password/", method='POST', headers=headers, json_body={"password": admin_password})
    if not r.ok:
        raise RuntimeError(f"Failed to set akadmin password ({r.status_code}): {r.text}")

    r = ssh_utils.cluster_curl(client, f"{_BASE}/core/users/{pk}/", method='PATCH', headers=headers, json_body={"email": admin_email})
    if not r.ok:
        raise RuntimeError(f"Failed to update akadmin email ({r.status_code}): {r.text}")

    click.echo("  akadmin password and email synced.")


def configure_oauth_provider(client: paramiko.SSHClient, bootstrap_token: str, forgejo_domain: str) -> tuple[str, str]:
    """
    Create an OAuth2 provider + application in Authentik for Forgejo.
    Returns (client_id, client_secret).
    """
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    scope_pks = _get_scope_mappings(client, _BASE, headers, ["openid", "email", "profile"])
    scope_pks.append(_get_or_create_groups_scope_mapping(client, _BASE, headers))

    provider_payload = {
        "name": "Forgejo",
        "authorization_flow": _get_default_authorization_flow(client, _BASE, headers),
        "redirect_uris": [
            {
                "matching_mode": "strict",
                "url": f"https://{forgejo_domain}/user/oauth2/authentik/callback",
            }
        ],
        "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True,
        "signing_key": _get_default_signing_key(client, _BASE, headers),
        "invalidation_flow": _get_default_invalidation_flow(client, _BASE, headers),
        "property_mappings": scope_pks,
    }

    # Re-use existing provider so client_id/secret stay stable across re-runs.
    existing = ssh_utils.cluster_curl(client, f"{_BASE}/providers/oauth2/?name=Forgejo", headers=headers)
    existing.raise_for_status()
    results = existing.json().get("results", [])
    if results:
        provider_pk = results[0]["pk"]
        click.echo("  Updating existing Authentik OAuth2 provider for Forgejo...")
        r = ssh_utils.cluster_curl(client, f"{_BASE}/providers/oauth2/{provider_pk}/", method='PATCH', headers=headers, json_body=provider_payload)
        if not r.ok:
            raise RuntimeError(f"Authentik provider update failed ({r.status_code}): {r.text}")
        provider = r.json()
    else:
        click.echo("  Creating Authentik OAuth2 provider for Forgejo...")
        r = ssh_utils.cluster_curl(client, f"{_BASE}/providers/oauth2/", method='POST', headers=headers, json_body=provider_payload)
        if not r.ok:
            raise RuntimeError(f"Authentik provider creation failed ({r.status_code}): {r.text}")
        provider = r.json()

    provider_pk = provider["pk"]
    client_id = provider["client_id"]
    client_secret = provider["client_secret"]

    # Ensure the application exists (idempotent — create only if missing).
    existing_app = ssh_utils.cluster_curl(client, f"{_BASE}/core/applications/?slug=forgejo", headers=headers)
    existing_app.raise_for_status()
    if not existing_app.json().get("results"):
        click.echo("  Creating Authentik application for Forgejo...")
        r = ssh_utils.cluster_curl(client, f"{_BASE}/core/applications/", method='POST', headers=headers, json_body={"name": "Forgejo", "slug": "forgejo", "provider": provider_pk})
        if not r.ok:
            raise RuntimeError(f"Authentik application creation failed ({r.status_code}): {r.text}")

    return client_id, client_secret


def create_groups(client: paramiko.SSHClient, bootstrap_token: str, group_names: list[str]) -> None:
    """Create Authentik groups idempotently. Existing groups are left unchanged."""
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    existing_r = ssh_utils.cluster_curl(client, f"{_BASE}/core/groups/?page_size=200", headers=headers)
    existing_r.raise_for_status()
    existing_names = {g["name"] for g in existing_r.json().get("results", [])}

    for name in group_names:
        if name in existing_names:
            click.echo(f"  Group '{name}' already exists, skipping.")
            continue
        r = ssh_utils.cluster_curl(client, f"{_BASE}/core/groups/", method='POST', headers=headers, json_body={"name": name})
        if not r.ok:
            raise RuntimeError(f"Failed to create group '{name}' ({r.status_code}): {r.text}")
        click.echo(f"  Created group '{name}'.")


def _get_or_create_groups_scope_mapping(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the PK of a scope mapping that adds a 'groups' claim to the OIDC token.
    Creates it if it doesn't exist yet.
    """
    r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/provider/scope/?scope_name=groups", headers=headers)
    r.raise_for_status()
    results = r.json().get("results", [])
    if results:
        return results[0]["pk"]

    payload = {
        "name": "authentik default OAuth Mapping: groups",
        "scope_name": "groups",
        "description": "Group membership for RBAC in connected applications",
        "expression": "return {\"groups\": [g.name for g in request.user.ak_groups.all()]}",
    }
    r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/provider/scope/", method='POST', headers=headers, json_body=payload)
    if not r.ok:
        raise RuntimeError(f"Failed to create groups scope mapping ({r.status_code}): {r.text}")
    return r.json()["pk"]


def _get_default_authorization_flow(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the pk of the default authorization flow."""
    r = ssh_utils.cluster_curl(ssh, f"{base}/flows/instances/?designation=authorization", headers=headers)
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise RuntimeError("No authorization flow found in Authentik. Is it fully initialized?")
    for flow in results:
        if "explicit" in flow.get("slug", ""):
            return flow["pk"]
    return results[0]["pk"]


def _get_default_invalidation_flow(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the pk of the default invalidation flow."""
    r = ssh_utils.cluster_curl(ssh, f"{base}/flows/instances/?designation=invalidation", headers=headers)
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise RuntimeError("No invalidation flow found in Authentik. Is it fully initialized?")
    return results[0]["pk"]


def _get_scope_mappings(ssh: paramiko.SSHClient, base: str, headers: dict, scope_names: list[str]) -> list[str]:
    """Return PKs of the built-in OAuth2 scope property mappings for the given scope names."""
    r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/all/", headers=headers)
    r.raise_for_status()
    managed_keys = {f"goauthentik.io/providers/oauth2/scope-{n}" for n in scope_names}
    pks = [
        m["pk"] for m in r.json()["results"]
        if m.get("managed") in managed_keys
    ]
    if not pks:
        raise RuntimeError(f"No scope mappings found for {scope_names}. Is Authentik fully initialized?")
    return pks


def _get_default_signing_key(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the pk of the first available signing certificate."""
    r = ssh_utils.cluster_curl(ssh, f"{base}/crypto/certificatekeypairs/?has_key=true", headers=headers)
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise RuntimeError("No signing key found in Authentik.")
    return results[0]["pk"]
