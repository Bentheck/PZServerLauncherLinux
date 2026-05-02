from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.models import JobStatus, OperationJob, RuntimeState, ServerProfile, User
from app.services.audit import record_audit
from app.services.zomboid import ZomboidService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RuntimeSnapshot:
    profile_id: str
    state: str = RuntimeState.stopped.value
    process_id: int | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_exit_reason: str | None = None
    latest_log_line: str | None = None


@dataclass
class ProcessRecord:
    profile_id: str
    process: asyncio.subprocess.Process
    watcher: asyncio.Task[None]
    stdout_reader: asyncio.Task[None]
    stderr_reader: asyncio.Task[None]


@dataclass(frozen=True, slots=True)
class InstallJobOptions:
    update: bool = False
    create_backup_before_update: bool = False
    stop_server_before_update: bool = False
    restart_after_completion: bool = False


class RuntimeManager:
    def __init__(self, session_factory: sessionmaker, zomboid_service: ZomboidService) -> None:
        self.session_factory = session_factory
        self.zomboid_service = zomboid_service
        self._statuses: dict[str, RuntimeSnapshot] = {}
        self._logs: dict[str, deque[str]] = {}
        self._commands: dict[str, deque[str]] = {}
        self._processes: dict[str, ProcessRecord] = {}

    def get_status(self, profile_id: str) -> RuntimeSnapshot:
        return self._statuses.get(profile_id, RuntimeSnapshot(profile_id=profile_id))

    def list_statuses(self) -> list[RuntimeSnapshot]:
        return [self.get_status(profile_id) for profile_id in sorted(self._statuses)]

    def recent_logs(self, profile_id: str, limit: int = 200) -> list[str]:
        return list(self._logs.get(profile_id, deque(maxlen=200)))[-limit:]

    def recent_commands(self, profile_id: str, limit: int = 20) -> list[str]:
        return list(self._commands.get(profile_id, deque(maxlen=50)))[-limit:]

    def is_profile_active(self, profile_id: str) -> bool:
        snapshot = self.get_status(profile_id)
        return profile_id in self._processes or snapshot.state in {
            RuntimeState.starting.value,
            RuntimeState.running.value,
            RuntimeState.stopping.value,
        }

    def append_log(self, profile_id: str, line: str) -> None:
        logs = self._logs.setdefault(profile_id, deque(maxlen=500))
        logs.append(line)
        self._write_profile_log(profile_id, line)

        snapshot = self.get_status(profile_id)
        snapshot.latest_log_line = line
        self._statuses[profile_id] = snapshot

    def clear_profile_state(self, profile_id: str) -> None:
        self._statuses.pop(profile_id, None)
        self._logs.pop(profile_id, None)
        self._commands.pop(profile_id, None)

    async def queue_install(
        self,
        profile: ServerProfile,
        actor_user_id: str | None,
        options: InstallJobOptions | None = None,
    ) -> OperationJob:
        install_options = options or InstallJobOptions()
        job = self._create_job(
            kind="update" if install_options.update else "install",
            profile_id=profile.id,
            summary=f"{'Update' if install_options.update else 'Install'} {profile.display_name}",
            actor_user_id=actor_user_id,
        )
        asyncio.create_task(self._run_install_job(job.id, profile.id, install_options))
        return job

    async def start_profile(self, profile: ServerProfile) -> RuntimeSnapshot:
        if profile.id in self._processes:
            raise ValueError("This profile is already running.")

        plan = self.zomboid_service.build_launch_plan(profile)
        if plan.blocked:
            snapshot = RuntimeSnapshot(
                profile_id=profile.id,
                state=RuntimeState.blocked.value,
                stopped_at=utcnow(),
                last_exit_reason=plan.notes,
                latest_log_line=plan.notes,
            )
            self._statuses[profile.id] = snapshot
            self.append_log(profile.id, plan.notes)
            return snapshot

        self.append_log(profile.id, plan.notes)
        snapshot = RuntimeSnapshot(
            profile_id=profile.id,
            state=RuntimeState.starting.value,
            started_at=utcnow(),
            latest_log_line=plan.notes,
        )
        self._statuses[profile.id] = snapshot

        process = await asyncio.create_subprocess_exec(
            *plan.command,
            cwd=str(plan.working_directory),
            env={**os.environ, **(plan.environment or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_reader = asyncio.create_task(self._read_output(profile.id, process.stdout, plan.redactions))
        stderr_reader = asyncio.create_task(self._read_output(profile.id, process.stderr, plan.redactions))
        watcher = asyncio.create_task(self._watch_process(profile.id, process, profile.auto_restart_on_crash))

        self._processes[profile.id] = ProcessRecord(
            profile_id=profile.id,
            process=process,
            watcher=watcher,
            stdout_reader=stdout_reader,
            stderr_reader=stderr_reader,
        )

        snapshot.state = RuntimeState.running.value
        snapshot.process_id = process.pid
        self._statuses[profile.id] = snapshot
        self.append_log(profile.id, f"Process started with PID {process.pid}.")
        return snapshot

    async def stop_profile(self, profile_id: str) -> RuntimeSnapshot:
        record = self._processes.get(profile_id)
        snapshot = self.get_status(profile_id)
        snapshot.state = RuntimeState.stopping.value
        self._statuses[profile_id] = snapshot

        if record is None:
            snapshot.state = RuntimeState.stopped.value
            snapshot.stopped_at = utcnow()
            snapshot.last_exit_reason = "No managed process was running."
            self._statuses[profile_id] = snapshot
            return snapshot

        if record.process.stdin:
            try:
                record.process.stdin.write(b"quit\n")
                await record.process.stdin.drain()
                await asyncio.wait_for(record.process.wait(), timeout=15)
            except (ProcessLookupError, asyncio.TimeoutError):
                record.process.kill()
                await record.process.wait()

        return self.get_status(profile_id)

    async def restart_profile(self, profile: ServerProfile) -> RuntimeSnapshot:
        await self.stop_profile(profile.id)
        return await self.start_profile(profile)

    async def stop_profiles(self, profile_ids: list[str]) -> list[RuntimeSnapshot]:
        snapshots: list[RuntimeSnapshot] = []
        seen: set[str] = set()
        for profile_id in profile_ids:
            normalized = profile_id.strip()
            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            snapshots.append(await self.stop_profile(normalized))

        return snapshots

    async def send_command(self, profile_id: str, command: str) -> None:
        record = self._processes.get(profile_id)
        if record is None or record.process.stdin is None:
            raise ValueError("The selected profile is not running.")

        trimmed = command.strip()
        if trimmed == "":
            raise ValueError("Command text is required.")

        record.process.stdin.write(trimmed.encode("utf-8") + b"\n")
        await record.process.stdin.drain()
        history = self._commands.setdefault(profile_id, deque(maxlen=50))
        history.append(trimmed)
        self.append_log(profile_id, f"> {trimmed}")

    async def _read_output(self, profile_id: str, stream, redactions: tuple[str, ...] = ()) -> None:
        compacted_noise_count = 0
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            for value in redactions:
                if value:
                    decoded = decoded.replace(value, "[redacted]")
            if self._is_compactable_pz_noise(decoded):
                compacted_noise_count += 1
                if compacted_noise_count == 1:
                    self.append_log(profile_id, "Compacting repeated vanilla Project Zomboid asset warning noise in launcher live logs.")
                continue
            self.append_log(profile_id, decoded)

        if compacted_noise_count:
            self.append_log(profile_id, f"Compacted {compacted_noise_count} repeated PZ asset warning line(s). Full raw output remains in the server console/debug logs.")

    @staticmethod
    def _is_compactable_pz_noise(line: str) -> bool:
        return any(
            marker in line
            for marker in (
                "Could not find icon:",
                "Missing texture: media/textures/weather/fogwhite.png",
                "No packet handler for type:",
                "Canceled loading wrong transition",
            )
        )

    async def _watch_process(self, profile_id: str, process: asyncio.subprocess.Process, auto_restart: bool) -> None:
        exit_code = await process.wait()
        snapshot = self.get_status(profile_id)
        snapshot.process_id = None
        snapshot.stopped_at = utcnow()
        snapshot.last_exit_reason = f"Process exited with code {exit_code}."
        snapshot.state = RuntimeState.stopped.value if exit_code == 0 else RuntimeState.crashed.value
        self._statuses[profile_id] = snapshot
        self._processes.pop(profile_id, None)
        self.append_log(profile_id, snapshot.last_exit_reason)

        if auto_restart and exit_code != 0:
            await asyncio.sleep(2)
            with self.session_factory() as db:
                profile = db.get(ServerProfile, profile_id)
                if profile is not None:
                    self.append_log(profile_id, "Auto-restart requested after crash.")
                    await self.start_profile(profile)

    def _create_job(self, *, kind: str, profile_id: str, summary: str, actor_user_id: str | None) -> OperationJob:
        with self.session_factory() as db:
            job = OperationJob(
                kind=kind,
                profile_id=profile_id,
                summary=summary,
                created_by_user_id=actor_user_id,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return job

    async def _run_install_job(self, job_id: str, profile_id: str, options: InstallJobOptions) -> None:
        with self.session_factory() as db:
            job = db.get(OperationJob, job_id)
            profile = db.get(ServerProfile, profile_id)
            if job is None or profile is None:
                return

            job.status = JobStatus.running.value
            job.progress_percent = 5
            job.started_at = utcnow()
            job.detail = f"Preparing SteamCMD {'update' if options.update else 'installation'}."
            db.commit()

        Path(profile.install_directory).mkdir(parents=True, exist_ok=True)
        Path(profile.cache_directory).mkdir(parents=True, exist_ok=True)
        stopped_runtime = False
        pre_update_backup_path: Path | None = None
        restarted_snapshot: RuntimeSnapshot | None = None
        failure_message: str | None = None

        try:
            if options.update and options.stop_server_before_update:
                snapshot = self.get_status(profile_id)
                if snapshot.state in {RuntimeState.starting.value, RuntimeState.running.value, RuntimeState.stopping.value}:
                    self.append_log(profile_id, "Stopping the managed server before running the update.")
                    await self.stop_profile(profile_id)
                    stopped_runtime = True
                    self._update_job(job_id, detail="Stopped the managed server before the update.", progress_percent=18)

            if options.update and options.create_backup_before_update:
                self._update_job(job_id, detail="Creating a pre-update backup.", progress_percent=24)
                pre_update_backup_path = self.zomboid_service.create_backup(profile)
                self.append_log(profile_id, f"Created pre-update backup {pre_update_backup_path.name}.")

            async def on_output(line: str) -> None:
                self.append_log(profile_id, line)
                with self.session_factory() as db:
                    current_job = db.get(OperationJob, job_id)
                    if current_job is not None:
                        current_job.detail = line[-500:]
                        current_job.progress_percent = min(current_job.progress_percent + 1, 92)
                        db.commit()

            exit_code = 1
            failure_message = "SteamCMD failed to run."
            self._update_job(
                job_id,
                detail=f"Running SteamCMD {'update' if options.update else 'installation'}.",
                progress_percent=35,
            )
            try:
                exit_code = await self.zomboid_service.run_command(
                    self.zomboid_service.resolve_install_command(profile),
                    working_directory=Path(profile.install_directory),
                    on_output=on_output,
                )
            except FileNotFoundError:
                failure_message = f"SteamCMD was not found at {self.zomboid_service.settings.steamcmd_path}."
            except OSError as exc:
                failure_message = f"SteamCMD could not be launched: {exc}."

            if exit_code == 0 and options.restart_after_completion:
                self.append_log(profile_id, f"Starting the managed server after the {'update' if options.update else 'install'} completed.")
                restarted_snapshot = await self.start_profile(profile)
        except (OSError, ValueError) as exc:
            exit_code = 1
            failure_message = str(exc)

        with self.session_factory() as db:
            current_job = db.get(OperationJob, job_id)
            if current_job is None:
                return

            current_job.completed_at = utcnow()
            current_job.progress_percent = 100 if exit_code == 0 else current_job.progress_percent
            if exit_code == 0:
                current_job.status = JobStatus.succeeded.value
                detail_parts = ["SteamCMD completed successfully."]
                if pre_update_backup_path is not None:
                    detail_parts.append(f"Pre-update backup: {pre_update_backup_path.name}.")
                if stopped_runtime:
                    detail_parts.append("Managed runtime was stopped before the update.")
                if restarted_snapshot is not None:
                    detail_parts.append(f"Runtime state after job: {restarted_snapshot.state}.")
                current_job.detail = " ".join(detail_parts)
            else:
                current_job.status = JobStatus.failed.value
                current_job.detail = failure_message
            db.commit()

            record_audit(
                db,
                event_type=f"profile.{current_job.kind}",
                subject_type="profile",
                subject_id=profile_id,
                actor=db.get(User, current_job.created_by_user_id) if current_job.created_by_user_id else None,
                message=current_job.detail,
            )

    def _update_job(self, job_id: str, *, detail: str, progress_percent: int | None = None) -> None:
        with self.session_factory() as db:
            current_job = db.get(OperationJob, job_id)
            if current_job is None:
                return

            current_job.detail = detail
            if progress_percent is not None:
                current_job.progress_percent = progress_percent
            db.commit()

    def _write_profile_log(self, profile_id: str, line: str) -> None:
        profiles_log_root = self.zomboid_service.settings.logs_root / "profiles"
        profiles_log_root.mkdir(parents=True, exist_ok=True)
        log_path = profiles_log_root / f"{profile_id}.log"
        timestamp = utcnow().isoformat()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {line}\n")
