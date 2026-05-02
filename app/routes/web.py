from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import (
    ensure_csrf_token,
    get_current_user,
    get_db,
    get_host_settings,
    get_profile_or_404,
    has_any_users,
    role_allows,
    validate_csrf,
)
from app.models import AuditEntry, HostSettings, JobStatus, OperationJob, ServerProfile, SettingsDraft, User, UserRole, WorkshopPreset
from app.security import InMemoryRateLimiter, ensure_password_policy, hash_password, slugify, verify_password
from app.services.audit import record_audit
from app.services.config_files import FlatIniDocument
from app.services.imports import LocalServerImportService
from app.services.profile_live import build_runtime_diagnostic, find_active_job, serialize_job, serialize_launch_plan
from app.services.runtime import InstallJobOptions

router = APIRouter(tags=["web"])
templates: Jinja2Templates | None = None
auth_limiter = InMemoryRateLimiter(limit=5, window_seconds=900)


PROFILE_TABS = [
    ("overview", "Overview"),
    ("install-update", "Install & Update"),
    ("general", "General"),
    ("sandbox", "Sandbox"),
    ("mods-maps", "Mods & Maps"),
    ("network-admin", "Network & Admin"),
    ("backups", "Backups"),
    ("logs", "Logs"),
    ("advanced-files", "Advanced Files"),
]

QUICK_RUNTIME_COMMANDS = [
    ("save", "Save World"),
    ("players", "List Players"),
    ("quit", "Graceful Quit"),
]

CONSOLE_SLOT_COUNT = 4
IMPORT_CACHE_SESSION_KEY = "import_cache_directory"
IMPORT_INSTALL_SESSION_KEY = "import_install_directory"

BRANCH_OPTIONS = [
    ("stable", "Stable"),
    ("unstable", "Unstable"),
]

USER_ROLE_OPTIONS = [
    (UserRole.viewer.value, "Viewer"),
    (UserRole.operator.value, "Operator"),
    (UserRole.admin.value, "Admin"),
    (UserRole.owner.value, "Owner"),
]


def parse_checkbox(value: str | None) -> bool:
    return value == "on"


