# Ubuntu Manual Deployment Notes

## Summary

This project no longer assumes a one-command VPS installer.

Recommended production shape is still:

`Internet -> Your reverse proxy -> 127.0.0.1:48231 -> PZServerLauncherLinux`

But the app does not try to own:

- package installation on the server
- user or group creation
- reverse proxy authoring
- `systemd` registration
- certificate management

That is left to the operator.

## Base Requirements

Typical Ubuntu requirements are:

- Python 3.12+
- a place to store the app package or source checkout
- a process manager if you want background hosting
- a reverse proxy if you want public HTTPS

Project Zomboid-specific requirements usually include:

- SteamCMD
- the dedicated server files for app id `380870`

## Basic Ubuntu Setup

This is the simple manual setup path, and it is the closest equivalent to the old installer script without forcing one VPS layout on every user.

### 1. Install the base packages

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo dpkg --add-architecture i386
sudo add-apt-repository multiverse
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip steamcmd
```

If you want remote web access, also install the reverse proxy you plan to use, such as `nginx` or `caddy`.

### 2. Put the app on disk

Pick a location you control, for example:

```bash
sudo mkdir -p /opt/pzserverlauncher
sudo chown -R "$USER":"$USER" /opt/pzserverlauncher
```

Then either extract a release artifact there or clone the repo there.

### 3. Install the package

From a source checkout:

```bash
cd /opt/pzserverlauncher
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

From a built wheel:

```bash
cd /opt/pzserverlauncher
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./dist/pzserverlauncherlinux-*.whl
```

### 4. Create the data and log folders

```bash
sudo mkdir -p /var/lib/pzserverlauncher /var/log/pzserverlauncher
sudo chown -R "$USER":"$USER" /var/lib/pzserverlauncher /var/log/pzserverlauncher
```

If you run the service as another account, assign ownership to that account instead.

### 5. Start the app

```bash
export PZSL_DATA_ROOT=/var/lib/pzserverlauncher
export PZSL_LOGS_ROOT=/var/log/pzserverlauncher
pzserverlauncherlinux --host 127.0.0.1 --port 48231
```

Then visit `http://127.0.0.1:48231` locally and complete the owner bootstrap flow.

### 6. Make it persistent if you want

Use whatever you already trust on that VPS.

Examples:

- `systemd`
- Docker / Compose
- Supervisor
- `screen`
- `tmux`

The sample unit under `systemd/pzserverlauncher.service` is just an example you can adapt.

## Process Manager

Use whatever you already trust on that VPS.

Examples:

- `systemd`
- Docker / Compose
- Supervisor
- `screen`
- `tmux`

The sample unit under `systemd/pzserverlauncher.service` is now just an example, not the expected install path.

## Reverse Proxy

Use the proxy layout that already fits your server.

Examples in this repo:

- [Nginx example](nginx.md)
- [Caddy example](caddy.md)

If the VPS already serves other sites, the cleanest option is usually:

- keep this app on `127.0.0.1:48231`
- add another reverse-proxy site or location
- avoid changing the rest of your existing stack unless you want to

## Notes

- Default managed directories still live under the configured data root.
- The Linux launch planner still expects `start-server.sh` when starting a managed server.
- IP-only access still works if your proxy setup supports it.
