from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    viewer = "Viewer"
    operator = "Operator"
    admin = "Admin"
    owner = "Owner"


ROLE_WEIGHTS: dict[UserRole, int] = {
    UserRole.viewer: 10,
    UserRole.operator: 20,
    UserRole.admin: 30,
    UserRole.owner: 40,
}


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class RuntimeState(str, enum.Enum):
    stopped = "stopped"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    crashed = "crashed"
    blocked = "blocked"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.viewer.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HostSettings(Base, TimestampMixin):
    __tablename__ = "host_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    bind_host: Mapped[str] = mapped_column(String(64), default="127.0.0.1")
    bind_port: Mapped[int] = mapped_column(Integer, default=48231)
    public_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_mode: Mapped[str] = mapped_column(String(32), default="ip")
    tls_mode: Mapped[str] = mapped_column(String(32), default="proxy-http-ip")
    reverse_proxy: Mapped[str] = mapped_column(String(32), default="nginx")
    reverse_proxy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    server_user: Mapped[str] = mapped_column(String(64), default="pzlauncher")
    steam_web_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_root: Mapped[str] = mapped_column(String(255))
    logs_root: Mapped[str] = mapped_column(String(255))


class ServerProfile(Base, TimestampMixin):
    __tablename__ = "server_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    server_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    install_directory: Mapped[str] = mapped_column(String(255))
    cache_directory: Mapped[str] = mapped_column(String(255))
    branch: Mapped[str] = mapped_column(String(32), default="stable")
    use_steam: Mapped[bool] = mapped_column(Boolean, default=True)
    preferred_memory_gb: Mapped[int] = mapped_column(Integer, default=4)
    max_players: Mapped[int] = mapped_column(Integer, default=8)
    default_port: Mapped[int] = mapped_column(Integer, default=16261)
    udp_port: Mapped[int] = mapped_column(Integer, default=16262)
    bind_ip: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    start_with_host: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_restart_on_crash: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    backup_retention_count: Mapped[int] = mapped_column(Integer, default=5)


class SettingsDraft(Base, TimestampMixin):
    __tablename__ = "settings_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(String(64), index=True)
    page_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class ModsMapsDraft(Base, TimestampMixin):
    __tablename__ = "mods_maps_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workshop_ids: Mapped[str] = mapped_column(Text, default="")
    mod_ids: Mapped[str] = mapped_column(Text, default="")
    map_ids: Mapped[str] = mapped_column(Text, default="")
    item_metadata_json: Mapped[str] = mapped_column(Text, default="[]")


class ModsMapsDraftItem(Base, TimestampMixin):
    __tablename__ = "mods_maps_draft_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String(64), index=True)
    mod_name: Mapped[str] = mapped_column(String(255), default="")
    mod_id: Mapped[str] = mapped_column(String(255), index=True)
    workshop_id: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    dependency_mod_ids: Mapped[str] = mapped_column(Text, default="")


class WorkshopPreset(Base, TimestampMixin):
    __tablename__ = "workshop_presets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    workshop_ids: Mapped[str] = mapped_column(Text, default="")
    mod_ids: Mapped[str] = mapped_column(Text, default="")
    map_ids: Mapped[str] = mapped_column(Text, default="")


class OperationJob(Base, TimestampMixin):
    __tablename__ = "operation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.queued.value)
    profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text, default="")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
