from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, get_host_settings, get_profile_or_404, require_role
from app.models import ModsMapsDraft, ModsMapsDraftItem, OperationJob, ServerProfile, User, UserRole
from app.security import slugify
from app.services.audit import record_audit
from app.services.profile_live import (
    build_runtime_diagnostic,
    find_active_job,
    serialize_job,
    serialize_launch_plan,
    serialize_runtime_snapshot,
)

router = APIRouter(prefix="/api", tags=["api"])


class ProfileCreateRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=128)
    server_name: str = Field(min_length=2, max_length=128)
    branch: str = "stable"
    preferred_memory_gb: int = 4
    max_players: int = 8
    default_port: int = 16261
    udp_port: int = 16262


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=2, max_length=128)
    role: str


class ModsMapsDraftRequest(BaseModel):
    workshop_ids: list[str] = Field(default_factory=list)
    mod_ids: list[str] = Field(default_factory=list)
    map_ids: list[str] = Field(default_factory=list)
    item_metadata: list[dict] = Field(default_factory=list)
    mod_items: list[dict] = Field(default_factory=list)


def api_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def serialize_import_candidate(candidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "display_name": candidate.display_name,
        "server_name": candidate.server_name,
        "cache_directory": candidate.cache_directory,
        "install_directory": candidate.install_directory,
        "branch": candidate.branch,
        "workshop_ids": candidate.workshop_ids,
        "mod_ids": candidate.mod_ids,
        "map_ids": candidate.map_ids,
        "diagnostics": candidate.diagnostics,
        "is_already_imported": candidate.is_already_imported,
        "can_import": candidate.can_import,
        "matching_profile_id": candidate.matching_profile_id,
        "matching_profile_display_name": candidate.matching_profile_display_name,
        "default_port": candidate.default_port,
        "udp_port": candidate.udp_port,
        "max_players": candidate.max_players,
        "bind_ip": candidate.bind_ip,
    }


