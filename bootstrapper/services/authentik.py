import time
import click
import paramiko

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils

DEFAULT_GROUPS = [
    "forgejo-admins",
    "platform-devs",
    "website-devs",
    "platform-readers",
    "website-readers",
]

_BASE = "http://authentik-server.authentik.svc.cluster.local/api/v3"
_SERVER = "http://authentik-server.authentik.svc.cluster.local"

# Pinned OIDC client ids: human-readable, stable across rebuilds, and (for
# kubernetes) required to match the apiserver's hardcoded --oidc-client-id.
# The matching client secrets are the *_oidc_client_secret generated secrets.
OIDC_CLIENT_IDS = {"forgejo": "forgejo", "argocd": "argocd", "kubernetes": "kubernetes"}


def render_blueprint(cfg: dict) -> str:
    """Render the platform-wiring blueprint ConfigMap (no secrets inside)."""
    authentik_cfg = cfg['authentik']
    return manifests.render(
        'k8s/authentik-blueprint.yaml.j2',
        admin_email=authentik_cfg['email'],
        groups=authentik_cfg.get('groups', DEFAULT_GROUPS),
        forgejo_domain=cfg['forgejo']['domain'],
        argocd_domain=cfg['argocd_domain'],
        headlamp_domain=cfg.get('headlamp_domain'),
        analytics_domain=cfg.get('analytics_domain'),
    )


def apply_wiring_manifests(ssh: paramiko.SSHClient, cfg: dict, gen: dict) -> None:
    """Apply the pinned-clients Secret and blueprint ConfigMap (pre-chart).

    Both must exist before the authentik Application deploys: the worker
    mounts the ConfigMap and reads the Secret via envFrom. The Secret is
    Layer 1 (never in git); the ConfigMap is also git-owned after seeding.
    """
    click.echo("  Applying Authentik pinned-clients Secret and platform blueprint...")
    ssh_utils.run(
        ssh,
        "k3s kubectl create namespace authentik --dry-run=client -o yaml"
        " | k3s kubectl apply -f -",
    )
    ssh_utils.run(ssh, f"mkdir -p {helm_module.DEPLOY_DIR}")
    # The forgejo-wiring Job (forgejo namespace) reads the pinned Forgejo
    # client secret too; Secrets are namespace-scoped, so apply a copy there.
    for ns in ('authentik', 'forgejo'):
        ssh_utils.run(
            ssh,
            f"k3s kubectl create namespace {ns} --dry-run=client -o yaml"
            f" | k3s kubectl apply -f -",
        )
        pinned = manifests.render(
            'k8s/authentik-pinned-clients.yaml.j2',
            namespace=ns,
            admin_password=cfg['authentik']['admin_password'],
            forgejo_oidc_client_secret=gen['forgejo_oidc_client_secret'],
            argocd_oidc_client_secret=gen['argocd_oidc_client_secret'],
            k8s_oidc_client_secret=gen['k8s_oidc_client_secret'],
        )
        path = f"{helm_module.DEPLOY_DIR}/authentik-pinned-clients-{ns}.yaml"
        ssh_utils.upload(ssh, pinned, path)
        ssh_utils.run(ssh, f"k3s kubectl apply -f {path}")
    ssh_utils.upload(ssh, render_blueprint(cfg), f"{helm_module.DEPLOY_DIR}/authentik-blueprint.yaml")
    ssh_utils.run(ssh, f"k3s kubectl apply -f {helm_module.DEPLOY_DIR}/authentik-blueprint.yaml")


def wait_for_blueprint(ssh: paramiko.SSHClient, slugs: list[str],
                       timeout: int = 300, interval: int = 10) -> None:
    """Wait until the platform blueprint has been applied by the worker.

    Uses only unauthenticated reads: each provider slug's OIDC discovery
    endpoint answers 200 once its provider + application exist, which is
    exactly the wiring the blueprint declares. No API token involved.
    """
    click.echo(f"  Waiting for Authentik blueprint (providers: {', '.join(slugs)})...")
    deadline = time.time() + timeout
    pending = list(slugs)
    while time.time() < deadline:
        still = []
        for slug in pending:
            try:
                r = ssh_utils.cluster_curl(
                    ssh, f"{_SERVER}/application/o/{slug}/.well-known/openid-configuration")
                if r.status_code != 200:
                    still.append(slug)
            except RuntimeError:
                still.append(slug)
        pending = still
        if not pending:
            click.echo("  Blueprint applied: all providers discoverable.")
            return
        time.sleep(interval)
    raise TimeoutError(f"Blueprint providers not discoverable within {timeout}s: {pending}")


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
    helm_module.upgrade_install(
        client, "authentik", "authentik/authentik", "authentik",
        manifests.render(
            'helm/authentik-values.yaml.j2',
            domain=domain,
            secret_key=secret_key,
            bootstrap_token=bootstrap_token,
            admin_password=admin_password,
            admin_email=admin_email,
            db_password=db_password,
            cluster_issuer=cluster_issuer,
        ),
    )
    click.echo("  Authentik installed.")


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


