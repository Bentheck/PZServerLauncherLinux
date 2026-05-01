# PZServerLauncherLinux Fresh Rewrite Plan

## Summary

Build `PZServerLauncherLinux` as a fresh Python/FastAPI web application for Ubuntu VPS hosts, using the existing Windows/.NET project at `D:\__Projets__\PZServerLauncher` only as the behavior reference.

The product should keep the same core capabilities:

- Install and update Project Zomboid dedicated servers
- Manage multiple server profiles
- Edit configs through a web UI instead of a desktop app
- Manage mods, maps, backups, logs, users, and runtime operations

It should not include Avalonia, MSI installer, Windows firewall management, or Windows startup registry logic.

Chosen defaults:

- Stack: `Python 3.12+`, `FastAPI`, `Jinja2`, `HTMX`, `SQLite`, `SQLAlchemy`, `Alembic`
- Deployment: `systemd` service plus install/update scripts
- Public access: FastAPI bound to `127.0.0.1`, exposed through Nginx or Caddy
- Access modes:
  - `https://your-domain` when a domain is available
  - `http://your-server-ip` or self-signed `https://your-server-ip` when domainless
- Data root: `/var/lib/pzserverlauncher`
- Service user: `pzlauncher`

## Why `127.0.0.1` Still Works For Remote Access

The app itself listens only on the VPS locally:

`FastAPI -> 127.0.0.1:48231`

Public traffic is handled by the reverse proxy:

`Internet -> Nginx/Caddy on 80/443 -> 127.0.0.1:48231`

That means:

- The admin app is still reachable from outside
- The Python app port is not directly exposed
- TLS, request filtering, and public access rules live in the proxy

For IP-only access without a domain:

- `http://SERVER_IP` is the simplest option
- `https://SERVER_IP` can work with a self-signed certificate
- Real CA-issued HTTPS for raw IPs is possible but less common and should not be the v1 default

## Key Changes

### 1. New Linux-Native App

- Scaffold a new repo structure in `D:\__Projets__\PZServerLauncherLinux`
- Use server-rendered FastAPI pages with HTMX partial updates
- Avoid a Node frontend build pipeline for v1
- Treat the old project as the functional blueprint, not as the implementation base

Suggested top-level structure:

- `app/`
- `tests/`
- `scripts/`
- `docs/`
- `systemd/`

### 2. Data, Auth, and Permissions

- Use SQLite for profiles, users, host settings, jobs, drafts, presets, backups metadata, and audit entries
- Add first-run owner bootstrap at `/setup`
- Disable `/setup` once an owner account exists
- Implement role-based access:
  - `Owner`
  - `Admin`
  - `Operator`
  - `Viewer`
- Use secure password hashing, signed session cookies, CSRF protection, login/setup rate limiting, and audit logs for sensitive actions

### 3. Ubuntu/VPS Deployment Model

- Run the application as a dedicated `systemd` service
- Create an install script that prepares:
  - Python runtime or venv
  - service user `pzlauncher`
  - app directories
  - permissions
  - systemd unit
- Publish the app behind Nginx or Caddy
- Keep the app bound to `127.0.0.1:48231` by default
- Document both domain-based and IP-only deployment

### 4. Project Zomboid Install and Runtime Management

- Install and update the dedicated server through SteamCMD using app id `380870`
- Default managed install path:
  - `/var/lib/pzserverlauncher/servers/<profile_id>/install`
- Default managed cache/config path:
  - `/var/lib/pzserverlauncher/servers/<profile_id>/cache`
- Implement a Linux launch planner that builds a launcher-owned Java command from the Linux server layout
- If launch cannot be safely built, block start with a clear diagnostic instead of guessing
- Supervise the running server with `asyncio.create_subprocess_exec`
- Support:
  - start
  - stop
  - restart
  - live output capture
  - stdin console commands
  - graceful stop before force kill
  - optional auto-restart on crash

### 5. Web Feature Parity

