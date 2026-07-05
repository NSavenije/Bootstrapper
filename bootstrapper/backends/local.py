import click
from .base import InfrastructureBackend


class LocalBackend(InfrastructureBackend):
    def __init__(self, local_cfg: dict = None):
        self._host = (local_cfg or {}).get('host', '127.0.0.1')

    def provision_server(self, api_token: str, ssh_key_path: str, server_type: str, server_name: str, location: str) -> tuple[str, int]:
        click.echo(f"  Using local target: {self._host}")
        return self._host, 0