def create_oauth_provider(
    ssh: paramiko.SSHClient,
    bootstrap_token: str,
    name: str,
    slug: str,
    app_name: str,
    redirect_uris: list[dict],
    launch_url: str | None = None,
    client_id: str | None = None,
    verified_email: bool = False,
) -> tuple[str, str]:
    """Create or update an Authentik OAuth2 provider + application.

    Idempotent: updates the provider if one with the given name already exists.
    Returns (client_id, client_secret).

    Pass `client_id` to pin a fixed, human-readable client id instead of Authentik's
    random one. Required for the kubernetes provider: the kube-apiserver validates a
    token's `aud` against a hardcoded `--oidc-client-id`, so the value must be known
    ahead of time and stable across rebuilds (see configure_k3s_oidc / k8s.py).

    Pass `verified_email` for consumers that reject tokens with email_verified=false
    (the kube-apiserver does; see _get_or_create_verified_email_scope_mapping).
    """
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}

    wanted_scopes = ["openid", "profile"] if verified_email else ["openid", "email", "profile"]
    scope_pks = _get_scope_mappings(ssh, _BASE, headers, wanted_scopes)
    if verified_email:
        scope_pks.append(_get_or_create_verified_email_scope_mapping(ssh, _BASE, headers))
    scope_pks.append(_get_or_create_groups_scope_mapping(ssh, _BASE, headers))

    payload = {
        "name": name,
        "authorization_flow": _get_default_authorization_flow(ssh, _BASE, headers),
        "invalidation_flow": _get_default_invalidation_flow(ssh, _BASE, headers),
        "signing_key": _get_default_signing_key(ssh, _BASE, headers),
        "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True,
        "property_mappings": scope_pks,
        "redirect_uris": redirect_uris,
        # Authentik 2024.10+ made grant_types an explicit per-provider allow-list
        # that defaults to empty. An empty list rejects every /authorize request
        # with "Invalid grant_type for provider" (the browser sees Forgejo/ArgoCD's
        # generic "error processing the authorization request"). All our SPs use the
        # standard authorization-code flow; refresh_token keeps sessions renewable.
        "grant_types": ["authorization_code", "refresh_token"],
    }
    if client_id is not None:
        payload["client_id"] = client_id

    r = ssh_utils.cluster_curl(ssh, f"{_BASE}/providers/oauth2/?name={name}", headers=headers)
    r.raise_for_status()
    results = r.json().get("results", [])

    if results:
        pk = results[0]["pk"]
        click.echo(f"  Updating Authentik provider for {name}...")
        r = ssh_utils.cluster_curl(ssh, f"{_BASE}/providers/oauth2/{pk}/", method='PATCH', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"Provider update failed ({r.status_code}): {r.text}")
    else:
        click.echo(f"  Creating Authentik provider for {name}...")
        r = ssh_utils.cluster_curl(ssh, f"{_BASE}/providers/oauth2/", method='POST', headers=headers, json_body=payload)
        if not r.ok:
            raise RuntimeError(f"Provider creation failed ({r.status_code}): {r.text}")

    provider = r.json()
    provider_pk = provider["pk"]
    client_id = provider["client_id"]
    client_secret = provider.get("client_secret", "")

    # meta_launch_url is what Authentik opens when a user clicks the application
    # tile in their library. OIDC is SP-initiated, so an empty launch URL sends
    # the user to the app's front page *without* triggering login — which looks
    # like SSO is broken. Pointing it at the SP's login-initiation endpoint makes
    # the tile start the OIDC flow, giving a true "click app -> logged in" UX.
    app_body = {"name": app_name, "slug": slug, "provider": provider_pk}
    if launch_url is not None:
        app_body["meta_launch_url"] = launch_url

    r = ssh_utils.cluster_curl(ssh, f"{_BASE}/core/applications/?slug={slug}", headers=headers)
    r.raise_for_status()
    existing_apps = r.json().get("results", [])
    if existing_apps:
        r = ssh_utils.cluster_curl(
            ssh, f"{_BASE}/core/applications/{slug}/", method='PATCH', headers=headers,
            json_body=app_body,
        )
        if not r.ok:
            raise RuntimeError(f"Application update failed ({r.status_code}): {r.text}")
        click.echo(f"  Updated Authentik application for {name}.")
    else:
        r = ssh_utils.cluster_curl(
            ssh, f"{_BASE}/core/applications/", method='POST', headers=headers,
            json_body=app_body,
        )
        if not r.ok:
            raise RuntimeError(f"Application creation failed ({r.status_code}): {r.text}")
        click.echo(f"  Created Authentik application for {name}.")

    return client_id, client_secret


