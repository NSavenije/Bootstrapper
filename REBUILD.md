# Clean rebuild: runbook & hard-won lessons

This document exists because a full teardown + rebuild (July 2026) surfaced a
string of bugs that had been invisible on the long-lived server. Read the
**one big lesson** first — it explains *why* the rebuild wasn't smooth and how to
make the next one boring.

## The one big lesson

> **The long-lived server accumulated manual hand-fixes. The automation
> (Bootstrapper) was never re-validated from zero, so every hand-fix quietly
> masked a bug in the code or templates. A clean rebuild pays all that debt at
> once.**

Two rules follow directly:

1. **The acceptance test for Bootstrapper is: a from-zero `provision` completes
   unattended AND every CI workflow goes green — with no manual `kubectl`/console
   tweaks.** If you can't rebuild push-button, it isn't done.
2. **Never hand-fix the live server without fixing the template/code that
   produced it.** A `kubectl edit`, a console click, a secret set by hand — each
   is a landmine for the next rebuild. Fix the cause, upstream, every time.

Run a clean teardown+rebuild **regularly** (before any talk/demo, and after any
change to provisioning code). That's the only thing that keeps the debt at zero.

## Bugs this rebuild exposed (all now fixed in-repo)

Each is a *class* of bug, not a one-off. Internalize the rule, not just the fix.

| # | Symptom | Root cause | Rule |
|---|---|---|---|
| 1 | `configure_oauth_provider` → "No scope mappings found" | Authentik's default OAuth2 scope mappings are seeded by a worker blueprint that finishes *after* `akadmin` exists | **Wait for the specific dependency, not a proxy.** `wait_for_authentik` proved akadmin; the code now polls for the scope mappings themselves. |
| 2 | `wire-k3s-oidc` → "container not found (forgejo)" | `k3s-killall.sh` tears down all pods; the API server returns before Forgejo is back, then we `kubectl exec` into it | Same rule: after a k3s restart, `rollout status deploy/forgejo` before touching it. |
| 3 | Can't create `FORGEJO_URL` variable — "invalid secret name" | Forgejo **reserves** the `FORGEJO_`, `GITEA_`, `GITHUB_` prefixes for Actions var/secret names | **Never** name Actions vars/secrets with those prefixes. We use `FORGE_URL` / `FORGE_DOMAIN`. |
| 4 | Every CI job fails at `actions/checkout` — "Could not resolve host: forgejo-http.forgejo.svc.cluster.local" | The runner was registered with the **cluster-internal** URL, which leaks into every job's `github.server_url`. CI jobs run as host-Docker **sibling containers**, not on the pod network — they can't resolve cluster DNS or reach ClusterIPs | **Anything a CI job touches must use a public, resolvable URL** (see below). Runner now registers on `https://<forgejo-domain>`. |
| 5 | Rework: `blogger-org` vs `blog` split across app files | Two naming schemes drifted over time | **Pick one org/team name and use it everywhere.** |

### The CI-job network boundary (rule 4, expanded — this one bites repeatedly)

A Forgejo Actions job container is a **sibling on the host Docker daemon**. It is
NOT inside the k3s pod network. Therefore, from inside a job:

- ❌ `*.svc.cluster.local` DNS does not resolve
- ❌ ClusterIPs (10.43.x.x) are not routable
- ✅ Public DNS + the node's public IP **are** reachable (jobs have internet;
  traffic to the node's own public IP is delivered host-locally)

So everything a job references must be public:
- **Runner registration `--instance`** → `https://<forgejo-domain>` (it becomes
  `github.server_url`, used by checkout).
- **Registry / API / content clone** → `https://<forgejo-domain>`.
- **KUBECONFIG for jobs** → server = the node's **public IP** (`sed
  s/127.0.0.1/<ip>/`). This works because the k3s API cert includes the public IP
  as a SAN and the packet is delivered host-locally (bypassing the cloud
  firewall, which blocks 6443 from the internet).

## Teardown + rebuild checklist

1. **Inventory Hetzner directly — don't trust local state.** The state file can be
   stale (a local test overwrote it here). List servers via the API, find the real
   `server_id`.
2. Delete the server + any orphaned firewalls. `rm .bootstrapper-state.yaml`.
   `ssh-keygen -R <old-ip>` and `ssh-keygen -R "[<forgejo-domain>]:2222"`.
3. **Hetzner usually reassigns the *same* primary IP** on immediate
   delete+recreate → DNS often needs **no** change. Verify the new IP first.
4. Set `server_type` + **all** optional domains (`headlamp_/portal_/analytics_/
   blog_`) in `config.yaml` *before* provisioning, so it's one pass.
5. `bootstrapper provision` (idempotent; reuses the server, resumes on rerun).
6. `bootstrapper wire-k3s-oidc` (waits for Forgejo rollout now), then
   `bootstrapper seed-demo-users`.
7. Set `platform-config` repo secrets + vars:
   - `KUBECONFIG` = `sed 's/127.0.0.1/<ip>/' k3s.yaml | base64 -w0`
   - `PLATFORM_TOKEN` (write:organization/repository/user/admin)
   - `PACKAGE_PULL_TOKEN` (read:package,read:user)
   - vars `FORGE_URL`, `FORGE_DOMAIN` (NOT `FORGEJO_*` — reserved)
8. Push `teams/<team>.yaml` → the provision-team workflow builds the landing zone.
9. Create + push app repos; each app repo needs its own `FORGEJO_TOKEN` secret
   (write:package) + `FORGE_DOMAIN` var.
10. Umami: create the website in the UI/API → copy its `website-id` into the
    site's `<head>` snippet (a fresh Umami DB invalidates the old id).

## Where this should go next (shrink the manual list)

Steps 7–10 are still manual. To make rebuilds truly push-button, fold them into
Bootstrapper: a `set-platform-secrets` command (7) and a `seed-app-team` command
(8–10). Every manual step above is a candidate bug for the *next* rebuild.
