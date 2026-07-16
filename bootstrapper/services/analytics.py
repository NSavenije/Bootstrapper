"""Umami — cookieless, privacy-first web analytics (no consent banner needed)."""
import shlex

import click
import paramiko

from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import manifests
from bootstrapper.deploy import ssh as ssh_utils

UMAMI_REPO = "https://charts.christianhuth.de"
DB_HOST = "authentik-postgresql.authentik.svc.cluster.local"
DB_NAME = "umami"
DB_USER = "umami"

# Service port is 3000 (the chart does not put it behind :80 like Authentik).
UMAMI_BASE = "http://umami.analytics.svc.cluster.local:3000"
ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "umami"


def _ensure_umami_database(ssh: paramiko.SSHClient, db_password: str) -> None:
    """Create an isolated umami role + database inside the Authentik PostgreSQL.

    Idempotent: the role is created only if missing (its password is always synced),
    and the database only if missing. Reusing the existing Postgres keeps the memory
    footprint to just the Umami app. db_password is token_urlsafe (no quotes), so it
    is safe to inline in the SQL string literals.
    """
    click.echo("  Ensuring umami database in Authentik PostgreSQL...")
    # Authentik's chart disables the `postgres` superuser; the `authentik` app user
    # is itself a superuser, so we connect as that to create the umami role/database.
    super_pw = ssh_utils.run(
        ssh,
        "k3s kubectl get secret -n authentik authentik-postgresql "
        "-o jsonpath=\"{.data.password}\" | base64 -d",
    ).strip()

    sql = (
        "DO $$ BEGIN\n"
        f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{DB_USER}') THEN\n"
        f"    CREATE ROLE {DB_USER} LOGIN PASSWORD '{db_password}';\n"
        "  END IF;\n"
        "END $$;\n"
        f"ALTER ROLE {DB_USER} PASSWORD '{db_password}';\n"
        f"SELECT 'CREATE DATABASE {DB_NAME} OWNER {DB_USER}'\n"
        f"  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{DB_NAME}')\\gexec\n"
    )
    cmd = (
        f"k3s kubectl exec -n authentik authentik-postgresql-0 -i -- "
        f"env PGPASSWORD={shlex.quote(super_pw)} psql -U authentik -d authentik -v ON_ERROR_STOP=1"
    )
    ssh_utils.run_with_stdin(ssh, cmd, sql.encode())
    click.echo("  umami database ready.")


def install_umami(
    ssh: paramiko.SSHClient,
    analytics_domain: str,
    db_password: str,
    app_secret: str,
    cluster_issuer: str = "letsencrypt-prod",
) -> None:
    """Install Umami via Helm (external DB), Traefik Ingress and cert-manager TLS."""
    _ensure_umami_database(ssh, db_password)

    click.echo("  Installing Umami via Helm...")
    helm_module.add_repo(ssh, "christianhuth", UMAMI_REPO)
    helm_module.upgrade_install(
        ssh, "umami", "christianhuth/umami", "analytics",
        manifests.render(
            'helm/umami-values.yaml.j2',
            analytics_domain=analytics_domain,
            db_host=DB_HOST,
            db_name=DB_NAME,
            db_user=DB_USER,
            db_password=db_password,
            app_secret=app_secret,
            cluster_issuer=cluster_issuer,
        ),
        timeout='5m',
    )
    click.echo("  Umami installed.")


def _login(ssh: paramiko.SSHClient, password: str) -> str | None:
    """Return an Umami API token for the admin user, or None if the password is wrong."""
    r = ssh_utils.cluster_curl(
        ssh, f"{UMAMI_BASE}/api/auth/login",
        method='POST',
        headers={"Content-Type": "application/json"},
        json_body={"username": ADMIN_USER, "password": password},
    )
    if not r.ok:
        return None
    return r.json().get("token")


def set_admin_password(ssh: paramiko.SSHClient, new_password: str) -> None:
    """Replace Umami's default admin password with a generated one.

    Umami seeds an `admin` user with the password `umami` on first start, and offers no
    way to override it (no bootstrap env var, unlike Authentik). It also has no OIDC, so
    it cannot hide behind Authentik the way the other services do — the login form is on
    the public internet with a password that is in every copy of the docs. Left alone,
    anyone can read and edit the analytics.

    Idempotent, and safe to run against a server whose password was already rotated: it
    tries the generated password first and returns if that works. Note this only knows
    the default and the generated password, so if the password was changed by hand to a
    third value, this raises rather than guessing.
    """
    if _login(ssh, new_password):
        click.echo("  Umami admin password already set.")
        return

    token = _login(ssh, DEFAULT_ADMIN_PASSWORD)
    if not token:
        raise RuntimeError(
            "Cannot log into Umami with either the generated or the default password. "
            "If it was changed by hand, update generated_secrets.umami_admin_password "
            "in .bootstrapper-state.yaml to match."
        )

    # v2 exposes the self-service route /api/me/password; there is no
    # /api/users/{id}/password (that 404s).
    r = ssh_utils.cluster_curl(
        ssh, f"{UMAMI_BASE}/api/me/password",
        method='POST',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json_body={"currentPassword": DEFAULT_ADMIN_PASSWORD, "newPassword": new_password},
    )
    if not r.ok:
        raise RuntimeError(f"Failed to set Umami admin password ({r.status_code}): {r.text}")

    if not _login(ssh, new_password):
        raise RuntimeError("Umami accepted the password change but the new password does not work.")
    click.echo("  Umami admin password rotated off the default.")
