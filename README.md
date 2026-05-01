# PZServerLauncherLinux

`PZServerLauncherLinux` is a Linux web control panel for Project Zomboid dedicated servers.

This repo is a fresh Python/FastAPI rewrite that uses the Windows project at `D:\__Projets__\PZServerLauncher` as the feature reference, while taking a simpler deployment stance:

- ship the application package
- let the operator choose their own process manager
- let the operator choose their own reverse proxy
- let the operator own their own VPS layout and security posture

## What Exists Today

The current app includes:

- owner bootstrap and login
- role-based access
- profile creation and import
- install/update job wiring for SteamCMD
- runtime start/stop/restart wiring for `start-server.sh`
- manual and scheduled backups with retention and restore
- structured editors for General, Sandbox, Mods & Maps, and Network & Admin
- consoles, logs, host, remote, and users workspaces

## Local Development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
pzserverlauncherlinux --reload
```

Open [http://127.0.0.1:48231](http://127.0.0.1:48231) and complete the owner bootstrap flow.

## Packaging

Build artifacts are standard Python package artifacts:

```powershell
python -m pip install -e .[dev]
python -m build
```

That produces a wheel and sdist under `dist/`.

The installed console entrypoint is:

```bash
pzserverlauncherlinux
```

Optional overrides:

```bash
pzserverlauncherlinux --host 127.0.0.1 --port 48231
```

## Basic Ubuntu Setup

This is the simple/default Ubuntu setup path, and it is the closest manual equivalent to the old VPS install script.

Advanced users may prefer to do some or all of this differently:

- use Docker instead of a Python virtual environment
- use their own process manager and service layout
- integrate the app into an existing reverse proxy stack in a custom way
- change filesystem paths, users, restart policy, or security posture

If you just want a straightforward Ubuntu setup, do this:

1. Install the base packages.

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository multiverse
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip steamcmd
```

If you want remote web access, also install the reverse proxy you plan to use, for example `nginx` or `caddy`.

2. Put the app on the server.

Either extract a release artifact somewhere like `/opt/pzserverlauncher`, or clone the repo there.

3. Create the Python environment and install the app.

From a source checkout:

```bash
cd /opt/pzserverlauncher
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

From a wheel:

```bash
cd /opt/pzserverlauncher
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./dist/pzserverlauncherlinux-*.whl
```

4. Create the standard data and log folders.

```bash
sudo mkdir -p /var/lib/pzserverlauncher /var/log/pzserverlauncher
sudo chown -R "$USER":"$USER" /var/lib/pzserverlauncher /var/log/pzserverlauncher
```

If you plan to run the app as another user, change the ownership to that user instead.

5. Start the app on loopback.

```bash
export PZSL_DATA_ROOT=/var/lib/pzserverlauncher
export PZSL_LOGS_ROOT=/var/log/pzserverlauncher
pzserverlauncherlinux --host 127.0.0.1 --port 48231
```

6. Open `http://127.0.0.1:48231` locally and complete the owner bootstrap flow.

7. If you want it to survive reboots, register it with your process manager.

The sample unit at `systemd/pzserverlauncher.service` is an example you can adapt, but it is not installed for you.

8. If you want remote access, put your own reverse proxy in front of it and forward traffic to `127.0.0.1:48231`.

That is the intended basic Ubuntu path. It is not meant to be the only valid way to deploy the app.

## Deployment Posture

This repo no longer treats VPS installation as a turnkey scripted flow.

You install the package, then decide for yourself how to run it:

- `systemd`
- Docker
- Supervisor
- `screen` / `tmux`
- another process manager you already use

You also decide for yourself how to expose it:

- `nginx`
- `caddy`
- private VPN-only access
- IP-only exposure
- a domain/subdomain on an existing proxy stack

Recommended app posture remains:

`reverse proxy -> 127.0.0.1:48231 -> PZServerLauncherLinux`

## Manual Deployment Notes

Helpful reference docs:

- [Ubuntu manual deployment notes](docs/ubuntu-vps-setup.md)
- [Nginx example](docs/nginx.md)
- [Caddy example](docs/caddy.md)

These are examples only. They are not meant to replace an operator's existing VPS conventions.
