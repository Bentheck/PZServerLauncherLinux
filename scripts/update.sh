#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF'
PZServerLauncherLinux no longer ships a turnkey update script.

Update the package and restart process management in the way that fits your own VPS stack.

Typical examples:
- reinstall the wheel in your existing virtual environment
- pull source and reinstall with pip
- restart your own systemd, Docker, Supervisor, screen, or tmux process

See:
- README.md
- docs/ubuntu-vps-setup.md
EOF
