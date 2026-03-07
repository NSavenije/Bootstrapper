# Platform Config

This repository is the GitOps control plane for the developer platform. Managed by the platform team. Application teams do not have access to this repository.

## Onboarding a new team

1. Create a branch
2. Add a file to `teams/<team-name>.yaml`:

```yaml
name: myteam
tier: application       # application (namespace-only) or platform (cluster-wide)
forgejo_org: myteam-org # Forgejo organisation that owns this team's repos
authentik: myteam-devs  # Authentik group whose members get team access
```

3. Open a PR — get approval from a second platform engineer
4. Merge → the `Provision Team Landing Zone` pipeline runs automatically

The pipeline creates:
- Forgejo organisation `<forgejo_org>`
- Two k8s namespaces: `<name>-dev` and `<name>-prd` with ResourceQuotas
- RBAC: Authentik OIDC group → namespace Role (for `kubectl` access via SSO)
- RBAC: ServiceAccount `<name>-deployer` → namespace Role (for CI pipelines)
- Argo CD AppProject `<name>` scoped to both namespaces
- `forgejo-registry` ImagePullSecret in both namespaces
- Argo CD ApplicationSet for dev (auto-sync from HEAD) and prd (manual sync)
- Argo CD RBAC: Authentik group members can view/manage their own apps

Once the landing zone is provisioned, the team can create repos with a `k8s/` directory in their Forgejo org. The dev ApplicationSet automatically discovers and deploys them to `<name>-dev`. Prd promotion is done via the Argo CD UI.

**Application teams never need to touch this repository.**

## Tier: platform vs application

| Field | `tier: application` | `tier: platform` |
|---|---|---|
| k8s namespaces | `<name>-dev`, `<name>-prd` | Same |
| AppProject destinations | both namespaces | All namespaces + cluster-wide |
| Cluster resource access | None | Full (CRDs, ClusterRoles, etc.) |
| Use case | Application teams | Observability, Security, Networking teams |

## Off-boarding a team

Remove the team's YAML file from `teams/`, open a PR, merge. Clean up the k8s namespaces and Argo CD ApplicationSets manually.

## Secrets

| Secret | Contents |
|---|---|
| `KUBECONFIG` | Base64-encoded kubeconfig — `ssh root@<ip> "sed 's/127.0.0.1/<ip>/' /etc/rancher/k3s/k3s.yaml" \| base64 -w0` |
| `PLATFORM_TOKEN` | Forgejo API token with scopes: `write:organization write:repository write:user write:admin` |
| `PACKAGE_PULL_TOKEN` | Forgejo API token with scope `read:package read:user` — used for `forgejo-registry` ImagePullSecrets in team namespaces |

Set these in Forgejo: Repository Settings → Secrets and Variables.

## Variables

| Variable | Example |
|---|---|
| `FORGEJO_URL` | `https://git.yourdomain.nl` |
| `FORGEJO_DOMAIN` | `git.yourdomain.nl` |
