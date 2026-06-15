#!/usr/bin/env bash
set -euo pipefail

compose_version="${COMPOSE_VERSION:-v5.1.4}"
buildx_version="${BUILDX_VERSION:-v0.34.1}"

dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

install -d -m 0755 /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
curl -fsSL \
  "https://github.com/docker/buildx/releases/download/${buildx_version}/buildx-${buildx_version}.linux-amd64" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod 0755 \
  /usr/local/lib/docker/cli-plugins/docker-compose \
  /usr/local/lib/docker/cli-plugins/docker-buildx

if [[ ! -f /swapfile ]]; then
  fallocate -l "${SWAP_SIZE:-4G}" /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile
fi
swapon /swapfile || true
grep -q '^/swapfile ' /etc/fstab ||
  echo '/swapfile swap swap defaults 0 0' >> /etc/fstab

install -d -o ec2-user -g ec2-user /opt/election
install -d -o ec2-user -g ec2-user /opt/election-data/hermes-runtime

docker --version
docker compose version
docker buildx version
