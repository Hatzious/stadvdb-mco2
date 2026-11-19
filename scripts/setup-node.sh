#!/usr/bin/env bash
# Installs everything a node needs. Safe to run again: apt skips what's already installed.
# Usage (on the node, from the repo folder):  sudo bash scripts/setup-node.sh

set -euo pipefail   # stop at the first error instead of carrying on

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo bash $0" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive   # don't stop to ask questions mid-install

echo "==> Updating package lists"
apt-get update

echo "==> Installing packages"
apt-get install -y \
    curl \
    wget \
    git \
    postgresql \
    openssh-server

# Add future setup steps below this line.

echo "==> Done. Installed versions:"
git --version
psql --version
