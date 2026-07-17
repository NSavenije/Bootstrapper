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
    values: dict | str,
    *,
    create_namespace: bool = True,
    wait: bool = True,
    timeout: str = '10m',
    version: str = None,
) -> None:
    """Run `helm upgrade --install` with values written to a temp file on the server.

    Using a temp file avoids shell-quoting issues with complex values structures.
    `values` may be a pre-rendered YAML string (from a Jinja2 template) or a dict.
    """
    values_yaml = values if isinstance(values, str) else yaml.dump(values, default_flow_style=False)
    values_path = f"{DEPLOY_DIR}/helm-values-{release}.yaml"

    ssh_utils.run(client, f"mkdir -p {DEPLOY_DIR}")
    ssh_utils.upload(client, values_yaml, values_path)

    # A release whose FIRST install failed (e.g. bad image tag, mid-run crash) has
    # no deployed revision, and `helm upgrade --install` refuses it. Uninstall the
    # failed transaction so re-running provision can recover unattended.
    status_cmd = (
        f"helm status {release} --kubeconfig {KUBECONFIG} --namespace {namespace} "
        f"-o yaml 2>/dev/null | grep -E '^  (status|first_deployed):' || true"
    )
    status_out = ssh_utils.run(client, status_cmd)
    if 'status: failed' in status_out or 'status: pending-install' in status_out:
        history_cmd = (
            f"helm history {release} --kubeconfig {KUBECONFIG} --namespace {namespace} "
            f"2>/dev/null | grep -c deployed || true"
        )
        if ssh_utils.run(client, history_cmd).strip() == '0':
            click.echo(f"  Release {release} has a failed initial install; removing it before retry.")
            ssh_utils.run(client, f"helm uninstall {release} --kubeconfig {KUBECONFIG} --namespace {namespace}")

    parts = [
        f"helm upgrade --install {release} {chart}",
        f"--kubeconfig {KUBECONFIG}",
        f"--namespace {namespace}",
        f"--timeout {timeout}",
        f"-f {values_path}",
    ]
    if create_namespace:
        parts.append("--create-namespace")
    if wait:
        parts.append("--wait")
    if version:
        parts.append(f"--version {version}")
    cmd = " ".join(parts)
    click.echo(f"  helm upgrade --install {release} {chart} (namespace: {namespace})...")
    ssh_utils.run(client, cmd)
    click.echo(f"  {release} installed/upgraded.")
