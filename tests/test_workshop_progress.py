from __future__ import annotations

from datetime import datetime, timezone

from app.services.runtime import RuntimeSnapshot
from app.services.workshop_progress import WorkshopDownloadProgressTracker

from test_profiles import bootstrap_owner, create_profile, get_profile


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def test_workshop_download_tracker_uses_configured_workshop_order() -> None:
    tracker = WorkshopDownloadProgressTracker(["111111111", "222222222", "333333333"])

    progress = tracker.observe("Downloading workshop content for 222222222 at 1048576 bytes.", utcnow())

    assert progress is not None
    assert progress.current_item_index == 2
    assert progress.total_item_count == 3
    assert progress.current_workshop_id == "222222222"
    assert progress.is_complete is False


def test_workshop_download_tracker_ignores_non_configured_byte_counters() -> None:
    tracker = WorkshopDownloadProgressTracker(["111111111", "222222222"])

    progress = tracker.observe("Downloading 999999999 bytes from the workshop cache.", utcnow())

    assert progress is None


def test_workshop_download_tracker_marks_last_configured_item_complete() -> None:
    tracker = WorkshopDownloadProgressTracker(["111111111", "222222222", "333333333"])

    progress = tracker.observe("Workshop item 333333333 download completed successfully.", utcnow())

    assert progress is not None
    assert progress.current_item_index == 3
    assert progress.is_complete is True
    assert progress.detail_label == "Workshop download complete (3/3) | Workshop ID 333333333"


def test_runtime_manager_tracks_and_clears_workshop_progress(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    runtime_manager = client.app.state.runtime_manager
    config_service = client.app.state.config_service

    config_service.save_mods_maps(profile, ["111111111", "222222222", "333333333"], [], [])
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="starting")
    runtime_manager._begin_workshop_download_session(profile)

    runtime_manager.append_log(profile_id, "Downloading workshop content for 222222222 at 1048576 bytes.")
    progress = runtime_manager.get_status(profile_id).workshop_download_progress

    assert progress is not None
    assert progress.current_item_index == 2
    assert progress.total_item_count == 3

    runtime_manager.clear_profile_state(profile_id)
    assert runtime_manager.get_status(profile_id).workshop_download_progress is None
