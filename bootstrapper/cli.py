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
from bootstrapper.services import gitops as gitops_module
from bootstrapper.services import k8s as k8s_module


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

    # --- Step 3: Install Helm + Argo CD, then let Argo CD deploy the platform ---
    click.echo("\n[3/6] Installing Helm and Argo CD; applying platform Applications...")
    forgejo_version = forgejo_module.resolve_forgejo_version(cfg['forgejo_version'])
    state['forgejo_version'] = forgejo_version
    secrets_module.save_state(state)
    helm_module.install_helm(ssh)
    k8s_module.restore_tls_secrets(ssh, state.get('tls_secrets', {}))
    cluster_issuer = 'selfsigned' if cfg['provider'] == 'local' else 'letsencrypt-prod'
    # Argo CD is the only service the CLI still installs via Helm (Layer 1);
    # its own Application takes over management in step 5.
    argocd_module.install_argocd(ssh, cfg['argocd_domain'], cluster_issuer=cluster_issuer)
    gitops_module.apply_oci_repo_secret(ssh)

    # The blueprint ConfigMap + pinned-clients Secret must exist before the
    # authentik Application deploys (the worker mounts/reads them).
    authentik_module.apply_wiring_manifests(ssh, cfg, gen)

    # Chart-based Applications are self-contained (registry + inline values),
    # so they can be applied before Forgejo — and thus the gitops repo — exists.
    # Render inputs are filled in as the wiring steps below produce them.
    inputs = {'forgejo_version': forgejo_version}
    gitops_module.apply_apps(ssh, cfg, state, inputs, set(gitops_module.BOOTSTRAP_APPS))
    gitops_module.wait_for_app(ssh, 'cert-manager')
    k8s_module.apply_cluster_issuer(ssh, cfg['provider'], authentik_cfg['email'])
    gitops_module.wait_for_app(ssh, 'forgejo')
    gitops_module.wait_for_app(ssh, 'authentik', timeout=1200)

    # --- Step 4: Configure platform services ---
    click.echo("\n[4/6] Configuring Forgejo, Authentik and SSO...")

    # Start the bootstrap curl pod for intra-cluster HTTP calls
    ssh_module.start_curl_pod(ssh)
    try:
        forgejo_module.wait_for_forgejo(ssh)

        # Authentik wiring is declared in the platform blueprint (groups,
        # providers with pinned client_id/client_secret, mappings, tiles,
        # akadmin) — wait for the worker to have applied it, no API writes.
        authentik_module.wait_for_blueprint(
            ssh, list(authentik_module.OIDC_CLIENT_IDS), timeout=600,
        )

        # Residual Forgejo wiring (platform API token + argocd credential
        # Secrets, runner registration token, Authentik OAuth source) is an
        # idempotent Job — re-run as a PostSync hook once the gitops repo
        # owns it. The admin user itself is chart-managed (gitea.admin).
        forgejo_module.apply_wiring_job(
            ssh,
            forgejo_cfg['domain'],
            authentik_cfg['domain'],
            authentik_cfg.get('admin_group', 'forgejo-admins'),
        )
        forgejo_api_token = forgejo_module.read_platform_token(ssh)

        # Save service state
        state['forgejo_api_token'] = forgejo_api_token
        state['authentik_client_id'] = authentik_module.OIDC_CLIENT_IDS['forgejo']
        state['authentik_client_secret'] = gen['forgejo_oidc_client_secret']
        secrets_module.save_state(state)

        # --- Step 5: SSO wiring + remaining Applications ---
        click.echo("\n[5/6] Wiring k3s/Argo CD SSO and applying remaining Applications...")
        k8s_client_id = authentik_module.OIDC_CLIENT_IDS['kubernetes']
        k8s_client_secret = gen['k8s_oidc_client_secret']
        state['k8s_client_id'] = k8s_client_id
        state['k8s_client_secret'] = k8s_client_secret
        secrets_module.save_state(state)
        inputs['k8s_client_id'] = k8s_client_id
        inputs['k8s_client_secret'] = k8s_client_secret
        if cfg.get('headlamp_domain'):
            gitops_module.apply_apps(ssh, cfg, state, inputs, {'headlamp'})
            gitops_module.wait_for_app(ssh, 'headlamp')

        argocd_client_id = authentik_module.OIDC_CLIENT_IDS['argocd']
        argocd_module.configure_argocd_sso(
            ssh,
            authentik_cfg['domain'],
            cfg['argocd_domain'],
            forgejo_cfg['domain'],
            forgejo_cfg['admin_username'],
            forgejo_api_token,
            oidc_client_id=argocd_client_id,
            oidc_client_secret=gen['argocd_oidc_client_secret'],
        )
        state['argocd_client_id'] = argocd_client_id
        secrets_module.save_state(state)
        inputs['argocd_client_id'] = argocd_client_id
        # Argo CD manages itself from here; its app sync rolls the server once
        # (checksum over the now-OIDC-bearing argocd-cm).
        gitops_module.apply_apps(ssh, cfg, state, inputs, {'argocd'})
        gitops_module.wait_for_app(ssh, 'argocd')

        # The runner Deployment comes from git (runner app, step 6); the CLI
        # only pre-builds the baked CI job image on the host Docker daemon.
        forgejo_module.build_ci_runner_image(ssh)

        if cfg.get('analytics_domain'):
            analytics_module.ensure_umami_database(ssh, gen['umami_db_password'])
            gitops_module.apply_apps(ssh, cfg, state, inputs, {'umami'})
            gitops_module.wait_for_app(ssh, 'umami')
            # Umami ships a default admin/umami login and has no OIDC to hide
            # behind, so a Job rotates it before the Ingress is reachable.
            # (Its Authentik dashboard tile is declared in the platform blueprint.)
            analytics_module.apply_wiring(ssh, gen['umami_admin_password'])

        # --- Step 6: Seed the platform-config and platform-gitops repositories ---
        click.echo("\n[6/6] Seeding platform-config and platform-gitops repositories...")
        forgejo_module.create_platform_org(ssh, forgejo_api_token)
        forgejo_module.seed_platform_config(ssh, forgejo_api_token, forgejo_cfg['domain'])
        # Push the complete gitops tree and apply the root Application: git
        # becomes the source of truth, and the remaining manifest apps
        # (cluster-issuer, headlamp-rbac, portal-redirect, runner) sync from it.
        gitops_module.seed_gitops(ssh, cfg, state)
        for app_name in ('cluster-issuer', 'authentik-blueprint', 'headlamp-rbac',
                         'portal-redirect', 'runner', 'forgejo-wiring', 'umami-wiring'):
            if app_name == 'portal-redirect' and not cfg.get('portal_domain'):
                continue
            if app_name == 'umami-wiring' and not cfg.get('analytics_domain'):
                continue
            gitops_module.wait_for_app(ssh, app_name)

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
    analytics_line = (
        f"\n  Analytics: https://{cfg['analytics_domain']}  "
        f"(Umami; login admin / {gen['umami_admin_password']})"
    ) if cfg.get('analytics_domain') else ""
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
        # The Forgejo OAuth source is owned by the forgejo-wiring Job (it uses
        # the public discovery URL and converges once DNS/TLS answer), so this
        # command only wires the apiserver flags and restarts k3s.
        k8s_module.wire_oidc(ssh, domain)
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
    try:
        # The kubernetes provider (pinned client) and the headlamp redirect
        # URI are declared in the platform blueprint; re-apply it so a
        # headlamp_domain added after provisioning lands there too.
        gen = secrets_module.generate(state)
        authentik_module.apply_wiring_manifests(ssh, cfg, gen)
        inputs = {
            'k8s_client_id': authentik_module.OIDC_CLIENT_IDS['kubernetes'],
            'k8s_client_secret': gen['k8s_oidc_client_secret'],
        }
        gitops_module.apply_apps(ssh, cfg, state, inputs, {'headlamp'})
        gitops_module.wait_for_app(ssh, 'headlamp')
        secrets_module.save_state(state)
    finally:
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
        analytics_module.ensure_umami_database(ssh, gen['umami_db_password'])
        gitops_module.apply_apps(ssh, cfg, state, {}, {'umami'})
        gitops_module.wait_for_app(ssh, 'umami')
        # Rotate the default admin/umami login off the public internet; the
        # Authentik dashboard tile is declared in the platform blueprint
        # (re-apply it so an analytics_domain added later lands there too).
        analytics_module.apply_wiring(ssh, gen['umami_admin_password'])
        authentik_module.apply_wiring_manifests(ssh, cfg, gen)
    finally:
        ssh.close()
    secrets_module.save_state(state)
    click.echo(f"""
Umami installed. Add this DNS record so its TLS cert can issue:
  {domain}  ->  {target_ip}

Then open https://{domain} and sign in with:
  admin / {gen['umami_admin_password']}
(also saved in {secrets_module.STATE_FILE})

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


@cli.command('install-gitops')
@click.option('--config', 'config_path', type=click.Path(exists=True), default='config.yaml', help='Path to YAML config file')
@click.option('--ssh-key', 'ssh_key', type=click.Path(exists=True), default=None, help='SSH private key path (overrides config)')
@click.option('--server-ip', default=None, help='Target server IP (overrides .bootstrapper-state.yaml)')
def install_gitops(config_path, ssh_key, server_ip):
    """Seed the platform-gitops repo and apply the App-of-Apps root Application.

    Renders one Argo CD Application per platform service from the same
    templates `provision` uses (adoption is a no-op diff), pushes them to a
    private platform-team/platform-gitops repo in Forgejo, and applies the
    platform-root Application. Idempotent — re-run after template changes.
    """
    cfg = cfg_module.load(config_path, {'ssh_key': ssh_key} if ssh_key else {})
    state = secrets_module.load_state()
    target_ip = server_ip or state.get('server_ip')
    if not target_ip:
        raise click.UsageError("No server IP: pass --server-ip or provision first (writes state).")
    if not state.get('forgejo_api_token'):
        raise click.UsageError("No forgejo_api_token in state: run provision first.")

    ssh = ssh_module.connect(
        target_ip, cfg['ssh_private_key'],
        username=cfg['ssh_user'],
        use_sudo=(cfg['ssh_user'] != 'root'),
    )
    ssh_module.start_curl_pod(ssh)
    try:
        gitops_module.seed_gitops(ssh, cfg, state)
    finally:
        ssh_module.stop_curl_pod(ssh)
        ssh.close()
    click.echo(f"""
platform-gitops seeded. Argo CD now shows the platform as Applications:
  https://{cfg['argocd_domain']}/applications
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
