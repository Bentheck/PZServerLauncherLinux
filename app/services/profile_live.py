from __future__ import annotations

from datetime import datetime, timezone

from app.models import JobStatus, OperationJob, RuntimeState
from app.services.runtime import RuntimeSnapshot
from app.services.workshop_progress import WorkshopDownloadProgress
from app.services.zomboid import LaunchPlan

ACTIVE_JOB_STATUSES = {JobStatus.running.value, JobStatus.queued.value}


def format_timestamp(value: datetime | None, empty_label: str) -> str:
    if value is None:
        return empty_label
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def serialize_workshop_download_progress(progress: WorkshopDownloadProgress | None) -> dict[str, object] | None:
    if progress is None:
        return None
    return {
        "current_item_index": progress.current_item_index,
        "total_item_count": progress.total_item_count,
        "current_workshop_id": progress.current_workshop_id,
        "last_raw_line": progress.last_raw_line,
        "is_complete": progress.is_complete,
        "updated_at": progress.updated_at.isoformat(),
        "status_label": progress.status_label,
        "detail_label": progress.detail_label,
    }


def runtime_latest_display_line(snapshot: RuntimeSnapshot, empty_label: str = "Awaiting activity.") -> str:
    if snapshot.workshop_download_progress is not None:
        return snapshot.workshop_download_progress.detail_label
    return snapshot.latest_log_line or empty_label


def serialize_runtime_snapshot(snapshot: RuntimeSnapshot) -> dict[str, object]:
    return {
        "profile_id": snapshot.profile_id,
        "state": snapshot.state,
        "process_id": snapshot.process_id,
        "started_at": snapshot.started_at.isoformat() if snapshot.started_at else None,
        "stopped_at": snapshot.stopped_at.isoformat() if snapshot.stopped_at else None,
        "last_exit_reason": snapshot.last_exit_reason,
        "latest_log_line": snapshot.latest_log_line,
        "latest_display_line": runtime_latest_display_line(snapshot),
        "workshop_download_progress": serialize_workshop_download_progress(snapshot.workshop_download_progress),
    }


def job_status_tone(status: str) -> str:
    if status == JobStatus.failed.value:
        return "danger"
    if status == JobStatus.succeeded.value:
        return "success"
    if status == JobStatus.running.value:
        return "info"
    if status == JobStatus.queued.value:
        return "warning"
    return "neutral"


def serialize_job(job: OperationJob, profile_lookup: dict[str, str]) -> dict[str, object]:
    progress_percent = max(0, min(int(job.progress_percent or 0), 100))
    return {
        "id": job.id,
        "kind": job.kind,
        "kind_label": job.kind.replace("-", " ").title(),
        "status": job.status,
        "status_tone": job_status_tone(job.status),
        "profile_id": job.profile_id,
        "profile_label": profile_lookup.get(job.profile_id or "", job.profile_id or "Unknown profile"),
        "summary": job.summary,
        "detail": job.detail or "No detail yet.",
        "progress_percent": progress_percent,
        "created_at": job.created_at.isoformat(),
        "created_at_label": format_timestamp(job.created_at, "Unknown"),
        "updated_at": job.updated_at.isoformat(),
        "updated_at_label": format_timestamp(job.updated_at, "Unknown"),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "started_at_label": format_timestamp(job.started_at, "Not started yet"),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "completed_at_label": format_timestamp(job.completed_at, "In progress"),
        "is_active": job.status in ACTIVE_JOB_STATUSES,
    }


def serialize_launch_plan(plan: LaunchPlan) -> dict[str, object]:
    command = list(plan.command)
    redacted_command = [
        "[redacted]" if any(secret and part == secret for secret in plan.redactions) else part
        for part in command
    ]
    return {
        "blocked": plan.blocked,
        "notes": plan.notes,
        "working_directory": str(plan.working_directory),
        "command": redacted_command,
        "command_label": " ".join(redacted_command) if redacted_command else "Blocked until files exist",
        "state_label": "Blocked" if plan.blocked else "Ready",
    }


