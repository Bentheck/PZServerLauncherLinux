from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import ServerProfile
from app.services.audit import record_audit
from app.services.runtime import RuntimeManager
from app.services.zomboid import ZomboidService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class BackupScheduleSummary:
    enabled: bool
    interval_hours: int
    retention_count: int
    last_backup_at: datetime | None
    last_backup_name: str | None
    next_backup_due_at: datetime | None
    is_due: bool
    status_label: str
    detail: str


class BackupScheduler:
    def __init__(
        self,
        session_factory: sessionmaker,
        zomboid_service: ZomboidService,
        runtime_manager: RuntimeManager,
        *,
        poll_interval_seconds: int = 60,
        now_provider=utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.zomboid_service = zomboid_service
        self.runtime_manager = runtime_manager
        self.poll_interval_seconds = poll_interval_seconds
        self.now_provider = now_provider
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_due_backups(self) -> list[Path]:
        created_paths: list[Path] = []
        with self.session_factory() as db:
            profiles = db.scalars(
                select(ServerProfile)
                .where(ServerProfile.backup_enabled.is_(True))
                .order_by(ServerProfile.display_name)
            ).all()

        for profile in profiles:
            summary = self.describe_profile(profile)
            if not summary.is_due:
                continue

            try:
                backup_path = self.zomboid_service.create_backup(profile)
            except OSError as exc:
                self.runtime_manager.append_log(profile.id, f"Scheduled backup failed: {exc}.")
                with self.session_factory() as db:
                    record_audit(
                        db,
                        event_type="profile.backup.scheduled-failed",
                        subject_type="profile",
                        subject_id=profile.id,
                        actor=None,
                        message=f"Scheduled backup failed for {profile.display_name}: {exc}.",
                    )
                continue

            created_paths.append(backup_path)
            self.runtime_manager.append_log(profile.id, f"Scheduled backup created {backup_path.name}.")
            with self.session_factory() as db:
                record_audit(
                    db,
                    event_type="profile.backup.scheduled",
                    subject_type="profile",
                    subject_id=profile.id,
                    actor=None,
                    message=f"Scheduled backup created {backup_path.name} for {profile.display_name}.",
                )

        return created_paths

    def describe_profile(self, profile: ServerProfile) -> BackupScheduleSummary:
        latest_backup = self._latest_backup(profile)
        last_backup_at = self._backup_modified_at(latest_backup)
        next_backup_due_at: datetime | None = None
        is_due = False

        if profile.backup_enabled:
            if last_backup_at is None:
                is_due = True
            else:
                next_backup_due_at = last_backup_at + timedelta(hours=max(1, profile.backup_interval_hours))
                is_due = next_backup_due_at <= self.now_provider()

        if not profile.backup_enabled:
            status_label = "Disabled"
            detail = "Scheduled backups are turned off for this profile."
        elif last_backup_at is None:
            status_label = "Due now"
            detail = "No managed backup exists yet, so the scheduler will create one on its next pass."
        elif is_due:
            status_label = "Due now"
            detail = (
                f"The last backup ran at {self._format_time(last_backup_at)}. "
                f"The {profile.backup_interval_hours}-hour interval has elapsed."
            )
        else:
            status_label = "Scheduled"
            detail = (
                f"Last backup: {self._format_time(last_backup_at)}. "
                f"Next scheduled pass: {self._format_time(next_backup_due_at)}."
            )

        return BackupScheduleSummary(
            enabled=profile.backup_enabled,
            interval_hours=profile.backup_interval_hours,
            retention_count=profile.backup_retention_count,
            last_backup_at=last_backup_at,
            last_backup_name=latest_backup.name if latest_backup else None,
            next_backup_due_at=next_backup_due_at,
            is_due=is_due,
            status_label=status_label,
            detail=detail,
        )

    async def _run_loop(self) -> None:
        while True:
            await self.run_due_backups()
            await asyncio.sleep(self.poll_interval_seconds)

    def _latest_backup(self, profile: ServerProfile) -> Path | None:
        backups = self.zomboid_service.list_backups(profile)
        if not backups:
            return None
        return backups[0]

    @staticmethod
    def _backup_modified_at(path: Path | None) -> datetime | None:
        if path is None or not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    @staticmethod
    def _format_time(value: datetime | None) -> str:
        if value is None:
            return "Not scheduled yet"
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
