#!/usr/bin/env bash
set -euo pipefail

STAMP="$(date -u +%Y%m%d-%H%M%S)"
ARCHIVE="/var/backups/pzserverlauncher-$STAMP.tar.gz"

sudo mkdir -p /var/backups
sudo tar -czf "$ARCHIVE" \
  /var/lib/pzserverlauncher \
  /var/log/pzserverlauncher \
  /opt/pzserverlauncher

echo "Created $ARCHIVE"
