# PZServerLauncherLinux Plan

## Goal

Build a Linux web version of `PZServerLauncher` for Ubuntu VPS hosts.

Feature reference:

`D:\__Projets__\PZServerLauncher`

## App Shape

- Python 3.12+
- FastAPI
- Jinja2
- SQLite
- SQLAlchemy
- Server-rendered web UI
- Local app bind: `127.0.0.1:48231`

Remote access is handled by SSH forwarding or a reverse proxy.

## Deployment Shape

Preferred basic install:

```bash
sudo apt install ./dist/pzserverlauncherlinux_0.1.0_all.deb
```

The `.deb` installs the app, creates the service user, creates data/log folders, and registers a disabled `systemd` service.

It does not expose the app publicly, configure certificates, or edit proxy config.

## Workspaces

- Dashboard
- Profiles
- Profile overview
- Install & Update
- General
- Sandbox
- Mods & Maps
- Network & Admin
- Backups
- Logs
- Advanced Files
- Consoles
- Host
- Remote
- Users

## Core Behavior

- First-run owner setup
- Login and roles
- Multi-profile management
- Local server import
- SteamCMD install/update
- Linux runtime start/stop/restart
- Live logs and console commands
- General `.ini` editing
- Full sandbox catalog and Lua presets
- Workshop/mod/map editing and browser
- Backup, restore, schedule, and world reset
- Audit trail

## Validation

- Unit and route tests for config editing, runtime, backups, auth, import, workshop, and UI flows
- Wheel contains templates, static files, and preset assets
- Ubuntu smoke test remains required before release