def unique_clean_list(values: list[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(item)
    return items


def clean_mods_maps_mod_items(values: list[dict]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        mod_id = str(value.get("mod_id", "") or "").strip()
        if not mod_id:
            continue
        lowered = mod_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(
            {
                "mod_id": mod_id,
                "mod_name": str(value.get("mod_name", value.get("title", "")) or "").strip() or mod_id,
                "workshop_id": str(value.get("workshop_id", "") or "").strip(),
                "is_active": bool(value.get("is_active", True)),
                "sort_order": len(items),
                "dependency_mod_ids": "\n".join(unique_clean_list([str(item) for item in value.get("dependency_mod_ids", []) if str(item).strip()])),
            }
        )
    return items


def apply_mod_item_sort_order(mod_items: list[dict[str, object]], mod_ids: list[str]) -> list[dict[str, object]]:
    active_order = {mod_id.lower(): index for index, mod_id in enumerate(mod_ids)}
    inactive_offset = len(active_order)
    for fallback_order, item in enumerate(mod_items):
        mod_id = str(item.get("mod_id", "") or "").lower()
        is_active = mod_id in active_order
        item["is_active"] = is_active
        item["sort_order"] = active_order.get(mod_id, inactive_offset + fallback_order)
    return sorted(mod_items, key=lambda item: int(item.get("sort_order", 0)))


def split_stored_list(value: str) -> list[str]:
    return unique_clean_list([item for line in value.splitlines() for item in line.split(";")])


def serialize_workshop_preview(preview) -> dict:
    return {
        "workshop_id": preview.item.workshop_id,
        "title": preview.item.title,
        "description": preview.item.description,
        "preview_url": preview.item.preview_url,
        "source_label": preview.item.source_label,
        "kind_label": preview.item.kind_label,
        "is_installed_locally": preview.item.is_installed_locally,
        "is_queued": preview.item.is_queued,
        "mod_ids": preview.item.mod_ids,
        "map_ids": preview.item.map_ids,
        "tags": preview.item.tags,
        "collection_children": [
            {
                "workshop_id": item.workshop_id,
                "title": item.title,
                "is_installed_locally": item.is_installed_locally,
                "is_queued": item.is_queued,
            }
            for item in preview.collection_children
        ],
        "dependency_children": [
            {
                "workshop_id": item.workshop_id,
                "title": item.title,
                "is_installed_locally": item.is_installed_locally,
                "is_queued": item.is_queued,
            }
            for item in preview.dependency_children
        ],
    }


@router.get("/host/info")
def host_info(request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    host_settings = get_host_settings(db, request)
    runtime_manager = request.app.state.runtime_manager
    return {
        "host": {
            "app_name": request.app.state.settings.app_name,
            "bind_host": host_settings.bind_host,
            "bind_port": host_settings.bind_port,
            "public_base_url": host_settings.public_base_url,
            "access_mode": host_settings.access_mode,
        },
        "running_profiles": sum(1 for item in runtime_manager.list_statuses() if item.state == "running"),
    }


@router.get("/profiles")
def list_profiles(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    runtime_manager = request.app.state.runtime_manager
    return [
        {
            "id": profile.id,
            "display_name": profile.display_name,
            "server_name": profile.server_name,
            "branch": profile.branch,
            "status": runtime_manager.get_status(profile.id).state,
            "install_directory": profile.install_directory,
            "cache_directory": profile.cache_directory,
        }
        for profile in profiles
    ]


@router.post("/profiles", status_code=status.HTTP_201_CREATED)
def create_profile(payload: ProfileCreateRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.operator)

    profile_id = slugify(payload.display_name)
    server_root = request.app.state.settings.servers_root / profile_id
    profile = ServerProfile(
        id=profile_id,
        display_name=payload.display_name.strip(),
        server_name=payload.server_name.strip(),
        install_directory=str(server_root / "install"),
        cache_directory=str(server_root / "cache"),
        branch=payload.branch,
        preferred_memory_gb=payload.preferred_memory_gb,
        max_players=payload.max_players,
        default_port=payload.default_port,
        udp_port=payload.udp_port,
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
    return {"id": profile.id, "display_name": profile.display_name}


@router.get("/profiles/import-candidates")
def list_import_candidates(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    candidates = request.app.state.import_service.discover(profiles)
    return [serialize_import_candidate(candidate) for candidate in candidates]


@router.post("/profiles/import-candidates/{candidate_id}/import", status_code=status.HTTP_201_CREATED)
def import_profile_candidate(candidate_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.operator)
    existing_profiles = db.scalars(select(ServerProfile).order_by(ServerProfile.display_name)).all()
    import_service = request.app.state.import_service
    candidate = import_service.get_candidate(candidate_id, existing_profiles)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import candidate not found.")
    if candidate.is_already_imported:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This candidate is already imported.")
    if not candidate.can_import:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=candidate.diagnostics[0] if candidate.diagnostics else "This candidate cannot be imported.",
        )

    try:
        profile = import_service.import_candidate(candidate_id, existing_profiles)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

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
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "server_name": profile.server_name,
        "branch": profile.branch,
        "install_directory": profile.install_directory,
        "cache_directory": profile.cache_directory,
    }


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    profile = get_profile_or_404(db, profile_id)
    status_snapshot = request.app.state.runtime_manager.get_status(profile_id)
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "server_name": profile.server_name,
        "branch": profile.branch,
        "preferred_memory_gb": profile.preferred_memory_gb,
        "max_players": profile.max_players,
        "default_port": profile.default_port,
        "udp_port": profile.udp_port,
        "status": status_snapshot.state,
        "install_directory": profile.install_directory,
        "cache_directory": profile.cache_directory,
    }


@router.get("/profiles/{profile_id}/status")
def profile_status(profile_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    get_profile_or_404(db, profile_id)
    snapshot = request.app.state.runtime_manager.get_status(profile_id)
    return snapshot.__dict__


@router.get("/profiles/{profile_id}/logs/recent")
def recent_logs(profile_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    get_profile_or_404(db, profile_id)
    lines = request.app.state.runtime_manager.recent_logs(profile_id)
    return {"lines": lines}


@router.post("/profiles/{profile_id}/mods-maps/draft")
def save_mods_maps_draft(profile_id: str, payload: ModsMapsDraftRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.operator)
    profile = get_profile_or_404(db, profile_id)
    draft_payload = {
        "workshop_ids": unique_clean_list(payload.workshop_ids),
        "mod_ids": unique_clean_list(payload.mod_ids),
        "map_ids": unique_clean_list(payload.map_ids),
        "mod_items": clean_mods_maps_mod_items(payload.mod_items or payload.item_metadata),
    }
    draft_payload["mod_items"] = apply_mod_item_sort_order(draft_payload["mod_items"], draft_payload["mod_ids"])
    row = db.scalar(select(ModsMapsDraft).where(ModsMapsDraft.profile_id == profile.id))
    if row is None:
        row = ModsMapsDraft(profile_id=profile.id)
        db.add(row)
    active_mod_ids = [
        str(item["mod_id"])
        for item in draft_payload["mod_items"]
        if bool(item.get("is_active", False))
    ]
    draft_payload["mod_ids"] = active_mod_ids
    row.workshop_ids = "\n".join(draft_payload["workshop_ids"])
    row.mod_ids = "\n".join(active_mod_ids)
    row.map_ids = "\n".join(draft_payload["map_ids"])
    existing_items = db.scalars(select(ModsMapsDraftItem).where(ModsMapsDraftItem.profile_id == profile.id)).all()
    for existing_item in existing_items:
        db.delete(existing_item)
    for item in draft_payload["mod_items"]:
        db.add(
            ModsMapsDraftItem(
                profile_id=profile.id,
                mod_name=item["mod_name"],
                mod_id=item["mod_id"],
                workshop_id=item["workshop_id"],
                is_active=bool(item["is_active"]),
                sort_order=int(item["sort_order"]),
                dependency_mod_ids=item["dependency_mod_ids"],
            )
        )
    db.commit()
    return {
        "saved": True,
        **draft_payload,
    }


@router.get("/profiles/{profile_id}/mods-maps/workshop/{workshop_id}/preview")
def mods_maps_workshop_preview(profile_id: str, workshop_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    profile = get_profile_or_404(db, profile_id)
    host_settings = get_host_settings(db, request)

    draft = db.scalar(select(ModsMapsDraft).where(ModsMapsDraft.profile_id == profile.id))
    if draft is not None:
        current_workshop_ids = split_stored_list(draft.workshop_ids)
        current_mod_ids = split_stored_list(draft.mod_ids)
        current_map_ids = split_stored_list(draft.map_ids)
    else:
        settings = request.app.state.config_service.load_mods_maps(profile)
        current_workshop_ids = list(settings["workshop_ids"])
        current_mod_ids = list(settings["mod_ids"])
        current_map_ids = list(settings["map_ids"])

    preview = request.app.state.workshop_browser_service.get_preview(
        profile,
        current_workshop_ids=current_workshop_ids,
        current_mod_ids=current_mod_ids,
        current_map_ids=current_map_ids,
        workshop_id=workshop_id,
        api_key=host_settings.steam_web_api_key,
    )
    if preview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workshop preview not found.")

    return serialize_workshop_preview(preview)


@router.get("/profiles/{profile_id}/live")
def live_profile_data(profile_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    profile = get_profile_or_404(db, profile_id)
    runtime_manager = request.app.state.runtime_manager
    launch_plan = serialize_launch_plan(request.app.state.zomboid_service.build_launch_plan(profile))
    jobs = db.scalars(
        select(OperationJob)
        .where(OperationJob.profile_id == profile.id)
        .order_by(OperationJob.created_at.desc())
        .limit(6)
    ).all()
    profile_lookup = {profile.id: profile.display_name}
    snapshot = runtime_manager.get_status(profile.id)
    serialized_jobs = [serialize_job(job, profile_lookup) for job in jobs]
    return {
        "profile": {
            "id": profile.id,
            "display_name": profile.display_name,
            "server_name": profile.server_name,
            "branch": profile.branch,
        },
        "runtime": serialize_runtime_snapshot(snapshot),
        "launch_plan": launch_plan,
        "diagnostic": build_runtime_diagnostic(snapshot, launch_plan, serialized_jobs),
        "active_job": find_active_job(serialized_jobs),
        "logs": runtime_manager.recent_logs(profile.id, limit=120),
        "commands": runtime_manager.recent_commands(profile.id, limit=12),
        "jobs": serialized_jobs,
    }


@router.get("/users")
def list_users(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    user = api_user(request, db)
    require_role(user, UserRole.admin)
    users = db.scalars(select(User).order_by(User.username)).all()
    return [
        {
            "id": item.id,
            "username": item.username,
            "display_name": item.display_name,
            "role": item.role,
            "is_active": item.is_active,
        }
        for item in users
    ]


@router.get("/jobs")
def list_jobs(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    user = api_user(request, db)
    require_role(user, UserRole.viewer)
    jobs = db.scalars(select(OperationJob).order_by(OperationJob.created_at.desc()).limit(25)).all()
    profiles = db.scalars(select(ServerProfile)).all()
    profile_lookup = {profile.id: profile.display_name for profile in profiles}
    return [serialize_job(job, profile_lookup) for job in jobs]
