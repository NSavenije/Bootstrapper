import click
from hcloud import Client
from hcloud.images import Image
from hcloud.server_types import ServerType
from hcloud.locations import Location
from hcloud.firewalls import FirewallRule, FirewallResource

from .base import InfrastructureBackend

# Ports that must be reachable from the internet. Everything else is blocked.
_FIREWALL_RULES = [
    FirewallRule(direction="in", protocol="icmp", source_ips=["0.0.0.0/0", "::/0"]),
    FirewallRule(direction="in", protocol="tcp", port="22",   source_ips=["0.0.0.0/0", "::/0"]),
    FirewallRule(direction="in", protocol="tcp", port="80",   source_ips=["0.0.0.0/0", "::/0"]),
    FirewallRule(direction="in", protocol="tcp", port="443",  source_ips=["0.0.0.0/0", "::/0"]),
    FirewallRule(direction="in", protocol="udp", port="443",  source_ips=["0.0.0.0/0", "::/0"]),
    FirewallRule(direction="in", protocol="tcp", port="2222", source_ips=["0.0.0.0/0", "::/0"]),
]


class HetznerBackend(InfrastructureBackend):
    def provision_server(self, api_token: str, ssh_key_path: str, server_type: str, server_name: str, location: str) -> tuple[str, int]:
        client = Client(token=api_token)

        with open(ssh_key_path, 'r') as f:
            public_key = f.read().strip()

        # Reuse existing key if fingerprint matches
        ssh_key_obj = None
        for key in client.ssh_keys.get_all():
            if key.public_key.strip() == public_key:
                ssh_key_obj = key
                break
        if not ssh_key_obj:
            ssh_key_obj = client.ssh_keys.create(
                name=f"bootstrapper-{server_name}",
                public_key=public_key,
            )

        # Reuse existing server if one with this name already exists (e.g. previous run crashed)
        existing = client.servers.get_by_name(server_name)
        if existing:
            click.echo(f"  Server '{server_name}' already exists, reusing it.")
            _apply_firewall(client, server_name, existing)
            return existing.public_net.ipv4.ip, existing.id

        response = client.servers.create(
            name=server_name,
            server_type=ServerType(name=server_type),
            image=Image(name="ubuntu-24.04"),
            location=Location(name=location),
            ssh_keys=[ssh_key_obj],
        )

        click.echo("  Waiting for server to become active...")
        response.action.wait_until_finished()
        for action in response.next_actions:
            action.wait_until_finished()

        server = response.server
        server.reload()
        _apply_firewall(client, server_name, server)
        return server.public_net.ipv4.ip, server.id


def _apply_firewall(client: Client, server_name: str, server) -> None:
    """Create (or reuse) a firewall allowing only necessary ports and attach it to the server."""
    firewall_name = f"{server_name}-fw"

    existing_fw = client.firewalls.get_by_name(firewall_name)
    if existing_fw:
        firewall = existing_fw
        click.echo(f"  Firewall '{firewall_name}' already exists, reusing it.")
    else:
        result = client.firewalls.create(name=firewall_name, rules=_FIREWALL_RULES)
        firewall = result.firewall
        click.echo(f"  Firewall '{firewall_name}' created (allowed inbound: 22, 80, 443, 2222).")

    actions = client.firewalls.apply_to_resources(
        firewall,
        [FirewallResource(type="server", server=server)],
    )
    for action in actions:
        action.wait_until_finished()
    click.echo("  Firewall attached to server.")
