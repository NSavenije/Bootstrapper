import click

from bootstrapper import config as cfg_module
from bootstrapper import secrets as secrets_module
from bootstrapper.backends.hetzner import HetznerBackend
from bootstrapper.backends.local import LocalBackend
from bootstrapper.deploy import docker as docker_module
from bootstrapper.deploy import helm as helm_module
from bootstrapper.deploy import ssh as ssh_module
from bootstrapper.services import argocd as argocd_module
from bootstrapper.services import authentik as authentik_module
from bootstrapper.services import forgejo as forgejo_module
from bootstrapper.services import k8s as k8s_module
from bootstrapper.services import sso as sso_module


@click.group()
def cli():
    """Bootstrapper CLI for self-hosted platform provisioning."""
    pass


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
    if state.get('server_ip'):
        click.echo(f"  Reusing existing server at {state['server_ip']} (from state file).")
        server_ip = state['server_ip']
    else:
        backend = _get_backend(cfg['provider'])
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
    ssh = ssh_module.connect(server_ip, cfg['ssh_private_key'])
    docker_module.install_docker(ssh)
    k8s_module.install_k3s(ssh)

    # --- Step 3: Install platform services via Helm ---
    click.echo("\n[3/6] Installing Helm, cert-manager, Forgejo and Authentik...")
    forgejo_version = forgejo_module.resolve_forgejo_version(cfg['forgejo_version'])
    helm_module.install_helm(ssh)
    k8s_module.restore_tls_secrets(ssh, state.get('tls_secrets', {}))
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
        secrets_module.save_state(state)

        # --- Step 5: Install Argo CD + runner ---
        click.echo("\n[5/6] Installing Argo CD, configuring SSO and deploying runner...")
        authentik_module.configure_k3s_oidc(ssh, gen['authentik_bootstrap_token'])
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
            forgejo_url="http://forgejo-http.forgejo.svc.cluster.local:3000",
            forgejo_domain=forgejo_cfg['domain'],
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
    blog_dns = f"\n  {cfg['blog_domain']}  ->  {server_ip}" if cfg.get('blog_domain') else ""

    click.echo(f"""
Bootstrap complete!

Server IP:  {server_ip}
Add these DNS A records:
  {forgejo_cfg['domain']}  ->  {server_ip}
  {authentik_cfg['domain']}  ->  {server_ip}
  {argocd_domain}  ->  {server_ip}{blog_dns}

Services (available after DNS + TLS):
  Forgejo:   https://{forgejo_cfg['domain']}
  Authentik: https://{authentik_cfg['domain']}
  Argo CD:   https://{argocd_domain}{blog_line}

Admin credentials (saved to .bootstrapper-state.yaml):
  Forgejo:   {forgejo_cfg['admin_username']} / {forgejo_cfg['admin_password']}
  Authentik: akadmin / {authentik_cfg['admin_password']}

Post-setup:
  1. Add DNS records above, then TLS certificates will auto-provision via cert-manager.
  2. After DNS propagates, wire k3s OIDC (SSH to server):
     printf 'kube-apiserver-arg:\\n  - oidc-issuer-url=https://{authentik_cfg['domain']}/application/o/kubernetes/\\n  - oidc-client-id=kubernetes\\n  - oidc-username-claim=email\\n  - oidc-groups-claim=groups\\n' >> /etc/rancher/k3s/config.yaml
     /usr/local/bin/k3s-killall.sh && systemctl start k3s
  3. Configure platform-config repo secrets (KUBECONFIG, PLATFORM_TOKEN):
     https://{forgejo_cfg['domain']}/platform-team/platform-config/settings/secrets
""")


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


def _get_backend(provider: str):
    if provider == 'hetzner':
        return HetznerBackend()
    if provider == 'local':
        return LocalBackend()
    raise click.UsageError(f"Unknown provider '{provider}'. Supported: hetzner, local")
