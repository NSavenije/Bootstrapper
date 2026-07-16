# Design: migrate Bootstrapper to a 3-layer App-of-Apps model

**Status:** proposed
**Audience:** an autonomous coding agent (and its reviewers) executing this rewrite
**Scope:** restructure the platform so its desired state is declarative and GitOps-managed, without a functional regression against the current imperative CLI.

---

## 1. Why

Today Bootstrapper is a **day-0 imperative tool**: a Python CLI runs once, over SSH, and produces a live platform. The only record of "what the platform should be" is code that already executed. That makes day-2 operations (upgrades, additions, recovery) unsupported — there is nothing to reconcile against — and it makes the wiring logic fragile, because it is spread across ordered API calls rather than declared.

The target replaces "a script that ran once" with "a git repo Argo CD continuously reconciles." Upgrades and additions become commits. The platform is then operated the same way its tenants operate their apps — which is also the product story ("Azure Landing Zones at Hetzner prices" is only true if the platform itself is GitOps-managed).

**This is a big change. The current tool works and has survived a real teardown+rebuild. Do not regress it.** Every phase below is independently shippable and independently verifiable, and prod is never touched until the model is proven on a throwaway dev box.

---

## 2. Hard rules (read before touching anything)

1. **Never touch production.** `noudsavenije.nl` and its Hetzner box stay on the current stack for the entire migration. All work happens on a **fresh, disposable dev box** under `nsavenije.nl`. If a step cannot be done on the dev box, stop and escalate.
2. **Never hand-fix a live server.** If the platform is wrong, fix the template/manifest/code that produced it, then re-run. A hand-fix masks a bug and breaks the next rebuild. (This is the meta-lesson from the July 2026 rebuild.)
3. **Every phase ends at a green verification gate.** Do not start phase N+1 until phase N's gate passes. The gates are written as concrete commands with expected results, below.
4. **Preserve every invariant in [Appendix A](#appendix-a-hard-won-invariants).** These are non-obvious, already-debugged facts. Re-deriving them means re-hitting production-breaking bugs.
5. **The acceptance test for the whole effort** is unchanged from the current tool: a from-zero build completes unattended AND every CI workflow goes green with zero manual `kubectl`/console tweaks.

---

## 3. Current architecture (starting point)

One Hetzner VPS, one k3s cluster. `bootstrapper provision` runs six ordered steps (see `README.md` → "What it does"). Two kinds of work are interleaved:

- **Bucket A — deploying things** (already declarative Helm/manifests): cert-manager + ClusterIssuer, Forgejo, Authentik, Argo CD, Headlamp, Umami, Traefik config, the runner, namespaces, Headlamp RBAC, portal redirect.
- **Bucket B — wiring things** (irreducibly imperative today): create Authentik OAuth2 providers with pinned `client_id`, read generated `client_secret`, write it into Forgejo's OAuth source + Headlamp values + apiserver `--oidc-client-id`; Forgejo admin/token/orgs; akadmin password sync; Umami password rotation; SSO group-team-map; k3s node OIDC args; seed platform-config.

The migration keeps Bucket A as Argo CD Applications and makes as much of Bucket B declarative as possible, leaving a small residue as idempotent Jobs.

---

## 4. Target architecture: three layers

```
Layer 1  INFRA BOOTSTRAP        (imperative, rare, outside the cluster)
         VPS + k3s + node OIDC config + install Argo CD + apply the root Application
         → stays a thin CLI (or Terraform/OpenTofu + cloud-init). Small and stable.

Layer 2  CLUSTER DESIRED STATE  (declarative, GitOps — the App-of-Apps repo)
         A root Application points at apps/. Child Applications:
         argo-cd (self-managed), cert-manager + ClusterIssuer, traefik-config,
         forgejo, authentik, headlamp (+ RBAC), umami, runner, portal-redirect,
         and the platform-config seed. Argo CD reconciles all of it.

Layer 3  CROSS-SERVICE WIRING   (declarative-first, imperative-residue)
         3a. Authentik blueprints  — providers, applications, groups, property
             mappings, grant_types, the verified-email mapping, dashboard tiles.
         3b. Idempotent Jobs (Argo PostSync hooks) — the wiring that genuinely
             cannot be declared: Forgejo admin/token/OAuth-source, Umami password
             rotation, platform-config seeding.
```

### 4.1 The key insight that makes wiring declarative

The current tool's hardest logic is "create a provider in Authentik, read the *generated* `client_secret`, and copy it into three other places." That read-from-A-write-to-B dependency is what forces imperative ordering.

**Invert it: pin both sides to a value we control.** Generate each OIDC client secret once and store it in a Kubernetes `Secret`. Then:

- The **Authentik blueprint** sets the provider's `client_id` *and* `client_secret` from that known value (blueprints can set both explicitly).
- **Forgejo's OAuth source**, **Headlamp's values**, and the **apiserver `--oidc-client-id`** all read the *same* known values from the same Secret / config.

No service ever needs to read a generated value out of another service. The dependency collapses from "ordered API calls" to "everyone references the same Secret," which is exactly what GitOps handles well. Secret generation itself (once, idempotent) belongs in Layer 1 or a single bootstrap Job; consider `sealed-secrets` or SOPS so the App-of-Apps repo can hold encrypted secrets safely.

---

## 5. Component mapping (current → target)

| Current (imperative) | Target layer | Target form |
|---|---|---|
| Create Hetzner VPS | 1 | CLI/Terraform (unchanged in spirit) |
| Install Docker + k3s | 1 | cloud-init / remote script |
| k3s apiserver OIDC args | 1 | `/etc/rancher/k3s/config.yaml` written before k3s starts (client_id is already static — `kubernetes`) |
| Install Argo CD | 1 | Helm/manifests at bootstrap, then self-managed by a Layer-2 `argo-cd` Application |
| Apply root App-of-Apps | 1 | one `kubectl apply` of the root Application |
| cert-manager + ClusterIssuer | 2 | Argo Application (chart) + manifest |
| Traefik HelmChartConfig | 2 | Argo Application (manifest) |
| Forgejo / Authentik / Headlamp / Umami install | 2 | Argo Applications (charts + values) |
| Headlamp OIDC→ClusterRole RBAC | 2 | Argo Application (already plain YAML: `oidc-rbac.yaml.j2`) |
| portal redirect | 2 | Argo Application (already plain YAML) |
| Forgejo Actions runner | 2 | Argo Application (`runner.yaml.j2`) |
| Authentik OAuth2 providers, groups, grant_types, verified-email mapping, pinned client_id/secret, dashboard tiles, akadmin | 3a | **Authentik blueprints** (mounted ConfigMap, applied by the worker) |
| Forgejo admin user + API token + OAuth source + org seeding | 3b | idempotent **Job** (or Forgejo first-boot config where possible) |
| Umami admin-password rotation | 3b | idempotent **Job** |
| Seed platform-config repo | 3b | idempotent **Job** |
| SSO group-team-map | 3b | Job (or Forgejo config) |

Anything already plain YAML (Headlamp RBAC, portal redirect, runner, Traefik config) is the cheapest to move — do those first within a phase to build confidence.

---

## 6. Phased plan with verification gates

Each phase: **Goal → Changes → Verification gate (must be green) → Rollback → Invariants at risk.** Work on the dev box only. Keep the current CLI runnable throughout as the reference implementation.

### Phase 0 — Dev environment + reference baseline
**Goal:** a disposable platform to migrate against, plus a known-good baseline to diff against.
**Changes:** none to the codebase. Rent a fresh Hetzner box. Point `*.nsavenije.nl` DNS (git, iam, argocd, headlamp, portal, stats) at it. Run the **current** `bootstrapper provision` against it end-to-end.
**Verification gate:**
- `https://git.nsavenije.nl`, `iam`, `argocd`, `headlamp`, `stats` all return 200 with a valid LE cert.
- SSO login works for Forgejo, Argo CD, and Headlamp (Headlamp shows cluster resources for an admin-group user).
- At least one CI workflow runs green.
- Capture the full resource inventory as the baseline: `kubectl get all,ingress,secret,application,applicationset -A > baseline.txt`.
**Rollback:** delete the box.
**Invariants at risk:** all of Appendix A — this proves the current tool still works from zero before you change it.

### Phase 1 — Introduce the App-of-Apps repo (no behaviour change)
**Goal:** stand up the Layer-2 repo structure alongside the CLI, deploying the *same* things the CLI already deploys, so Argo CD adoption is a no-op diff.
**Changes:**
- Create the GitOps repo layout (a new repo, e.g. `platform-gitops`, or a `deploy/` tree): a root `Application` and one child `Application` per Bucket-A service, each wrapping the **existing** Helm values verbatim.
- Do **not** yet remove anything from the CLI. Apply the root App by hand on the dev box.
**Verification gate:**
- `argocd app list` shows every child Application `Synced` + `Healthy`.
- `kubectl get all -A` diff vs `baseline.txt` is empty or benign (same images, same replica counts). Argo adopting existing resources must not recreate or churn them.
- All Phase-0 endpoint/SSO checks still green.
**Rollback:** delete the root Application (Argo leaves adopted resources in place with `prune=false` during this phase — set it so).
**Invariants at risk:** ArgoCD Traefik scheme (A.4), Forgejo service name (A.6).

### Phase 2 — Cutover Bucket A to Argo CD
**Goal:** services are deployed *by Argo CD*, not the CLI. The CLI shrinks to Layer 1 + "apply root App."
**Changes:**
- Remove the Bucket-A Helm installs from the CLI provision flow (cert-manager, Forgejo, Authentik, Headlamp, Umami, runner, Traefik config, ClusterIssuer, Headlamp RBAC, portal). The CLI now: VPS → k3s → install Argo CD → apply root App.
- Enable `automated: {prune, selfHeal}` on the child Applications (except where a manual gate is intended).
**Verification gate:**
- **Rebuild the dev box from zero** using only the shrunken CLI + Argo. Compare to `baseline.txt`: same services, Healthy.
- All endpoint/SSO/CI checks green.
- `argocd app get <each>` shows `Synced`.
**Rollback:** revert the CLI changes; the Phase-1 repo still works.
**Invariants at risk:** cert-manager TLS restore (rebuild must not burn ACME — A.7), Traefik host-port config (A.5), all of Appendix A via the from-zero rebuild.

### Phase 3 — Authentik wiring → blueprints (declarative)
**Goal:** delete the imperative Authentik API glue; declare it instead.
**Changes:**
- Generate OIDC client secrets once (Layer-1 Job or bootstrap step), store in k8s Secrets (via sealed-secrets/SOPS so they can live in git).
- Author Authentik **blueprints** for: the OAuth2 providers (Forgejo, Argo CD, Kubernetes) with **pinned `client_id` and `client_secret`**, `grant_types: [authorization_code, refresh_token]`, the **verified-email** scope mapping, the groups scope mapping, the groups themselves, the dashboard tiles (incl. Umami link), and akadmin bootstrap.
- Forgejo OAuth source, Headlamp values, and apiserver `--oidc-client-id` now read the pinned values (no cross-service reads).
- Remove the corresponding functions from `services/authentik.py` from the provision path.
**Verification gate:**
- Fresh rebuild. **Zero imperative Authentik API calls** in the provision path (grep the flow).
- Every OIDC login works (Forgejo, Argo CD, Headlamp-with-cluster-access).
- Token inspection confirms `grant_types` non-empty and `email_verified: true` for the kubernetes provider.
- `journalctl -u k3s | grep -i oidc` shows no `expected audience` or `email not verified` errors.
**Rollback:** re-enable the imperative Authentik functions (kept in git history / behind a flag until this gate is proven twice).
**Invariants at risk:** A.1 (grant_types), A.2 (email_verified), A.3 (client_id=kubernetes), A.8 (scope-mapping readiness).

### Phase 4 — Residual wiring → idempotent Jobs
**Goal:** no Python API glue left in the provision path.
**Changes:**
- Forgejo admin/token/OAuth-source/org seeding, Umami password rotation, platform-config seeding, group-team-map → idempotent Kubernetes **Jobs** run as Argo CD `PostSync` hooks (or Forgejo first-boot config where it exists). Each must be safe to re-run.
**Verification gate:**
- Fresh rebuild with **no** imperative API calls anywhere in provision.
- Re-syncing every Application twice changes nothing (idempotency).
- Full endpoint/SSO/CI acceptance test green, unattended.
**Rollback:** Jobs are additive; disable a Job and fall back to the CLI function it replaced.
**Invariants at risk:** A.3–A.6, A.9 (FORGE_ prefix), A.10 (runner public URL).

### Phase 5 — Thin Layer 1 + backups
**Goal:** finish the infra layer and close the scariest day-2 gap.
**Changes:**
- Optionally replace the imperative VPS+k3s bootstrap with Terraform/OpenTofu + cloud-init; the CLI becomes a thin wrapper or is retired.
- Add **GitHub push-mirror** config for Forgejo repos (managed off-box backup — the agreed first backup).
- Add a **`pg_dump` CronJob** for the Authentik/Umami Postgres to off-box storage, and document state-file backup.
**Verification gate:**
- From zero on a brand-new box: infra bootstrap + root App → full platform, unattended, acceptance test green.
- A test repo pushed to Forgejo appears in its GitHub mirror.
- The pg_dump CronJob produces a restorable dump (test a restore into a scratch DB).
**Rollback:** keep the existing CLI bootstrap path until Terraform is proven.
**Invariants at risk:** A.7 (TLS restore), A.11 (SSH host-key hygiene on rebuild).

### Phase 6 — Production cutover plan (plan, don't execute here)
**Goal:** a written, reviewed runbook to migrate `noudsavenije.nl` to the new model — executed later, by a human decision, not this run.
**Changes:** produce `docs/prod-cutover-runbook.md`: backup → stand up new stack on a parallel box → validate → repoint DNS → decommission. Include rollback-to-old-box.
**Verification gate:** the runbook is reviewed; a **dry run on the dev box** (simulating prod data volumes) completes. **Do not repoint production DNS in this run.**

---

## 7. Deliverables

- `platform-gitops/` (or `deploy/`) — root Application + child Applications (Layer 2).
- Authentik blueprints (Layer 3a) and Argo `PostSync` Jobs (Layer 3b).
- A thinned Layer-1 bootstrap (CLI and/or Terraform + cloud-init).
- Secret-management wiring (sealed-secrets/SOPS) for pinned OIDC secrets.
- Backup manifests: GitHub mirror config + `pg_dump` CronJob.
- `docs/prod-cutover-runbook.md`.
- Updated `README.md` reflecting the new operating model (the day-2 section already anticipates it).

---

## Appendix A — Hard-won invariants (must not regress)

Each of these is an already-debugged, non-obvious fact. Preserve them; the verification gates are designed to catch a regression in each.

- **A.1 — Authentik `grant_types` must be set.** 2024.10+ made it a per-provider allow-list defaulting to empty; empty rejects every `/authorize` ("Invalid grant_type for provider") and breaks all OIDC logins. Set `["authorization_code", "refresh_token"]`.
- **A.2 — kube-apiserver requires `email_verified: true`.** With `--oidc-username-claim=email`, Authentik's built-in email mapping (`email_verified: False`) is rejected ("oidc: email not verified"). Use a dedicated verified-email scope mapping for the kubernetes provider only. This is sound because emails are operator-set, not self-registered.
- **A.3 — the kubernetes OIDC `client_id` must be the literal `kubernetes`.** The apiserver hard-codes `--oidc-client-id=kubernetes` and validates token `aud` against it. Pin the provider's client_id (and, in the target, its client_secret) so this is stable across rebuilds. Headlamp reuses this client.
- **A.4 — Argo CD server needs `traefik...serversscheme: http`.** argocd-server runs `--insecure` (plain HTTP), but the chart's Ingress hard-wires the `https`/443 backend; Traefik then attempts TLS to a plaintext server → 502. The service annotation forces HTTP to the backend (TLS still terminates at the Ingress).
- **A.5 — Traefik terminates TLS on host ports 80/443** via a HelmChartConfig applied to the k3s manifests dir *before* k3s starts; HTTP→HTTPS redirect is on. Forgejo SSH is a LoadBalancer on `2222` (NodePort range forbids 2222).
- **A.6 — Forgejo's in-cluster service is `forgejo-http` (port 3000)**, not `forgejo`. Authentik's API service is on port **80**, not 9000.
- **A.7 — TLS secrets are saved to state and restored before installs** so cert-manager skips ACME on rebuild (avoids Let's Encrypt rate limits). Preserve this across the rebuilds these phases perform.
- **A.8 — default OAuth2 scope mappings are seeded by a worker blueprint *after* akadmin exists.** Wait for the specific mappings (poll, `page_size=1000`), not a proxy signal.
- **A.9 — never use `FORGEJO_` / `GITEA_` / `GITHUB_` prefixes** for Forgejo Actions secrets *or* variables (reserved → 400). Use `FORGE_URL`, `FORGE_DOMAIN`, `FORGE_TOKEN`, `PACKAGE_PULL_TOKEN`.
- **A.10 — the runner must register on the PUBLIC Forgejo URL**, not cluster-internal DNS. CI jobs are host-Docker sibling containers, not on the pod network; the instance URL leaks into every job's `github.server_url` and must be publicly resolvable (checkout, registry, API, KUBECONFIG server). Anything a CI job touches must use a public URL.
- **A.11 — clear SSH host keys between rebuilds** that reuse a hostname: `ssh-keygen -R <ip>` and `ssh-keygen -R "[git.<domain>]:2222"`.
- **A.12 — the `.prod-only` marker semantics** (three mutually-exclusive ApplicationSets: `dev`, `prd`, `prd-auto`) must be preserved — single-environment apps opt out of `-dev` and the manual `-prd` gate. Two sets matching one repo generate duplicate Applications / same-host Ingresses.
- **A.13 — pin image and chart versions deliberately.** An unpinned dependency (Starlette) once 500'd the blog on rebuild; unpinned base images are the same trap with a slower fuse.

## Appendix B — Open decisions (resolve early, record the choice)

- **Secret management:** sealed-secrets vs SOPS+age vs Argo CD Vault plugin. Needed before Phase 3 puts OIDC secrets in git.
- **Layer 1 tooling:** keep the thin Python CLI vs move to Terraform/OpenTofu + cloud-init. Phase 2 works either way; decide before Phase 5.
- **Umami password:** Umami has no bootstrap env var — confirm the Job approach is still needed, or whether a newer Umami supports declarative admin creds.
- **App-of-Apps repo location:** ✅ RESOLVED (Phase 1) — its own private Forgejo repo, `platform-team/platform-gitops`, rendered and seeded by `bootstrapper install-gitops` from the same Jinja templates provision uses. The chicken-and-egg is handled the same way `platform-config` already is: the CLI creates and pushes the repo, then applies the root Application.

## Appendix C — Decisions log / learnings per phase

**Phase 0 (2026-07-16, gate GREEN):** baseline on `nsavenije.nl` (box 46.225.179.83, workspace `Repos\bootstrapper-dev`). Three tool bugs found and fixed on the `app-of-apps` branch: Forgejo v16.0.0 released without a container image (`resolve_forgejo_version` now verifies the image manifest exists), Helm refusing `upgrade --install` after a failed first install (auto-uninstall of failed rev-1 releases), and k3s bouncing once (~20s) after `k3s-killall` + start (`_wait_for_k3s_stable` requires 6 consecutive `/readyz` OKs). `wire-k3s-oidc` is a required post-provision step. First Forgejo SSO login lands on `/user/link_account` (baseline behavior; candidate to enable oauth2_client auto-registration in the rewrite). Gate evidence: 6 endpoints 200+LE, `verify_sso.py` full round trips (Argo CD, Forgejo, kubernetes client → apiserver listed nodes), provision-team CI green, `baseline.txt` captured.

**Phase 1 (2026-07-16, gate GREEN):** `install-gitops` seeds 15 files; 11 Applications all Synced+Healthy after adoption syncs; **zero resource recreations** (UID-compared); only churn was argocd rolling its own server/repo-server (live pods predated the CLI's argocd-cm OIDC patch — the rendered `checksum/cm` was the correct one). Learnings baked into `services/gitops.py`:
- Argo CD 3.x tracks by **annotation** (`argocd.argoproj.io/tracking-id`); adopted resources need one no-op sync to stamp it — content was byte-identical for 7/9 apps on first diff.
- The Bitnami postgres subchart **re-generates `postgres-password` per render** → `ignoreDifferences` + `RespectIgnoreDifferences` on the authentik app, or it flaps OutOfSync forever.
- `argocd-rbac-cm` (workflow-rewritten per team) and `argocd-secret` (runtime keys + CLI-applied OIDC secret) are ignoreDifferences until Phases 3–4 make them declarative.
- Helm-OCI charts (Forgejo) need a repository Secret with `enableOCI: "true"` (`argocd-oci-repo.yaml.j2`); Application sources cannot express it inline.
- The argocd app has three `requiresPruning` leftovers (redis-secret-init hook artifacts): **never enable prune on the argocd app** until they are accounted for.
- Do NOT change the global `application.resourceTrackingMethod`: tenant appset apps already track by annotation.
- Provision now persists `forgejo_version`, `k8s_client_id/secret`, `argocd_client_id` in state; `install-gitops` falls back to reading `/opt/bootstrapper/helm-values-*.yaml` on boxes provisioned before that.

**Phase 2 (2026-07-16, gate GREEN):** from-zero rebuild on a fresh box (new IP 91.99.171.25, DNS repointed by the user) with the shrunken CLI: exit 0 unattended; all 11 Applications Synced+Healthy; endpoints 200; the three state-saved TLS secrets restored without ACME; SSO all green (incl. apiserver accepting an OIDC id_token); provision-team CI green and the blog landing zone regenerated. Structure decisions:
- **Chicken-and-egg broken by staging:** chart Applications are self-contained (chart registry + inline values), so cert-manager/forgejo/authentik are kubectl-applied *before* Forgejo exists; headlamp/argocd/umami follow as wiring inputs appear; the four manifest apps (cluster-issuer, headlamp-rbac, portal-redirect, runner) sync from the seeded repo via the root app at the end. The ClusterIssuer additionally gets a direct early apply (TLS can't wait for the repo) and is git-adopted later with identical content.
- The CLI installs exactly one thing via Helm: Argo CD (Layer 1); its own Application takes over at step 5 (one expected server/repo-server roll from the OIDC-bearing checksum/cm).
- **Traefik HelmChartConfig stays Layer 1** (k3s manifests dir, must precede k3s start — deviation from the §5 mapping table, deliberate).
- `sh.helm.release.v1.*` Secrets are gone by design (Argo renders, Helm never runs for Bucket A); expect them missing when diffing against pre-cutover inventories.
- New fix: `configure_forgejo_oauth_source` polls the OIDC discovery URL until 200 before writing the auth source — after wire-k3s-oidc's k3s restart, Authentik can still be returning 503, which failed the whole command (Forgejo validates the URL on write).
- Old `install_*` helpers remain in the modules unused (rollback path); delete them in Phase 4 once two full rebuilds have passed.

**Phase 3 (2026-07-16, gate GREEN):** from-zero rebuild (box back on 46.225.179.83) with **zero imperative Authentik API calls in provision** (grep-verified): the platform blueprint (`authentik-blueprint.yaml.j2` → ConfigMap, git-owned post-seed) declares akadmin, groups, both custom scope mappings, the three providers with pinned client_id+client_secret, applications, and the Umami tile. Gate evidence: all 12 apps Synced+Healthy, SSO all green (apiserver accepted the pinned-client id_token → proves grant_types + email_verified functionally), zero oidc errors in the k3s journal, provision-team CI green. Decisions/learnings:
- **Secret management (Appendix B) resolved for now:** pinned credentials live in the `authentik-pinned-clients` Secret applied by Layer 1 (never in git); the blueprint reads them via `!Env` through `global.envFrom`, so the blueprint ConfigMap itself is secret-free and git-safe. sealed-secrets/SOPS deferred — revisit in Phase 5 alongside backups (the gitops repo still holds chart values with generated secrets; it is private and per-environment).
- Blueprint field names verified against live 2026.5.5 models before writing (`grant_types` list, RedirectURI structure, signing key name "authentik Self-signed Certificate").
- Readiness without API tokens: poll each provider's unauthenticated `/.well-known/openid-configuration` — 200 means the worker applied the blueprint (same file also carries the akadmin entry, applied in order before the providers).
- **Forgejo caches its OAuth source**: `admin auth update-oauth` writes the DB behind the goth registry, so credential rotation silently kept the old client_id until restart — `configure_forgejo_oauth_source` now restarts the deployment on the update path (adds apply dynamically).
- Host-side discovery probes must use the service ClusterIP for `.svc.cluster.local` URLs (host resolves no cluster DNS).
- `install-headlamp` / `install-analytics` standalone commands still use the imperative Authentik path — reconcile or retire in Phase 4.

**Phase 4 (2026-07-16, gate GREEN):** from-zero rebuild (box 91.99.171.25) with **no imperative service-API wiring in provision at all**: residual glue runs as two idempotent Jobs, applied directly at bootstrap and re-run as PostSync hooks of their git-owned apps. `forgejo-wiring` mints/validates the platform token (→ argocd credential Secrets), ensures the runner registration-token Secret (runner manifest and gitops repo are now token-free), and converges the Authentik OAuth source **hash-gated** (restart only on real change). `umami-wiring` rotates the default admin password (refuses to guess if hand-changed). Gate evidence: unattended build green; **double re-sync of all 14 apps changed nothing** (secret resourceVersions + deployment generations identical); SSO/endpoints/CI green; zero oidc journal errors. Decisions/learnings:
- The Forgejo admin user is chart-managed (`gitea.admin`) — `create_admin` was redundant and is gone from provision.
- **Repo-content seeding stays Layer 1** (platform-config + platform-gitops): the CLI must push the very repo Argo pulls — same genesis class as "apply root App". Deviation from the §5 mapping row for platform-config, deliberate.
- The group-team-map remains owned by the provision-team workflow (driven from `teams/*.yaml`), not a platform Job.
- `wire-k3s-oidc` now only writes apiserver flags; the OAuth source converges via the Job once DNS/TLS answer (the Job is the convergence loop).
- The wiring Jobs need cross-namespace RBAC (secrets in argocd/kube-system) and read the chart's `forgejo-admin` Secret + a namespace-local copy of `authentik-pinned-clients` (applied by Layer 1 to both authentik and forgejo namespaces).
- **TLS rate-limit guard:** `_TLS_SECRETS` now saves/restores all six certs (headlamp/portal/umami were being re-issued per rebuild — ~4 of Let's Encrypt's 5-duplicates/week consumed before the fix). Avoid unnecessary full rebuilds; with all six restored a rebuild issues zero certificates.
- ⚠️ Operational hazard: `STATE_FILE` is cwd-relative — a state-touching script run from the Bootstrapper repo root hits PROD state (happened once; prod `tls_secrets` restored from the live cluster minutes later). Dev scripts must run from the dev workspace and assert `server_ip` before writing.

**Phase 5 (2026-07-16, gate GREEN — adapted):** backups live via the optional `github_mirror_token` config field. `github-mirror` CronJob (nightly + sync_on_commit push-mirrors): every Forgejo repo mirrored to a private `forgejo-<owner>-<name>` GitHub repo — verified: all repos private, a fresh push visible on GitHub in ~10s. `db-backup` CronJob: nightly pg_dump of authentik+umami, gzip + AES-256 encrypted with the generated `backup_encryption_key` **before** upload to a private `platform-db-backups` repo — **restore drill passed** (download → decrypt with state key → psql into scratch DB: 215 tables, 7 users, 0 errors). Decisions/learnings:
- **Layer-1 tooling (Appendix B) resolved: the Python CLI stays.** Terraform/OpenTofu would add a second toolchain for one server + one firewall; the hcloud path is ~90 lines and proven by four green from-zero builds.
- **Gate adaptation:** the from-zero leg was deliberately skipped — Let's Encrypt's 5-duplicates/week limit was nearly consumed (three cert names were being re-issued per rebuild before the `_TLS_SECRETS` fix). The backup code path is config-gated and additive; its from-zero proof rides along the next scheduled rebuild.
- The CronJob manifests are secret-free and git-owned; credentials are two Layer-1 Secrets (`github-mirror-credentials`, `db-backup-credentials`).
- GitHub contents-API uploads must feed the base64 payload from a file (`jq --rawfile`) — argv blows ARG_MAX for multi-MB dumps; timestamped artifact paths avoid the sha/overwrite dance entirely.
- State-file backup stays deliberately manual (any automated destination would need credentials stored in the state file it protects); documented in the README with the encryption-key warning.
