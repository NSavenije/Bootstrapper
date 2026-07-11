import sys
import click

# Force UTF-8 output on Windows so Unicode in remote command output doesn't crash.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from bootstrapper import config as cfg_module
from bootstrapper import secrets as secrets_module
from bootstrapper.backends.hetzner import HetznerBackend
from bootstrapper.backends.local import LocalBackend
from bootstrapper.deploy import docker as docker_module
from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import ssh as ssh_module
from bootstrapper.services import analytics as analytics_module
from bootstrapper.services import argocd as argocd_module
from bootstrapper.services import authentik as authentik_module
from bootstrapper.services import forgejo as forgejo_module
from bootstrapper.services import headlamp as headlamp_module
from bootstrapper.services import k8s as k8s_module
from bootstrapper.services import sso as sso_module


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option('-v', '--verbose', is_flag=True, default=False, help='Print every SSH command and its output.')
@click.pass_context
def cli(ctx, verbose):
    """Bootstrapper CLI for self-hosted platform provisioning."""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ssh_module.set_verbose(verbose)


@cli.command()
@click.option('--config', 'config_path', type=click.Path(exists=True), help='Path to YAML config file')
@click.option('--provider', default=None, help='Cloud provider: hetzner or local (overrides config)')
@click.option('--forgejo-version', default=None, help='Forgejo Docker image tag (overrides config)')
@click.option('--authentik-version', default=None, help='Authentik Docker image tag (overrides config)')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH public key path (overrides config)')
@click.option('--api-token', default=None, help='Cloud provider API token (overrides config)')
def provision(config_path, provider, forgejo_version, authentik_version, ssh_key, api_token):
    """Provision infrastructure and deploy the full developer platform."""
    cfg = cfg_module.load(config_path, {
        'provider': provider,
        'forgejo_version': forgejo_version,
        'authentik_version': authentik_version,
        'ssh_key': ssh_key,
        'api_token': api_token,
    })

    forgejo_cfg = cfg['forgejo']
    authentik_cfg = cfg['authentik']

    # Load or create state (for idempotency and secret persistence)
    state = secrets_module.load_state()
    gen = secrets_module.generate(state)

    # --- Step 1: Provision server ---
    click.echo(f"\n[1/6] Provisioning server on {cfg['provider']}...")
    # For local, provision_server is instant (returns configured host) — always call it
    # so the state file's IP from a previous cloud run is never accidentally reused.
    if cfg['provider'] != 'local' and state.get('server_ip'):
        click.echo(f"  Reusing existing server at {state['server_ip']} (from state file).")
        server_ip = state['server_ip']
    else:
        backend = _get_backend(cfg)
        server_ip, server_id = backend.provision_server(
            api_token=cfg['api_token'],
            ssh_key_path=cfg['ssh_key'],
            server_type=cfg['server_type'],
            server_name=cfg['server_name'],
            location=cfg['location'],
        )
        state['server_ip'] = server_ip
        state['server_id'] = server_id
        secrets_module.save_state(state)
        click.echo(f"  Server provisioned: {server_ip}")

    # --- Step 2: SSH + install Docker + k3s ---
    click.echo("\n[2/6] Connecting via SSH, installing Docker and k3s...")
    ssh = ssh_module.connect(
        server_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    docker_module.install_docker(ssh)
    k8s_module.install_k3s(ssh)

    # --- Step 3: Install platform services via Helm ---
    click.echo("\n[3/6] Installing Helm, cert-manager, Forgejo and Authentik...")
    forgejo_version = forgejo_module.resolve_forgejo_version(cfg['forgejo_version'])
    helm_module.install_helm(ssh)
    k8s_module.restore_tls_secrets(ssh, state.get('tls_secrets', {}))
    if cfg['provider'] == 'local':
        cluster_issuer = k8s_module.install_cert_manager_selfsigned(ssh)
    else:
        cluster_issuer = k8s_module.install_cert_manager(ssh, authentik_cfg['email'])
    forgejo_module.install_forgejo(
        ssh,
        forgejo_cfg['domain'],
        forgejo_cfg['admin_username'],
        forgejo_cfg['admin_password'],
        forgejo_cfg['email'],
        forgejo_version,
        cluster_issuer=cluster_issuer,
    )
    authentik_module.install_authentik(
        ssh,
        authentik_cfg['domain'],
        gen['authentik_secret_key'],
        gen['authentik_bootstrap_token'],
        authentik_cfg['admin_password'],
        authentik_cfg['email'],
        gen['authentik_db_password'],
        cluster_issuer=cluster_issuer,
    )

    # --- Step 4: Configure platform services ---
    click.echo("\n[4/6] Configuring Forgejo, Authentik and SSO...")

    # Start the bootstrap curl pod for intra-cluster HTTP calls
    ssh_module.start_curl_pod(ssh)
    try:
        forgejo_module.wait_for_forgejo(ssh)
        forgejo_module.create_admin(
            ssh,
            forgejo_cfg['admin_username'],
            forgejo_cfg['admin_password'],
            forgejo_cfg['email'],
        )
        forgejo_api_token = forgejo_module.create_api_token(
            ssh,
            forgejo_cfg['admin_username'],
            forgejo_cfg['admin_password'],
        )

        # Runner token — fetch once, persist in state (idempotent)
        runner_token = state.get('runner_token')
        if not runner_token:
            runner_token = forgejo_module.create_runner_token(
                ssh, forgejo_cfg['admin_username'], forgejo_api_token,
            )
            state['runner_token'] = runner_token
            secrets_module.save_state(state)

        authentik_module.wait_for_authentik(ssh, gen['authentik_bootstrap_token'])
        authentik_module.sync_akadmin(
            ssh,
            gen['authentik_bootstrap_token'],
            authentik_cfg['admin_password'],
            authentik_cfg['email'],
        )
        client_id, client_secret = authentik_module.configure_oauth_provider(
            ssh,
            gen['authentik_bootstrap_token'],
            forgejo_cfg['domain'],
        )
        click.echo("  Creating Authentik groups...")
        authentik_module.create_groups(
            ssh,
            gen['authentik_bootstrap_token'],
            authentik_cfg.get('groups', authentik_module.DEFAULT_GROUPS),
        )

        click.echo("\n  Configuring SSO (Authentik -> Forgejo)...")
        sso_module.configure_forgejo_oauth_source(
            ssh,
            authentik_cfg['domain'],
            client_id,
            client_secret,
            admin_group=authentik_cfg.get('admin_group', 'forgejo-admins'),
        )

        # Save service state
        state['forgejo_api_token'] = forgejo_api_token
        state['authentik_client_id'] = client_id
        state['authentik_client_secret'] = client_secret
        secrets_module.save_state(state)

        # --- Step 5: Install Argo CD + runner ---
        click.echo("\n[5/6] Installing Argo CD, configuring SSO and deploying runner...")
        k8s_client_id, k8s_client_secret = authentik_module.configure_k3s_oidc(
            ssh, gen['authentik_bootstrap_token'],
            headlamp_domain=cfg.get('headlamp_domain'),
        )
        if cfg.get('headlamp_domain'):
            headlamp_module.install_headlamp(
                ssh, cfg['headlamp_domain'], authentik_cfg['domain'],
                k8s_client_id, k8s_client_secret, cluster_issuer=cluster_issuer,
            )
            headlamp_module.configure_oidc_rbac(
                ssh,
                authentik_cfg.get('groups', authentik_module.DEFAULT_GROUPS),
                admin_group=authentik_cfg.get('admin_group', 'forgejo-admins'),
            )
        argocd_module.install_argocd(ssh, cfg['argocd_domain'], cluster_issuer=cluster_issuer)
        argocd_module.configure_argocd_sso(
            ssh,
            gen['authentik_bootstrap_token'],
            authentik_cfg['domain'],
            cfg['argocd_domain'],
            forgejo_cfg['domain'],
            forgejo_cfg['admin_username'],
            forgejo_api_token,
        )
        forgejo_module.deploy_runner(
            ssh,
            runner_token,
            # Register on the PUBLIC url, not cluster-internal DNS: CI jobs run as
            # host-Docker sibling containers (not on the pod network), so the
            # instance URL leaks into every job's github.server_url and must be
            # resolvable + reachable from there (checkout, registry, API).
            forgejo_url=f"https://{forgejo_cfg['domain']}",
            forgejo_domain=forgejo_cfg['domain'],
        )

        if cfg.get('portal_domain'):
            authentik_module.install_portal_redirect(
                ssh, cfg['portal_domain'], authentik_cfg['domain'], cluster_issuer=cluster_issuer,
            )

        if cfg.get('analytics_domain'):
            analytics_module.install_umami(
                ssh, cfg['analytics_domain'],
                gen['umami_db_password'], gen['umami_app_secret'],
                cluster_issuer=cluster_issuer,
            )

        # --- Step 6: Seed platform-config repository ---
        click.echo("\n[6/6] Seeding platform-config repository in Forgejo...")
        forgejo_module.create_platform_org(ssh, forgejo_api_token)
        forgejo_module.seed_platform_config(ssh, forgejo_api_token, forgejo_cfg['domain'])

    finally:
        ssh_module.stop_curl_pod(ssh)

    # Save any TLS secrets that are already issued — used to skip ACME on reprovision
    tls_secrets = k8s_module.save_tls_secrets(ssh)
    if tls_secrets:
        state['tls_secrets'] = tls_secrets
        secrets_module.save_state(state)

    ssh.close()

    # --- Summary ---
    argocd_domain = cfg['argocd_domain']
    blog_line = f"\n  Blog:      https://{cfg['blog_domain']}" if cfg.get('blog_domain') else ""
    headlamp_line = f"\n  Headlamp:  https://{cfg['headlamp_domain']}" if cfg.get('headlamp_domain') else ""
    portal_line = f"\n  Portal:    https://{cfg['portal_domain']}  (-> Authentik dashboard)" if cfg.get('portal_domain') else ""
    analytics_line = f"\n  Analytics: https://{cfg['analytics_domain']}  (Umami; login admin/umami — change it)" if cfg.get('analytics_domain') else ""
    blog_hosts = f"\n  {server_ip}  {cfg['blog_domain']}" if cfg.get('blog_domain') else ""

    if cfg['provider'] == 'local':
        tls_note = "Certs are self-signed — browsers will warn; add an exception or import the CA."
        hosts_block = (
            f"Add these entries to /etc/hosts (or your local DNS):\n"
            f"  {server_ip}  {forgejo_cfg['domain']}\n"
            f"  {server_ip}  {authentik_cfg['domain']}\n"
            f"  {server_ip}  {argocd_domain}{blog_hosts}"
        )
        post_setup = (
            f"Post-setup:\n"
            f"  1. Add /etc/hosts entries above.\n"
            f"  2. Wire k3s OIDC (no DNS propagation needed for self-signed):\n"
            f"     bootstrapper wire-k3s-oidc --config {config_path}\n"
            f"  3. Configure platform-config repo secrets (KUBECONFIG, PLATFORM_TOKEN):\n"
            f"     https://{forgejo_cfg['domain']}/platform-team/platform-config/settings/secrets"
        )
    else:
        blog_dns = f"\n  {cfg['blog_domain']}  ->  {server_ip}" if cfg.get('blog_domain') else ""
        tls_note = "TLS certificates will auto-provision via cert-manager after DNS propagates."
        hosts_block = (
            f"Server IP:  {server_ip}\n"
            f"Add these DNS A records:\n"
            f"  {forgejo_cfg['domain']}  ->  {server_ip}\n"
            f"  {authentik_cfg['domain']}  ->  {server_ip}\n"
            f"  {argocd_domain}  ->  {server_ip}{blog_dns}"
        )
        post_setup = (
            f"Post-setup:\n"
            f"  1. Add DNS records above, then {tls_note}\n"
            f"  2. After DNS propagates, wire k3s OIDC:\n"
            f"     bootstrapper wire-k3s-oidc --config {config_path}\n"
            f"  3. Configure platform-config repo secrets (KUBECONFIG, PLATFORM_TOKEN):\n"
            f"     https://{forgejo_cfg['domain']}/platform-team/platform-config/settings/secrets"
        )

    click.echo(f"""
Bootstrap complete!

{hosts_block}

Services:
  Forgejo:   https://{forgejo_cfg['domain']}
  Authentik: https://{authentik_cfg['domain']}
  Argo CD:   https://{argocd_domain}{headlamp_line}{portal_line}{analytics_line}{blog_line}

Admin credentials (saved to .bootstrapper-state.yaml):
  Forgejo:   {forgejo_cfg['admin_username']} / {forgejo_cfg['admin_password']}
  Authentik: akadmin / {authentik_cfg['admin_password']}

{post_setup}
""")

@cli.command('wire-k3s-oidc')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
def wire_k3s_oidc(ssh_key, config_path):
    """Add OIDC flags to k3s config and restart k3s."""
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    domain = cfg['authentik']['domain']

    state = secrets_module.load_state()
    ssh = ssh_module.connect(
        state['server_ip'], cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    try:
        k8s_module.wire_oidc(ssh, domain)
        sso_module.configure_forgejo_oauth_source(
            ssh,
            domain,
            state['authentik_client_id'],
            state['authentik_client_secret'],
            admin_group=cfg['authentik'].get('admin_group', 'forgejo-admins'),
            public=True,
        )
    finally:
        ssh.close()

@cli.command('seed-demo-users')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
@click.option('--password', default=None, help='Password for all demo users (default: authentik.admin_password from config)')
def seed_demo_users(config_path, ssh_key, server_ip, password):
    """Create one demo user per Authentik group for testing and live demos.

    For every group under `authentik.groups` in the config, creates a member named
    `demo-<group>` (e.g. demo-forgejo-admins), all sharing one password so you can
    log in as any role instantly. Idempotent — re-run to reset passwords. Demo
    accounts only; do not use for real identities.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    authentik_cfg = cfg['authentik']
    groups = authentik_cfg.get('groups', authentik_module.DEFAULT_GROUPS)
    pw = password or authentik_cfg['admin_password']
    email_domain = authentik_cfg['email'].split('@', 1)[-1]

    target_ip = server_ip or secrets_module.load_state().get('server_ip')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")

    click.echo(f"Seeding {len(groups)} demo user(s) on {target_ip}...")
    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    try:
        results = authentik_module.seed_demo_users(ssh, groups, pw, email_domain)
    finally:
        ssh.close()

    click.echo("\nDemo users ready (all share the same password):\n")
    width = max(len(u) for u, _, _ in results)
    for username, group, action in results:
        click.echo(f"  {username:<{width}}  group={group:<18} [{action}]")
    click.echo(f"\n  Password: {pw}")
    click.echo(f"  Sign in at: https://{authentik_cfg['domain']}\n")


@cli.command('install-headlamp')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
@click.option('--headlamp-domain', default=None, help='Headlamp hostname (overrides config headlamp_domain)')
def install_headlamp_cmd(config_path, ssh_key, server_ip, headlamp_domain):
    """Install the Headlamp Kubernetes UI with Authentik SSO on an existing platform.

    Reuses the 'kubernetes' OIDC client, adds Headlamp's callback, points the
    Kubernetes app tile at Headlamp, deploys the chart, and binds Authentik groups
    to cluster roles. Add a DNS A record for the hostname -> server IP for TLS.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    authentik_cfg = cfg['authentik']
    domain = headlamp_domain or cfg.get('headlamp_domain')
    if not domain:
        raise click.UsageError("No Headlamp domain: pass --headlamp-domain or set headlamp_domain in config.")

    state = secrets_module.load_state()
    target_ip = server_ip or state.get('server_ip')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")

    click.echo(f"Installing Headlamp at {domain} on {target_ip}...")
    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    ssh_module.start_curl_pod(ssh)
    try:
        token = authentik_module.ensure_admin_token(
            ssh, state.get('generated_secrets', {}).get('authentik_bootstrap_token'),
        )
        client_id, client_secret = authentik_module.configure_k3s_oidc(ssh, token, headlamp_domain=domain)
        headlamp_module.install_headlamp(ssh, domain, authentik_cfg['domain'], client_id, client_secret)
        headlamp_module.configure_oidc_rbac(
            ssh,
            authentik_cfg.get('groups', authentik_module.DEFAULT_GROUPS),
            admin_group=authentik_cfg.get('admin_group', 'forgejo-admins'),
        )
    finally:
        ssh_module.stop_curl_pod(ssh)
        ssh.close()
    click.echo(f"""
Headlamp installed. Final step — add this DNS record so TLS can issue:
  {domain}  ->  {target_ip}

Then open https://{domain} (or click the Kubernetes tile in Authentik) and sign in.
""")


@cli.command('install-analytics')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
@click.option('--analytics-domain', default=None, help='Umami hostname (overrides config analytics_domain)')
def install_analytics(config_path, ssh_key, server_ip, analytics_domain):
    """Install Umami cookieless analytics (no consent banner) on an existing platform.

    Runs lean: reuses the Authentik PostgreSQL with an isolated umami database.
    Add a DNS A record for the hostname -> server IP so its TLS cert can issue.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    domain = analytics_domain or cfg.get('analytics_domain')
    if not domain:
        raise click.UsageError("No analytics domain: pass --analytics-domain or set analytics_domain in config.")

    state = secrets_module.load_state()
    gen = secrets_module.generate(state)
    secrets_module.save_state(state)
    target_ip = server_ip or state.get('server_ip')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")

    click.echo(f"Installing Umami at {domain} on {target_ip}...")
    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    try:
        analytics_module.install_umami(
            ssh, domain, gen['umami_db_password'], gen['umami_app_secret'],
        )
    finally:
        ssh.close()
    click.echo(f"""
Umami installed. Add this DNS record so its TLS cert can issue:
  {domain}  ->  {target_ip}

Then open https://{domain} and sign in with admin / umami (change the password).
Add a website there for your blog, copy its tracking snippet into the site's
<head>, and — being cookieless — you need no consent banner.
""")


@cli.command('install-portal')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
@click.option('--portal-domain', default=None, help='Portal hostname (overrides config portal_domain)')
def install_portal(config_path, ssh_key, server_ip, portal_domain):
    """Publish a friendly portal.<domain> that redirects to the Authentik dashboard.

    Deploys a Traefik redirect + cert. Add a DNS A record for the hostname ->
    server IP so its TLS cert can issue.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    domain = portal_domain or cfg.get('portal_domain')
    if not domain:
        raise click.UsageError("No portal domain: pass --portal-domain or set portal_domain in config.")

    state = secrets_module.load_state()
    target_ip = server_ip or state.get('server_ip')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")

    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    try:
        authentik_module.install_portal_redirect(ssh, domain, cfg['authentik']['domain'])
    finally:
        ssh.close()
    click.echo(f"""
Portal published. Add this DNS record so its TLS cert can issue:
  {domain}  ->  {target_ip}

Then https://{domain} lands users on your Authentik app dashboard.
""")


@cli.command('build-runner-image')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
def build_runner_image(config_path, ssh_key, server_ip):
    """(Re)build the baked CI runner image and refresh the runner to use it.

    Builds platform-ci:latest on the host, re-applies the runner config (mapping
    ubuntu-latest/docker/ci labels to it) and restarts the runner. Use this to
    iterate on the image without a full re-provision.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    state = secrets_module.load_state()
    target_ip = server_ip or state.get('server_ip')
    runner_token = state.get('runner_token')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")
    if not runner_token:
        raise click.UsageError("No runner_token in state; run a full provision first.")

    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    try:
        forgejo_module.deploy_runner(
            ssh,
            runner_token,
            # Public url — CI jobs are host-Docker containers that can't resolve
            # cluster DNS; the registration URL becomes each job's github.server_url.
            forgejo_url=f"https://{cfg['forgejo']['domain']}",
            forgejo_domain=cfg['forgejo']['domain'],
        )
    finally:
        ssh.close()
    click.echo("\nRunner image rebuilt and runner refreshed. Jobs now start from the baked image.")


@cli.command('server-types')
@click.option('--api-token', required=True, envvar='HCLOUD_TOKEN', help='Hetzner API token')
def server_types(api_token):
    """List available (non-deprecated) Hetzner server types."""
    from hcloud import Client
    client = Client(token=api_token)
    types = [t for t in client.server_types.get_all() if not t.deprecation]
    types.sort(key=lambda t: t.memory)
    click.echo(f"{'Name':<12} {'vCPU':>5} {'RAM (GB)':>9} {'Disk (GB)':>10}  Architecture")
    click.echo("-" * 55)
    for t in types:
        click.echo(f"{t.name:<12} {t.cores:>5} {t.memory:>9.1f} {t.disk:>10}  {t.architecture}")


def _get_backend(cfg: dict):
    provider = cfg['provider']
    if provider == 'hetzner':
        return HetznerBackend()
    if provider == 'local':
        return LocalBackend(cfg.get('local', {}))
    raise click.UsageError(f"Unknown provider '{provider}'. Supported: hetzner, local")
