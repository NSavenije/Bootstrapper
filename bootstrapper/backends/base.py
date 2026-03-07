class InfrastructureBackend:
    def provision_server(self, api_token: str, ssh_key_path: str, server_type: str, server_name: str, location: str) -> tuple[str, int]:
        """Provision a server and return (ip_address, server_id)."""
        raise NotImplementedError()
