import json as _json
import shlex
import time

import click
import paramiko


CURL_POD_NAME = "bootstrapper-curl"
CURL_POD_NS = "default"


def connect(host: str, private_key_path: str, username: str = 'root', retries: int = 12, delay: int = 10) -> paramiko.SSHClient:
    """Connect via SSH, retrying until the server accepts connections."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    for attempt in range(1, retries + 1):
        try:
            client.connect(host, username=username, key_filename=private_key_path, timeout=10)
            return client
        except Exception as e:
            if attempt == retries:
                raise ConnectionError(f"Could not SSH into {host} after {retries} attempts: {e}")
            click.echo(f"  SSH not ready yet (attempt {attempt}/{retries}), retrying in {delay}s...")
            time.sleep(delay)


def run(client: paramiko.SSHClient, command: str) -> str:
    """Run a command over SSH, raising on non-zero exit."""
    _, stdout, stderr = client.exec_command(command)
    # Drain both streams before checking exit status to avoid deadlock
    out = stdout.read().decode()
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        combined = (out + err).strip()
        raise RuntimeError(f"Command failed (exit {exit_code}): {command}\n{combined}")
    return out


def run_with_stdin(client: paramiko.SSHClient, command: str, stdin_bytes: bytes) -> str:
    """Run a command over SSH with stdin data, raising on non-zero exit."""
    stdin_chan, stdout, stderr = client.exec_command(command)
    stdin_chan.write(stdin_bytes)
    stdin_chan.flush()
    stdin_chan.channel.shutdown_write()
    out = stdout.read().decode()
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        combined = (out + err).strip()
        raise RuntimeError(f"Command failed (exit {exit_code}): {command}\n{combined}")
    return out


def upload(client: paramiko.SSHClient, content: str, remote_path: str) -> None:
    """Upload a string as a file over SFTP."""
    sftp = client.open_sftp()
    with sftp.open(remote_path, 'w') as f:
        f.write(content)
    sftp.close()


class ClusterResponse:
    """requests.Response-compatible wrapper around a cluster_curl result."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self) -> dict:
        return _json.loads(self.text)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:300]}")


def start_curl_pod(client: paramiko.SSHClient) -> None:
    """Create the bootstrap curl pod used for intra-cluster HTTP calls.

    The pod runs in the default namespace where it can reach any service via
    cluster DNS (service.namespace.svc.cluster.local).
    """
    click.echo("  Starting bootstrap curl pod...")
    run(client, f"k3s kubectl delete pod {CURL_POD_NAME} -n {CURL_POD_NS} --ignore-not-found=true")
    run(
        client,
        f"k3s kubectl run {CURL_POD_NAME} -n {CURL_POD_NS} "
        f"--image=curlimages/curl:8.17.0 --restart=Never -- sleep 7200",
    )
    run(
        client,
        f"k3s kubectl wait pod/{CURL_POD_NAME} -n {CURL_POD_NS} "
        f"--for=condition=Ready --timeout=60s",
    )
    click.echo("  Bootstrap curl pod ready.")


def stop_curl_pod(client: paramiko.SSHClient) -> None:
    """Delete the bootstrap curl pod."""
    try:
        run(client, f"k3s kubectl delete pod {CURL_POD_NAME} -n {CURL_POD_NS} --ignore-not-found=true")
    except RuntimeError:
        pass


def cluster_curl(
    client: paramiko.SSHClient,
    url: str,
    method: str = 'GET',
    headers: dict = None,
    json_body: dict = None,
    auth: tuple = None,
) -> ClusterResponse:
    """Make an HTTP request to a cluster-internal URL via the bootstrap curl pod.

    start_curl_pod() must have been called before any cluster_curl() calls.

    JSON bodies are passed via stdin (kubectl exec -i) to avoid shell quoting issues.
    Header values and the URL are shlex-quoted for the outer SSH shell.
    """
    header_flags = ""
    for k, v in (headers or {}).items():
        header_flags += f" -H {shlex.quote(f'{k}: {v}')}"

    if auth:
        username, password = auth
        header_flags += f" -u {shlex.quote(f'{username}:{password}')}"

    if json_body is not None:
        body_bytes = _json.dumps(json_body).encode('utf-8')
        # Pass body via stdin; curl reads it with -d @-
        ct_flag = "" if (headers and 'Content-Type' in headers) else " -H 'Content-Type: application/json'"
        cmd = (
            f"k3s kubectl exec -n {CURL_POD_NS} {CURL_POD_NAME} -i -- "
            f"curl -s -w '\\n%{{http_code}}' -X {method}"
            f"{header_flags}{ct_flag}"
            f" -d @- {shlex.quote(url)}"
        )
        output = run_with_stdin(client, cmd, body_bytes)
    else:
        cmd = (
            f"k3s kubectl exec -n {CURL_POD_NS} {CURL_POD_NAME} -- "
            f"curl -s -w '\\n%{{http_code}}' -X {method}"
            f"{header_flags}"
            f" {shlex.quote(url)}"
        )
        output = run(client, cmd)

    lines = output.rstrip('\n').split('\n')
    status_code = int(lines[-1]) if lines and lines[-1].isdigit() else 0
    body = '\n'.join(lines[:-1])
    return ClusterResponse(status_code, body)
