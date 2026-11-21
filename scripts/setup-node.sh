#!/usr/bin/env bash
# Installs everything a node needs. Safe to run again: apt skips what's already installed,
# and uv is only installed if it isn't there yet.
# Usage (on the node, from the repo folder, as your regular user):  sudo bash scripts/setup-node.sh

set -euo pipefail   # stop at the first error instead of carrying on

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo bash $0" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive   # don't stop to ask questions mid-install

APT_PACKAGES=(
    curl
    wget
    git
    postgresql
    openssh-server
    tmux
    nginx
)

echo "==> Updating package lists"
apt-get update

echo "==> Installing packages"
apt-get install -y "${APT_PACKAGES[@]}"

# ---------------------------------------------------------------------------
# uv (Python version and package manager), from its official install script.
# It installs per user, into ~/.local/bin, so it goes to the user who ran sudo,
# not to root. That's the account that will run the webapp.
# ---------------------------------------------------------------------------
UV_USER="${SUDO_USER:-root}"
UV_HOME="$(getent passwd "$UV_USER" | cut -d: -f6)"
UV_BIN="$UV_HOME/.local/bin/uv"
UV_STATUS="not attempted"

echo "==> Setting up uv for user '$UV_USER'"
if [ "$UV_USER" = "root" ]; then
    echo "    Note: you're logged in as root, so uv goes to root. Run this with sudo from your"
    echo "    regular user instead if that user should run the webapp."
fi

if [ -x "$UV_BIN" ]; then
    UV_STATUS="already installed"
    echo "    uv is already installed; skipping. (Update it with: uv self update)"
elif ! command -v curl > /dev/null; then
    UV_STATUS="FAILED: curl is missing"
    echo "    curl isn't installed, so uv's install script can't be downloaded." >&2
else
    echo "    Running uv's official install script"
    if sudo -u "$UV_USER" -H sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'; then
        if [ -x "$UV_BIN" ]; then
            UV_STATUS="installed successfully"
        else
            UV_STATUS="FAILED: install script ran, but $UV_BIN is missing"
        fi
    else
        UV_STATUS="FAILED: install script exited with an error"
    fi
fi
echo "    uv: $UV_STATUS"

# Add future setup steps above this line.

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo
echo "==> Installed versions"
for package in "${APT_PACKAGES[@]}"; do
    version="$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || echo "NOT INSTALLED")"
    printf "    %-16s %s\n" "$package" "$version"
done
if [ -x "$UV_BIN" ]; then
    printf "    %-16s %s\n" "uv" "$(sudo -u "$UV_USER" -H "$UV_BIN" --version)"
else
    printf "    %-16s %s\n" "uv" "NOT INSTALLED"
fi
printf "    %-16s %s\n" "python3" "$(python3 --version 2>&1)"

echo
case "$UV_STATUS" in
    FAILED*)
        echo "==> Finished with a problem: uv $UV_STATUS" >&2
        exit 1
        ;;
    "installed successfully")
        echo "==> Done. uv was just installed: log out and back in (or run 'source ~/.bashrc')"
        echo "    so the 'uv' command is found."
        ;;
    *)
        echo "==> Done."
        ;;
esac
