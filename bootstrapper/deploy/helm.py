"""Helm operations executed on the remote server via SSH."""
import yaml
import click
import paramiko

from . import ssh as ssh_utils

DEPLOY_DIR = '/opt/bootstrapper'
KUBECONFIG = '/etc/rancher/k3s/k3s.yaml'


def install_helm(client: paramiko.SSHClient) -> None:
    """Download and install the Helm binary on the remote server."""
    click.echo("  Installing Helm...")
    ssh_utils.run(client, "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash")
    click.echo("  Helm installed.")


def add_repo(client: paramiko.SSHClient, name: str, url: str) -> None:
    """Add a Helm chart repository (idempotent)."""
    ssh_utils.run(client, f"helm repo add {name} {url}")
    ssh_utils.run(client, "helm repo update")


def upgrade_install(
    client: paramiko.SSHClient,
    release: str,
    chart: str,
    namespace: str,
    values: dict,
    *,
    create_namespace: bool = True,
    wait: bool = True,
    timeout: str = '10m',
    version: str = None,
) -> None:
    """Run `helm upgrade --install` with values written to a temp file on the server.

    Using a temp file avoids shell-quoting issues with complex values structures.
    """
    values_yaml = yaml.dump(values, default_flow_style=False)
    values_path = f"{DEPLOY_DIR}/helm-values-{release}.yaml"

    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, values_yaml, values_path)

    cmd = (
        f"KUBECONFIG={KUBECONFIG} helm upgrade --install {release} {chart}"
        f" --namespace {namespace}"
        f"{' --create-namespace' if create_namespace else ''}"
        f"{' --wait' if wait else ''}"
        f" --timeout {timeout}"
        f"{f' --version {version}' if version else ''}"
        f" -f {values_path}"
    )
    click.echo(f"  helm upgrade --install {release} {chart} (namespace: {namespace})...")
    ssh_utils.run(client, cmd)
    click.echo(f"  {release} installed/upgraded.")
