import click
import paramiko
import requests

from . import ssh as ssh_utils


def install_docker(client: paramiko.SSHClient) -> None:
    """Install Docker and the Compose plugin on the remote server.

    Docker is still needed on the host for the Forgejo Actions runner,
    which mounts /var/run/docker.sock to run job containers.
    """
    click.echo("  Installing Docker...")
    ssh_utils.run(client, "apt-get update -qq")
    ssh_utils.run(client, "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl")
    ssh_utils.run(client, "curl -fsSL https://get.docker.com | sh")
    ssh_utils.run(client, "systemctl enable --now docker")


def resolve_forgejo_version(version: str) -> str:
    """Resolve 'latest' to the actual latest Forgejo release tag via Codeberg API."""
    if version != 'latest':
        return version
    r = requests.get(
        'https://codeberg.org/api/v1/repos/forgejo/forgejo/releases?limit=1&pre-release=false',
        timeout=10,
    )
    r.raise_for_status()
    tag = r.json()[0]['tag_name'].lstrip('v')  # e.g. "14.0.2"
    click.echo(f"  Resolved Forgejo 'latest' -> {tag}")
    return tag