def configure_oauth_provider(client: paramiko.SSHClient, bootstrap_token: str, forgejo_domain: str) -> tuple[str, str]:
    """Create an OAuth2 provider + application in Authentik for Forgejo.
    Returns (client_id, client_secret).
    """
    return create_oauth_provider(
        client, bootstrap_token,
        name="Forgejo", slug="forgejo", app_name="Forgejo",
        redirect_uris=[{
            "matching_mode": "strict",
            "url": f"https://{forgejo_domain}/user/oauth2/authentik/callback",
        }],
        # Forgejo's /user/oauth2/<source-name> route kicks off the OIDC login
        # directly, so clicking the Forgejo tile in Authentik logs the user in.
        launch_url=f"https://{forgejo_domain}/user/oauth2/authentik",
    )


def create_link_application(
    ssh: paramiko.SSHClient,
    bootstrap_token: str,
    name: str,
    slug: str,
    launch_url: str,
) -> None:
    """Create/update a provider-less Authentik application: a dashboard bookmark tile.

    For services that can't speak OIDC — e.g. Umami OSS, which ships only its own
    local admin login — this still surfaces them on the Authentik dashboard as a
    launch link so they sit alongside the SSO-integrated apps. Users authenticate
    with the service's own credentials after clicking through. Idempotent.
    """
    headers = {"Authorization": f"Bearer {bootstrap_token}", "Content-Type": "application/json"}
    app_body = {
        "name": name,
        "slug": slug,
        "meta_launch_url": launch_url,
        "open_in_new_tab": True,
    }

    r = ssh_utils.cluster_curl(ssh, f"{_BASE}/core/applications/?slug={slug}", headers=headers)
    r.raise_for_status()
    if r.json().get("results"):
        r = ssh_utils.cluster_curl(ssh, f"{_BASE}/core/applications/{slug}/", method='PATCH', headers=headers, json_body=app_body)
    else:
        r = ssh_utils.cluster_curl(ssh, f"{_BASE}/core/applications/", method='POST', headers=headers, json_body=app_body)
    if not r.ok:
        raise RuntimeError(f"Link application '{name}' failed ({r.status_code}): {r.text}")
    click.echo(f"  Authentik dashboard tile for {name} ready.")


def configure_k3s_oidc(
    ssh: paramiko.SSHClient,
    bootstrap_token: str,
    headlamp_domain: str | None = None,
) -> tuple[str, str]:
    """Create/update the Authentik OIDC provider for Kubernetes. Returns (client_id, client_secret).

    kube-apiserver requires HTTPS for the OIDC issuer URL, so we cannot
    configure k3s during bootstrap (DNS/TLS not live yet). The provider is
    created here so it is ready once DNS propagates. After DNS/TLS is live:

      echo 'kube-apiserver-arg:
        - oidc-issuer-url=https://<authentik_domain>/application/o/kubernetes/
        - oidc-client-id=<client_id>
        - oidc-username-claim=email
        - oidc-groups-claim=groups' >> /etc/rancher/k3s/config.yaml
      /usr/local/bin/k3s-killall.sh && systemctl start k3s

    When `headlamp_domain` is set, Headlamp reuses this same client (so its tokens
    carry the audience k3s trusts): its callback is added as a redirect URI and the
    Kubernetes app tile is pointed at Headlamp instead of being hidden.
    """
    click.echo("  Creating Authentik OIDC provider for Kubernetes...")
    # localhost:8000 is the kubectl oidc-login (CLI) callback — always present.
    redirect_uris = [{"matching_mode": "strict", "url": "http://localhost:8000"}]
    # CLI-only by default: no browsable UI, so hide the tile from the user library.
    launch_url = "blank://blank"
    if headlamp_domain:
        redirect_uris.append(
            {"matching_mode": "strict", "url": f"https://{headlamp_domain}/oidc-callback"}
        )
        launch_url = f"https://{headlamp_domain}"

    client_id, client_secret = create_oauth_provider(
        ssh, bootstrap_token,
        name="kubernetes", slug="kubernetes", app_name="Kubernetes",
        redirect_uris=redirect_uris,
        launch_url=launch_url,
        # Pinned so it matches the kube-apiserver's hardcoded --oidc-client-id
        # (k8s.py) and stays stable across rebuilds. Headlamp reuses this client.
        client_id="kubernetes",
        # --oidc-username-claim=email means the apiserver rejects tokens whose
        # email_verified isn't true, and Authentik's built-in email mapping says false.
        verified_email=True,
    )
    click.echo("  Authentik k8s OIDC provider created (k3s wired after DNS/TLS is live).")
    return client_id, client_secret


