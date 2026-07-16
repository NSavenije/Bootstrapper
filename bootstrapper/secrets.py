import secrets as _secrets
import os
import yaml


STATE_FILE = '.bootstrapper-state.yaml'


def load_state() -> dict:
    """Load existing state from disk, or return empty dict."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return yaml.safe_load(f) or {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, 'w') as f:
        yaml.dump(state, f, default_flow_style=False)


def generate(state: dict) -> dict:
    """
    Return generated secrets, reusing any already persisted in state.
    Mutates state in place so new secrets are included on save.
    """
    generated = state.get('generated_secrets', {})
    for key in ('authentik_db_password', 'authentik_secret_key', 'authentik_bootstrap_token',
                'umami_db_password', 'umami_app_secret', 'umami_admin_password',
                # Pinned OIDC client secrets: generated once here, set on the
                # Authentik providers by blueprint, and read by every consumer
                # (Forgejo source, Argo CD, Headlamp) — no cross-service reads.
                'forgejo_oidc_client_secret', 'argocd_oidc_client_secret',
                'k8s_oidc_client_secret',
                # Encrypts DB dumps before they leave the cluster; losing it
                # makes every backup unreadable — back up the state file.
                'backup_encryption_key'):
        if key not in generated:
            generated[key] = _secrets.token_urlsafe(32)
    state['generated_secrets'] = generated
    return generated