Match the current launcher feature map with web pages for:

- Dashboard
- Profiles
- Profile Overview
- Install and Update
- General
- Sandbox
- Mods and Maps
- Network and Admin
- Backups
- Logs
- Advanced Files
- Host
- Users
- Remote Access

Behavior goals:

- Structured editors for normal settings
- Raw file editing still available as the escape hatch
- Multi-profile management preserved
- Backups before risky operations
- Live runtime status and logs visible in the browser

### 6. Settings and File Handling

- Support structured editing for Project Zomboid config files where safe
- Prioritize `.ini`, `SandboxVars.lua`, mods/maps lists, and common runtime settings
- Preserve unknown fields and formatting where practical
- Fall back to raw editing when a file is too ambiguous or unsafe to rewrite structurally
- Save settings drafts for non-secret pages
- Avoid draft persistence for secret-bearing fields if needed

### 7. Logging, Jobs, and Operations

- Persist logs under `/var/log/pzserverlauncher`
- Keep recent logs available in-memory for responsive UI updates
- Add background job tracking for:
  - install
  - update
  - backup
  - restore
  - world reset
- Stream live logs through WebSockets or Server-Sent Events
- Record operator actions in an audit trail

## Interfaces

Web routes:

- `/setup`
- `/login`
- `/logout`
- `/dashboard`
- `/profiles`
- `/profiles/{id}/overview`
- `/profiles/{id}/install-update`
- `/profiles/{id}/general`
- `/profiles/{id}/sandbox`
- `/profiles/{id}/mods-maps`
- `/profiles/{id}/network-admin`
- `/profiles/{id}/backups`
- `/profiles/{id}/logs`
- `/profiles/{id}/advanced-files`
- `/host`
- `/users`
- `/remote`

API groups:

- `/api/host`
- `/api/profiles`
- `/api/profiles/{id}/settings`
- `/api/profiles/{id}/runtime`
- `/api/profiles/{id}/backups`
- `/api/users`
- `/api/jobs`

Core models:

- `User`
- `Role`
- `HostSettings`
- `ReverseProxySettings`
- `ServerProfile`
- `BackupPolicy`
- `SettingsDraft`
- `WorkshopPreset`
- `OperationJob`
- `RuntimeStatus`
- `AuditEntry`

Runtime states:

- `stopped`
- `starting`
- `running`
- `stopping`
- `crashed`
- `blocked`

Deployment artifacts to create:

- `scripts/install-ubuntu.sh`
- `scripts/update.sh`
- `scripts/backup-app-data.sh`
- `systemd/pzserverlauncher.service`
- `docs/nginx.md`
- `docs/caddy.md`
- `docs/ubuntu-vps-setup.md`

## Test Plan

Unit tests:

- Config parsing and round-trip writes for `.ini` and Lua settings
- Profile creation and path defaulting
- Port conflict and memory validation
- Linux launch planning for supported and unsupported layouts
- SteamCMD script generation
- Backup creation, restore, and retention cleanup
- Auth, roles, CSRF, and bootstrap lockout

Integration tests:

- Create profile
- Save settings
- Queue install/update
- Block invalid launches with visible diagnostics
- List jobs
- View recent logs
- User and role management

Manual Ubuntu VPS verification:

- Install script creates `pzlauncher`, folders, permissions, and service
- App starts on `127.0.0.1:48231`
- Reverse proxy exposes it by domain and by IP
- Owner bootstrap works once
- SteamCMD install/update works
- Logs stream live
- Backups and restore work

## Assumptions

- Target OS for v1 is Ubuntu VPS, primarily Ubuntu 24.04 LTS or newer
- This repo is a fresh rewrite and does not need to preserve the .NET API surface
- The old project is the feature reference, not the codebase to extend
- Docker is out of scope for v1
- Windows desktop support is out of scope for this repo
- IP-only access should be supported, but domain-based HTTPS remains the cleanest recommended deployment