def ensure_admin_token(
    ssh: paramiko.SSHClient,
    candidate_token: str | None = None,
    identifier: str = "bootstrapper-cli",
) -> str:
    """Return a working Authentik API token, minting a non-expiring akadmin token if needed.

    The bootstrap token expires on long-lived servers, so standalone post-provision
    commands can't rely on the one in the state file. If `candidate_token` still
    works it is returned as-is; otherwise a stable non-expiring token is created
    (idempotently, by identifier) via `ak shell`. Requires the curl pod to be up.
    """
    if candidate_token:
        r = ssh_utils.cluster_curl(
            ssh, f"{_BASE}/core/users/?page_size=1",
            headers={"Authorization": f"Bearer {candidate_token}"},
        )
        if r.ok:
            return candidate_token
        click.echo("  Stored Authentik token invalid/expired; minting a fresh one...")

    script = (
        "from authentik.core.models import Token, TokenIntents, User\n"
        "u = User.objects.get(username='akadmin')\n"
        f"t = Token.objects.filter(identifier={identifier!r}).first()\n"
        "if t is None:\n"
        f"    t = Token.objects.create(identifier={identifier!r}, user=u,\n"
        "        intent=TokenIntents.INTENT_API, expiring=False, description='bootstrapper CLI')\n"
        "print('TOKEN|' + t.key)\n"
    )
    out = ssh_utils.run_with_stdin(
        ssh,
        "k3s kubectl exec -n authentik deploy/authentik-server -i -- ak shell",
        script.encode(),
    )
    for line in out.splitlines():
        if line.startswith("TOKEN|"):
            return line.split("|", 1)[1].strip()
    raise RuntimeError(f"could not mint Authentik token; ak shell output:\n{out[-500:]}")


