#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF'
PZServerLauncherLinux no longer ships a turnkey Ubuntu install script.

Install the package yourself and wire it into your own VPS layout instead.

Useful references:
- README.md
- docs/ubuntu-vps-setup.md
- docs/nginx.md
- docs/caddy.md

Example package flow:
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -e .
  pzserverlauncherlinux --host 127.0.0.1 --port 48231
EOF