def find_active_job(jobs: list[dict[str, object]]) -> dict[str, object] | None:
    for status in (JobStatus.running.value, JobStatus.queued.value):
        match = next((job for job in jobs if job["status"] == status), None)
        if match is not None:
            return match
    return None


def find_latest_failed_job(jobs: list[dict[str, object]]) -> dict[str, object] | None:
    return next((job for job in jobs if job["status"] == JobStatus.failed.value), None)


def build_runtime_diagnostic(
    snapshot: RuntimeSnapshot,
    launch_plan: dict[str, object],
    jobs: list[dict[str, object]],
) -> dict[str, object]:
    active_job = find_active_job(jobs)
    latest_failed_job = find_latest_failed_job(jobs)
    latest_line = runtime_latest_display_line(snapshot)

    if active_job is not None:
        kind_label = str(active_job["kind_label"])
        progress_percent = int(active_job["progress_percent"])
        if active_job["status"] == JobStatus.queued.value:
            return {
                "tone": "warning",
                "label": "Queued",
                "headline": f"{kind_label} job is queued.",
                "detail": active_job["detail"],
                "recommended_action": "Keep this page open. The launcher will update the progress panel automatically when the job starts.",
                "active_job_id": active_job["id"],
            }

        return {
            "tone": "info",
            "label": "Working",
            "headline": f"{kind_label} job is in progress ({progress_percent}%).",
            "detail": active_job["detail"],
            "recommended_action": "Recent logs and job progress refresh automatically while the launcher keeps working.",
            "active_job_id": active_job["id"],
        }

    if bool(launch_plan["blocked"]) and snapshot.state not in {
        RuntimeState.running.value,
        RuntimeState.starting.value,
        RuntimeState.stopping.value,
    }:
        return {
            "tone": "danger",
            "label": "Blocked",
            "headline": "Start is blocked until the managed install is valid.",
            "detail": launch_plan["notes"],
            "recommended_action": "Run Install or Safe Update so the managed Linux server files can be rebuilt before starting again.",
            "active_job_id": None,
        }

    if snapshot.state == RuntimeState.crashed.value:
        return {
            "tone": "danger",
            "label": "Crashed",
            "headline": "The managed server exited unexpectedly.",
            "detail": snapshot.last_exit_reason or latest_line,
            "recommended_action": "Review the recent log tail below. If files or mods changed, update or restore before starting again.",
            "active_job_id": None,
        }

    if snapshot.state == RuntimeState.running.value:
        return {
            "tone": "success",
            "label": "Running",
            "headline": "The managed server is running.",
            "detail": latest_line,
            "recommended_action": "Use the Logs tab for the live console or send commands from this page whenever you need them.",
            "active_job_id": None,
        }

    if snapshot.state == RuntimeState.starting.value:
        return {
            "tone": "info",
            "label": "Starting",
            "headline": "The managed server is starting.",
            "detail": latest_line,
            "recommended_action": "If it stays in this state, watch the log tail for startup errors or a missing dependency.",
            "active_job_id": None,
        }

    if snapshot.state == RuntimeState.stopping.value:
        return {
            "tone": "warning",
            "label": "Stopping",
            "headline": "The managed server is shutting down.",
            "detail": latest_line,
            "recommended_action": "Wait for the process to stop before restoring files or starting another update.",
            "active_job_id": None,
        }

    if latest_failed_job is not None:
        return {
            "tone": "danger",
            "label": "Attention",
            "headline": f"Latest {str(latest_failed_job['kind_label']).lower()} job failed.",
            "detail": latest_failed_job["detail"],
            "recommended_action": "Fix the failure shown here, then re-run the job once the host is ready.",
            "active_job_id": None,
        }

    return {
        "tone": "info",
        "label": "Stopped",
        "headline": "The managed server is stopped.",
        "detail": snapshot.last_exit_reason or "No managed process is running right now.",
        "recommended_action": "Start the profile when you are ready, or run Install first if this VPS has not been deployed yet.",
        "active_job_id": None,
    }
