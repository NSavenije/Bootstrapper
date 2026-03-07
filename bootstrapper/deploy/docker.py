import click
import paramiko

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
