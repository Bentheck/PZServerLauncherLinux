# Ubuntu Setup

Use the `.deb` for the normal VPS install. It sets up the app, SteamCMD, folders, service user, and `systemd` service.

Manual install is only for users who do not want the `.deb` and know what they are doing.

## Build

Build on Linux:

```bash
./scripts/build-deb.sh
```

Output:

```text
dist/pzserverlauncherlinux_1.3.0_all.deb
```

## Install

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository -y multiverse
sudo apt-get update
sudo apt install ./dist/pzserverlauncherlinux_1.3.0_all.deb
```

The package:

- installs the app in `/opt/pzserverlauncher`
- installs SteamCMD
- installs Python runtime dependencies
- creates user `pzlauncher`
- creates `/var/lib/pzserverlauncher`
- creates `/var/log/pzserverlauncher`
- installs command `pzserverlauncherlinux`
- registers `pzserverlauncherlinux.service`

## Service

The package installs a `systemd` service. `systemd` is Ubuntu's background service manager.

It does not start automatically after install.

```bash
sudo systemctl start pzserverlauncherlinux
sudo systemctl enable pzserverlauncherlinux
```

Useful commands:

```bash
sudo systemctl status pzserverlauncherlinux
sudo systemctl restart pzserverlauncherlinux
journalctl -u pzserverlauncherlinux -f
```

## First Setup

The app listens on the VPS at `127.0.0.1:48231`. That address is private to the VPS.

From your computer:

```bash
ssh -L 48231:127.0.0.1:48231 user@server
```

Then open:

```text
http://127.0.0.1:48231
```

## Remote Access

For normal remote access, reverse proxy public traffic to `127.0.0.1:48231`.

Examples:

- [Nginx](nginx.md)
- [Caddy](caddy.md)

## SteamCMD

SteamCMD is installed automatically as a package dependency.

## Manual Install

Use this only if you do not want the `.deb` and know what you are doing.

You must provide:

- Python 3.10+
- SteamCMD
- data and log folders
- a process manager, if you want background hosting
- a reverse proxy, if you want normal remote access

Install packages:

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository -y multiverse
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

For first setup on a VPS:

```bash
ssh -L 48231:127.0.0.1:48231 user@server
```

Then open `http://127.0.0.1:48231` on your own computer.