def parse_textarea_list(value: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for line in value.replace("\r\n", "\n").split("\n"):
        for piece in line.split(";"):
            normalized = piece.strip()
            if not normalized:
                continue

            lowered = normalized.lower()
            if lowered in seen:
                continue

            seen.add(lowered)
            items.append(normalized)

    return items


def format_bytes(value: int) -> str:
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{int(value)} B"


def build_backup_entries(backup_files: list[Path]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for backup in backup_files:
        stats = backup.stat()
        entries.append(
            {
                "name": backup.name,
                "path": str(backup),
                "size_label": format_bytes(stats.st_size),
                "modified_label": datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
        )
    return entries


def build_job_entries(jobs: list[OperationJob], profiles_by_id: dict[str, str]) -> list[dict[str, object]]:
    return [serialize_job(job, profiles_by_id) for job in jobs]


def profile_install_detected(profile: ServerProfile) -> bool:
    install_path = Path(profile.install_directory)
    return (install_path / "start-server.sh").exists() or (install_path / "StartServer.sh").exists()


def build_profile_posture_row(request: Request, profile: ServerProfile) -> dict[str, object]:
    runtime_manager = request.app.state.runtime_manager
    zomboid_service = request.app.state.zomboid_service
    config_service = request.app.state.config_service
    raw_paths = config_service.raw_file_paths(profile)
    install_detected = profile_install_detected(profile)
    ini_detected = raw_paths["ini"].exists()
    sandbox_detected = raw_paths["sandbox"].exists()
    server_root_detected = raw_paths["ini"].parent.exists()
    backup_count = len(zomboid_service.list_backups(profile))
    launch_plan = zomboid_service.build_launch_plan(profile)
    snapshot = runtime_manager.get_status(profile.id)
    modded = False
    if ini_detected:
        document = FlatIniDocument.parse(raw_paths["ini"].read_text(encoding="utf-8", errors="replace"))
        modded = any(
            (document.get(key, "") or "").strip()
            for key in ("WorkshopItems", "Mods", "Map")
        )

    has_recovery = profile.backup_enabled or backup_count > 0
    launch_ready = install_detected and server_root_detected and ini_detected and sandbox_detected and not launch_plan.blocked
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "server_name": profile.server_name,
        "branch": profile.branch,
        "ports": f"{profile.default_port}/{profile.udp_port}",
        "runtime_state": snapshot.state,
        "start_with_host": profile.start_with_host,
        "auto_restart_on_crash": profile.auto_restart_on_crash,
        "backup_enabled": profile.backup_enabled,
        "backup_count": backup_count,
        "has_recovery": has_recovery,
        "install_detected": install_detected,
        "server_root_detected": server_root_detected,
        "ini_detected": ini_detected,
        "sandbox_detected": sandbox_detected,
        "launch_ready": launch_ready,
        "launch_blocked": launch_plan.blocked,
        "is_modded": modded,
        "latest_log_line": snapshot.latest_log_line or "",
        "last_exit_reason": snapshot.last_exit_reason or "",
    }


def find_blocking_profile_job(db: Session, profile_id: str) -> OperationJob | None:
    return db.scalar(
        select(OperationJob)
        .where(
            OperationJob.profile_id == profile_id,
            OperationJob.status.in_([JobStatus.queued.value, JobStatus.running.value]),
        )
        .order_by(OperationJob.created_at.desc())
    )


def count_active_owners(db: Session) -> int:
    return len(
        db.scalars(
            select(User).where(
                User.role == UserRole.owner.value,
                User.is_active.is_(True),
            )
        ).all()
    )


def get_import_probe_paths(request: Request) -> tuple[str, str]:
    return (
        str(request.session.get(IMPORT_CACHE_SESSION_KEY, "") or ""),
        str(request.session.get(IMPORT_INSTALL_SESSION_KEY, "") or ""),
    )


def build_import_service(request: Request) -> LocalServerImportService:
    cache_directory, install_directory = get_import_probe_paths(request)
    if cache_directory.strip():
        install_directories = [Path(install_directory)] if install_directory.strip() else []
        return LocalServerImportService(
            request.app.state.settings,
            request.app.state.config_service,
            cache_roots=[Path(cache_directory)],
            install_directories=install_directories,
        )

    return request.app.state.import_service


def discover_import_candidates(request: Request, profiles: list[ServerProfile]):
    return build_import_service(request).discover(profiles)


def safe_next_url(candidate: str | None, fallback: str) -> str:
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def sandbox_ui_state_from_request(request: Request) -> dict[str, str]:
    return {
        "preset_id": request.query_params.get("preset_id", "").strip(),
        "search": request.query_params.get("q", "").strip(),
        "category": request.query_params.get("category", "").strip(),
    }


def sandbox_ui_state_from_form(form) -> dict[str, str]:
    return {
        "preset_id": str(form.get("sandbox_preset_id", "") or "").strip(),
        "search": str(form.get("sandbox_search", "") or "").strip(),
        "category": str(form.get("sandbox_category", "") or "").strip(),
    }


def sandbox_url(profile_id: str, *, preset_id: str = "", search: str = "", category: str = "") -> str:
    query: dict[str, str] = {}
    if preset_id:
        query["preset_id"] = preset_id
    if search:
        query["q"] = search
    if category:
        query["category"] = category

    base = f"/profiles/{profile_id}/sandbox"
    return f"{base}?{urlencode(query)}" if query else base


def sandbox_redirect(profile_id: str, *, preset_id: str = "", search: str = "", category: str = "") -> RedirectResponse:
    return redirect(sandbox_url(profile_id, preset_id=preset_id, search=search, category=category))


def mods_maps_ui_state_from_request(request: Request) -> dict[str, str]:
    return {
        "search": request.query_params.get("q", "").strip(),
        "preview": request.query_params.get("preview", "").strip(),
    }


def mods_maps_ui_state_from_form(form) -> dict[str, str]:
    return {
        "search": str(form.get("browser_query", "") or "").strip(),
        "preview": str(form.get("preview_workshop_id", "") or "").strip(),
    }


def mods_maps_url(profile_id: str, *, search: str = "", preview: str = "") -> str:
    query: dict[str, str] = {}
    if search:
        query["q"] = search
    if preview:
        query["preview"] = preview

    base = f"/profiles/{profile_id}/mods-maps"
    return f"{base}?{urlencode(query)}" if query else base


def mods_maps_redirect(profile_id: str, *, search: str = "", preview: str = "") -> RedirectResponse:
    return redirect(mods_maps_url(profile_id, search=search, preview=preview))


def normalize_console_slot_number(value: int | str | None) -> int:
    try:
        number = int(value or 1)
    except (TypeError, ValueError):
        return 1

    if number < 1:
        return 1
    if number > CONSOLE_SLOT_COUNT:
        return CONSOLE_SLOT_COUNT
    return number


def get_console_slots(request: Request) -> list[str | None]:
    raw = request.session.get("console_slots")
    slots: list[str | None] = []
    if isinstance(raw, list):
        for index in range(CONSOLE_SLOT_COUNT):
            value = raw[index] if index < len(raw) else None
            if isinstance(value, str) and value.strip():
                slots.append(value.strip())
            else:
                slots.append(None)
    else:
        slots = [None] * CONSOLE_SLOT_COUNT
    return slots


def save_console_slots(request: Request, slots: list[str | None]) -> None:
    request.session["console_slots"] = [(slot or "") for slot in slots[:CONSOLE_SLOT_COUNT]]


def get_selected_console_slot(request: Request) -> int:
    return normalize_console_slot_number(request.session.get("console_selected_slot"))


def save_selected_console_slot(request: Request, slot_number: int) -> None:
    request.session["console_selected_slot"] = normalize_console_slot_number(slot_number)


def ensure_console_slots_initialized(request: Request, profiles: list[ServerProfile]) -> list[str | None]:
    slots = get_console_slots(request)
    if request.session.get("console_slots_initialized"):
        return slots

    seeded_slots = list(slots)
    profile_index = 0
    for slot_index in range(CONSOLE_SLOT_COUNT):
        if seeded_slots[slot_index]:
            continue
        if profile_index >= len(profiles):
            break
        seeded_slots[slot_index] = profiles[profile_index].id
        profile_index += 1

    request.session["console_slots_initialized"] = True
    save_console_slots(request, seeded_slots)
    return seeded_slots


def get_settings_draft(db: Session, *, profile_id: str, page_id: str) -> SettingsDraft | None:
    return db.scalar(
        select(SettingsDraft).where(
            SettingsDraft.profile_id == profile_id,
            SettingsDraft.page_id == page_id,
        )
    )


def load_sandbox_draft_values(db: Session, *, profile_id: str) -> dict[str, str] | None:
    row = get_settings_draft(db, profile_id=profile_id, page_id="sandbox")
    if row is None:
        return None

    try:
        payload = json.loads(row.payload_json)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    values: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        values[key] = "" if value is None else str(value)
    return values


def load_sandbox_editor_values(
    db: Session,
    *,
    sandbox_service,
    profile: ServerProfile,
) -> tuple[dict[str, str], dict[str, str], bool]:
    live_values = sandbox_service.load_values(profile)
    draft_values = load_sandbox_draft_values(db, profile_id=profile.id)
    editor_values = dict(live_values)
    if draft_values is not None:
        for field_name in sandbox_service.fields_by_name:
            if field_name in draft_values:
                editor_values[field_name] = draft_values[field_name]
    return live_values, editor_values, draft_values is not None


def normalize_sandbox_form_values(form, *, sandbox_service, current_values: dict[str, str]) -> dict[str, str]:
    visible_fields = [str(value) for value in form.getlist("__visible_field")]
    return sandbox_service.normalize_values(form, current_values, visible_fields=visible_fields or None)


def save_sandbox_draft(db: Session, *, profile_id: str, values: dict[str, str]) -> SettingsDraft:
    row = get_settings_draft(db, profile_id=profile_id, page_id="sandbox")
    payload_json = json.dumps(values, sort_keys=True)
    if row is None:
        row = SettingsDraft(profile_id=profile_id, page_id="sandbox", payload_json=payload_json)
        db.add(row)
    else:
        row.payload_json = payload_json

    db.commit()
    db.refresh(row)
    return row


def delete_sandbox_draft(db: Session, *, profile_id: str) -> bool:
    row = get_settings_draft(db, profile_id=profile_id, page_id="sandbox")
    if row is None:
        return False

    db.delete(row)
    db.commit()
    return True


def configure_templates(templates_value: Jinja2Templates) -> None:
    global templates
    templates = templates_value


def render(request: Request, template_name: str, **context: Any) -> HTMLResponse:
    if templates is None:
        raise RuntimeError("Templates have not been configured.")

    session_factory = request.app.state.session_factory
    with session_factory() as db:
        current_user = get_current_user(request, db)

    flashes = request.session.pop("flashes", [])
    payload = {
        "request": request,
        "current_user": current_user,
        "csrf_token": ensure_csrf_token(request),
        "flashes": flashes,
    }
    payload.update(context)
    return templates.TemplateResponse(request, template_name, payload)


def flash(request: Request, kind: str, message: str) -> None:
    messages = request.session.get("flashes", [])
    messages.append({"kind": kind, "message": message})
    request.session["flashes"] = messages


def redirect(url: str, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status_code)


def current_user_or_redirect(request: Request, db: Session, minimum_role: UserRole = UserRole.viewer) -> User | RedirectResponse:
    user = get_current_user(request, db)
    if user is None:
        return redirect("/login")

    if not role_allows(user, minimum_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to that page.")

    return user


def retirement_guard_message(request: Request, db: Session, profile: ServerProfile) -> str | None:
    blocking_job = find_blocking_profile_job(db, profile.id)
    if blocking_job is not None:
        return (
            f"Wait for the active {blocking_job.kind} job to finish before uninstalling or deleting {profile.display_name}."
        )

    runtime_manager = request.app.state.runtime_manager
    snapshot = runtime_manager.get_status(profile.id)
    if profile.id in runtime_manager._processes or snapshot.state in {"starting", "running", "stopping"}:
        return f"Stop {profile.display_name} before uninstalling or deleting it."

    return None


def build_dashboard_summary(request: Request, db: Session, user: User) -> dict[str, object]:
    host_settings = get_host_settings(db, request)
    runtime_manager = request.app.state.runtime_manager
    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    jobs = db.scalars(select(OperationJob).order_by(OperationJob.created_at.desc()).limit(8)).all()
    audits = db.scalars(select(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(8)).all()
    import_candidates = discover_import_candidates(request, profiles)
    profile_rows = [build_profile_posture_row(request, profile) for profile in profiles]
    profile_lookup = {profile.id: profile.display_name for profile in profiles}

    running_count = sum(1 for row in profile_rows if row["runtime_state"] == "running")
    installed_profile_count = sum(1 for row in profile_rows if row["install_detected"])
    launch_ready_count = sum(1 for row in profile_rows if row["launch_ready"])
    recovery_ready_count = sum(1 for row in profile_rows if row["has_recovery"])
    modded_profile_count = sum(1 for row in profile_rows if row["is_modded"])
    blocked_profile_count = sum(1 for row in profile_rows if row["launch_blocked"])
    first_run = not profiles and not import_candidates
    has_import_candidates = bool(import_candidates)

    if has_import_candidates:
        setup_mode_headline = "Import the loaded server or start a new managed one."
        setup_mode_summary = (
            "A Project Zomboid footprint has been loaded from the path you supplied. Import it if you want to keep its files, "
            "or create a clean managed server instead."
        )
        setup_primary_action_label = "Review Imports"
        setup_primary_action_href = "/profiles#import-intake"
        setup_secondary_action_label = "Point To Server"
        current_focus_summary = (
            f"{len(import_candidates)} import candidate(s) are ready for intake. "
            "Bring one under management first, then verify install, backup, and launch posture before the first live boot."
        )
        current_focus_hint = "Import first if you already have a server path. Create first if you want a clean managed server."
        next_step_summary = "Review the import candidates next, then open the imported profile and confirm install, cache, and recovery posture."
    elif profiles:
        setup_mode_headline = "Bring the next server online or tighten the fleet."
        setup_mode_summary = (
            "The integrated runtime is already supervising your fleet. Use this board to decide what to launch, repair, or back up next."
        )
        setup_primary_action_label = "Open Profiles"
        setup_primary_action_href = "/profiles"
        setup_secondary_action_label = "Point To Import"
        current_focus_summary = (
            "This is now a real fleet board. Use it to decide what to launch, repair, or tighten next across the managed servers."
        )
        current_focus_hint = "Move into Profiles when you want to tune one server in depth, or jump into Consoles for live runtime work."
        if recovery_ready_count < len(profile_rows):
            next_step_summary = "Capture recovery coverage for the profiles still missing backups before deeper update or mod work."
        elif installed_profile_count < len(profile_rows):
            next_step_summary = "Finish installing the remaining profiles so every branch has a dedicated server footprint."
        elif blocked_profile_count > 0:
            next_step_summary = "Open the blocked profiles next and clear their launch diagnostics before the next live boot."
        elif modded_profile_count > 0:
            next_step_summary = "Review the modded profiles next so Workshop, Mods, and Map order still match the local cache."
        else:
            next_step_summary = "The fleet looks clean. The next useful move is tuning Sandbox or General settings on the profile you plan to launch next."
    else:
        setup_mode_headline = "Start with a new managed server or adopt a local one."
        setup_mode_summary = (
            "No managed servers exist yet. Create the first profile for a clean setup, or point the launcher at an existing server cache."
        )
        setup_primary_action_label = "Create First Profile"
        setup_primary_action_href = "/profiles#create-profile"
        setup_secondary_action_label = "Point To Import"
        current_focus_summary = (
            "The fastest path is create or import, install the dedicated server footprint, capture a first backup, then finish tuning before launch."
        )
        current_focus_hint = "Create First Profile starts from scratch. Import uses the exact cache/install paths you provide."
        next_step_summary = "Create or import the first profile to start building a real server fleet."

    can_manage_profiles = role_allows(user, UserRole.operator)
    if not can_manage_profiles and setup_primary_action_href == "/profiles#create-profile":
        setup_primary_action_label = "Open Profiles"
        setup_primary_action_href = "/profiles"

    if not profiles:
        fleet_risk_summary = "No fleet posture is available until the first profile exists."
        fleet_summary = "No server fleet posture is available until the first profile exists."
    elif recovery_ready_count < len(profile_rows):
        fleet_risk_summary = f"{len(profile_rows) - recovery_ready_count} profile(s) still need recovery coverage before riskier update or mod work."
        fleet_summary = (
            f"{installed_profile_count}/{len(profile_rows)} installed | {launch_ready_count} launch-ready | "
            f"{recovery_ready_count} recovery-ready | {blocked_profile_count} launch blocked | {modded_profile_count} modded"
        )
    elif installed_profile_count < len(profile_rows):
        fleet_risk_summary = f"{len(profile_rows) - installed_profile_count} profile(s) still do not have a detected install footprint."
        fleet_summary = (
            f"{installed_profile_count}/{len(profile_rows)} installed | {launch_ready_count} launch-ready | "
            f"{recovery_ready_count} recovery-ready | {blocked_profile_count} launch blocked | {modded_profile_count} modded"
        )
    elif blocked_profile_count > 0:
        fleet_risk_summary = f"{blocked_profile_count} installed profile(s) are currently launch blocked until the launcher can build a valid plan."
        fleet_summary = (
            f"{installed_profile_count}/{len(profile_rows)} installed | {launch_ready_count} launch-ready | "
            f"{recovery_ready_count} recovery-ready | {blocked_profile_count} launch blocked | {modded_profile_count} modded"
        )
    else:
        fleet_risk_summary = "Backups and launch footprints look healthy across the current fleet."
        fleet_summary = (
            f"{installed_profile_count}/{len(profile_rows)} installed | {launch_ready_count} launch-ready | "
            f"{recovery_ready_count} recovery-ready | {blocked_profile_count} launch blocked | {modded_profile_count} modded"
        )

    if running_count > 0:
        consoles_workspace_summary = f"{running_count} running server(s) can be pinned into the 4-slot console board."
    else:
        consoles_workspace_summary = "Pin up to four servers in Consoles so live runtime output stays one workspace away."

    app_workspace_summary = (
        f"The web app stays loopback-bound on {host_settings.bind_host}:{host_settings.bind_port} while "
        f"{host_settings.reverse_proxy} handles the public edge."
    )

    return {
        "profile_count": len(profiles),
        "running_count": running_count,
        "installed_profile_count": installed_profile_count,
        "launch_ready_count": launch_ready_count,
        "recovery_ready_count": recovery_ready_count,
        "modded_profile_count": modded_profile_count,
        "blocked_profile_count": blocked_profile_count,
        "jobs": build_job_entries(jobs, profile_lookup),
        "audits": audits,
        "profiles": profiles,
        "import_candidates": import_candidates,
        "import_candidate_count": len(import_candidates),
        "has_import_candidates": has_import_candidates,
        "has_no_import_candidates": not has_import_candidates,
        "is_first_run": first_run,
        "setup_mode_headline": setup_mode_headline,
        "setup_mode_summary": setup_mode_summary,
        "setup_primary_action_label": setup_primary_action_label,
        "setup_primary_action_href": setup_primary_action_href,
        "setup_secondary_action_label": setup_secondary_action_label,
        "current_focus_summary": current_focus_summary,
        "current_focus_hint": current_focus_hint,
        "fleet_risk_summary": fleet_risk_summary,
        "fleet_summary": fleet_summary,
        "next_step_summary": next_step_summary,
        "app_workspace_summary": app_workspace_summary,
        "consoles_workspace_summary": consoles_workspace_summary,
        "import_summary": (
            f"{len(import_candidates)} import candidate(s) loaded."
            if import_candidates
            else "No import candidates are loaded yet. Open Profiles and enter an existing server cache path."
        ),
        "recent_job_summary": (
            f"{len(jobs)} recent job(s) recorded."
            if jobs
            else "No recent host jobs have been recorded yet."
        ),
        "can_manage_profiles": can_manage_profiles,
        "dashboard_steps": [
            "Step 1: Create a starter profile or import a server path you provide.",
            "Step 2: Confirm install, cache, branch, and backup posture.",
            "Step 3: Tune settings, capture a restore point, then launch and watch the runtime live.",
        ],
    }


def build_host_summary(request: Request, db: Session) -> dict[str, object]:
    host_settings = get_host_settings(db, request)
    runtime_manager = request.app.state.runtime_manager
    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    managed_rows = [build_profile_posture_row(request, profile) for profile in profiles]
    running_profiles = sum(1 for row in managed_rows if row["runtime_state"] == "running")
    startup_profiles = sum(1 for row in managed_rows if row["start_with_host"])
    recovery_ready_profiles = sum(1 for row in managed_rows if row["has_recovery"])
    install_ready_profiles = sum(1 for row in managed_rows if row["install_detected"])
    launch_ready_profiles = sum(1 for row in managed_rows if row["launch_ready"])
    blocked_profiles = sum(1 for row in managed_rows if row["launch_blocked"])
    active_jobs = db.scalars(
        select(OperationJob)
        .where(OperationJob.status.in_([JobStatus.queued.value, JobStatus.running.value]))
        .order_by(OperationJob.created_at.desc())
        .limit(5)
    ).all()
    shutdown_request = getattr(request.app.state, "host_shutdown_request", None)

    exposure = host_settings.public_base_url or "Loopback only until a public URL is configured"
    checklist: list[dict[str, str]] = []
    if not profiles:
        checklist.append({"status": "Needs setup", "message": "Create or import a profile so the host has something to supervise."})
    if not host_settings.public_base_url and host_settings.access_mode != "private":
        checklist.append({"status": "Needs setup", "message": "Set the public base URL so the remote page can match the actual VPS exposure."})
    if host_settings.reverse_proxy not in {"nginx", "caddy"}:
        checklist.append({"status": "Review", "message": "Choose a supported reverse proxy posture for public access."})
    if startup_profiles == 0 and profiles:
        checklist.append({"status": "Optional", "message": "No profiles are marked to start with the host yet."})
    if recovery_ready_profiles < len(profiles):
        checklist.append({"status": "Review", "message": "Some profiles still do not have scheduled backups or saved restore points yet."})
    if running_profiles == 0 and profiles:
        checklist.append({"status": "Idle", "message": "No managed server is running right now."})
    if blocked_profiles > 0:
        checklist.append({"status": "Review", "message": "One or more profiles are currently launch blocked and need install or config attention."})
    if active_jobs:
        checklist.append({"status": "Active", "message": f"{len(active_jobs)} host job(s) are still queued or running."})
    if shutdown_request is not None:
        checklist.append({"status": "Pending", "message": "A coordinated runtime shutdown request is staged on this host."})
    if not checklist:
        checklist.append({"status": "Healthy", "message": "Host supervision, remote posture, and recovery coverage are in a good place."})

    if not profiles:
        risk_summary = "No machine-level risk posture exists until the first managed profile is created or imported."
    elif recovery_ready_profiles < len(profiles):
        risk_summary = f"{len(profiles) - recovery_ready_profiles} profile(s) still need recovery coverage before deeper maintenance."
    elif blocked_profiles > 0:
        risk_summary = f"{blocked_profiles} profile(s) are currently launch blocked and should be reviewed before the next reboot or startup-roster run."
    elif running_profiles == 0:
        risk_summary = "The host is idle right now. That is safe, but there is no live runtime coverage to watch."
    else:
        risk_summary = "The host roster, runtime state, and recovery posture look healthy."

    if running_profiles > 0:
        operator_summary = "Use this page to stop the live fleet cleanly, rehearse the startup roster, and stage service maintenance."
        shutdown_summary = "For runtime maintenance, stop the servers first when possible, then stop the systemd service from the host shell."
    else:
        operator_summary = "Use this page to rehearse the startup roster, tighten host posture, and stage the next maintenance window safely."
        shutdown_summary = "No managed server is running, so this is a safe moment to stage a launcher-service restart or stop."

    service_stop_command = "sudo systemctl stop pzserverlauncher"
    service_restart_command = "sudo systemctl restart pzserverlauncher"

    return {
        "host_settings": host_settings,
        "profiles": profiles,
        "managed_rows": managed_rows,
        "managed_profile_count": len(profiles),
        "running_profile_count": running_profiles,
        "startup_profile_count": startup_profiles,
        "recovery_ready_profile_count": recovery_ready_profiles,
        "install_ready_profile_count": install_ready_profiles,
        "launch_ready_profile_count": launch_ready_profiles,
        "blocked_profile_count": blocked_profiles,
        "host_status_summary": f"{running_profiles} running / {len(profiles)} managed",
        "host_lifecycle_summary": (
            f"{startup_profiles} profile(s) start with host, {recovery_ready_profiles} profile(s) are recovery-ready, "
            f"and the app origin stays on {host_settings.bind_host}:{host_settings.bind_port}."
        ),
        "host_risk_summary": risk_summary,
        "host_operator_summary": operator_summary,
        "host_shutdown_summary": shutdown_summary,
        "host_action_summary": checklist[0]["message"],
        "host_recovery_summary": (
            "Scheduled backups and saved restore points both count as recovery coverage for the host roster."
        ),
        "host_startup_label": (
            "Startup roster ready"
            if startup_profiles > 0
            else "No startup roster yet"
        ),
        "host_startup_coverage_summary": (
            f"{startup_profiles} of {len(profiles)} managed profile(s) are configured to start with the host."
        ),
        "host_recovery_coverage_summary": (
            f"{recovery_ready_profiles} of {len(profiles)} managed profile(s) already have recovery coverage."
        ),
        "host_runtime_coverage_summary": (
            f"{running_profiles} of {len(profiles)} managed profile(s) are actively supervised right now."
        ),
        "host_startup_fleet_summary": (
            f"{len(profiles)} managed profile(s) are available for coordinated startup, crash recovery, and stop-all operations."
        ),
        "host_fleet_summary": (
            f"{install_ready_profiles}/{len(profiles)} installs detected | {launch_ready_profiles} launch-ready | "
            f"{blocked_profiles} blocked | {recovery_ready_profiles} recovery-ready"
        ),
        "host_exposure_summary": (
            f"Public posture: {host_settings.access_mode} via {host_settings.reverse_proxy} | "
            f"{host_settings.tls_mode} | {exposure}"
        ),
        "host_security_summary": (
            "FastAPI should stay loopback-bound while the reverse proxy handles public exposure, TLS, and origin filtering."
        ),
        "host_checklist": checklist,
        "host_next_step_summary": checklist[0]["message"],
        "runtime_statuses": runtime_manager.list_statuses(),
        "host_service_user_summary": (
            f"The launcher expects the system service user '{host_settings.server_user}' and stores data under "
            f"{host_settings.data_root} with logs under {host_settings.logs_root}."
        ),
        "host_default_managed_root": str(request.app.state.settings.servers_root),
        "host_service_stop_command": service_stop_command,
        "host_service_restart_command": service_restart_command,
        "host_shutdown_request": shutdown_request,
        "host_has_shutdown_request": shutdown_request is not None,
    }


def build_remote_summary(request: Request, db: Session) -> dict[str, object]:
    host_settings = get_host_settings(db, request)
    public_url = host_settings.public_base_url or ""
    listener = f"http://{host_settings.bind_host}:{host_settings.bind_port}"

    if host_settings.access_mode == "domain":
        endpoint_mode = "Domain"
        recommended_url = public_url or "https://example.com"
        recommendation = "Use a real domain with Let's Encrypt if this panel will be exposed to the public internet."
    elif host_settings.access_mode == "ip":
        endpoint_mode = "IP only"
        recommended_url = public_url or "http://203.0.113.10"
        recommendation = "IP-only mode works, but HTTPS is either self-signed or more operationally awkward than the domain path."
    else:
        endpoint_mode = "Private"
        recommended_url = public_url or listener
        recommendation = "Private mode is best when you only reach the panel through a VPN, LAN, or SSH tunnel."

    checklist: list[dict[str, str]] = []
    if not public_url and host_settings.access_mode != "private":
        checklist.append({"status": "Needs setup", "message": "Set the public base URL so operators know which URL the reverse proxy should serve."})
    if host_settings.access_mode == "domain" and host_settings.tls_mode != "proxy-letsencrypt":
        checklist.append({"status": "Review", "message": "Domain mode is strongest when TLS mode is set to Let's Encrypt."})
    if host_settings.access_mode == "ip" and host_settings.tls_mode == "proxy-letsencrypt":
        checklist.append({"status": "Review", "message": "Let's Encrypt usually expects a domain. Switch to HTTP over IP or self-signed HTTPS for raw IP access."})
    if host_settings.reverse_proxy == "nginx":
        checklist.append({"status": "Guide", "message": "Use docs/nginx.md as the base reverse proxy reference for this posture."})
    else:
        checklist.append({"status": "Guide", "message": "Use docs/caddy.md as the base reverse proxy reference for this posture."})

    return {
        "host_settings": host_settings,
        "remote_listener": listener,
        "remote_endpoint_mode": endpoint_mode,
        "remote_recommended_url": recommended_url,
        "remote_recommendation": recommendation,
        "remote_checklist": checklist,
        "remote_proxy_summary": (
            f"{host_settings.reverse_proxy} fronts {listener} while the public entrypoint stays "
            f"{public_url or 'not configured yet'}."
        ),
        "remote_tls_summary": (
            "Let's Encrypt is best for domain mode. IP-only mode usually means HTTP or self-signed HTTPS."
        ),
    }


def build_consoles_summary(request: Request, db: Session) -> dict[str, object]:
    runtime_manager = request.app.state.runtime_manager
    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    slots = ensure_console_slots_initialized(request, profiles)
    selected_slot_number = get_selected_console_slot(request)
    profiles_by_id = {profile.id: profile for profile in profiles}
    normalized_slots = [slot if slot in profiles_by_id else None for slot in slots]
    if normalized_slots != slots:
        save_console_slots(request, normalized_slots)
        slots = normalized_slots

    def runtime_weight(profile: ServerProfile) -> int:
        state = runtime_manager.get_status(profile.id).state
        if state == "running":
            return 3
        if state in {"starting", "stopping"}:
            return 2
        if state == "crashed":
            return 1
        return 0

    picker_profiles = sorted(
        profiles,
        key=lambda profile: (-runtime_weight(profile), profile.display_name.lower()),
    )

    console_slots: list[dict[str, object]] = []
    running_profile_count = 0
    for index in range(CONSOLE_SLOT_COUNT):
        slot_number = index + 1
        profile_id = slots[index]
        profile = profiles_by_id.get(profile_id or "")
        snapshot = runtime_manager.get_status(profile.id) if profile is not None else None
        if snapshot is not None and snapshot.state == "running":
            running_profile_count += 1
        recent_logs = runtime_manager.recent_logs(profile.id, limit=80) if profile is not None else []
        console_slots.append(
            {
                "slot_number": slot_number,
                "slot_label": f"Slot {slot_number}",
                "is_selected": slot_number == selected_slot_number,
                "profile": profile,
                "profile_id": profile.id if profile is not None else "",
                "profile_display_name": profile.display_name if profile is not None else "Empty slot",
                "branch": profile.branch if profile is not None else "No profile pinned",
                "runtime_state": snapshot.state if snapshot is not None else "idle",
                "status_summary": (
                    f"Runtime {snapshot.state} | latest: {snapshot.latest_log_line or 'Awaiting output'}"
                    if snapshot is not None
                    else "Pick a managed server from the roster to pin this slot."
                ),
                "activity_summary": (
                    snapshot.last_exit_reason
                    or snapshot.latest_log_line
                    or "Live runtime output will appear here."
                    if snapshot is not None
                    else "No live runtime is attached yet."
                ),
                "latest_log_line": snapshot.latest_log_line if snapshot is not None else "",
                "log_text": "\n".join(recent_logs) if recent_logs else "No output yet.",
                "live_url": f"/api/profiles/{profile.id}/live" if profile is not None else "",
                "can_send_commands": snapshot is not None and snapshot.state == "running",
            }
        )

    slot_lookup = {slot_id: index + 1 for index, slot_id in enumerate(slots) if slot_id}
    profile_picker_items: list[dict[str, object]] = []
    for profile in picker_profiles:
        snapshot = runtime_manager.get_status(profile.id)
        assigned_slot_number = slot_lookup.get(profile.id)
        if assigned_slot_number == selected_slot_number:
            assignment_label = f"Pinned in target slot {selected_slot_number}"
        elif assigned_slot_number is not None:
            assignment_label = f"Move from Slot {assigned_slot_number} to Slot {selected_slot_number}"
        else:
            assignment_label = f"Pin to Slot {selected_slot_number}"

        profile_picker_items.append(
            {
                "profile": profile,
                "id": profile.id,
                "display_name": profile.display_name,
                "branch": profile.branch,
                "runtime_state": snapshot.state,
                "latest_log_line": snapshot.latest_log_line or "No recent log line yet.",
                "assigned_slot_number": assigned_slot_number,
                "assignment_label": assignment_label,
            }
        )

    selected_profile = profiles_by_id.get(slots[selected_slot_number - 1] or "")
    selection_summary = (
        f"Slot {selected_slot_number} is targeting {selected_profile.display_name}."
        if selected_profile is not None
        else f"Slot {selected_slot_number} is empty. Pin a server from the roster."
    )
    visible_console_count = sum(1 for slot in console_slots if slot["profile"] is not None)
    return {
        "console_slots": console_slots,
        "console_picker_items": profile_picker_items,
        "selected_console_slot_number": selected_slot_number,
        "console_selection_summary": selection_summary,
        "console_has_profiles": bool(profiles),
        "console_has_no_profiles": not profiles,
        "visible_console_count": visible_console_count,
        "running_profile_count": running_profile_count,
        "consoles_page_summary": (
            f"Pin up to four live server consoles, swap them from the roster, and keep the active runtime output one workspace click away."
        ),
        "consoles_running_summary": (
            "No servers are currently running."
            if running_profile_count == 0
            else f"{running_profile_count} server{'s' if running_profile_count != 1 else ''} running right now."
        ),
        "consoles_visible_summary": (
            "No console slots are pinned yet."
            if visible_console_count == 0
            else f"{visible_console_count} of {CONSOLE_SLOT_COUNT} console slots in use."
        ),
        "consoles_auto_refresh_summary": "Live sync runs about every 3 seconds using the same runtime feed as the profile workspaces.",
        "consoles_picker_summary": (
            "Choose the target slot, then pin any server from the roster. Running servers stay at the top of the list."
            if profiles
            else "Create or import a server first, then come back here to pin its console."
        ),
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not has_any_users(db):
        return redirect("/setup")

    user = get_current_user(request, db)
    if user is None:
        return redirect("/login")

    return redirect("/dashboard")


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if has_any_users(db):
        return redirect("/login")

    return render(request, "setup.html")


@router.post("/setup")
def setup_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    try:
        auth_limiter.check(f"setup:{request.client.host if request.client else 'unknown'}")
        ensure_password_policy(password)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect("/setup")

    if has_any_users(db):
        flash(request, "warning", "Owner bootstrap is already complete.")
        return redirect("/login")

    user = User(
        username=username.strip(),
        display_name=display_name.strip() or username.strip(),
        password_hash=hash_password(password),
        role=UserRole.owner.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    host_settings = get_host_settings(db, request)
    host_settings.bind_host = request.app.state.settings.bind_host
    host_settings.bind_port = request.app.state.settings.bind_port
    db.commit()

    record_audit(
        db,
        event_type="owner.bootstrapped",
        subject_type="host",
        subject_id="host",
        actor=user,
        message=f"Bootstrapped owner account {user.username}.",
    )
    request.session["user_id"] = user.id
    flash(request, "success", "Owner account created.")
    return redirect("/dashboard")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not has_any_users(db):
        return redirect("/setup")

    return render(request, "login.html")


@router.post("/login")
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    try:
        auth_limiter.check(f"login:{request.client.host if request.client else 'unknown'}")
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect("/login")

    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        flash(request, "error", "Invalid username or password.")
        return redirect("/login")

    if not user.is_active:
        flash(request, "error", "This account is inactive.")
        return redirect("/login")

    user.last_login_at = request.app.state.now()
    db.commit()
    request.session["user_id"] = user.id
    flash(request, "success", f"Welcome back, {user.display_name}.")
    return redirect("/dashboard")


@router.post("/logout")
def logout_submit(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    request.session.clear()
    return redirect("/login")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    return render(request, "dashboard.html", **build_dashboard_summary(request, db, user))


@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    runtime_manager = request.app.state.runtime_manager
    import_candidates = discover_import_candidates(request, profiles)
    return render(
        request,
        "profiles.html",
        profiles=profiles,
        runtime_manager=runtime_manager,
        can_manage=role_allows(user, UserRole.operator),
        import_candidates=import_candidates,
        import_candidate_count=len(import_candidates),
        import_cache_directory=get_import_probe_paths(request)[0],
        import_install_directory=get_import_probe_paths(request)[1],
        import_scan_summary=(
            f"{len(import_candidates)} import candidate(s) loaded from the supplied path."
            if import_candidates
            else "Enter the existing server cache path to load import candidates. Example: /home/pz/Zomboid"
        ),
    )


@router.post("/profiles")
def profiles_create(
    request: Request,
    db: Session = Depends(get_db),
    display_name: str = Form(...),
    server_name: str = Form(...),
    branch: str = Form("stable"),
    preferred_memory_gb: int = Form(4),
    max_players: int = Form(8),
    default_port: int = Form(16261),
    udp_port: int = Form(16262),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile_id = slugify(display_name)
    server_root = request.app.state.settings.servers_root / profile_id
    profile = ServerProfile(
        id=profile_id,
        display_name=display_name.strip(),
        server_name=server_name.strip(),
        install_directory=str(server_root / "install"),
        cache_directory=str(server_root / "cache"),
        branch=branch,
        preferred_memory_gb=preferred_memory_gb,
        max_players=max_players,
        default_port=default_port,
        udp_port=udp_port,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    record_audit(
        db,
        event_type="profile.created",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Created profile {profile.display_name}.",
    )
    flash(request, "success", f"Created profile {profile.display_name}.")
    return redirect(f"/profiles/{profile.id}/overview")


@router.post("/profiles/imports/scan")
def profiles_import_scan(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    next_url: str = Form(""),
    cache_directory: str = Form(""),
    install_directory: str = Form(""),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.viewer)
    if isinstance(user, RedirectResponse):
        return user

    normalized_cache = cache_directory.strip()
    normalized_install = install_directory.strip()
    if normalized_cache:
        request.session[IMPORT_CACHE_SESSION_KEY] = normalized_cache
        if normalized_install:
            request.session[IMPORT_INSTALL_SESSION_KEY] = normalized_install
        else:
            request.session.pop(IMPORT_INSTALL_SESSION_KEY, None)
    elif not request.session.get(IMPORT_CACHE_SESSION_KEY):
        flash(request, "error", "Enter the existing server cache path before loading imports.")
        return redirect(safe_next_url(next_url, "/profiles"))

    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    candidates = discover_import_candidates(request, profiles)
    if candidates:
        flash(request, "success", f"Loaded {len(candidates)} import candidate(s).")
    else:
        flash(request, "warning", "No server .ini files were found under the supplied cache path.")
    return redirect(safe_next_url(next_url, "/profiles"))


@router.post("/profiles/imports/{candidate_id}")
def profiles_import_candidate(
    candidate_id: str,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    next_url: str = Form(""),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    fallback_url = safe_next_url(next_url, "/profiles")

    existing_profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    import_service = build_import_service(request)
    candidate = import_service.get_candidate(candidate_id, existing_profiles)
    if candidate is None:
        flash(request, "error", "The selected import candidate could not be found.")
        return redirect(fallback_url)

    if candidate.is_already_imported and candidate.matching_profile_id is not None:
        flash(request, "warning", f"{candidate.display_name} is already managed.")
        return redirect(f"/profiles/{candidate.matching_profile_id}/overview")

    if not candidate.can_import:
        flash(request, "error", candidate.diagnostics[0] if candidate.diagnostics else "This candidate cannot be imported yet.")
        return redirect(fallback_url)

    try:
        profile = import_service.import_candidate(candidate_id, existing_profiles)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect(fallback_url)

    db.add(profile)
    db.commit()
    db.refresh(profile)
    record_audit(
        db,
        event_type="profile.imported",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Imported existing server footprint {candidate.server_name} as {profile.display_name}.",
    )
    flash(request, "success", f"Imported {profile.display_name}.")
    return redirect(f"/profiles/{profile.id}/overview")


def _profile_workspace(
    request: Request,
    db: Session,
    profile_id: str,
    page_id: str,
) -> HTMLResponse:
    user = current_user_or_redirect(request, db)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    runtime_manager = request.app.state.runtime_manager
    backup_scheduler = request.app.state.backup_scheduler
    config_service = request.app.state.config_service
    sandbox_service = request.app.state.sandbox_service
    config_service.ensure_profile_files(profile)
    runtime_snapshot = runtime_manager.get_status(profile.id)
    backup_files = request.app.state.zomboid_service.list_backups(profile)
    backup_entries = build_backup_entries(backup_files)
    backup_schedule = backup_scheduler.describe_profile(profile)
    launch_plan = request.app.state.zomboid_service.build_launch_plan(profile)
    launch_plan_live = serialize_launch_plan(launch_plan)
    profile_jobs = db.scalars(
        select(OperationJob)
        .where(OperationJob.profile_id == profile.id)
        .order_by(OperationJob.created_at.desc())
        .limit(8)
    ).all()
    profile_job_entries = build_job_entries(profile_jobs, {profile.id: profile.display_name})
    active_job = find_active_job(profile_job_entries)
    runtime_diagnostic = build_runtime_diagnostic(runtime_snapshot, launch_plan_live, profile_job_entries)
    general_settings = config_service.load_general_settings(profile)
    network_settings = config_service.load_network_settings(profile)
    mods_maps_settings = config_service.load_mods_maps(profile)
    named_presets = config_service.list_named_presets(db, profile.id)
    advanced_files = config_service.read_advanced_files(profile)
    sandbox_context: dict[str, Any] = {}
    mods_maps_context: dict[str, Any] = {}

    if page_id == "sandbox":
        sandbox_state = sandbox_ui_state_from_request(request)
        _, sandbox_editor_values, sandbox_has_draft = load_sandbox_editor_values(
            db,
            sandbox_service=sandbox_service,
            profile=profile,
        )
        sandbox_presets = sandbox_service.list_presets()
        sandbox_selected_preset = sandbox_service.resolve_preset(sandbox_presets, sandbox_state["preset_id"])
        sandbox_selected_preset_id = sandbox_selected_preset.id if sandbox_selected_preset is not None else ""
        sandbox_categories = sandbox_service.build_category_views(
            sandbox_editor_values,
            sandbox_selected_preset,
            search_text=sandbox_state["search"],
            active_category_id=sandbox_state["category"],
        )
        sandbox_category_overview = sandbox_service.build_category_views(
            sandbox_editor_values,
            sandbox_selected_preset,
        )
        sandbox_context = {
            "sandbox_editor_values": sandbox_editor_values,
            "sandbox_categories": sandbox_categories,
            "sandbox_category_overview": sandbox_category_overview,
            "sandbox_presets": sandbox_presets,
            "sandbox_selected_preset": sandbox_selected_preset,
            "sandbox_selected_preset_id": sandbox_selected_preset_id,
            "sandbox_search": sandbox_state["search"],
            "sandbox_active_category": sandbox_state["category"],
            "sandbox_has_draft": sandbox_has_draft,
            "sandbox_path": str(sandbox_service.path_for_profile(profile)),
            "sandbox_url_builder": lambda **params: sandbox_url(profile.id, **params),
        }

    if page_id == "mods-maps":
        mods_maps_state = mods_maps_ui_state_from_request(request)
        host_settings = get_host_settings(db, request)
        workshop_browser_result = request.app.state.workshop_browser_service.search(
            profile,
            current_workshop_ids=list(mods_maps_settings["workshop_ids"]),
            current_mod_ids=list(mods_maps_settings["mod_ids"]),
            current_map_ids=list(mods_maps_settings["map_ids"]),
            query=mods_maps_state["search"],
            api_key=host_settings.steam_web_api_key,
        )
        workshop_browser_preview = None
        if mods_maps_state["preview"]:
            workshop_browser_preview = request.app.state.workshop_browser_service.get_preview(
                profile,
                current_workshop_ids=list(mods_maps_settings["workshop_ids"]),
                current_mod_ids=list(mods_maps_settings["mod_ids"]),
                current_map_ids=list(mods_maps_settings["map_ids"]),
                workshop_id=mods_maps_state["preview"],
                api_key=host_settings.steam_web_api_key,
            )
        mods_maps_validation = config_service.build_mods_maps_validation(
            profile,
            list(mods_maps_settings["workshop_ids"]),
            list(mods_maps_settings["mod_ids"]),
            list(mods_maps_settings["map_ids"]),
        )
        mods_maps_context = {
            "mods_maps_query": mods_maps_state["search"],
            "mods_maps_preview_id": mods_maps_state["preview"],
            "workshop_browser_result": workshop_browser_result,
            "workshop_browser_preview": workshop_browser_preview,
            "mods_maps_validation": mods_maps_validation,
            "workshop_browser_has_api_key": bool((host_settings.steam_web_api_key or "").strip()),
            "workshop_browser_can_configure": role_allows(user, UserRole.admin),
        }

    return render(
        request,
        "profile_workspace.html",
        profile=profile,
        active_page=page_id,
        tabs=PROFILE_TABS,
        runtime_snapshot=runtime_snapshot,
        backup_files=backup_files,
        backup_entries=backup_entries,
        backup_schedule=backup_schedule,
        launch_plan=launch_plan,
        launch_plan_live=launch_plan_live,
        runtime_diagnostic=runtime_diagnostic,
        active_job=active_job,
        profile_jobs=profile_job_entries,
        recent_logs=runtime_manager.recent_logs(profile.id, limit=80),
        can_manage=role_allows(user, UserRole.operator),
        general_settings=general_settings,
        network_settings=network_settings,
        mods_maps_settings=mods_maps_settings,
        named_presets=named_presets,
        advanced_files=advanced_files,
        backup_restore_keyword="RESTORE",
        profile_live_enabled=True,
        profile_live_api_url=f"/api/profiles/{profile.id}/live",
        recent_commands=runtime_manager.recent_commands(profile.id, limit=12),
        quick_runtime_commands=QUICK_RUNTIME_COMMANDS,
        branch_options=BRANCH_OPTIONS,
        profile_install_is_managed=request.app.state.zomboid_service.is_managed_install_directory(profile),
        profile_cache_is_managed=request.app.state.zomboid_service.is_managed_cache_directory(profile),
        **sandbox_context,
        **mods_maps_context,
    )


@router.get("/profiles/{profile_id}/overview", response_class=HTMLResponse)
def profile_overview(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "overview")


@router.get("/profiles/{profile_id}/install-update", response_class=HTMLResponse)
def profile_install_update(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "install-update")


@router.post("/profiles/{profile_id}/install-update/settings")
def profile_install_update_settings_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    install_directory: str = Form(""),
    cache_directory: str = Form(""),
    branch: str = Form("stable"),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    normalized_install_directory = install_directory.strip()
    normalized_cache_directory = cache_directory.strip()

    if not normalized_install_directory or not normalized_cache_directory:
        flash(request, "error", "Install and cache directories are both required.")
        return redirect(f"/profiles/{profile.id}/install-update")

    if not Path(normalized_install_directory).is_absolute() or not Path(normalized_cache_directory).is_absolute():
        flash(request, "error", "Install and cache directories must use absolute paths.")
        return redirect(f"/profiles/{profile.id}/install-update")

    if branch not in {item[0] for item in BRANCH_OPTIONS}:
        flash(request, "error", "Choose a valid branch.")
        return redirect(f"/profiles/{profile.id}/install-update")

    profile.install_directory = normalized_install_directory
    profile.cache_directory = normalized_cache_directory
    profile.branch = branch
    db.commit()

    record_audit(
        db,
        event_type="profile.install-update.settings-updated",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Updated install/cache folders and branch for {profile.display_name}.",
    )
    flash(
        request,
        "success",
        f"Updated install/cache folders and branch for {profile.display_name}. Existing files were not moved automatically.",
    )
    return redirect(f"/profiles/{profile.id}/install-update")


@router.post("/profiles/{profile_id}/install-update/uninstall")
def profile_install_update_uninstall_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    confirmation_text: str = Form(""),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    if confirmation_text.strip().upper() != "UNINSTALL":
        flash(request, "error", "Type UNINSTALL to confirm removing the managed server files.")
        return redirect(f"/profiles/{profile.id}/install-update")

    guard_message = retirement_guard_message(request, db, profile)
    if guard_message is not None:
        flash(request, "error", guard_message)
        return redirect(f"/profiles/{profile.id}/install-update")

    try:
        result = request.app.state.zomboid_service.uninstall_server(profile)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect(f"/profiles/{profile.id}/install-update")

    runtime_manager = request.app.state.runtime_manager
    runtime_manager.clear_profile_state(profile.id)
    runtime_manager.append_log(profile.id, result.message)

    record_audit(
        db,
        event_type="profile.install.uninstalled",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=result.message,
    )
    flash(request, "success", result.message)
    return redirect(f"/profiles/{profile.id}/install-update")


@router.post("/profiles/{profile_id}/install-update/delete")
def profile_install_update_delete_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    confirmation_text: str = Form(""),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    if confirmation_text.strip().upper() != "DELETE":
        flash(request, "error", "Type DELETE to confirm removing this profile from the launcher.")
        return redirect(f"/profiles/{profile.id}/install-update")

    guard_message = retirement_guard_message(request, db, profile)
    if guard_message is not None:
        flash(request, "error", guard_message)
        return redirect(f"/profiles/{profile.id}/install-update")

    runtime_manager = request.app.state.runtime_manager
    runtime_manager.clear_profile_state(profile.id)
    result = request.app.state.zomboid_service.delete_profile_files(profile)

    related_jobs = db.scalars(select(OperationJob).where(OperationJob.profile_id == profile.id)).all()
    related_drafts = db.scalars(select(SettingsDraft).where(SettingsDraft.profile_id == profile.id)).all()
    related_presets = db.scalars(select(WorkshopPreset).where(WorkshopPreset.profile_id == profile.id)).all()
    related_audits = db.scalars(select(AuditEntry).where(AuditEntry.subject_id == profile.id)).all()

    db.delete(profile)
    for row in [*related_jobs, *related_drafts, *related_presets, *related_audits]:
        db.delete(row)
    db.commit()

    record_audit(
        db,
        event_type="profile.deleted",
        subject_type="profile",
        subject_id=profile_id,
        actor=user,
        message=result.message,
    )
    flash(request, "success", result.message)
    return redirect("/profiles")


@router.get("/profiles/{profile_id}/general", response_class=HTMLResponse)
def profile_general(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "general")


@router.post("/profiles/{profile_id}/general")
async def profile_general_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    profile.display_name = str(form.get("display_name", "") or "").strip()
    profile.server_name = str(form.get("server_name", "") or "").strip()
    profile.preferred_memory_gb = int(str(form.get("preferred_memory_gb", profile.preferred_memory_gb) or profile.preferred_memory_gb))
    profile.max_players = int(str(form.get("max_players", profile.max_players) or profile.max_players))
    profile.default_port = int(str(form.get("default_port", profile.default_port) or profile.default_port))
    profile.udp_port = int(str(form.get("udp_port", profile.udp_port) or profile.udp_port))
    profile.start_with_host = parse_checkbox(form.get("start_with_host"))
    profile.auto_restart_on_crash = parse_checkbox(form.get("auto_restart_on_crash"))
    request.app.state.config_service.save_general_settings(
        profile,
        {
            "public_name": str(form.get("public_name", "") or ""),
            "public_description": str(form.get("public_description", "") or ""),
            "public": parse_checkbox(form.get("public")),
            "open": parse_checkbox(form.get("open_signup")),
            "pvp": parse_checkbox(form.get("pvp")),
            "pause_empty": parse_checkbox(form.get("pause_empty")),
            "global_chat": parse_checkbox(form.get("global_chat")),
            "server_welcome_message": str(form.get("server_welcome_message", "") or ""),
            "spawn_items": str(form.get("spawn_items", "") or ""),
            "loot_respawn_hours": str(form.get("loot_respawn_hours", "") or ""),
            "loot_respawn_max_items": str(form.get("loot_respawn_max_items", "") or ""),
            "construction_prevents_loot_respawn": parse_checkbox(form.get("construction_prevents_loot_respawn")),
            "sleep_allowed": parse_checkbox(form.get("sleep_allowed")),
            "sleep_needed": parse_checkbox(form.get("sleep_needed")),
            "no_fire": parse_checkbox(form.get("no_fire")),
            "announce_death": parse_checkbox(form.get("announce_death")),
            "drop_whitelist_on_death": parse_checkbox(form.get("drop_whitelist_on_death")),
            "allow_sledgehammer_destruction": parse_checkbox(form.get("allow_sledgehammer_destruction")),
            "respawn_with_self": parse_checkbox(form.get("respawn_with_self")),
            "respawn_with_other": parse_checkbox(form.get("respawn_with_other")),
            "world_item_removal_hours": str(form.get("world_item_removal_hours", "") or ""),
            "world_item_removal_list": str(form.get("world_item_removal_list", "") or ""),
            "player_safehouse": parse_checkbox(form.get("player_safehouse")),
            "admin_safehouse": parse_checkbox(form.get("admin_safehouse")),
            "safehouse_allow_trespass": parse_checkbox(form.get("safehouse_allow_trespass")),
            "safehouse_allow_fire": parse_checkbox(form.get("safehouse_allow_fire")),
            "safehouse_allow_loot": parse_checkbox(form.get("safehouse_allow_loot")),
            "safehouse_allow_respawn": parse_checkbox(form.get("safehouse_allow_respawn")),
            "safehouse_allow_non_residential": parse_checkbox(form.get("safehouse_allow_non_residential")),
            "disable_safehouse_when_player_connected": parse_checkbox(form.get("disable_safehouse_when_player_connected")),
            "disable_safehouse_when_player_disconnected": parse_checkbox(form.get("disable_safehouse_when_player_disconnected")),
            "safehouse_days_to_claim": str(form.get("safehouse_days_to_claim", "") or ""),
            "safehouse_removal_hours": str(form.get("safehouse_removal_hours", "") or ""),
            "faction_enabled": parse_checkbox(form.get("faction_enabled")),
            "faction_days_to_create": str(form.get("faction_days_to_create", "") or ""),
            "faction_players_for_tag": str(form.get("faction_players_for_tag", "") or ""),
            "allow_trade_ui": parse_checkbox(form.get("allow_trade_ui")),
            "default_port": str(form.get("default_port", "") or ""),
            "udp_port": str(form.get("udp_port", "") or ""),
            "rcon_port": str(form.get("rcon_port", "") or ""),
            "preferred_memory_gb": str(form.get("preferred_memory_gb", "") or ""),
            "start_with_host": parse_checkbox(form.get("start_with_host")),
            "auto_restart_on_crash": parse_checkbox(form.get("auto_restart_on_crash")),
        },
    )
    db.commit()

    record_audit(
        db,
        event_type="profile.updated",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Updated general settings for {profile.display_name}.",
    )
    flash(request, "success", f"Saved general settings for {profile.display_name}.")
    return redirect(f"/profiles/{profile.id}/general")


@router.get("/profiles/{profile_id}/sandbox", response_class=HTMLResponse)
def profile_sandbox(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "sandbox")


@router.post("/profiles/{profile_id}/sandbox")
async def profile_sandbox_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    sandbox_service = request.app.state.sandbox_service
    _, editor_values, _ = load_sandbox_editor_values(db, sandbox_service=sandbox_service, profile=profile)
    normalized_values = normalize_sandbox_form_values(form, sandbox_service=sandbox_service, current_values=editor_values)
    path = sandbox_service.save_editor_values(profile, normalized_values)
    delete_sandbox_draft(db, profile_id=profile.id)
    record_audit(
        db,
        event_type="profile.sandbox.saved",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Saved structured sandbox settings at {path}.",
    )
    flash(request, "success", "Saved structured sandbox settings and cleared the sandbox draft.")
    return sandbox_redirect(profile.id, **sandbox_ui_state_from_form(form))


@router.post("/profiles/{profile_id}/sandbox/draft")
async def profile_sandbox_draft_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    sandbox_service = request.app.state.sandbox_service
    action = str(form.get("action", "save_draft") or "save_draft").strip()

    if action == "discard_draft":
        deleted = delete_sandbox_draft(db, profile_id=profile.id)
        if deleted:
            record_audit(
                db,
                event_type="profile.sandbox.draft.discarded",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Discarded sandbox draft for {profile.display_name}.",
            )
            flash(request, "success", "Discarded the sandbox draft.")
        else:
            flash(request, "warning", "There was no sandbox draft to discard.")
        return sandbox_redirect(profile.id, **sandbox_ui_state_from_form(form))

    _, editor_values, _ = load_sandbox_editor_values(db, sandbox_service=sandbox_service, profile=profile)
    normalized_values = normalize_sandbox_form_values(form, sandbox_service=sandbox_service, current_values=editor_values)
    save_sandbox_draft(db, profile_id=profile.id, values=normalized_values)
    record_audit(
        db,
        event_type="profile.sandbox.draft.saved",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Saved sandbox draft for {profile.display_name}.",
    )
    flash(request, "success", "Saved the sandbox draft without touching the live file.")
    return sandbox_redirect(profile.id, **sandbox_ui_state_from_form(form))


@router.post("/profiles/{profile_id}/sandbox/presets")
async def profile_sandbox_presets_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    sandbox_service = request.app.state.sandbox_service
    raw_action = str(form.get("action", "") or "").strip()
    action, _, action_argument = raw_action.partition(":")
    state = sandbox_ui_state_from_form(form)
    redirect_preset_id = state["preset_id"]
    _, editor_values, _ = load_sandbox_editor_values(db, sandbox_service=sandbox_service, profile=profile)
    normalized_values = normalize_sandbox_form_values(form, sandbox_service=sandbox_service, current_values=editor_values)
    presets = sandbox_service.list_presets()
    selected_preset = sandbox_service.resolve_preset(
        presets,
        str(form.get("preset_id", "") or redirect_preset_id).strip() or redirect_preset_id,
    )
    redirect_preset_id = selected_preset.id if selected_preset is not None else redirect_preset_id

    try:
        if action == "apply_preset":
            if selected_preset is None:
                flash(request, "error", "Select a sandbox preset first.")
                return sandbox_redirect(profile.id, **state)

            preset_values = sandbox_service.apply_preset_values(normalized_values, selected_preset)
            save_sandbox_draft(db, profile_id=profile.id, values=preset_values)
            record_audit(
                db,
                event_type="profile.sandbox.preset.applied",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Applied sandbox preset '{selected_preset.name}' into the draft.",
            )
            flash(request, "success", f"Applied preset '{selected_preset.name}' into the sandbox draft.")
        elif action == "reset_category":
            if selected_preset is None:
                flash(request, "error", "Select a sandbox preset first.")
                return sandbox_redirect(profile.id, **state)

            category_id = str(action_argument or form.get("target_category_id", "") or state["category"]).strip()
            if not category_id:
                flash(request, "error", "Pick a sandbox category to reset.")
                return sandbox_redirect(profile.id, preset_id=redirect_preset_id, search=state["search"], category=state["category"])

            preset_values = sandbox_service.reset_category_to_preset(normalized_values, selected_preset, category_id)
            save_sandbox_draft(db, profile_id=profile.id, values=preset_values)
            record_audit(
                db,
                event_type="profile.sandbox.preset.category-reset",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Reset sandbox category '{category_id}' to preset '{selected_preset.name}'.",
            )
            flash(request, "success", f"Reset the '{category_id}' sandbox category to '{selected_preset.name}'.")
        elif action == "reset_all":
            if selected_preset is None:
                flash(request, "error", "Select a sandbox preset first.")
                return sandbox_redirect(profile.id, **state)

            preset_values = sandbox_service.apply_preset_values(normalized_values, selected_preset)
            save_sandbox_draft(db, profile_id=profile.id, values=preset_values)
            record_audit(
                db,
                event_type="profile.sandbox.preset.reset-all",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Reset all sandbox fields to preset '{selected_preset.name}'.",
            )
            flash(request, "success", f"Reset all sandbox fields to '{selected_preset.name}'.")
        elif action == "save_custom_preset":
            preset_name = str(form.get("custom_preset_name", "") or "").strip()
            saved_preset = sandbox_service.save_custom_preset(preset_name, normalized_values)
            redirect_preset_id = saved_preset.id
            record_audit(
                db,
                event_type="profile.sandbox.preset.custom-saved",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Saved custom sandbox preset '{saved_preset.name}'.",
            )
            flash(request, "success", f"Saved custom sandbox preset '{saved_preset.name}'.")
        elif action == "delete_custom_preset":
            preset_id = str(form.get("preset_id", "") or redirect_preset_id).strip()
            sandbox_service.delete_custom_preset(preset_id)
            record_audit(
                db,
                event_type="profile.sandbox.preset.custom-deleted",
                subject_type="profile",
                subject_id=profile.id,
                actor=user,
                message=f"Deleted custom sandbox preset '{preset_id}'.",
            )
            flash(request, "success", "Deleted the custom sandbox preset.")
            redirect_preset_id = ""
        else:
            flash(request, "error", "Unknown sandbox preset action.")
    except ValueError as exc:
        flash(request, "error", str(exc))

    return sandbox_redirect(
        profile.id,
        preset_id=redirect_preset_id,
        search=state["search"],
        category=state["category"],
    )


@router.post("/profiles/{profile_id}/sandbox/reset-world")
async def profile_sandbox_reset_world_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    runtime_manager = request.app.state.runtime_manager
    create_backup_before_reset = parse_checkbox(form.get("create_backup_before_reset"))
    restart_after_reset = parse_checkbox(form.get("restart_after_reset"))
    snapshot = runtime_manager.get_status(profile.id)
    was_running = snapshot.state in {"starting", "running", "stopping"}

    if was_running:
        await runtime_manager.stop_profile(profile.id)
        runtime_manager.append_log(profile.id, "Stopped the managed server before resetting the world.")

    result = request.app.state.zomboid_service.reset_world(
        profile,
        create_backup_before_reset=create_backup_before_reset,
    )
    runtime_manager.append_log(profile.id, f"Reset world data under {result.world_directory}.")
    if result.backup_path is not None:
        runtime_manager.append_log(profile.id, f"Created pre-reset backup {result.backup_path.name}.")
    if result.ini_updated:
        runtime_manager.append_log(
            profile.id,
            f"Updated ResetID to {result.reset_id} and generated a new Seed ({result.seed}).",
        )

    restart_summary = ""
    if restart_after_reset:
        restarted_snapshot = await runtime_manager.start_profile(profile)
        restart_summary = f" Runtime state is now {restarted_snapshot.state}."

    record_audit(
        db,
        event_type="profile.sandbox.world-reset",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Reset world data for {profile.display_name}.",
    )
    flash(
        request,
        "success",
        "Reset the sandbox world data."
        f"{' Created a backup first.' if result.backup_path is not None else ''}"
        f"{restart_summary}",
    )
    return sandbox_redirect(profile.id, **sandbox_ui_state_from_form(form))


@router.get("/profiles/{profile_id}/mods-maps", response_class=HTMLResponse)
def profile_mods_maps(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "mods-maps")


@router.post("/profiles/{profile_id}/mods-maps")
def profile_mods_maps_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),
    workshop_ids_text: str = Form(""),
    mod_ids_text: str = Form(""),
    map_ids_text: str = Form(""),
    preset_name: str = Form(""),
    preset_id: str = Form(""),
    steam_web_api_key: str = Form(""),
    browser_query: str = Form(""),
    preview_workshop_id: str = Form(""),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    config_service = request.app.state.config_service

    workshop_ids = parse_textarea_list(workshop_ids_text)
    mod_ids = parse_textarea_list(mod_ids_text)
    map_ids = parse_textarea_list(map_ids_text)
    ui_state = {
        "search": browser_query.strip(),
        "preview": preview_workshop_id.strip(),
    }

    if action == "save_workshop_api_key":
        if not role_allows(user, UserRole.admin):
            flash(request, "error", "Only an admin can manage the Steam Workshop API key.")
            return mods_maps_redirect(profile.id, **ui_state)

        host_settings = get_host_settings(db, request)
        submitted_key = steam_web_api_key
        if submitted_key.strip() == "":
            flash(request, "error", "Enter a Steam Web API key before saving it.")
            return mods_maps_redirect(profile.id, **ui_state)

        host_settings.steam_web_api_key = submitted_key.strip()
        db.commit()
        record_audit(
            db,
            event_type="host.workshop-api-key.saved",
            subject_type="host",
            subject_id="host",
            actor=user,
            message="Saved the Steam Workshop Web API key.",
        )
        flash(request, "success", "Saved the Steam Workshop Web API key.")
        return mods_maps_redirect(profile.id, **ui_state)

    if action == "clear_workshop_api_key":
        if not role_allows(user, UserRole.admin):
            flash(request, "error", "Only an admin can manage the Steam Workshop API key.")
            return mods_maps_redirect(profile.id, **ui_state)

        host_settings = get_host_settings(db, request)
        host_settings.steam_web_api_key = None
        db.commit()
        record_audit(
            db,
            event_type="host.workshop-api-key.cleared",
            subject_type="host",
            subject_id="host",
            actor=user,
            message="Cleared the Steam Workshop Web API key.",
        )
        flash(request, "success", "Cleared the Steam Workshop Web API key.")
        return mods_maps_redirect(profile.id, **ui_state)

    resolved_workshop_ids = config_service.resolve_workshop_ids_from_content(profile, workshop_ids, mod_ids, map_ids)
    auto_resolved_count = len([item for item in resolved_workshop_ids if item.lower() not in {value.lower() for value in workshop_ids}])

    if action == "save_live":
        config_service.save_mods_maps(profile, resolved_workshop_ids, mod_ids, map_ids)
        record_audit(
            db,
            event_type="profile.mods-maps.saved",
            subject_type="profile",
            subject_id=profile.id,
            actor=user,
            message=f"Saved live mods/maps preset for {profile.display_name}.",
        )
        flash(
            request,
            "success",
            "Saved the live workshop, mod, and map lists."
            f"{f' Auto-mapped {auto_resolved_count} workshop ID(s) from local content.' if auto_resolved_count else ''}",
        )
    elif action == "save_preset":
        if not preset_name.strip():
            flash(request, "error", "Preset name is required.")
            return mods_maps_redirect(profile.id, **ui_state)

        config_service.save_named_preset(
            db,
            profile_id=profile.id,
            name=preset_name,
            workshop_ids=resolved_workshop_ids,
            mod_ids=mod_ids,
            map_ids=map_ids,
        )
        record_audit(
            db,
            event_type="profile.mods-maps.preset-saved",
            subject_type="profile",
            subject_id=profile.id,
            actor=user,
            message=f"Saved named workshop preset '{preset_name.strip()}'.",
        )
        flash(
            request,
            "success",
            f"Saved preset '{preset_name.strip()}'."
            f"{f' Auto-mapped {auto_resolved_count} workshop ID(s) from local content.' if auto_resolved_count else ''}",
        )
    elif action == "apply_preset":
        preset = request.app.state.config_service.get_named_preset(db, profile_id=profile.id, preset_id=preset_id)
        if preset is None:
            flash(request, "error", "Preset not found.")
            return mods_maps_redirect(profile.id, **ui_state)

        resolved_preset_workshop_ids = config_service.resolve_workshop_ids_from_content(
            profile,
            preset.workshop_ids,
            preset.mod_ids,
            preset.map_ids,
        )
        preset_auto_resolved_count = len(
            [item for item in resolved_preset_workshop_ids if item.lower() not in {value.lower() for value in preset.workshop_ids}]
        )
        config_service.save_mods_maps(profile, resolved_preset_workshop_ids, preset.mod_ids, preset.map_ids)
        record_audit(
            db,
            event_type="profile.mods-maps.preset-applied",
            subject_type="profile",
            subject_id=profile.id,
            actor=user,
            message=f"Applied preset '{preset.name}' to the live config.",
        )
        flash(
            request,
            "success",
            f"Applied preset '{preset.name}' to the live config."
            f"{f' Auto-mapped {preset_auto_resolved_count} workshop ID(s) from local content.' if preset_auto_resolved_count else ''}",
        )
    elif action == "delete_preset":
        deleted_name = request.app.state.config_service.delete_named_preset(db, profile_id=profile.id, preset_id=preset_id)
        if deleted_name is None:
            flash(request, "error", "Preset not found.")
            return mods_maps_redirect(profile.id, **ui_state)

        record_audit(
            db,
            event_type="profile.mods-maps.preset-deleted",
            subject_type="profile",
            subject_id=profile.id,
            actor=user,
            message=f"Deleted preset '{deleted_name}'.",
        )
        flash(request, "success", f"Deleted preset '{deleted_name}'.")
    elif action == "scan_live":
        scan_result = request.app.state.config_service.scan_installed_workshop(profile)
        config_service.save_mods_maps(profile, scan_result.workshop_ids, scan_result.mod_ids, scan_result.map_ids)
        summary = f"Imported {len(scan_result.workshop_ids)} workshop IDs, {len(scan_result.mod_ids)} mods, and {len(scan_result.map_ids)} maps from installed workshop content."
        if scan_result.diagnostics:
            summary = f"{summary} {' '.join(scan_result.diagnostics)}"
        record_audit(
            db,
            event_type="profile.mods-maps.scanned",
            subject_type="profile",
            subject_id=profile.id,
            actor=user,
            message=summary,
        )
        flash(request, "success", summary)
    else:
        flash(request, "error", "Unknown Mods & Maps action.")

    return mods_maps_redirect(profile.id, **ui_state)


@router.get("/profiles/{profile_id}/network-admin", response_class=HTMLResponse)
def profile_network_admin(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "network-admin")


@router.post("/profiles/{profile_id}/network-admin")
async def profile_network_admin_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    request.app.state.config_service.save_network_settings(
        profile,
        {
            "bind_ip": str(form.get("bind_ip", "") or ""),
            "steam_mode": parse_checkbox(form.get("steam_mode")),
            "rcon_port": str(form.get("rcon_port", "") or ""),
            "server_password": str(form.get("server_password", "") or ""),
            "rcon_password": str(form.get("rcon_password", "") or ""),
            "admin_username": str(form.get("admin_username", "") or ""),
            "admin_password": str(form.get("admin_password", "") or ""),
            "server_tag": str(form.get("server_tag", "") or ""),
            "reset_id": str(form.get("reset_id", "") or ""),
            "upnp": parse_checkbox(form.get("upnp")),
            "auto_create_user_in_whitelist": parse_checkbox(form.get("auto_create_user_in_whitelist")),
            "do_lua_checksum": parse_checkbox(form.get("do_lua_checksum")),
            "ping_limit": str(form.get("ping_limit", "") or ""),
            "steam_vac": parse_checkbox(form.get("steam_vac")),
            "kick_fast_players": parse_checkbox(form.get("kick_fast_players")),
            "deny_login_overloaded": parse_checkbox(form.get("deny_login_overloaded")),
            "client_command_filter": str(form.get("client_command_filter", "") or ""),
            "save_world_every_minutes": str(form.get("save_world_every_minutes", "") or ""),
            "display_user_name": parse_checkbox(form.get("display_user_name")),
            "show_first_and_last_name": parse_checkbox(form.get("show_first_and_last_name")),
            "safety_system": parse_checkbox(form.get("safety_system")),
            "show_safety": parse_checkbox(form.get("show_safety")),
            "safety_toggle_timer": str(form.get("safety_toggle_timer", "") or ""),
            "safety_cooldown_timer": str(form.get("safety_cooldown_timer", "") or ""),
            "max_accounts_per_user": str(form.get("max_accounts_per_user", "") or ""),
            "allow_non_ascii_username": parse_checkbox(form.get("allow_non_ascii_username")),
            "player_save_on_damage": parse_checkbox(form.get("player_save_on_damage")),
            "mouse_over_display_name": parse_checkbox(form.get("mouse_over_display_name")),
            "hide_players_behind_you": parse_checkbox(form.get("hide_players_behind_you")),
            "player_bump_player": parse_checkbox(form.get("player_bump_player")),
            "map_remote_player_visibility": str(form.get("map_remote_player_visibility", "") or ""),
            "use_tcp_for_map_traffic": parse_checkbox(form.get("use_tcp_for_map_traffic")),
            "voice_enable": parse_checkbox(form.get("voice_enable")),
            "voice_3d": parse_checkbox(form.get("voice_3d")),
            "voice_min_distance": str(form.get("voice_min_distance", "") or ""),
            "voice_max_distance": str(form.get("voice_max_distance", "") or ""),
            "minutes_per_page": str(form.get("minutes_per_page", "") or ""),
        },
    )
    db.commit()

    record_audit(
        db,
        event_type="profile.network-admin.saved",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Updated network/admin settings for {profile.display_name}.",
    )
    flash(request, "success", "Saved network and admin settings.")
    return redirect(f"/profiles/{profile.id}/network-admin")


@router.get("/profiles/{profile_id}/backups", response_class=HTMLResponse)
def profile_backups(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "backups")


@router.post("/profiles/{profile_id}/backups")
def profile_backup_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    backup_enabled: str | None = Form(None),
    backup_interval_hours: int = Form(...),
    backup_retention_count: int = Form(...),
    create_backup_now: str | None = Form(None),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    profile.backup_enabled = backup_enabled == "on"
    profile.backup_interval_hours = backup_interval_hours
    profile.backup_retention_count = backup_retention_count
    db.commit()

    if create_backup_now == "yes":
        backup_path = request.app.state.zomboid_service.create_backup(profile)
        request.app.state.runtime_manager.append_log(profile.id, f"Created backup {backup_path.name}.")
        flash(request, "success", f"Created backup {backup_path.name}.")
    else:
        flash(request, "success", "Saved backup policy.")

    record_audit(
        db,
        event_type="profile.backup-policy.updated",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Updated backup policy for {profile.display_name}.",
    )
    return redirect(f"/profiles/{profile.id}/backups")


@router.post("/profiles/{profile_id}/backups/restore")
async def profile_backup_restore_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    validate_csrf(request, str(form.get("csrf_token", "")))
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    confirmation_text = str(form.get("confirmation_text", "") or "").strip().upper()
    if confirmation_text != "RESTORE":
        flash(request, "error", "Type RESTORE to confirm restoring a backup.")
        return redirect(f"/profiles/{profile.id}/backups")

    runtime_manager = request.app.state.runtime_manager
    stop_before_restore = parse_checkbox(form.get("stop_before_restore"))
    restart_after_restore = parse_checkbox(form.get("restart_after_restore"))
    create_backup_before_restore = parse_checkbox(form.get("create_backup_before_restore"))
    snapshot = runtime_manager.get_status(profile.id)
    was_running = snapshot.state in {"starting", "running", "stopping"}

    if was_running and not stop_before_restore:
        flash(request, "error", "The managed server is running. Stop it first or enable 'Stop the server before restore'.")
        return redirect(f"/profiles/{profile.id}/backups")

    if was_running:
        await runtime_manager.stop_profile(profile.id)
        runtime_manager.append_log(profile.id, "Stopped the managed server before restoring a backup.")

    try:
        result = request.app.state.zomboid_service.restore_backup(
            profile,
            backup_name=str(form.get("backup_name", "") or ""),
            create_backup_before_restore=create_backup_before_restore,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect(f"/profiles/{profile.id}/backups")

    restored_parts: list[str] = []
    if result.install_restored:
        restored_parts.append("install")
    if result.cache_restored:
        restored_parts.append("cache")

    runtime_manager.append_log(profile.id, f"Restored backup {result.source_backup_path.name}.")
    if result.safety_backup_path is not None:
        runtime_manager.append_log(profile.id, f"Created safety backup {result.safety_backup_path.name} before restore.")

    restart_summary = ""
    if restart_after_restore:
        restarted_snapshot = await runtime_manager.start_profile(profile)
        restart_summary = f" Runtime state is now {restarted_snapshot.state}."

    record_audit(
        db,
        event_type="profile.backup.restore",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Restored backup {result.source_backup_path.name} for {profile.display_name}.",
    )
    flash(
        request,
        "success",
        f"Restored {', '.join(restored_parts) if restored_parts else 'managed data'} from {result.source_backup_path.name}."
        f"{' Created a safety backup first.' if result.safety_backup_path is not None else ''}"
        f"{restart_summary}",
    )
    return redirect(f"/profiles/{profile.id}/backups")


@router.get("/profiles/{profile_id}/logs", response_class=HTMLResponse)
def profile_logs(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "logs")


@router.get("/profiles/{profile_id}/advanced-files", response_class=HTMLResponse)
def profile_advanced_files(profile_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _profile_workspace(request, db, profile_id, "advanced-files")


@router.post("/profiles/{profile_id}/advanced-files")
def profile_advanced_files_submit(
    profile_id: str,
    request: Request,
    db: Session = Depends(get_db),
    file_kind: str = Form(...),
    content: str = Form(""),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    try:
        path = request.app.state.config_service.save_advanced_file(profile, file_kind=file_kind, content=content)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return redirect(f"/profiles/{profile.id}/advanced-files")

    if file_kind == "ini":
        request.app.state.config_service.sync_profile_metadata_from_ini(profile)
        db.commit()

    record_audit(
        db,
        event_type="profile.advanced-file.saved",
        subject_type="profile",
        subject_id=profile.id,
        actor=user,
        message=f"Saved raw {file_kind} file at {path}.",
    )
    flash(request, "success", f"Saved raw {file_kind} file.")
    return redirect(f"/profiles/{profile.id}/advanced-files")


@router.get("/consoles", response_class=HTMLResponse)
def consoles_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db, UserRole.viewer)
    if isinstance(user, RedirectResponse):
        return user

    return render(
        request,
        "consoles.html",
        quick_runtime_commands=QUICK_RUNTIME_COMMANDS,
        can_manage_console=role_allows(user, UserRole.operator),
        **build_consoles_summary(request, db),
    )


@router.post("/consoles/slots/select")
def consoles_select_slot_submit(
    request: Request,
    db: Session = Depends(get_db),
    slot_number: int = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.viewer)
    if isinstance(user, RedirectResponse):
        return user

    save_selected_console_slot(request, slot_number)
    return redirect("/consoles")


@router.post("/consoles/slots/assign")
def consoles_assign_slot_submit(
    request: Request,
    db: Session = Depends(get_db),
    profile_id: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    slots = get_console_slots(request)
    selected_slot = get_selected_console_slot(request)
    normalized_slots = [slot if slot else None for slot in slots]
    for index, slot_profile_id in enumerate(normalized_slots):
        if slot_profile_id == profile.id:
            normalized_slots[index] = None
    normalized_slots[selected_slot - 1] = profile.id
    save_console_slots(request, normalized_slots)
    flash(request, "success", f"Pinned {profile.display_name} to Slot {selected_slot}.")
    return redirect("/consoles")


@router.post("/consoles/slots/clear")
def consoles_clear_slot_submit(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    slots = get_console_slots(request)
    selected_slot = get_selected_console_slot(request)
    profile_id = slots[selected_slot - 1]
    slots[selected_slot - 1] = None
    save_console_slots(request, slots)
    if profile_id:
        flash(request, "success", f"Cleared Slot {selected_slot}.")
    else:
        flash(request, "warning", f"Slot {selected_slot} was already empty.")
    return redirect("/consoles")


@router.post("/profiles/{profile_id}/runtime/{action}")
async def runtime_action(
    profile_id: str,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    command_text: str = Form(""),
    next_url: str = Form(""),
    create_backup_before_update: str | None = Form(None),
    stop_server_before_update: str | None = Form(None),
    restart_after_completion: str | None = Form(None),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.operator)
    if isinstance(user, RedirectResponse):
        return user

    profile = get_profile_or_404(db, profile_id)
    runtime_manager = request.app.state.runtime_manager
    redirect_url = safe_next_url(next_url, f"/profiles/{profile.id}/overview")

    try:
        if action == "install":
            await runtime_manager.queue_install(
                profile,
                user.id,
                InstallJobOptions(
                    update=False,
                    restart_after_completion=parse_checkbox(restart_after_completion),
                ),
            )
            flash(request, "success", "Install job queued.")
        elif action == "update":
            install_options = InstallJobOptions(
                update=True,
                create_backup_before_update=parse_checkbox(create_backup_before_update),
                stop_server_before_update=parse_checkbox(stop_server_before_update),
                restart_after_completion=parse_checkbox(restart_after_completion),
            )
            await runtime_manager.queue_install(
                profile,
                user.id,
                install_options,
            )
            flash(
                request,
                "success",
                "Safe update job queued."
                if install_options.create_backup_before_update or install_options.stop_server_before_update
                else "Update job queued.",
            )
        elif action == "start":
            snapshot = await runtime_manager.start_profile(profile)
            flash(request, "success", f"Runtime state: {snapshot.state}.")
        elif action == "stop":
            snapshot = await runtime_manager.stop_profile(profile.id)
            flash(request, "success", f"Runtime state: {snapshot.state}.")
        elif action == "restart":
            snapshot = await runtime_manager.restart_profile(profile)
            flash(request, "success", f"Runtime state: {snapshot.state}.")
        elif action == "command":
            await runtime_manager.send_command(profile.id, command_text.strip())
            flash(request, "success", f"Sent command: {command_text.strip()}.")
        else:
            flash(request, "error", "Unknown runtime action.")
    except ValueError as exc:
        flash(request, "error", str(exc))

    return redirect(redirect_url)


@router.get("/host", response_class=HTMLResponse)
def host_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    return render(request, "host.html", **build_host_summary(request, db))


@router.post("/host")
async def host_submit(
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form("save"),
    public_base_url: str = Form(""),
    access_mode: str = Form("ip"),
    tls_mode: str = Form("proxy-http-ip"),
    reverse_proxy: str = Form("nginx"),
    server_user: str = Form("pzlauncher"),
    confirm_stop_all: str = Form(""),
    stop_servers_first: str | None = Form(None),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    runtime_manager = request.app.state.runtime_manager

    if action == "run-startup-roster":
        startup_profiles = db.scalars(
            select(ServerProfile)
            .where(ServerProfile.start_with_host.is_(True))
            .order_by(ServerProfile.display_name)
        ).all()
        if not startup_profiles:
            flash(request, "warning", "No profiles are marked to start with the host yet.")
            return redirect("/host")

        started_count = 0
        already_active_count = 0
        blocked_count = 0
        for profile in startup_profiles:
            if runtime_manager.is_profile_active(profile.id):
                already_active_count += 1
                continue

            try:
                snapshot = await runtime_manager.start_profile(profile)
            except ValueError:
                blocked_count += 1
                continue

            if snapshot.state == "running":
                started_count += 1
            elif snapshot.state == "blocked":
                blocked_count += 1

        detail_parts = []
        if started_count:
            detail_parts.append(f"started {started_count}")
        if already_active_count:
            detail_parts.append(f"left {already_active_count} already active")
        if blocked_count:
            detail_parts.append(f"{blocked_count} blocked")

        detail = ", ".join(detail_parts) if detail_parts else "no runtime changes"
        record_audit(
            db,
            event_type="host.startup-roster.run",
            subject_type="host",
            subject_id="host",
            actor=user,
            message=f"Ran the startup roster and {detail}.",
        )
        flash(request, "success", f"Startup roster run complete: {detail}.")
        return redirect("/host")

    if action == "stop-all":
        profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
        active_profiles = [profile for profile in profiles if runtime_manager.is_profile_active(profile.id)]
        if not active_profiles:
            flash(request, "warning", "No managed servers are running right now.")
            return redirect("/host")

        if confirm_stop_all.strip().upper() != "STOP ALL":
            flash(request, "error", 'Type "STOP ALL" to confirm a coordinated fleet shutdown.')
            return redirect("/host")

        snapshots = await runtime_manager.stop_profiles([profile.id for profile in active_profiles])
        record_audit(
            db,
            event_type="host.stop-all",
            subject_type="host",
            subject_id="host",
            actor=user,
            message=f"Stopped {len(snapshots)} managed profile(s) from the host page.",
        )
        flash(request, "success", f"Stopped {len(snapshots)} managed profile(s).")
        return redirect("/host")

    if action == "stage-runtime-stop":
        profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
        active_profiles = [profile for profile in profiles if runtime_manager.is_profile_active(profile.id)]
        stopped_profile_count = 0
        if parse_checkbox(stop_servers_first):
            await runtime_manager.stop_profiles([profile.id for profile in active_profiles])
            stopped_profile_count = len(active_profiles)

        requested_at = request.app.state.now()
        request.app.state.host_shutdown_request = {
            "requested_at_label": requested_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "requested_by": user.display_name or user.username,
            "stop_servers_first": parse_checkbox(stop_servers_first),
            "stopped_profile_count": stopped_profile_count,
            "command": "sudo systemctl stop pzserverlauncher",
            "restart_command": "sudo systemctl restart pzserverlauncher",
        }
        record_audit(
            db,
            event_type="host.runtime-stop.staged",
            subject_type="host",
            subject_id="host",
            actor=user,
            message=(
                "Staged a coordinated runtime stop request."
                if stopped_profile_count == 0
                else f"Stopped {stopped_profile_count} managed profile(s) and staged a coordinated runtime stop request."
            ),
        )
        flash(request, "warning", "Runtime stop staged. Run the systemd stop command on the host shell when you are ready.")
        return redirect("/host")

    if action == "clear-runtime-stop-request":
        request.app.state.host_shutdown_request = None
        record_audit(
            db,
            event_type="host.runtime-stop.cleared",
            subject_type="host",
            subject_id="host",
            actor=user,
            message="Cleared the staged runtime stop request.",
        )
        flash(request, "success", "Cleared the staged runtime stop request.")
        return redirect("/host")

    if access_mode not in {"domain", "ip", "private"}:
        flash(request, "error", "Choose a valid access mode.")
        return redirect("/host")
    if tls_mode not in {"proxy-letsencrypt", "proxy-http-ip", "proxy-selfsigned-ip"}:
        flash(request, "error", "Choose a valid TLS mode.")
        return redirect("/host")
    if reverse_proxy not in {"nginx", "caddy"}:
        flash(request, "error", "Choose a supported reverse proxy.")
        return redirect("/host")

    host_settings = get_host_settings(db, request)
    host_settings.public_base_url = public_base_url.strip() or None
    host_settings.access_mode = access_mode
    host_settings.tls_mode = tls_mode
    host_settings.reverse_proxy = reverse_proxy
    host_settings.server_user = server_user.strip() or request.app.state.settings.default_server_user
    db.commit()

    record_audit(
        db,
        event_type="host.updated",
        subject_type="host",
        subject_id="host",
        actor=user,
        message="Updated host and reverse proxy settings.",
    )
    flash(request, "success", "Saved host settings.")
    return redirect("/host")


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    users = db.scalars(select(User).order_by(User.username)).all()
    return render(
        request,
        "users.html",
        users=users,
        user_role_options=USER_ROLE_OPTIONS,
        active_owner_count=count_active_owners(db),
    )


@router.post("/users")
def users_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    normalized_username = username.strip()
    normalized_display_name = display_name.strip()
    normalized_role = role.strip()

    if normalized_role not in {value for value, _ in USER_ROLE_OPTIONS}:
        flash(request, "error", "Choose a valid role.")
        return redirect("/users")

    if normalized_role == UserRole.owner.value and user.role != UserRole.owner.value:
        flash(request, "error", "Only an owner can create another owner account.")
        return redirect("/users")

    existing = db.scalar(select(User).where(User.username == normalized_username))
    if existing is not None:
        flash(request, "error", f"Username '{normalized_username}' already exists.")
        return redirect("/users")

    ensure_password_policy(password)
    created = User(
        username=normalized_username,
        display_name=normalized_display_name,
        password_hash=hash_password(password),
        role=normalized_role,
    )
    db.add(created)
    db.commit()

    record_audit(
        db,
        event_type="user.created",
        subject_type="user",
        subject_id=created.id,
        actor=user,
        message=f"Created user {created.username} with role {created.role}.",
    )
    flash(request, "success", f"Created user {created.username}.")
    return redirect("/users")


@router.post("/users/{user_id}")
def users_update_submit(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    display_name: str = Form(""),
    role: str = Form("Viewer"),
    password: str = Form(""),
    is_active: str | None = Form(None),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    current_user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(current_user, RedirectResponse):
        return current_user

    target = db.get(User, user_id)
    if target is None:
        flash(request, "error", "User not found.")
        return redirect("/users")

    normalized_display_name = display_name.strip() or target.display_name
    normalized_role = role.strip()
    if normalized_role not in {value for value, _ in USER_ROLE_OPTIONS}:
        flash(request, "error", "Choose a valid role.")
        return redirect("/users")

    if normalized_role == UserRole.owner.value and current_user.role != UserRole.owner.value:
        flash(request, "error", "Only an owner can promote another account to owner.")
        return redirect("/users")

    next_is_active = is_active == "on"
    active_owner_total = count_active_owners(db)
    owner_demotion = target.role == UserRole.owner.value and normalized_role != UserRole.owner.value
    owner_deactivation = target.role == UserRole.owner.value and not next_is_active
    if active_owner_total <= 1 and (owner_demotion or owner_deactivation):
        flash(request, "error", "Keep at least one active owner account available.")
        return redirect("/users")

    if current_user.id == target.id and not next_is_active:
        flash(request, "error", "You cannot deactivate your own account from this session.")
        return redirect("/users")

    target.display_name = normalized_display_name
    target.role = normalized_role
    target.is_active = next_is_active

    password_message = ""
    if password.strip():
        ensure_password_policy(password)
        target.password_hash = hash_password(password)
        password_message = " Password was reset."

    db.commit()
    record_audit(
        db,
        event_type="user.updated",
        subject_type="user",
        subject_id=target.id,
        actor=current_user,
        message=f"Updated user {target.username}.",
    )
    flash(
        request,
        "success",
        f"Updated user {target.username}.{password_message}",
    )
    return redirect("/users")


@router.get("/remote", response_class=HTMLResponse)
def remote_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    return render(request, "remote.html", **build_remote_summary(request, db))


@router.post("/remote")
def remote_submit(
    request: Request,
    db: Session = Depends(get_db),
    public_base_url: str = Form(""),
    access_mode: str = Form("ip"),
    tls_mode: str = Form("proxy-http-ip"),
    reverse_proxy: str = Form("nginx"),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    validate_csrf(request, csrf_token)
    user = current_user_or_redirect(request, db, UserRole.admin)
    if isinstance(user, RedirectResponse):
        return user

    if access_mode not in {"domain", "ip", "private"}:
        flash(request, "error", "Choose a valid access mode.")
        return redirect("/remote")
    if tls_mode not in {"proxy-letsencrypt", "proxy-http-ip", "proxy-selfsigned-ip"}:
        flash(request, "error", "Choose a valid TLS mode.")
        return redirect("/remote")
    if reverse_proxy not in {"nginx", "caddy"}:
        flash(request, "error", "Choose a supported reverse proxy.")
        return redirect("/remote")

    host_settings = get_host_settings(db, request)
    host_settings.public_base_url = public_base_url.strip() or None
    host_settings.access_mode = access_mode
    host_settings.tls_mode = tls_mode
    host_settings.reverse_proxy = reverse_proxy
    db.commit()

    record_audit(
        db,
        event_type="remote.updated",
        subject_type="host",
        subject_id="host",
        actor=user,
        message="Updated remote access posture.",
    )
    flash(request, "success", "Saved remote access settings.")
    return redirect("/remote")
