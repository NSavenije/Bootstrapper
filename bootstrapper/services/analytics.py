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
