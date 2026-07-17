"""Backups (Phase 5): GitHub push-mirrors + encrypted nightly DB dumps.

Forgejo mirrors every repo to a private GitHub repo on each commit; a
CronJob reconciles the wiring nightly so new repos get mirrors too. A second
CronJob pg_dumps the authentik/umami databases, encrypts them with the
generated backup key, and uploads to a private platform-db-backups repo.
Both CronJob manifests are secret-free and git-owned via the gitops repo;
the credentials Secrets are Layer 1 (config token + generated key).
"""
import shlex

import click
import paramiko

from bootstrapper.deploy import ssh as ssh_utils


def apply_credentials(ssh: paramiko.SSHClient, github_token: str, encryption_key: str) -> None:
    """Apply the backup credential Secrets (Layer 1, never in git)."""
    click.echo("  Applying backup credential Secrets...")
    ssh_utils.run(
        ssh,
        "k3s kubectl create secret generic github-mirror-credentials -n forgejo"
        f" --from-literal=token={shlex.quote(github_token)}"
        " --dry-run=client -o yaml | k3s kubectl apply -f -",
    )
    ssh_utils.run(
        ssh,
        "k3s kubectl create secret generic db-backup-credentials -n authentik"
        f" --from-literal=github-token={shlex.quote(github_token)}"
        f" --from-literal=encryption-key={shlex.quote(encryption_key)}"
        " --dry-run=client -o yaml | k3s kubectl apply -f -",
    )


def kickoff(ssh: paramiko.SSHClient, cronjob: str, namespace: str, timeout: int = 900) -> None:
    """Run a CronJob immediately and wait for the spawned Job to succeed."""
    from bootstrapper.services import k8s as k8s_module  # noqa: F401 (wait pattern)
    import time
    from bootstrapper.deploy import ssh as _ssh  # for clarity; same module

    job = f"{cronjob}-manual"
    ssh_utils.run(ssh, f"k3s kubectl delete job {job} -n {namespace} --ignore-not-found")
    ssh_utils.run(ssh, f"k3s kubectl create job {job} -n {namespace} --from=cronjob/{cronjob}")
    click.echo(f"  Waiting for job {job} to complete...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = ssh_utils.run(
            ssh,
            f"k3s kubectl get job {job} -n {namespace} "
            f"-o jsonpath='{{.status.succeeded}}/{{.status.failed}}' 2>/dev/null || true",
        ).strip()
        succeeded, _, failed = status.partition('/')
        if succeeded.isdigit() and int(succeeded) >= 1:
            click.echo(f"  Job {job} completed.")
            return
        if failed.isdigit() and int(failed) >= 3:
            break
        time.sleep(10)
    logs = ssh_utils.run(
        ssh, f"k3s kubectl logs -n {namespace} job/{job} --tail=40 2>&1 || true")
    raise RuntimeError(f"Job {job} did not succeed within {timeout}s. Logs:\n{logs}")
