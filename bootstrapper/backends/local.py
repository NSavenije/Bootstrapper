from .base import InfrastructureBackend


class LocalBackend(InfrastructureBackend):
    def provision_server(self, api_token: str, ssh_key_path: str, server_type: str, server_name: str, location: str) -> tuple[str, int]:
        return "127.0.0.1", 0
