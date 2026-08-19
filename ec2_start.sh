#!/usr/bin/env bash
set -euo pipefail

# Start the Windy forecast stack on an EC2/Linux host.
# Expected files in the repo directory:
#   - windy_login.json
#
# This script creates the required data directories, then builds and
# starts the Docker Compose service in detached mode.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

required_files=(
  "windy_login.json"
)

for file in "${required_files[@]}"; do
  if [[ ! -f "${ROOT_DIR}/${file}" ]]; then
    echo "[ERROR] Missing required file: ${file}"
    echo "  Place it in: ${ROOT_DIR}"
    exit 1
  fi
done

required_dirs=(
  "windy_screenshots"
  "windy_videos"
)

for dir in "${required_dirs[@]}"; do
  mkdir -p "${ROOT_DIR}/${dir}"
done

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Docker is not installed or not on PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[WARN] docker compose is not available. Installing the Compose plugin manually..."

  if ! command -v curl >/dev/null 2>&1; then
    sudo dnf install -y curl
  fi

  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64)
      compose_arch="x86_64"
      ;;
    aarch64|arm64)
      compose_arch="aarch64"
      ;;
    *)
      echo "[ERROR] Unsupported architecture for Compose plugin: ${arch}"
      exit 1
      ;;
  esac

  plugin_dir="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins"
  mkdir -p "${plugin_dir}"
  curl -fsSL \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${compose_arch}" \
    -o "${plugin_dir}/docker-compose"
  chmod +x "${plugin_dir}/docker-compose"

  if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] Compose plugin installation completed, but docker compose still is unavailable."
    exit 1
  fi
fi

cd "${ROOT_DIR}"
docker compose build
docker compose up -d

echo "[OK] Windy capture container is running."
echo "Use 'docker logs -f windy-capture' to watch progress."
