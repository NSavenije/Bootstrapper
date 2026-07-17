import sys
import click
import yaml


def load(config_path: str | None, overrides: dict | None) -> dict:
    """
    Load config from YAML file (if provided) and apply CLI overrides on top.
    Returns a fully resolved config dict.
    """
    cfg = {}
    if config_path:
        with open(config_path, 'r') as f:
            cfg = yaml.safe_load(f) or {}

    # Top-level overrides (CLI takes precedence)
    for key, value in overrides.items():
        if value is not None:
            cfg[key] = value

    # Apply defaults for required top-level keys
    cfg.setdefault('provider', 'hetzner')
    cfg.setdefault('server_type', 'cpx21')
    cfg.setdefault('server_name', 'bootstrap-server')
    cfg.setdefault('location', 'fsn1')
    cfg.setdefault('forgejo_version', '14.0.2')
    cfg.setdefault('authentik_version', '2024.10.5')
    cfg.setdefault('argocd_domain', None)
    cfg.setdefault('blog_domain', None)
    cfg.setdefault('headlamp_domain', None)  # optional; enables the Headlamp k8s UI
    cfg.setdefault('portal_domain', None)    # optional; friendly redirect to the Authentik dashboard
    cfg.setdefault('analytics_domain', None)  # optional; enables Umami cookieless analytics
    # optional; enables GitHub push-mirrors + encrypted DB dumps (private repos)
    cfg.setdefault('github_mirror_token', None)

    # Local-provider defaults: no cloud token/key needed; SSH user may differ from root
    if cfg['provider'] == 'local':
        local = cfg.get('local', {})
        cfg.setdefault('api_token', '')
        cfg.setdefault('ssh_key', '')
        cfg['ssh_user'] = local.get('ssh_user', 'root')
    else:
        cfg['ssh_user'] = 'root'

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    """Fail fast with clear messages for missing required fields."""
    is_local = cfg.get('provider') == 'local'
    required = {
        'ssh_private_key': 'SSH private key path (config ssh_private_key)',
    }
    if not is_local:
        required['api_token'] = 'Cloud provider API token (--api-token or config api_token)'
        required['ssh_key'] = 'SSH public key path (--ssh-key or config ssh_key)'
    for key, description in required.items():
        if not cfg.get(key):
            click.echo(f"Error: missing required config: {description}", err=True)
            sys.exit(1)

    for service in ('forgejo', 'authentik'):
        svc = cfg.get(service, {})
        for field in ('admin_username', 'admin_password', 'domain', 'email'):
            if not svc.get(field):
                click.echo(f"Error: missing required config: {service}.{field}", err=True)
                sys.exit(1)
        if len(svc.get('admin_password', '')) < 8:
            click.echo(f"Error: {service}.admin_password must be at least 8 characters", err=True)
            sys.exit(1)

    if not cfg.get('argocd_domain'):
        click.echo("Error: missing required config: argocd_domain (e.g. argocd.yourdomain.nl)", err=True)
        sys.exit(1)
