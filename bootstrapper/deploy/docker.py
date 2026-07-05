import click
import paramiko

from . import ssh as ssh_utils


def install_docker(client: paramiko.SSHClient) -> None:
    """Install Docker on the remote server, skipping if already present.

    Docker is needed on the host for the Forgejo Actions runner,
    which mounts /var/run/docker.sock to run job containers.
    On WSL2 with Docker Desktop the socket is already available, so this is a no-op.
    """
    try:
        ssh_utils.run(client, "docker version")
        click.echo("  Docker already installed, skipping.")
        return
    except RuntimeError:
        pass

    click.echo("  Installing Docker...")
    ssh_utils.run(client, "apt-get update -qq")
    ssh_utils.run(client, "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl")
    ssh_utils.run(client, "curl -fsSL https://get.docker.com | sh")
    ssh_utils.run(client, "systemctl enable --now docker")