def install_portal_redirect(
    client: paramiko.SSHClient,
    portal_domain: str,
    authentik_domain: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Publish portal.<domain> as a friendly redirect to the Authentik dashboard.

    Deploys a Traefik redirect Middleware + Ingress (with its own cert) so the
    memorable portal.<domain> lands users on the Authentik app launcher — the
    platform's single sign-on front door — without exposing Authentik on a second
    hostname.
    """
    click.echo(f"  Publishing portal redirect {portal_domain} -> Authentik dashboard...")
    manifest = manifests.render(
        'k8s/portal-redirect.yaml.j2',
        portal_domain=portal_domain,
        authentik_domain=authentik_domain,
        cluster_issuer=cluster_issuer,
    )
    remote_path = f"{helm_module.DEPLOY_DIR}/portal-redirect.yaml"
    ssh_utils.run(client, f"mkdir -p {helm_module.DEPLOY_DIR}")
    ssh_utils.upload(client, manifest, remote_path)
    ssh_utils.run(client, f"k3s kubectl apply -f {remote_path}")
    click.echo("  Portal redirect published.")


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


def seed_demo_users(
    ssh: paramiko.SSHClient,
    group_names: list[str],
    password: str,
    email_domain: str,
    username_prefix: str = "demo-",
) -> list[tuple[str, str, str]]:
    """Create one demo user per group, all sharing `password`, each a member of its group.

    Purpose: give every group a ready-to-log-in account for demos and for testing
    the SSO flows end to end (e.g. that a forgejo-admins member lands as a Forgejo
    admin). All users share one password on purpose — these are throwaway demo
    accounts, never real identities.

    Runs against Authentik's Django shell (`ak shell`) over SSH rather than the REST
    API: it needs no API token (the bootstrap token expires on long-lived servers)
    and mirrors the run_with_stdin CLI pattern already used for Forgejo admin
    commands. Idempotent — re-running resets each user's password and re-asserts
    group membership. Groups are created if missing.

    Returns a list of (username, group, "created"|"updated").
    """
    # Values are injected as Python literals via !r so any characters in the
    # password/group names are safely quoted; the script is piped in on stdin.
    script = (
        "from authentik.core.models import User, Group\n"
        f"groups = {list(group_names)!r}\n"
        f"password = {password!r}\n"
        f"email_domain = {email_domain!r}\n"
        f"prefix = {username_prefix!r}\n"
        "for gname in groups:\n"
        "    group, _ = Group.objects.get_or_create(name=gname)\n"
        "    username = prefix + gname\n"
        "    user, created = User.objects.get_or_create(username=username, defaults={'name': 'Demo ' + gname})\n"
        "    user.name = 'Demo ' + gname\n"
        "    user.email = username + '@' + email_domain\n"
        "    user.is_active = True\n"
        "    user.set_password(password)\n"
        "    user.save()\n"
        "    user.ak_groups.add(group)\n"
        "    print('DEMOUSER|' + username + '|' + gname + '|' + ('created' if created else 'updated'))\n"
    )

    out = ssh_utils.run_with_stdin(
        ssh,
        "k3s kubectl exec -n authentik deploy/authentik-server -i -- ak shell",
        script.encode(),
    )

    results: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        if line.startswith("DEMOUSER|"):
            _, username, gname, action = line.split("|", 3)
            results.append((username, gname, action))
    if not results:
        raise RuntimeError(f"seed_demo_users produced no users; ak shell output:\n{out[-500:]}")
    return results


_VERIFIED_EMAIL_MAPPING_NAME = "OAuth Mapping: email (verified)"


def _get_or_create_verified_email_scope_mapping(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the PK of an 'email' scope mapping that asserts email_verified: true.

    Authentik has no email-verification concept, so its built-in email mapping
    hardcodes `email_verified: False`. The kube-apiserver refuses any token with a
    falsy email_verified when --oidc-username-claim=email ("oidc: email not
    verified"), which rejects every Headlamp login. We can't patch the built-in
    mapping: it's blueprint-managed (goauthentik.io/providers/oauth2/scope-email)
    and an Authentik upgrade would silently revert it. So we own a separate mapping
    and attach it only to the kubernetes provider, leaving other apps untouched.

    Trusting these addresses is sound here: they're set by the operator during
    provisioning, not self-registered.
    """
    r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/provider/scope/?scope_name=email", headers=headers)
    r.raise_for_status()
    for m in r.json().get("results", []):
        if m["name"] == _VERIFIED_EMAIL_MAPPING_NAME:
            return m["pk"]

    payload = {
        "name": _VERIFIED_EMAIL_MAPPING_NAME,
        "scope_name": "email",
        "description": "Email claim with email_verified=true, required by kube-apiserver",
        "expression": 'return {"email": request.user.email, "email_verified": True}',
    }
    r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/provider/scope/", method='POST', headers=headers, json_body=payload)
    if not r.ok:
        raise RuntimeError(f"Failed to create verified email scope mapping ({r.status_code}): {r.text}")
    return r.json()["pk"]


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


def _get_scope_mappings(
    ssh: paramiko.SSHClient, base: str, headers: dict, scope_names: list[str],
    timeout: int = 180, interval: int = 6,
) -> list[str]:
    """Return PKs of the built-in OAuth2 scope property mappings for the given scope names.

    The default OAuth2 scope mappings are seeded by an Authentik worker blueprint
    that can finish a few seconds *after* akadmin exists, so a fresh install has a
    window where the user is ready but these mappings are not. Poll until every
    requested scope is present rather than failing on that race. page_size is set
    high so all mappings arrive in one page (the default would truncate).
    """
    managed_keys = {f"goauthentik.io/providers/oauth2/scope-{n}" for n in scope_names}
    deadline = time.time() + timeout
    found: dict[str, str] = {}
    while True:
        r = ssh_utils.cluster_curl(ssh, f"{base}/propertymappings/all/?page_size=1000", headers=headers)
        r.raise_for_status()
        found = {
            m["managed"]: m["pk"]
            for m in r.json()["results"]
            if m.get("managed") in managed_keys
        }
        if len(found) == len(managed_keys):
            return list(found.values())
        if time.time() >= deadline:
            missing = managed_keys - set(found)
            raise RuntimeError(
                f"Scope mappings still missing after {timeout}s: {sorted(missing)}. "
                "Is Authentik fully initialized?"
            )
        click.echo(f"  Waiting for Authentik scope mappings ({len(found)}/{len(managed_keys)})...")
        time.sleep(interval)


def _get_default_signing_key(ssh: paramiko.SSHClient, base: str, headers: dict) -> str:
    """Return the pk of the first available signing certificate."""
    r = ssh_utils.cluster_curl(ssh, f"{base}/crypto/certificatekeypairs/?has_key=true", headers=headers)
    r.raise_for_status()
    results = r.json()["results"]
    if not results:
        raise RuntimeError("No signing key found in Authentik.")
    return results[0]["pk"]
