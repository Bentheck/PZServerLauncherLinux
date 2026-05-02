#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

VERSION="$("$PYTHON_BIN" - <<'PY'
from pathlib import Path

for line in Path("pyproject.toml").read_text(encoding="utf-8").splitlines():
    if line.startswith("version = "):
        print(line.split("=", 1)[1].strip().strip('"'))
        break
else:
    raise SystemExit("pyproject.toml does not define project.version")
PY
)"

PKG="pzserverlauncherlinux"
DEB_ROOT="$ROOT/build/debroot"
OUT="$ROOT/dist/${PKG}_${VERSION}_all.deb"

"$PYTHON_BIN" -m pip install --upgrade build
"$PYTHON_BIN" -m build --wheel

rm -rf "$DEB_ROOT"
mkdir -p \
  "$DEB_ROOT/DEBIAN" \
  "$DEB_ROOT/usr/share/pzserverlauncherlinux/wheels" \
  "$DEB_ROOT/lib/systemd/system" \
  "$DEB_ROOT/etc/default"

cp "$ROOT/dist"/pzserverlauncherlinux-"$VERSION"-*.whl "$DEB_ROOT/usr/share/pzserverlauncherlinux/wheels/"
cp "$ROOT/packaging/deb/postinst" "$DEB_ROOT/DEBIAN/postinst"
cp "$ROOT/packaging/deb/prerm" "$DEB_ROOT/DEBIAN/prerm"
cp "$ROOT/packaging/deb/postrm" "$DEB_ROOT/DEBIAN/postrm"
cp "$ROOT/packaging/deb/pzserverlauncherlinux.service" "$DEB_ROOT/lib/systemd/system/pzserverlauncherlinux.service"
cp "$ROOT/packaging/deb/pzserverlauncherlinux.default" "$DEB_ROOT/etc/default/pzserverlauncherlinux"
chmod 0755 "$DEB_ROOT/DEBIAN/postinst" "$DEB_ROOT/DEBIAN/prerm" "$DEB_ROOT/DEBIAN/postrm"

cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: games
Priority: optional
Architecture: all
Maintainer: Bentheck
Depends: python3, python3-venv, python3-pip, steamcmd
Description: Linux web launcher for Project Zomboid dedicated servers
 Web control panel for managing Project Zomboid dedicated server profiles,
 settings, runtime, backups, logs, and Workshop configuration.
EOF

dpkg-deb --build "$DEB_ROOT" "$OUT"
echo "$OUT"
