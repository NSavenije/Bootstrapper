# Baked CI image for the Forgejo Actions runner.
#
# Job containers used to start from a bare `node:22-bookworm` and reinstall the
# same tools on every run (PyYAML, the Docker CLI, kubectl). Baking them in here
# makes jobs start fast and stop depending on apt being reachable mid-pipeline.
#
# Built on the host Docker daemon (the same one the runner mounts via
# /var/run/docker.sock) and referenced by local tag, so there is no registry,
# no pull auth, and no cert-trust to manage. See runner.yaml.j2 (force_pull:false).
FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Base tooling + PyYAML (the recurring reinstall) from Debian repos.
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      ca-certificates curl gnupg git jq unzip zip \
      python3 python3-yaml python3-pip \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI from Docker's official apt repo. Debian's docker.io is too old
# (API 1.41); only the client is needed since jobs talk to the host daemon.
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update -q \
 && apt-get install -y -q --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# kubectl, for platform workflows that deploy with a KUBECONFIG secret.
RUN ARCH="$(dpkg --print-architecture)" \
 && curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
      -o /usr/local/bin/kubectl \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client=true

# Sanity: fail the build if a baked tool is missing.
RUN node --version && python3 -c "import yaml" && docker --version && git --version && jq --version
