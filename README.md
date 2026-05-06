# PZServerLauncherLinux

Linux web control panel for Project Zomboid dedicated servers.

Feature reference: `D:\__Projets__\PZServerLauncher`.

## Features

- Owner setup, login, roles, users
- Profiles, import, install, update, uninstall
- General, Sandbox, Mods & Maps, Network & Admin editors
- Backups, restore, scheduled backups, world reset
- Runtime controls, logs, consoles, jobs, audit trail
- Host and remote access pages

## Local Development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
pzserverlauncherlinux --reload
```

Open `http://127.0.0.1:48231`.

## Package

```powershell
python -m pip install -e .[dev]
python -m build
```

Artifacts are written to `dist/`.

Ubuntu `.deb` packages are built on Linux:

```bash
./scripts/build-deb.sh
```

Run:

```bash
pzserverlauncherlinux --host 127.0.0.1 --port 48231
```

## Basic Ubuntu Setup

Recommended basic path:

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository multiverse
sudo apt-get update
sudo apt install ./dist/pzserverlauncherlinux_1.2.0_all.deb
```

The package installs the app, installs SteamCMD, creates `pzlauncher`, adds data/log folders, and registers a disabled `systemd` service.

`systemd` is Ubuntu's background service manager. It starts, stops, restarts, and monitors server apps.

Start it:

```bash
sudo systemctl start pzserverlauncherlinux
```

Enable on boot:

```bash
sudo systemctl enable pzserverlauncherlinux
```

Useful service commands:

```bash
sudo systemctl status pzserverlauncherlinux
sudo systemctl restart pzserverlauncherlinux
journalctl -u pzserverlauncherlinux -f
```

First setup from your computer:

```bash
ssh -L 48231:127.0.0.1:48231 user@server
```

Then open `http://127.0.0.1:48231` on your own computer.

For normal remote access, reverse proxy to `127.0.0.1:48231`.

Manual install is only for users who do not want the `.deb` and know what they are doing.

## Manual Install

Use this only if you do not want the `.deb` and know what you are doing.

You must provide:

- Python 3.12+
- SteamCMD
- data and log folders
- a process manager, if you want it to run in the background
- a reverse proxy, if you want normal remote access

Install Ubuntu packages:

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository multiverse
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip steamcmd
```

Create folders:

```bash
sudo mkdir -p /opt/pzserverlauncher /var/lib/pzserverlauncher /var/log/pzserverlauncher
sudo chown -R "$USER":"$USER" /opt/pzserverlauncher /var/lib/pzserverlauncher /var/log/pzserverlauncher
cd /opt/pzserverlauncher
```

Install from source:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Or install from wheel:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./dist/pzserverlauncherlinux-*.whl
```

Run:

```bash
export PZSL_DATA_ROOT=/var/lib/pzserverlauncher
export PZSL_LOGS_ROOT=/var/log/pzserverlauncher
pzserverlauncherlinux --host 127.0.0.1 --port 48231
```

For first setup on a VPS, use SSH forwarding:

```bash
ssh -L 48231:127.0.0.1:48231 user@server
```

Then open `http://127.0.0.1:48231` on your own computer.

For background hosting, create your own `systemd`, Supervisor, `screen`, or `tmux` setup.

## Deployment

The `.deb` does not expose the app publicly or configure certificates.

Recommended shape:

`reverse proxy -> 127.0.0.1:48231 -> PZServerLauncherLinux`

References:

- [Ubuntu setup](docs/ubuntu-vps-setup.md)
- [Nginx](docs/nginx.md)
- [Caddy](docs/caddy.md)
- [Example systemd unit](systemd/pzserverlauncher.service)
