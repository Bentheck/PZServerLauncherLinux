from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.main import start_profiles_marked_for_host
from app.models import AuditEntry, HostSettings, ModsMapsDraft, ModsMapsDraftItem, OperationJob, ServerProfile, SettingsDraft, User, WorkshopPreset
from app.services.imports import LocalServerImportService
from app.services.release_check import LauncherUpdateStatus
from app.services.runtime import InstallJobOptions, RuntimeSnapshot
from app.services.workshop_progress import WorkshopDownloadProgress
from app.services.workshop_browser import (
    SteamWorkshopRemoteItem,
    WorkshopBrowserItem,
    WorkshopBrowserPreview,
    WorkshopBrowserPreviewChild,
    WorkshopBrowserSearchResult,
    WorkshopBrowserService,
)
from tests.conftest import extract_csrf


def bootstrap_owner(client) -> None:
    setup_page = client.get("/setup")
    csrf = extract_csrf(setup_page.text)
    client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "username": "owner",
            "display_name": "Owner",
            "password": "super-secure-password",
        },
    )


def create_profile(client) -> str:
    profiles_page = client.get("/profiles")
    csrf = extract_csrf(profiles_page.text)
    response = client.post(
        "/profiles",
        data={
            "csrf_token": csrf,
            "display_name": "Main Server",
            "server_name": "mainserver",
            "branch": "stable",
            "preferred_memory_gb": "6",
            "max_players": "12",
            "default_port": "16261",
            "udp_port": "16262",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/profiles/main-server/overview"
    return "main-server"


class FakeReleaseCheckService:
    def __init__(self, *statuses: LauncherUpdateStatus) -> None:
        self._statuses = list(statuses)
        self.calls: list[bool] = []

    def get_status(self, force_refresh: bool = False) -> LauncherUpdateStatus:
        self.calls.append(force_refresh)
        index = min(len(self.calls) - 1, len(self._statuses) - 1)
        return self._statuses[index]


def get_sandbox_draft(client, profile_id: str) -> SettingsDraft | None:
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(SettingsDraft).where(
                SettingsDraft.profile_id == profile_id,
                SettingsDraft.page_id == "sandbox",
            )
        )


def get_profile(client, profile_id: str) -> ServerProfile:
    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        return profile


def create_workshop_item(
    client,
    profile_id: str,
    *,
    workshop_id: str = "123456789",
    title: str = "Fancy Pack",
    mod_id: str = "FancyMod",
    map_id: str = "MapOne",
) -> Path:
    profile = get_profile(client, profile_id)
    workshop_root = Path(profile.install_directory) / "steamapps" / "workshop" / "content" / "108600" / workshop_id
    mod_root = workshop_root / "mods" / mod_id
    (mod_root / "media" / "maps" / map_id).mkdir(parents=True, exist_ok=True)
    (mod_root / "mod.info").write_text(
        "\n".join(
            [
                f"name={title}",
                f"id={mod_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (workshop_root / "workshop.txt").write_text(f"title={title}\n", encoding="utf-8")
    return workshop_root


def test_workshop_browser_search_kind_limits_remote_queries() -> None:
    class EmptyWorkshopCatalog:
        def list_installed_workshop_catalog(self, profile):
            return []

    service = WorkshopBrowserService(EmptyWorkshopCatalog())
    calls: list[str] = []

    def search_remote_items(api_key: str, query: str, take: int, required_tags=None):
        calls.append("mod")
        return [
            SteamWorkshopRemoteItem(
                workshop_id="111",
                title="Mod Result",
                description="Mod ID: ModResult",
                preview_url=None,
                tags=[],
                kind="item",
                child_workshop_ids=[],
            )
        ]

    def search_remote_collections(api_key: str, query: str, take: int, required_tags=None):
        calls.append("collection")
        return [
            SteamWorkshopRemoteItem(
                workshop_id="222",
                title="Collection Result",
                description="",
                preview_url=None,
                tags=[],
                kind="collection",
                child_workshop_ids=["111"],
            )
        ]

    service._search_remote_items = search_remote_items
    service._search_remote_collections = search_remote_collections

    result = service.search(
        SimpleNamespace(),
        current_workshop_ids=[],
        current_mod_ids=[],
        current_map_ids=[],
        query="result",
        api_key="steam-key",
        search_kind="collection",
    )

    assert calls == ["collection"]
    assert result.search_kind == "collection"
    assert [item.kind for item in result.items] == ["collection"]
    assert "Steam Workshop collection search is enabled" in result.diagnostics[0]

    calls.clear()
    result = service.search(
        SimpleNamespace(),
        current_workshop_ids=[],
        current_mod_ids=[],
        current_map_ids=[],
        query="result",
        api_key="steam-key",
        search_kind="mod",
    )

    assert calls == ["mod"]
    assert result.search_kind == "mod"
    assert [item.kind for item in result.items] == ["item"]
    assert "Steam Workshop mod search is enabled" in result.diagnostics[0]

    calls.clear()
    result = service.search(
        SimpleNamespace(),
        current_workshop_ids=[],
        current_mod_ids=[],
        current_map_ids=[],
        query="result",
        api_key="steam-key",
        search_kind="map",
    )

    assert calls == ["mod"]
    assert result.search_kind == "map"


def test_workshop_browser_search_passes_multiple_required_tags_to_steam() -> None:
    service = WorkshopBrowserService(SimpleNamespace())
    payload: dict[str, str] = {}

    service._apply_required_tags(payload, ["Build 42", "Map", "Build 42"])

    assert payload == {
        "requiredtags[0]": "Build 42",
        "requiredtags[1]": "Map",
    }


def test_workshop_browser_preview_recursively_resolves_dependencies() -> None:
    class EmptyWorkshopCatalog:
        def list_installed_workshop_catalog(self, profile):
            return []

    service = WorkshopBrowserService(EmptyWorkshopCatalog())
    remote_items = {
        "root": SteamWorkshopRemoteItem(
            workshop_id="root",
            title="Root Mod",
            description="Mod ID: RootMod",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=["dep-one"],
        ),
        "dep-one": SteamWorkshopRemoteItem(
            workshop_id="dep-one",
            title="Dependency One",
            description="Mod ID: DependencyOne",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=["dep-two"],
        ),
        "dep-two": SteamWorkshopRemoteItem(
            workshop_id="dep-two",
            title="Dependency Two",
            description="Mod ID: DependencyTwo",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=[],
        ),
    }

    service._get_collection = lambda workshop_id: None
    service._get_detail = lambda workshop_id: remote_items.get(workshop_id)
    service._get_details = lambda workshop_ids: [
        remote_items[workshop_id]
        for workshop_id in workshop_ids
        if workshop_id in remote_items
    ]

    preview = service.get_preview(
        SimpleNamespace(),
        current_workshop_ids=["dep-one"],
        current_mod_ids=[],
        current_map_ids=[],
        workshop_id="root",
        api_key="steam-key",
    )

    assert preview is not None
    assert [dependency.workshop_id for dependency in preview.dependency_children] == ["dep-one", "dep-two"]
    assert [dependency.is_queued for dependency in preview.dependency_children] == [True, False]
    assert preview.workshop_ids_to_add == ["root"]
    assert preview.dependency_workshop_ids_to_add == ["dep-two"]
    assert preview.dependency_mod_ids_to_add == ["DependencyOne", "DependencyTwo", "RootMod"]


def test_workshop_browser_preview_uses_steam_required_items_fallback() -> None:
    class EmptyWorkshopCatalog:
        def list_installed_workshop_catalog(self, profile):
            return []

    service = WorkshopBrowserService(EmptyWorkshopCatalog())
    remote_items = {
        "root": SteamWorkshopRemoteItem(
            workshop_id="root",
            title="Root Mod",
            description="Mod ID: RootMod",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=[],
        ),
        "dep-one": SteamWorkshopRemoteItem(
            workshop_id="dep-one",
            title="Dependency One",
            description="Mod ID: DependencyOne",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=[],
        ),
        "dep-two": SteamWorkshopRemoteItem(
            workshop_id="dep-two",
            title="Dependency Two",
            description="Mod ID: DependencyTwo",
            preview_url=None,
            tags=[],
            kind="item",
            child_workshop_ids=[],
        ),
    }
    required_items = {
        "root": ["dep-one"],
        "dep-one": ["dep-two"],
    }

    service._get_collection = lambda workshop_id: None
    service._get_detail = lambda workshop_id: remote_items.get(workshop_id)
    service._get_details = lambda workshop_ids: [
        remote_items[workshop_id]
        for workshop_id in workshop_ids
        if workshop_id in remote_items
    ]
    service._get_required_workshop_ids = lambda workshop_id: required_items.get(workshop_id, [])

    preview = service.get_preview(
        SimpleNamespace(),
        current_workshop_ids=[],
        current_mod_ids=[],
        current_map_ids=[],
        workshop_id="root",
        api_key="steam-key",
    )

    assert preview is not None
    assert [dependency.workshop_id for dependency in preview.dependency_children] == ["dep-one", "dep-two"]
    assert preview.dependency_workshop_ids_to_add == ["dep-one", "dep-two"]


def create_import_candidate(
    client,
    *,
    public_name: str = "Legacy Coast",
    server_name: str = "legacycoast",
) -> tuple[Path, Path]:
    import_root = client.app.state.settings.data_root / "import-fixtures" / server_name
    cache_root = import_root / "Zomboid"
    server_root = cache_root / "Server"
    server_root.mkdir(parents=True, exist_ok=True)
    (server_root / f"{server_name}.ini").write_text(
        "\n".join(
            [
                f"PublicName={public_name}",
                "DefaultPort=17261",
                "UDPPort=17262",
                "MaxPlayers=14",
                "PreferredMemoryInGigabytes=10",
                "StartWithHost=true",
                "AutoRestartOnCrash=false",
                "BindIP=10.0.0.25",
                "WorkshopItems=111111;222222",
                "Mods=ModA;ModB",
                "Map=MapOne",
                "",
            ]
        ),
        encoding="utf-8",
    )

    install_root = import_root / "Steam" / "steamapps" / "common" / "Project Zomboid Dedicated Server"
    install_root.mkdir(parents=True, exist_ok=True)
    (install_root / "start-server.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (install_root.parent.parent / "appmanifest_380870.acf").write_text(
        '"AppState"\n{\n    "UserConfig"\n    {\n        "betakey"    "unstable"\n    }\n}\n',
        encoding="utf-8",
    )

    workshop_root = install_root / "steamapps" / "workshop" / "content" / "108600"
    item_root = workshop_root / "111111"
    mod_root = item_root / "mods" / "ModA"
    (mod_root / "media" / "maps" / "MapOne").mkdir(parents=True, exist_ok=True)
    (mod_root / "mod.info").write_text("name=Mod A\nid=ModA\n", encoding="utf-8")
    (item_root / "workshop.txt").write_text("title=Legacy Pack A\n", encoding="utf-8")

    item_root = workshop_root / "222222"
    mod_root = item_root / "mods" / "ModB"
    mod_root.mkdir(parents=True, exist_ok=True)
    (mod_root / "mod.info").write_text("name=Mod B\nid=ModB\n", encoding="utf-8")
    (item_root / "workshop.txt").write_text("title=Legacy Pack B\n", encoding="utf-8")
    return cache_root, install_root


def configure_import_service(client, cache_root: Path, install_root: Path) -> None:
    client.app.state.import_service = LocalServerImportService(
        client.app.state.settings,
        client.app.state.config_service,
        cache_roots=[cache_root],
        install_directories=[install_root],
    )


def test_create_profile_and_open_workspace(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    workspace = client.get(f"/profiles/{profile_id}/overview")
    assert workspace.status_code == 200
    assert "Main Server" in workspace.text
    assert "Install &amp; Update" in workspace.text or "Install & Update" in workspace.text


def test_dashboard_surfaces_quick_start_fleet_status_and_import_intake(client) -> None:
    bootstrap_owner(client)
    cache_root, install_root = create_import_candidate(client)
    configure_import_service(client, cache_root, install_root)

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Quick Start" in page.text
    assert "What Needs Attention Next" in page.text
    assert "Current Focus" in page.text
    assert "Import Intake" in page.text
    assert "Legacy Coast" in page.text


def test_logs_page_exposes_live_runtime_surface(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/logs")
    assert page.status_code == 200
    assert f'data-live-url="/api/profiles/{profile_id}/live"' in page.text
    assert "Send Command" in page.text
    assert "Save World" in page.text
    assert "List Players" in page.text
    assert "Graceful Quit" in page.text
    assert "Recent Commands" in page.text


def test_consoles_page_exposes_picker_and_live_board(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get("/consoles")
    assert page.status_code == 200
    assert "Console Picker" in page.text
    assert "Live Console Board" in page.text
    assert "Target Slot" in page.text
    assert "Main Server" in page.text
    assert f'data-live-url="/api/profiles/{profile_id}/live"' in page.text


def test_consoles_slots_can_be_selected_assigned_and_cleared(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get("/consoles")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/consoles/slots/select",
        data={"csrf_token": csrf, "slot_number": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/consoles"

    page = client.get("/consoles")
    assert "Slot 2 is empty. Pin a server from the roster." in page.text
    csrf = extract_csrf(page.text)
    response = client.post(
        "/consoles/slots/assign",
        data={"csrf_token": csrf, "profile_id": profile_id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/consoles"

    page = client.get("/consoles")
    assert "Slot 2 is targeting Main Server." in page.text
    csrf = extract_csrf(page.text)
    response = client.post(
        "/consoles/slots/clear",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/consoles"

    page = client.get("/consoles")
    assert "Slot 2 is empty. Pin a server from the roster." in page.text


def test_profiles_page_surfaces_local_import_candidates(client) -> None:
    bootstrap_owner(client)
    cache_root, install_root = create_import_candidate(client)

    page = client.get("/profiles")
    assert page.status_code == 200
    assert "Import Existing Server" in page.text
    assert "Load Import" in page.text
    csrf = extract_csrf(page.text)

    response = client.post(
        "/profiles/imports/scan",
        data={
            "csrf_token": csrf,
            "next_url": "/profiles",
            "cache_directory": str(cache_root),
            "install_directory": str(install_root),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    page = response
    assert "Legacy Coast" in page.text
    assert "2 workshop" in page.text
    assert str(cache_root) in page.text
    assert str(install_root) in page.text


def test_profile_files_get_valid_bootstrap_defaults(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    paths = client.app.state.config_service.raw_file_paths(profile)
    paths["ini"].parent.mkdir(parents=True, exist_ok=True)

    paths["ini"].write_text("Map=\n", encoding="utf-8")
    paths["sandbox"].write_text("", encoding="utf-8")
    paths["spawnregions"].write_text("", encoding="utf-8")
    paths["spawnpoints"].write_text("", encoding="utf-8")

    client.app.state.config_service.ensure_profile_files(profile)

    assert "Map=Muldraugh, KY" in paths["ini"].read_text(encoding="utf-8")
    assert 'SandboxVars = require "Sandbox/Apocalypse"' in paths["sandbox"].read_text(encoding="utf-8")
    assert "function SpawnRegions()" in paths["spawnregions"].read_text(encoding="utf-8")
    assert "Muldraugh, KY" in paths["spawnregions"].read_text(encoding="utf-8")
    assert "function SpawnPoints()" in paths["spawnpoints"].read_text(encoding="utf-8")


def test_launch_plan_requires_bootstrap_admin_password(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    install_path = Path(profile.install_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "start-server.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    client.app.state.config_service.ensure_profile_files(profile)

    plan = client.app.state.zomboid_service.build_launch_plan(profile)

    assert plan.blocked is True
    assert plan.command == []
    assert "bootstrap admin password" in plan.notes


def test_import_discovery_skips_unreadable_install_probe(client, monkeypatch) -> None:
    inaccessible = Path("/home/ubuntu/Steam/steamapps/common/Project Zomboid Dedicated Server")
    original_exists = Path.exists

    def guarded_exists(path: Path) -> bool:
        if str(path).startswith(str(inaccessible)):
            raise PermissionError("permission denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    service = LocalServerImportService(
        client.app.state.settings,
        client.app.state.config_service,
        cache_roots=[],
        install_directories=[inaccessible],
    )

    assert service.discover([]) == []


def test_profile_import_adopts_existing_cache_and_install(client) -> None:
    bootstrap_owner(client)
    cache_root, install_root = create_import_candidate(client)
    configure_import_service(client, cache_root, install_root)

    candidate = client.app.state.import_service.discover([])[0]
    page = client.get("/profiles")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/profiles/imports/{candidate.candidate_id}",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/profiles/legacy-coast/overview"

    profile = get_profile(client, "legacy-coast")
    assert profile.display_name == "Legacy Coast"
    assert profile.server_name == "legacycoast"
    assert profile.cache_directory == str(cache_root)
    assert profile.install_directory == str(install_root)
    assert profile.branch == "unstable"
    assert profile.default_port == 17261
    assert profile.udp_port == 17262
    assert profile.max_players == 14
    assert profile.preferred_memory_gb == 10
    assert profile.bind_ip == "10.0.0.25"
    assert profile.start_with_host is True
    assert profile.auto_restart_on_crash is False


def test_import_candidates_api_marks_already_imported_and_blocks_reimport(client) -> None:
    bootstrap_owner(client)
    cache_root, install_root = create_import_candidate(client)
    configure_import_service(client, cache_root, install_root)

    candidates = client.get("/api/profiles/import-candidates")
    assert candidates.status_code == 200
    payload = candidates.json()
    assert len(payload) == 1
    candidate_id = payload[0]["candidate_id"]
    assert payload[0]["branch"] == "unstable"
    assert payload[0]["can_import"] is True

    response = client.post(f"/api/profiles/import-candidates/{candidate_id}/import")
    assert response.status_code == 201
    assert response.json()["id"] == "legacy-coast"

    candidates = client.get("/api/profiles/import-candidates")
    payload = candidates.json()
    assert payload[0]["is_already_imported"] is True
    assert payload[0]["can_import"] is False
    assert payload[0]["matching_profile_id"] == "legacy-coast"

    response = client.post(f"/api/profiles/import-candidates/{candidate_id}/import")
    assert response.status_code == 409


def test_overview_page_exposes_diagnostic_and_active_job_markers(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/overview")
    assert page.status_code == 200
    assert "data-diagnostic-headline" in page.text
    assert "data-active-job-title" in page.text
    assert "data-active-job-progress-bar" in page.text
    assert "data-launch-plan-state" in page.text
    assert "data-launch-plan-notes" in page.text


def test_install_update_page_passes_safe_update_options(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    install_page = client.get(f"/profiles/{profile_id}/install-update")
    assert "Choose Where This Server Lives" in install_page.text
    assert "Uninstall Server" in install_page.text
    assert "Delete Profile" in install_page.text
    assert "Queue Safe Update Job" in install_page.text
    assert "Create a pre-update backup first" in install_page.text
    csrf = extract_csrf(install_page.text)
    captured: dict[str, object] = {}

    async def fake_queue_install(profile, actor_user_id, options=None):
        captured["profile_id"] = profile.id
        captured["actor_user_id"] = actor_user_id
        captured["options"] = options
        return object()

    client.app.state.runtime_manager.queue_install = fake_queue_install  # type: ignore[method-assign]
    response = client.post(
        f"/profiles/{profile_id}/runtime/update",
        data={
            "csrf_token": csrf,
            "next_url": f"/profiles/{profile_id}/install-update",
            "create_backup_before_update": "on",
            "stop_server_before_update": "on",
            "restart_after_completion": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/profiles/{profile_id}/install-update"
    options = captured["options"]
    assert isinstance(options, InstallJobOptions)
    assert options.update is True
    assert options.create_backup_before_update is True
    assert options.stop_server_before_update is True
    assert options.restart_after_completion is True


def test_install_update_settings_persist_paths_and_branch(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    install_page = client.get(f"/profiles/{profile_id}/install-update")
    csrf = extract_csrf(install_page.text)
    custom_install = client.app.state.settings.data_root / "custom-install-root" / "main-server"
    custom_cache = client.app.state.settings.data_root / "custom-cache-root" / "main-server"
    response = client.post(
        f"/profiles/{profile_id}/install-update/settings",
        data={
            "csrf_token": csrf,
            "install_directory": str(custom_install),
            "cache_directory": str(custom_cache),
            "branch": "unstable",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/profiles/{profile_id}/install-update"

    updated_profile = get_profile(client, profile_id)
    assert updated_profile.install_directory == str(custom_install)
    assert updated_profile.cache_directory == str(custom_cache)
    assert updated_profile.branch == "unstable"


def test_install_update_uninstall_removes_managed_install_only(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    install_path = Path(profile.install_directory)
    cache_path = Path(profile.cache_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)
    (install_path / "server.txt").write_text("installed", encoding="utf-8")
    (cache_path / "cache.txt").write_text("kept", encoding="utf-8")

    install_page = client.get(f"/profiles/{profile_id}/install-update")
    csrf = extract_csrf(install_page.text)
    response = client.post(
        f"/profiles/{profile_id}/install-update/uninstall",
        data={
            "csrf_token": csrf,
            "confirmation_text": "UNINSTALL",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/profiles/{profile_id}/install-update"
    assert not install_path.exists()
    assert cache_path.exists()
    assert (cache_path / "cache.txt").read_text(encoding="utf-8") == "kept"
    assert get_profile(client, profile_id).id == profile_id


def test_install_update_delete_profile_cleans_managed_artifacts_and_leaves_external_paths(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    external_install = client.app.state.settings.data_root / "external-install" / profile_id
    external_cache = client.app.state.settings.data_root / "external-cache" / profile_id
    external_install.mkdir(parents=True, exist_ok=True)
    external_cache.mkdir(parents=True, exist_ok=True)
    (external_install / "server.txt").write_text("leave-me", encoding="utf-8")
    (external_cache / "profile.txt").write_text("leave-me-too", encoding="utf-8")

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.install_directory = str(external_install)
        profile.cache_directory = str(external_cache)
        db.add(
            WorkshopPreset(
                profile_id=profile_id,
                name="Pack",
                workshop_ids="111",
                mod_ids="ModA",
                map_ids="MapA",
            )
        )
        db.add(SettingsDraft(profile_id=profile_id, page_id="sandbox", payload_json="{}"))
        db.add(
            OperationJob(
                kind="install",
                profile_id=profile_id,
                summary="Install Main Server",
                detail="Done",
                status="succeeded",
                progress_percent=100,
            )
        )
        db.add(
            AuditEntry(
                event_type="profile.created",
                subject_type="profile",
                subject_id=profile_id,
                actor_label="system",
                message="Created profile.",
            )
        )
        db.commit()

    backup_root = client.app.state.settings.backups_root / profile_id
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "snapshot.tar.gz").write_bytes(b"backup")
    log_path = client.app.state.settings.logs_root / "profiles" / f"{profile_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("profile log", encoding="utf-8")

    install_page = client.get(f"/profiles/{profile_id}/install-update")
    csrf = extract_csrf(install_page.text)
    response = client.post(
        f"/profiles/{profile_id}/install-update/delete",
        data={
            "csrf_token": csrf,
            "confirmation_text": "DELETE",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/profiles"

    assert external_install.exists()
    assert external_cache.exists()
    assert (external_install / "server.txt").read_text(encoding="utf-8") == "leave-me"
    assert (external_cache / "profile.txt").read_text(encoding="utf-8") == "leave-me-too"
    assert not backup_root.exists()
    assert not log_path.exists()

    with client.app.state.session_factory() as db:
        assert db.get(ServerProfile, profile_id) is None
        assert db.scalars(select(WorkshopPreset).where(WorkshopPreset.profile_id == profile_id)).all() == []
        assert db.scalars(select(SettingsDraft).where(SettingsDraft.profile_id == profile_id)).all() == []
        assert db.scalars(select(OperationJob).where(OperationJob.profile_id == profile_id)).all() == []
        audits = db.scalars(select(AuditEntry).where(AuditEntry.subject_id == profile_id)).all()
        assert len(audits) == 1
        assert audits[0].event_type == "profile.deleted"


def test_install_update_retirement_actions_are_blocked_while_running(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    client.app.state.runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="running")

    install_page = client.get(f"/profiles/{profile_id}/install-update")
    csrf = extract_csrf(install_page.text)
    response = client.post(
        f"/profiles/{profile_id}/install-update/uninstall",
        data={
            "csrf_token": csrf,
            "confirmation_text": "UNINSTALL",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Stop Main Server before uninstalling or deleting it." in response.text


def test_live_profile_api_returns_runtime_logs_commands_and_jobs(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    runtime_manager = client.app.state.runtime_manager
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(
        profile_id=profile_id,
        state="running",
        process_id=4321,
        latest_log_line="Server ready",
    )
    runtime_manager.append_log(profile_id, "Boot sequence complete")

    writes: list[bytes] = []

    class FakeStdin:
        def write(self, data: bytes) -> None:
            writes.append(data)

        async def drain(self) -> None:
            return None

    runtime_manager._processes[profile_id] = SimpleNamespace(process=SimpleNamespace(stdin=FakeStdin()))
    asyncio.run(runtime_manager.send_command(profile_id, "save"))

    job = runtime_manager._create_job(
        kind="update",
        profile_id=profile_id,
        summary="Update Main Server",
        actor_user_id=None,
    )
    with client.app.state.session_factory() as db:
        saved_job = db.get(type(job), job.id)
        assert saved_job is not None
        saved_job.status = "running"
        saved_job.detail = "Applying workshop files"
        saved_job.progress_percent = 44
        db.commit()

    response = client.get(f"/api/profiles/{profile_id}/live")
    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["state"] == "running"
    assert payload["runtime"]["process_id"] == 4321
    assert payload["diagnostic"]["headline"] == "Update job is in progress (44%)."
    assert payload["diagnostic"]["label"] == "Working"
    assert payload["active_job"]["id"] == job.id
    assert payload["launch_plan"]["blocked"] is True
    assert payload["launch_plan"]["command_label"] == "Blocked until files exist"
    assert "Boot sequence complete" in payload["logs"]
    assert "> save" in payload["logs"]
    assert payload["commands"][-1] == "save"
    assert payload["jobs"][0]["summary"] == "Update Main Server"
    assert payload["jobs"][0]["progress_percent"] == 44
    assert payload["jobs"][0]["status_tone"] == "info"
    assert writes == [b"save\n"]


def test_live_profile_api_returns_workshop_download_progress_display(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    runtime_manager = client.app.state.runtime_manager
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(
        profile_id=profile_id,
        state="starting",
        latest_log_line="Downloading workshop content for 222222222 at 1048576 bytes.",
        workshop_download_progress=WorkshopDownloadProgress(
            current_item_index=2,
            total_item_count=3,
            current_workshop_id="222222222",
            last_raw_line="Downloading workshop content for 222222222 at 1048576 bytes.",
            is_complete=False,
            updated_at=datetime.now(timezone.utc),
        ),
    )

    response = client.get(f"/api/profiles/{profile_id}/live")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["latest_log_line"] == "Downloading workshop content for 222222222 at 1048576 bytes."
    assert payload["runtime"]["latest_display_line"] == "Downloading workshop item 2/3 | Workshop ID 222222222"
    assert payload["runtime"]["workshop_download_progress"]["current_item_index"] == 2
    assert payload["runtime"]["workshop_download_progress"]["current_workshop_id"] == "222222222"


def test_live_profile_api_surfaces_blocked_launch_guidance(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    response = client.get(f"/api/profiles/{profile_id}/live")
    assert response.status_code == 200
    payload = response.json()
    assert payload["launch_plan"]["blocked"] is True
    assert "start-server.sh" in payload["launch_plan"]["notes"]
    assert payload["diagnostic"]["tone"] == "danger"
    assert payload["diagnostic"]["label"] == "Blocked"
    assert "Run Install or Safe Update" in payload["diagnostic"]["recommended_action"]
    assert payload["active_job"] is None


def test_profile_overview_prefers_workshop_download_progress_in_pinned_runtime_sections(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    runtime_manager = client.app.state.runtime_manager
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(
        profile_id=profile_id,
        state="starting",
        latest_log_line="Downloading workshop content for 222222222 at 1048576 bytes.",
        workshop_download_progress=WorkshopDownloadProgress(
            current_item_index=2,
            total_item_count=3,
            current_workshop_id="222222222",
            last_raw_line="Downloading workshop content for 222222222 at 1048576 bytes.",
            is_complete=False,
            updated_at=datetime.now(timezone.utc),
        ),
    )

    page = client.get(f"/profiles/{profile_id}/overview")

    assert page.status_code == 200
    assert "Downloading workshop item 2/3 | Workshop ID 222222222" in page.text


def test_backup_page_exposes_scheduler_status(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.backup_enabled = True
        profile.backup_interval_hours = 6
        profile.backup_retention_count = 4
        db.commit()

    page = client.get(f"/profiles/{profile_id}/backups")
    assert page.status_code == 200
    assert "Pending first pass" in page.text
    assert "Due now" in page.text
    assert "every 6 hours" in page.text


def test_backup_scheduler_creates_due_backup_once_and_audits_it(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.backup_enabled = True
        profile.backup_interval_hours = 24
        profile.backup_retention_count = 3
        db.commit()

    created = asyncio.run(client.app.state.backup_scheduler.run_due_backups())
    assert len(created) == 1
    assert created[0].exists()

    created_again = asyncio.run(client.app.state.backup_scheduler.run_due_backups())
    assert created_again == []

    logs = client.app.state.runtime_manager.recent_logs(profile_id)
    assert any("Scheduled backup created" in line for line in logs)

    with client.app.state.session_factory() as db:
        audits = db.scalars(
            select(AuditEntry)
            .where(AuditEntry.subject_id == profile_id)
            .order_by(AuditEntry.created_at.desc())
        ).all()
    assert audits
    assert audits[0].event_type == "profile.backup.scheduled"


def test_general_settings_persist_to_ini(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    general_page = client.get(f"/profiles/{profile_id}/general")
    assert "Spawn, Loot, and Cleanup" in general_page.text
    assert "Allow player safehouses" in general_page.text
    assert "Players Required For A Faction Tag" in general_page.text
    assert "Start this server when the host starts" in general_page.text
    csrf = extract_csrf(general_page.text)
    response = client.post(
        f"/profiles/{profile_id}/general",
        data={
            "csrf_token": csrf,
            "display_name": "Main Server",
            "server_name": "mainserver",
            "preferred_memory_gb": "6",
            "max_players": "16",
            "default_port": "16261",
            "udp_port": "16262",
            "rcon_port": "28015",
            "start_with_host": "on",
            "auto_restart_on_crash": "on",
            "public_name": "Main Public Server",
            "public_description": "A cozy apocalypse.",
            "public": "on",
            "open_signup": "on",
            "pvp": "on",
            "pause_empty": "on",
            "global_chat": "on",
            "server_welcome_message": "Welcome survivors\nMind the helicopters",
            "spawn_items": "Base.Axe\nBase.WaterBottleFull",
            "loot_respawn_hours": "72",
            "loot_respawn_max_items": "6",
            "construction_prevents_loot_respawn": "on",
            "sleep_allowed": "on",
            "sleep_needed": "",
            "no_fire": "on",
            "announce_death": "on",
            "drop_whitelist_on_death": "on",
            "allow_sledgehammer_destruction": "on",
            "respawn_with_self": "on",
            "respawn_with_other": "",
            "world_item_removal_hours": "24.0",
            "world_item_removal_list": "Base.Hat\nBase.MugWhite",
            "player_safehouse": "on",
            "admin_safehouse": "on",
            "safehouse_allow_trespass": "on",
            "safehouse_allow_fire": "",
            "safehouse_allow_loot": "on",
            "safehouse_allow_respawn": "on",
            "safehouse_allow_non_residential": "on",
            "disable_safehouse_when_player_connected": "on",
            "disable_safehouse_when_player_disconnected": "",
            "safehouse_days_to_claim": "3",
            "safehouse_removal_hours": "96",
            "faction_enabled": "on",
            "faction_days_to_create": "2",
            "faction_players_for_tag": "4",
            "allow_trade_ui": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    ini_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver.ini"
    content = ini_path.read_text(encoding="utf-8")
    assert "PublicName=Main Public Server" in content
    assert "PublicDescription=A cozy apocalypse." in content
    assert "Public=true" in content
    assert "PVP=true" in content
    assert "GlobalChat=true" in content
    assert "ServerWelcomeMessage=Welcome survivors<LINE>Mind the helicopters" in content
    assert "SpawnItems=Base.Axe,Base.WaterBottleFull" in content
    assert "HoursForLootRespawn=72" in content
    assert "MaxItemsForLootRespawn=6" in content
    assert "ConstructionPreventsLootRespawn=true" in content
    assert "SleepAllowed=true" in content
    assert "NoFire=true" in content
    assert "DropOffWhiteListAfterDeath=true" in content
    assert "PlayerRespawnWithSelf=true" in content
    assert "WorldItemRemovalList=Base.Hat,Base.MugWhite" in content
    assert "AdminSafehouse=true" in content
    assert "SafehouseAllowFire=false" in content
    assert "SafehouseDaySurvivedToClaim=3" in content
    assert "SafeHouseRemovalTime=96" in content
    assert "FactionPlayersRequiredForTag=4" in content
    assert "AllowTradeUI=true" in content
    assert "DefaultPort=16261" in content
    assert "UDPPort=16262" in content
    assert "RCONPort=28015" in content
    assert "PreferredMemoryInGigabytes=6" in content
    assert "StartWithHost=true" in content
    assert "AutoRestartOnCrash=true" in content
    assert "MaxPlayers=16" in content

    updated_profile = get_profile(client, profile_id)
    assert updated_profile.start_with_host is True
    assert updated_profile.auto_restart_on_crash is True


def test_advanced_ini_save_syncs_runtime_profile_metadata(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/advanced-files")
    csrf = extract_csrf(page.text)
    profile = get_profile(client, profile_id)
    ini_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini"
    content = ini_path.read_text(encoding="utf-8")
    content = content.replace("MaxPlayers=12", "MaxPlayers=20")
    content = content.replace("DefaultPort=16261", "DefaultPort=17261")
    content = content.replace("UDPPort=16262", "UDPPort=17262")
    content = content.replace("PreferredMemoryInGigabytes=6", "PreferredMemoryInGigabytes=12")
    content = content.replace("StartWithHost=false", "StartWithHost=true")
    content = content.replace("AutoRestartOnCrash=false", "AutoRestartOnCrash=true")
    content = content.replace("BindIP=0.0.0.0", "BindIP=10.10.10.5")

    response = client.post(
        f"/profiles/{profile_id}/advanced-files",
        data={
            "csrf_token": csrf,
            "file_kind": "ini",
            "content": content,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    updated_profile = get_profile(client, profile_id)
    assert updated_profile.max_players == 20
    assert updated_profile.default_port == 17261
    assert updated_profile.udp_port == 17262
    assert updated_profile.preferred_memory_gb == 12
    assert updated_profile.start_with_host is True
    assert updated_profile.auto_restart_on_crash is True
    assert updated_profile.bind_ip == "10.10.10.5"


def test_start_with_host_profiles_are_started_on_host_bootstrap(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.start_with_host = True
        db.commit()

    calls: list[str] = []

    async def fake_start(profile):
        calls.append(profile.id)
        return RuntimeSnapshot(profile_id=profile.id, state="running")

    client.app.state.runtime_manager.start_profile = fake_start  # type: ignore[method-assign]
    asyncio.run(start_profiles_marked_for_host(client.app))
    assert calls == [profile_id]


def test_network_page_exposes_windows_parity_fields(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/network-admin")
    assert page.status_code == 200
    assert "Auto Create Whitelist Users" in page.text
    assert "Enforce Lua Checksum" in page.text
    assert "Deny Login When Overloaded" in page.text
    assert "Show First &amp; Last Name" in page.text or "Show First & Last Name" in page.text
    assert "Use TCP For Map Traffic" in page.text
    assert "Minutes Per Page" in page.text
    assert "Admin Username" in page.text


def test_network_settings_and_write_only_passwords_persist(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    network_page = client.get(f"/profiles/{profile_id}/network-admin")
    csrf = extract_csrf(network_page.text)
    client.post(
        f"/profiles/{profile_id}/network-admin",
        data={
            "csrf_token": csrf,
            "bind_ip": "0.0.0.0",
            "steam_mode": "on",
            "rcon_port": "27015",
            "server_password": "join-secret",
            "rcon_password": "rcon-secret",
            "admin_username": "bootstrap-admin",
            "admin_password": "bootstrap-secret",
            "server_tag": "PVE",
            "reset_id": "4",
            "auto_create_user_in_whitelist": "on",
            "do_lua_checksum": "on",
            "ping_limit": "220",
            "steam_vac": "on",
            "kick_fast_players": "on",
            "deny_login_overloaded": "on",
            "client_command_filter": "allow-listed",
            "save_world_every_minutes": "15",
            "display_user_name": "on",
            "show_first_and_last_name": "on",
            "safety_system": "on",
            "show_safety": "on",
            "safety_toggle_timer": "45",
            "safety_cooldown_timer": "90",
            "max_accounts_per_user": "2",
            "allow_non_ascii_username": "on",
            "player_save_on_damage": "on",
            "mouse_over_display_name": "on",
            "hide_players_behind_you": "on",
            "player_bump_player": "on",
            "map_remote_player_visibility": "3",
            "use_tcp_for_map_traffic": "on",
            "upnp": "on",
            "voice_enable": "on",
            "voice_3d": "on",
            "voice_min_distance": "8.0",
            "voice_max_distance": "120.0",
            "minutes_per_page": "2",
        },
    )

    network_page = client.get(f"/profiles/{profile_id}/network-admin")
    csrf = extract_csrf(network_page.text)
    client.post(
        f"/profiles/{profile_id}/network-admin",
        data={
            "csrf_token": csrf,
            "bind_ip": "10.0.0.5",
            "steam_mode": "on",
            "rcon_port": "28015",
            "server_password": "",
            "rcon_password": "",
            "admin_username": "updated-admin",
            "admin_password": "",
            "server_tag": "PVP",
            "reset_id": "9",
            "auto_create_user_in_whitelist": "on",
            "do_lua_checksum": "on",
            "ping_limit": "180",
            "steam_vac": "on",
            "kick_fast_players": "",
            "deny_login_overloaded": "on",
            "client_command_filter": "trusted-only",
            "save_world_every_minutes": "10",
            "display_user_name": "on",
            "show_first_and_last_name": "",
            "safety_system": "on",
            "show_safety": "",
            "safety_toggle_timer": "30",
            "safety_cooldown_timer": "60",
            "max_accounts_per_user": "4",
            "allow_non_ascii_username": "",
            "player_save_on_damage": "on",
            "mouse_over_display_name": "on",
            "hide_players_behind_you": "",
            "player_bump_player": "",
            "map_remote_player_visibility": "2",
            "use_tcp_for_map_traffic": "",
            "upnp": "",
            "voice_enable": "on",
            "voice_3d": "",
            "voice_min_distance": "10.0",
            "voice_max_distance": "90.0",
            "minutes_per_page": "5",
        },
    )

    ini_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver.ini"
    content = ini_path.read_text(encoding="utf-8")
    launcher_secrets_path = client.app.state.config_service.launcher_secrets_path(get_profile(client, profile_id))
    launcher_secrets = launcher_secrets_path.read_text(encoding="utf-8")
    assert "Password=" in content
    assert "Password=join-secret" not in content
    assert "RCONPassword=rcon-secret" in content
    assert "AdminPassword=bootstrap-secret" in launcher_secrets
    assert "AdminUsername=updated-admin" in launcher_secrets
    assert "BindIP=10.0.0.5" in content
    assert "RCONPort=28015" in content
    assert "Tag=PVP" in content
    assert "ResetID=9" in content
    assert "DoLuaChecksum=" not in content
    assert "PingLimit=180" in content
    assert "KickFastPlayers=false" in content
    assert "ClientCommandFilter=trusted-only" in content
    assert "SaveWorldEveryMinutes=10" in content
    assert "UPnP=false" in content
    assert "ShowFirstAndLastName=false" in content
    assert "SafetyToggleTimer=30" in content
    assert "SafetyCooldownTimer=60" in content
    assert "MaxAccountsPerUser=4" in content
    assert "MapRemotePlayerVisibility=2" in content
    assert "UseTCPForMapTraffic=false" in content
    assert "Voice3D=false" in content
    assert "MinutesPerPage=5" in content


def test_network_settings_preserve_missing_do_lua_checksum_when_inherited_value_is_saved(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    client.app.state.config_service.ensure_profile_files(profile)
    ini_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini"
    ini_path.write_text(
        "\n".join(
            [
                "PublicName=Main Server",
                "BindIP=0.0.0.0",
                "RCONPort=27015",
                "PingLimit=250",
                "SteamVAC=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    values = dict(client.app.state.config_service.load_network_settings(profile))
    values["server_password"] = ""
    values["rcon_password"] = ""
    values["admin_password"] = ""

    client.app.state.config_service.save_network_settings(profile, values)

    content = ini_path.read_text(encoding="utf-8")
    assert "DoLuaChecksum=" not in content


def test_network_settings_write_do_lua_checksum_false_when_admin_unchecks_inherited_value(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    client.app.state.config_service.ensure_profile_files(profile)
    ini_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini"
    ini_path.write_text(
        "\n".join(
            [
                "PublicName=Main Server",
                "BindIP=0.0.0.0",
                "RCONPort=27015",
                "PingLimit=250",
                "SteamVAC=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    values = dict(client.app.state.config_service.load_network_settings(profile))
    values["server_password"] = ""
    values["rcon_password"] = ""
    values["admin_password"] = ""
    values["do_lua_checksum"] = False

    client.app.state.config_service.save_network_settings(profile, values)

    content = ini_path.read_text(encoding="utf-8")
    assert "DoLuaChecksum=false" in content


def test_generated_default_ini_does_not_force_do_lua_checksum(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    ini_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini"
    if ini_path.exists():
        ini_path.unlink()

    client.app.state.config_service.ensure_profile_files(profile)

    content = ini_path.read_text(encoding="utf-8")
    assert "DoLuaChecksum=" not in content


def test_runtime_compacts_known_project_zomboid_warning_noise(client) -> None:
    manager = client.app.state.runtime_manager

    assert manager._is_compactable_pz_noise("WARN : General > Could not find icon: Build_WallWood")
    assert manager._is_compactable_pz_noise("WARN : Packet > No packet handler for type: \"Drink\"")
    assert manager._is_compactable_pz_noise("LOG  : General > Missing texture: media/textures/weather/fogwhite.png")
    assert manager._is_compactable_pz_noise("WARN : ActionSystem > Canceled loading wrong transition from walk to walk")
    assert not manager._is_compactable_pz_noise("LOG  : Network > *** SERVER STARTED ****")


def test_network_admin_bootstrap_flows_into_launch_plan(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)

    network_page = client.get(f"/profiles/{profile_id}/network-admin")
    csrf = extract_csrf(network_page.text)
    client.post(
        f"/profiles/{profile_id}/network-admin",
        data={
            "csrf_token": csrf,
            "bind_ip": "10.0.0.9",
            "steam_mode": "on",
            "rcon_port": "27015",
            "admin_username": "warden",
            "admin_password": "fresh-secret",
            "voice_enable": "on",
            "voice_3d": "on",
            "voice_min_distance": "10.0",
            "voice_max_distance": "100.0",
            "minutes_per_page": "1",
        },
    )

    install_path = Path(profile.install_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "start-server.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    refreshed = get_profile(client, profile_id)
    plan = client.app.state.zomboid_service.build_launch_plan(refreshed)
    assert "-cachedir" not in plan.command
    assert plan.environment is not None
    assert plan.environment["HOME"].endswith("runtime-home")
    assert plan.environment["JAVA_TOOL_OPTIONS"].startswith("-Duser.home=")
    assert "-adminusername" in plan.command
    assert "warden" in plan.command
    assert "-adminpassword" in plan.command
    assert "fresh-secret" in plan.command
    assert plan.redactions == ("fresh-secret",)
    assert "-ip" in plan.command
    assert "10.0.0.9" in plan.command
    assert "Bootstrap admin 'warden' is configured for launch." in plan.notes


def test_launch_plan_omits_wildcard_bind_and_applies_memory(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.bind_ip = "0.0.0.0"
        profile.preferred_memory_gb = 3
        db.commit()

    profile = get_profile(client, profile_id)
    install_path = Path(profile.install_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "start-server.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (install_path / "ProjectZomboid64.json").write_text(
        '{"vmArgs":["-Djava.awt.headless=true","-Xmx8g"]}',
        encoding="utf-8",
    )

    secrets_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}_LauncherSecrets.ini"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text("AdminUsername=warden\nAdminPassword=fresh-secret\n", encoding="utf-8")

    refreshed = get_profile(client, profile_id)
    plan = client.app.state.zomboid_service.build_launch_plan(refreshed)

    assert "-ip" not in plan.command
    assert "Applied -Xmx3g to ProjectZomboid64.json." in plan.notes
    assert '"-Xmx3g"' in (install_path / "ProjectZomboid64.json").read_text(encoding="utf-8")


def test_host_page_exposes_roster_metrics_and_checklist(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.start_with_host = True
        profile.backup_enabled = True
        db.commit()

    page = client.get("/host")
    assert page.status_code == 200
    assert "Lifecycle Actions" in page.text
    assert "Host Operator Guidance" in page.text
    assert "Main Server" in page.text
    assert "1 of 1 managed profile(s) are configured to start with the host." in page.text


def test_host_page_can_run_startup_roster_and_stage_runtime_stop(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    with client.app.state.session_factory() as db:
        profile = db.get(ServerProfile, profile_id)
        assert profile is not None
        profile.start_with_host = True
        db.commit()

    started_profiles: list[str] = []

    async def fake_start(profile: ServerProfile) -> RuntimeSnapshot:
        started_profiles.append(profile.id)
        return RuntimeSnapshot(profile_id=profile.id, state="running")

    client.app.state.runtime_manager.start_profile = fake_start

    page = client.get("/host")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/host",
        data={
            "csrf_token": csrf,
            "action": "run-startup-roster",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert profile_id in started_profiles
    assert "Startup roster run complete: started 1." in response.text

    page = client.get("/host")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/host",
        data={
            "csrf_token": csrf,
            "action": "stage-runtime-stop",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Pending Runtime Maintenance" in response.text
    assert "sudo systemctl stop pzserverlauncher" in response.text
    assert client.app.state.host_shutdown_request is not None


def test_host_page_can_stop_all_managed_servers_with_confirmation(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    runtime_manager = client.app.state.runtime_manager
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="running")
    stopped_profiles: list[str] = []

    async def fake_stop(profile_id: str) -> RuntimeSnapshot:
        stopped_profiles.append(profile_id)
        runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="stopped")
        return runtime_manager._statuses[profile_id]

    runtime_manager.stop_profile = fake_stop

    page = client.get("/host")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/host",
        data={
            "csrf_token": csrf,
            "action": "stop-all",
            "confirm_stop_all": "STOP ALL",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert stopped_profiles == [profile_id]
    assert "Stopped 1 managed profile(s)." in response.text


def test_host_page_displays_launcher_update_status(client) -> None:
    bootstrap_owner(client)
    client.app.state.release_check_service = FakeReleaseCheckService(
        LauncherUpdateStatus(
            state="update_available",
            current_version="1.0.0",
            latest_version="1.1.0",
            release_title="PZServerLauncherLinux 1.1.0",
            release_page_url="https://github.com/Bentheck/PZServerLauncherLinux/releases/tag/v1.1.0",
            published_at_utc=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
            checked_at_utc=datetime(2026, 5, 5, 12, 5, 0, tzinfo=timezone.utc),
            status_message="Version 1.1.0 is available on GitHub. You're on 1.0.0.",
        )
    )

    page = client.get("/host")

    assert page.status_code == 200
    assert "Launcher Updates" in page.text
    assert "Update available" in page.text
    assert "Version 1.1.0" in page.text
    assert "Open Release Page" in page.text


def test_host_page_check_updates_forces_refresh(client) -> None:
    bootstrap_owner(client)
    service = FakeReleaseCheckService(
        LauncherUpdateStatus(
            state="up_to_date",
            current_version="1.0.0",
            latest_version="1.0.0",
            release_title="PZServerLauncherLinux 1.0.0",
            release_page_url="https://github.com/Bentheck/PZServerLauncherLinux/releases/tag/v1.0.0",
            published_at_utc=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
            checked_at_utc=datetime(2026, 5, 5, 12, 5, 0, tzinfo=timezone.utc),
            status_message="You're on 1.0.0. The latest stable release is 1.0.0.",
        ),
        LauncherUpdateStatus(
            state="update_available",
            current_version="1.0.0",
            latest_version="1.1.0",
            release_title="PZServerLauncherLinux 1.1.0",
            release_page_url="https://github.com/Bentheck/PZServerLauncherLinux/releases/tag/v1.1.0",
            published_at_utc=datetime(2026, 5, 5, 12, 10, 0, tzinfo=timezone.utc),
            checked_at_utc=datetime(2026, 5, 5, 12, 15, 0, tzinfo=timezone.utc),
            status_message="Version 1.1.0 is available on GitHub. You're on 1.0.0.",
        ),
    )
    client.app.state.release_check_service = service

    page = client.get("/host")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/host",
        data={
            "csrf_token": csrf,
            "action": "check-updates",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert service.calls == [False, True, False]
    assert "Version 1.1.0 is available on GitHub. You&#39;re on 1.0.0." in response.text


def test_host_page_renders_unavailable_update_state(client) -> None:
    bootstrap_owner(client)
    client.app.state.release_check_service = FakeReleaseCheckService(
        LauncherUpdateStatus(
            state="unavailable",
            current_version="1.0.0",
            latest_version=None,
            release_title=None,
            release_page_url="https://github.com/Bentheck/PZServerLauncherLinux/releases",
            published_at_utc=None,
            checked_at_utc=datetime(2026, 5, 5, 12, 20, 0, tzinfo=timezone.utc),
            status_message="Unable to check GitHub releases right now.",
        )
    )

    page = client.get("/host")

    assert page.status_code == 200
    assert "Check unavailable" in page.text
    assert "Version unavailable" in page.text


def test_remote_page_exposes_recommended_url_and_checklist(client) -> None:
    bootstrap_owner(client)

    page = client.get("/remote")
    assert page.status_code == 200
    assert "Recommended URL" in page.text
    assert "Deployment Posture" in page.text
    assert "Use docs/nginx.md as the base reverse proxy reference for this posture." in page.text


def test_users_page_can_update_user_and_reset_password(client) -> None:
    bootstrap_owner(client)

    page = client.get("/users")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/users",
        data={
            "csrf_token": csrf,
            "username": "operator1",
            "display_name": "Operator One",
            "password": "operator-password",
            "role": "Operator",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with client.app.state.session_factory() as db:
        created_user = db.scalar(select(User).where(User.username == "operator1"))
        assert created_user is not None
        user_id = created_user.id

    page = client.get("/users")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/users/{user_id}",
        data={
            "csrf_token": csrf,
            "display_name": "Operator Prime",
            "role": "Admin",
            "password": "operator-password-2",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    login_page = client.get("/login")
    login_csrf = extract_csrf(login_page.text)
    login_response = client.post(
        "/login",
        data={
            "csrf_token": login_csrf,
            "username": "operator1",
            "password": "operator-password-2",
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/dashboard"

    with client.app.state.session_factory() as db:
        updated_user = db.get(User, user_id)
        assert updated_user is not None
        assert updated_user.display_name == "Operator Prime"
        assert updated_user.role == "Admin"
        assert updated_user.is_active is True


def test_users_page_blocks_last_owner_demotion_and_inactive_login(client) -> None:
    bootstrap_owner(client)

    with client.app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "owner"))
        assert owner is not None
        owner_id = owner.id

    page = client.get("/users")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/users/{owner_id}",
        data={
            "csrf_token": csrf,
            "display_name": "Owner",
            "role": "Admin",
            "password": "",
            "is_active": "on",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Keep at least one active owner account available." in response.text

    page = client.get("/users")
    csrf = extract_csrf(page.text)
    client.post(
        "/users",
        data={
            "csrf_token": csrf,
            "username": "viewer1",
            "display_name": "Viewer One",
            "password": "viewer-password",
            "role": "Viewer",
        },
        follow_redirects=False,
    )
    with client.app.state.session_factory() as db:
        viewer = db.scalar(select(User).where(User.username == "viewer1"))
        assert viewer is not None
        viewer_id = viewer.id

    page = client.get("/users")
    csrf = extract_csrf(page.text)
    client.post(
        f"/users/{viewer_id}",
        data={
            "csrf_token": csrf,
            "display_name": "Viewer One",
            "role": "Viewer",
            "password": "",
        },
        follow_redirects=False,
    )

    client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    login_page = client.get("/login")
    login_csrf = extract_csrf(login_page.text)
    login_response = client.post(
        "/login",
        data={
            "csrf_token": login_csrf,
            "username": "viewer1",
            "password": "viewer-password",
        },
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert "This account is inactive." in login_response.text


def test_mods_maps_page_exposes_workshop_browser_preview_and_diagnostics(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    create_workshop_item(client, profile_id)

    page = client.get(f"/profiles/{profile_id}/mods-maps?q=Fancy&preview=123456789")
    assert page.status_code == 200
    assert "Workshop Browser" in page.text
    assert "Mods in the Editor" in page.text
    assert "Maps in the Editor" in page.text
    assert '<option value="map"' in page.text
    assert "Diagnostics" in page.text
    assert "steamcommunity.com/dev/apikey" in page.text
    assert "enter your server name or IP" in page.text
    assert "Fancy Pack" in page.text
    assert "FancyMod" in page.text
    assert "MapOne" in page.text
    assert "Filter the editor" in page.text
    assert "Remove from Editor" in page.text
    assert "data-mod-editor-filter" in page.text
    assert "data-remove-pack" in page.text
    assert "Queue Whole Pack" not in page.text


def test_mods_maps_save_live_auto_resolves_workshop_ids_from_local_content(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    create_workshop_item(client, profile_id)

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_live",
            "browser_query": "Fancy",
            "preview_workshop_id": "123456789",
            "workshop_ids_text": "",
            "mod_ids_text": "FancyMod",
            "map_ids_text": "MapOne",
            "preset_name": "",
            "preset_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/profiles/{profile_id}/mods-maps?q=Fancy&preview=123456789"

    ini_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver.ini"
    content = ini_path.read_text(encoding="utf-8")
    assert "WorkshopItems=123456789" in content
    assert "Mods=FancyMod" in content
    assert "Map=MapOne" in content


def test_mods_maps_draft_persists_queued_editor_values_until_live_save(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    response = client.post(
        f"/api/profiles/{profile_id}/mods-maps/draft",
        json={
            "workshop_ids": ["111111", "111111", "222222"],
            "mod_ids": ["ModA", "ModB"],
            "map_ids": ["MapOne"],
            "mod_items": [
                {
                    "mod_id": "ModB",
                    "mod_name": "Mod B Title",
                    "workshop_id": "222222",
                },
                {
                    "mod_id": "ModA",
                    "mod_name": "Mod A Title",
                    "workshop_id": "111111",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["workshop_ids"] == ["111111", "222222"]

    with client.app.state.session_factory() as db:
        draft = db.scalar(select(ModsMapsDraft).where(ModsMapsDraft.profile_id == profile_id))
        assert draft is not None
        assert draft.workshop_ids == "111111\n222222"
        draft_items = db.scalars(
            select(ModsMapsDraftItem)
            .where(ModsMapsDraftItem.profile_id == profile_id)
            .order_by(ModsMapsDraftItem.sort_order.asc())
        ).all()
        assert len(draft_items) == 2
        assert draft_items[0].mod_name == "Mod A Title"
        assert draft_items[0].mod_id == "ModA"
        assert draft_items[0].workshop_id == "111111"
        assert draft_items[0].is_active is True
        assert draft_items[0].sort_order == 0
        assert draft_items[1].mod_name == "Mod B Title"
        assert draft_items[1].mod_id == "ModB"
        assert draft_items[1].workshop_id == "222222"
        assert draft_items[1].is_active is True
        assert draft_items[1].sort_order == 1

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    assert page.status_code == 200
    assert "The Mods & Maps editor has unsaved draft changes" in page.text
    assert "<textarea name=\"workshop_ids_text\">111111\n222222</textarea>" in page.text
    assert "<textarea name=\"mod_ids_text\">ModA\nModB</textarea>" in page.text
    assert 'data-workshop-id="111111"' in page.text
    assert "Mod A Title" in page.text
    assert "Mod Name" in page.text
    assert "Mod ID" in page.text
    assert "Workshop ID" in page.text

    csrf = extract_csrf(page.text)
    response = client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_live",
            "browser_query": "",
            "browser_search_kind": "all",
            "preview_workshop_id": "",
            "workshop_ids_text": "111111\n222222",
            "mod_ids_text": "ModA\nModB",
            "map_ids_text": "MapOne",
            "preset_name": "",
            "preset_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with client.app.state.session_factory() as db:
        assert db.scalar(select(ModsMapsDraft).where(ModsMapsDraft.profile_id == profile_id)) is None
        assert db.scalars(select(ModsMapsDraftItem).where(ModsMapsDraftItem.profile_id == profile_id)).all() == []


def test_mods_maps_draft_keeps_inactive_mod_items_visible(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    response = client.post(
        f"/api/profiles/{profile_id}/mods-maps/draft",
        json={
            "workshop_ids": ["111111", "222222"],
            "mod_ids": ["ModA"],
            "map_ids": [],
            "mod_items": [
                {
                    "mod_id": "ModA",
                    "mod_name": "Active Mod",
                    "workshop_id": "111111",
                },
                {
                    "mod_id": "ModB",
                    "mod_name": "Inactive Mod",
                    "workshop_id": "222222",
                },
            ],
        },
    )
    assert response.status_code == 200

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    assert page.status_code == 200
    assert "Active Mod" in page.text
    assert "Inactive Mod" in page.text
    assert '<textarea name="mod_ids_text">ModA</textarea>' in page.text
    assert 'data-mod-id="ModB"' in page.text
    assert 'data-active="no"' in page.text


def test_mods_maps_draft_keeps_fully_inactive_mod_items_visible(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    response = client.post(
        f"/api/profiles/{profile_id}/mods-maps/draft",
        json={
            "workshop_ids": ["222222"],
            "mod_ids": [],
            "map_ids": [],
            "mod_items": [
                {
                    "mod_id": "ModB",
                    "mod_name": "Inactive Mod",
                    "workshop_id": "222222",
                },
            ],
        },
    )
    assert response.status_code == 200

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    assert page.status_code == 200
    assert "Inactive Mod" in page.text
    assert '<textarea name="mod_ids_text"></textarea>' in page.text
    assert 'data-mod-id="ModB"' in page.text
    assert 'data-workshop-id="222222"' in page.text
    assert 'data-active="no"' in page.text


def test_mods_maps_save_live_uses_table_active_mods_as_source_of_truth(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    response = client.post(
        f"/api/profiles/{profile_id}/mods-maps/draft",
        json={
            "workshop_ids": ["111111", "222222"],
            "mod_ids": ["ModA"],
            "map_ids": ["MapOne"],
            "mod_items": [
                {
                    "mod_id": "ModA",
                    "mod_name": "Active Mod",
                    "workshop_id": "111111",
                },
                {
                    "mod_id": "ModB",
                    "mod_name": "Inactive Mod",
                    "workshop_id": "222222",
                },
            ],
        },
    )
    assert response.status_code == 200

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_live",
            "browser_query": "",
            "browser_search_kind": "all",
            "preview_workshop_id": "",
            "workshop_ids_text": "111111\n222222",
            "mod_ids_text": "ModA\nModB",
            "map_ids_text": "MapOne",
            "preset_name": "",
            "preset_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    ini_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver.ini"
    content = ini_path.read_text(encoding="utf-8")
    assert "Mods=ModA" in content
    assert "ModB" not in content


def test_mods_maps_named_preset_backend_remains_compatible(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    csrf = extract_csrf(page.text)
    client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_preset",
            "preset_name": "Baseline Pack",
            "workshop_ids_text": "111111\n222222",
            "mod_ids_text": "ModA\nModB",
            "map_ids_text": "MapOne",
            "preset_id": "",
        },
    )

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    assert "Baseline Pack" not in page.text
    with client.app.state.session_factory() as db:
        preset = db.scalar(select(WorkshopPreset).where(WorkshopPreset.profile_id == profile_id))
        assert preset is not None
        preset_id = preset.id

    csrf = extract_csrf(page.text)
    client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_live",
            "preset_name": "",
            "workshop_ids_text": "999999",
            "mod_ids_text": "OtherMod",
            "map_ids_text": "OtherMap",
            "preset_id": "",
        },
    )

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    csrf = extract_csrf(page.text)
    client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "apply_preset",
            "preset_id": preset_id,
            "preset_name": "",
            "workshop_ids_text": "",
            "mod_ids_text": "",
            "map_ids_text": "",
        },
    )

    ini_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver.ini"
    content = ini_path.read_text(encoding="utf-8")
    assert "WorkshopItems=111111;222222" in content
    assert "Mods=ModA;ModB" in content
    assert "Map=MapOne" in content


def test_mods_maps_page_exposes_steam_search_and_collection_preview(client) -> None:
    class FakeWorkshopBrowserService:
        def search(self, profile, *, current_workshop_ids, current_mod_ids, current_map_ids, query, api_key, search_kind="all", required_tags=None, take=24):
            assert query == "collection pack"
            assert search_kind == "collection"
            assert required_tags == ["Build 42", "Map"]
            assert api_key == "steam-key-123"
            return WorkshopBrowserSearchResult(
                query=query,
                search_kind=search_kind,
                has_api_key=True,
                diagnostics=["Steam Workshop collection search is enabled with the configured Web API key."],
                items=[
                    WorkshopBrowserItem(
                        workshop_id="999888777",
                        title="Mega Collection",
                        description="A curated set of apocalypse essentials.",
                        preview_url="https://cdn.example.com/collection.png",
                        source="steam-collection",
                        source_label="Steam collection",
                        kind="collection",
                        kind_label="Collection",
                        is_installed_locally=False,
                        is_queued=False,
                        mod_ids=[],
                        map_ids=[],
                        child_workshop_ids=["111", "222"],
                        collection_item_count=2,
                        tags=["collection", "hardcore", "Build 42", "Map"],
                    )
                ],
            )

        def get_preview(self, profile, *, current_workshop_ids, current_mod_ids, current_map_ids, workshop_id, api_key):
            assert workshop_id == "999888777"
            return WorkshopBrowserPreview(
                item=WorkshopBrowserItem(
                    workshop_id="999888777",
                    title="Mega Collection",
                    description="A curated set of apocalypse essentials.",
                    preview_url="https://cdn.example.com/collection.png",
                    source="steam-collection",
                    source_label="Steam collection",
                    kind="collection",
                    kind_label="Collection",
                    is_installed_locally=False,
                    is_queued=False,
                    mod_ids=["MegaMod", "WeatherMod"],
                    map_ids=["MegaMap"],
                    child_workshop_ids=["111", "222"],
                    collection_item_count=2,
                    tags=["collection", "hardcore"],
                ),
                workshop_ids_to_add=["111", "222"],
                mod_ids_to_add=["MegaMod", "WeatherMod"],
                map_ids_to_add=["MegaMap"],
                collection_children=[
                    WorkshopBrowserPreviewChild("111", "Child One", True, False),
                    WorkshopBrowserPreviewChild("222", "Child Two", False, False),
                ],
                dependency_children=[],
                dependency_workshop_ids_to_add=[],
                dependency_mod_ids_to_add=[],
                dependency_map_ids_to_add=[],
                mod_names_by_id={"MegaMod": "Mega Collection", "WeatherMod": "Mega Collection"},
            )

    bootstrap_owner(client)
    profile_id = create_profile(client)
    client.app.state.workshop_browser_service = FakeWorkshopBrowserService()
    with client.app.state.session_factory() as db:
        host_settings = db.get(HostSettings, 1)
        if host_settings is None:
            host_settings = HostSettings(
                id=1,
                bind_host=client.app.state.settings.bind_host,
                bind_port=client.app.state.settings.bind_port,
                data_root=str(client.app.state.settings.data_root),
                logs_root=str(client.app.state.settings.logs_root),
                server_user=client.app.state.settings.default_server_user,
            )
            db.add(host_settings)
        host_settings.steam_web_api_key = "steam-key-123"
        db.commit()

    page = client.get(f"/profiles/{profile_id}/mods-maps?q=collection pack&kind=collection&tags=Build%2042,Map&preview=999888777")
    assert page.status_code == 200
    assert "Local Cache and Steam Search" in page.text
    assert "Steam Workshop collection search is enabled with the configured Web API key." in page.text
    assert '<option value="collection" selected>Collections only</option>' in page.text
    assert "/mods-maps?q=collection%20pack&kind=collection&tags=Build%2042%2CMap&preview=999888777" in page.text
    assert 'value="Build 42"' in page.text
    assert 'value="Map"' in page.text
    assert page.text.count("checked") >= 2
    assert "Mega Collection" in page.text
    assert "Add Collection To Editor" in page.text
    assert "Remove from Editor" in page.text
    assert "Add Mod IDs And Workshop" not in page.text
    assert "Add Maps And Workshop" not in page.text
    assert "Child One" in page.text
    assert "Child Two" in page.text
    assert 'data-workshop-ids="111||222"' in page.text
    assert 'data-remove-pack' in page.text
    assert 'data-queue-mods' not in page.text
    assert 'data-queue-maps' not in page.text


def test_mods_maps_admin_can_save_and_clear_steam_workshop_api_key(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    page = client.get(f"/profiles/{profile_id}/mods-maps")
    csrf = extract_csrf(page.text)
    response = client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "save_workshop_api_key",
            "steam_web_api_key": "steam-key-456",
            "browser_query": "collection",
            "browser_search_kind": "collection",
            "preview_workshop_id": "999",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/profiles/{profile_id}/mods-maps?q=collection&kind=collection&preview=999"

    with client.app.state.session_factory() as db:
        host_settings = db.get(HostSettings, 1)
        assert host_settings is not None
        assert host_settings.steam_web_api_key == "steam-key-456"

    response = client.post(
        f"/profiles/{profile_id}/mods-maps",
        data={
            "csrf_token": csrf,
            "action": "clear_workshop_api_key",
            "browser_query": "collection",
            "browser_search_kind": "collection",
            "preview_workshop_id": "999",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with client.app.state.session_factory() as db:
        host_settings = db.get(HostSettings, 1)
        assert host_settings is not None
        assert host_settings.steam_web_api_key is None


def test_sandbox_settings_persist_to_lua(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox",
        data={
            "csrf_token": csrf,
            "day_length": "27",
            "time_since_apo": "6",
            "start_month": "10",
            "start_day": "17",
            "start_time": "6",
            "water_shut": "9",
            "electricity_shut": "8",
            "zombies": "6",
            "distribution": "2",
            "zombie_respawn": "1",
            "zombie_migration": "on",
            "zombie_lore_speed": "1",
            "zombie_lore_transmission": "4",
            "zombie_lore_memory": "4",
            "zombie_lore_sight": "3",
            "zombie_lore_hearing": "2",
            "drag_down": "",
            "population_multiplier": "0.15",
            "population_start_multiplier": "0.5",
            "population_peak_multiplier": "2.0",
            "population_peak_day": "42",
            "respawn_hours": "48.0",
            "respawn_multiplier": "0.25",
            "hours_for_loot_respawn": "168",
            "construction_prevents_loot_respawn": "on",
            "temperature": "5",
            "rain": "1",
            "nature_abundance": "4",
            "alarm": "2",
            "helicopter": "4",
            "meta_event": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    sandbox_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver_SandboxVars.lua"
    content = sandbox_path.read_text(encoding="utf-8")
    assert "DayLength = 27," in content
    assert "WaterShut = 9," in content
    assert "ElecShut = 8," in content
    assert "Zombies = 6," in content
    assert "ZombieRespawn = 1," in content
    assert "Temperature = 5," in content
    assert "MetaEvent = 1," in content
    assert "ZombieLore = {" in content
    assert "Transmission = 4," in content
    assert "ZombiesDragDown = false," in content
    assert "ZombieConfig = {" in content
    assert "PopulationPeakDay = 42," in content
    assert "RespawnMultiplier = 0.25," in content


def test_sandbox_page_exposes_windows_catalog_sections(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    assert sandbox_page.status_code == 200
    assert "Advanced zombie settings" in sandbox_page.text
    assert "Loot rarity" in sandbox_page.text
    assert "In-game Map" in sandbox_page.text
    assert "XP multipliers" in sandbox_page.text
    assert "Livestock" in sandbox_page.text
    assert "Windows Catalog Overview" in sandbox_page.text


def test_sandbox_preset_apply_stays_in_draft_until_live_save(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    assert "Six Months Later (Shipped)" in sandbox_page.text
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox/presets",
        data={
            "csrf_token": csrf,
            "action": "apply_preset",
            "sandbox_preset_id": "builtin:SixMonthsLater",
            "preset_id": "builtin:SixMonthsLater",
            "sandbox_search": "",
            "sandbox_category": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    draft = get_sandbox_draft(client, profile_id)
    assert draft is not None
    payload = json.loads(draft.payload_json)
    assert payload["time_since_apo"] == "7"
    assert payload["start_month"] == "12"

    sandbox_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver_SandboxVars.lua"
    content = sandbox_path.read_text(encoding="utf-8")
    assert "TimeSinceApo = 1," in content

    sandbox_page = client.get(response.headers["location"])
    assert "The editor is showing draft values" in sandbox_page.text
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox",
        data={
            "csrf_token": csrf,
            "sandbox_preset_id": "builtin:SixMonthsLater",
            "preset_id": "builtin:SixMonthsLater",
            "sandbox_search": "",
            "sandbox_category": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert get_sandbox_draft(client, profile_id) is None

    content = sandbox_path.read_text(encoding="utf-8")
    assert "TimeSinceApo = 7," in content
    assert "StartMonth = 12," in content


def test_sandbox_custom_preset_can_be_saved_and_deleted(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox/presets",
        data={
            "csrf_token": csrf,
            "action": "save_custom_preset",
            "custom_preset_name": "Harsh Winter",
            "sandbox_preset_id": "builtin:Apocalypse",
            "preset_id": "builtin:Apocalypse",
            "sandbox_search": "",
            "sandbox_category": "",
            "time_since_apo": "9",
            "temperature": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    custom_path = client.app.state.settings.data_root / "sandbox-presets" / "b42" / "custom" / "Harsh Winter.lua"
    assert custom_path.exists()
    content = custom_path.read_text(encoding="utf-8")
    assert "TimeSinceApo = 9," in content
    assert "Temperature = 1," in content

    sandbox_page = client.get(response.headers["location"])
    assert "Harsh Winter (Custom)" in sandbox_page.text
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox/presets",
        data={
            "csrf_token": csrf,
            "action": "delete_custom_preset",
            "sandbox_preset_id": "user:Harsh Winter",
            "preset_id": "user:Harsh Winter",
            "sandbox_search": "",
            "sandbox_category": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not custom_path.exists()


def test_sandbox_world_reset_removes_world_and_updates_ini(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    csrf = extract_csrf(sandbox_page.text)
    cache_root = Path(client.app.state.settings.servers_root) / profile_id / "cache"
    world_path = cache_root / "Saves" / "Multiplayer" / "mainserver"
    world_path.mkdir(parents=True, exist_ok=True)
    (world_path / "map_t.bin").write_text("world-data", encoding="utf-8")

    ini_path = cache_root / "Server" / "mainserver.ini"
    ini_path.write_text("ResetID=4\nSeed=oldseed\n", encoding="utf-8")

    response = client.post(
        f"/profiles/{profile_id}/sandbox/reset-world",
        data={
            "csrf_token": csrf,
            "sandbox_preset_id": "builtin:Apocalypse",
            "preset_id": "builtin:Apocalypse",
            "sandbox_search": "",
            "sandbox_category": "",
            "create_backup_before_reset": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not world_path.exists()

    content = ini_path.read_text(encoding="utf-8")
    assert "ResetID=5" in content
    assert "Seed=oldseed" not in content
    assert "Seed=" in content

    backups = list((client.app.state.settings.backups_root / profile_id).glob("*.tar.gz"))
    assert backups


def test_backup_restore_restores_install_and_cache(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)

    install_path = Path(profile.install_directory)
    cache_path = Path(profile.cache_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)
    (install_path / "restore-me.txt").write_text("before-backup", encoding="utf-8")
    (cache_path / "Server").mkdir(parents=True, exist_ok=True)
    (cache_path / "Server" / "mainserver.ini").write_text("ResetID=2\nSeed=before\n", encoding="utf-8")

    backup_path = client.app.state.zomboid_service.create_backup(profile)

    (install_path / "restore-me.txt").write_text("after-backup", encoding="utf-8")
    (cache_path / "Server" / "mainserver.ini").write_text("ResetID=9\nSeed=after\n", encoding="utf-8")

    backups_page = client.get(f"/profiles/{profile_id}/backups")
    csrf = extract_csrf(backups_page.text)
    response = client.post(
        f"/profiles/{profile_id}/backups/restore",
        data={
            "csrf_token": csrf,
            "backup_name": backup_path.name,
            "create_backup_before_restore": "on",
            "stop_before_restore": "on",
            "confirmation_text": "RESTORE",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert (install_path / "restore-me.txt").read_text(encoding="utf-8") == "before-backup"
    assert (cache_path / "Server" / "mainserver.ini").read_text(encoding="utf-8") == "ResetID=2\nSeed=before\n"
    backups = list((client.app.state.settings.backups_root / profile_id).glob("*.tar.gz"))
    assert len(backups) == 2


def test_backup_restore_can_stop_and_restart_managed_server(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)

    install_path = Path(profile.install_directory)
    cache_path = Path(profile.cache_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)
    (install_path / "restore-me.txt").write_text("stable-snapshot", encoding="utf-8")
    backup_path = client.app.state.zomboid_service.create_backup(profile)
    (install_path / "restore-me.txt").write_text("mutated", encoding="utf-8")

    runtime_manager = client.app.state.runtime_manager
    calls: list[tuple[str, str]] = []

    async def fake_stop(profile_id_value: str):
        calls.append(("stop", profile_id_value))
        snapshot = RuntimeSnapshot(profile_id=profile_id_value, state="stopped")
        runtime_manager._statuses[profile_id_value] = snapshot
        return snapshot

    async def fake_start(profile_value):
        calls.append(("start", profile_value.id))
        snapshot = RuntimeSnapshot(profile_id=profile_value.id, state="running")
        runtime_manager._statuses[profile_value.id] = snapshot
        return snapshot

    runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="running")
    runtime_manager.stop_profile = fake_stop  # type: ignore[method-assign]
    runtime_manager.start_profile = fake_start  # type: ignore[method-assign]

    backups_page = client.get(f"/profiles/{profile_id}/backups")
    csrf = extract_csrf(backups_page.text)
    response = client.post(
        f"/profiles/{profile_id}/backups/restore",
        data={
            "csrf_token": csrf,
            "backup_name": backup_path.name,
            "stop_before_restore": "on",
            "restart_after_restore": "on",
            "confirmation_text": "RESTORE",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == [("stop", profile_id), ("start", profile_id)]
    assert (install_path / "restore-me.txt").read_text(encoding="utf-8") == "stable-snapshot"


def test_update_job_can_create_preupdate_backup_and_restart_runtime(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)
    profile = get_profile(client, profile_id)
    install_path = Path(profile.install_directory)
    cache_path = Path(profile.cache_directory)
    install_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)

    runtime_manager = client.app.state.runtime_manager
    runtime_manager._statuses[profile_id] = RuntimeSnapshot(profile_id=profile_id, state="running")
    calls: list[tuple[str, str]] = []

    async def fake_run_command(command, *, working_directory, on_output):
        calls.append(("run", str(working_directory)))
        await on_output("SteamCMD says hello")
        return 0

    def fake_create_backup(profile_value, *, prune=True):
        calls.append(("backup", profile_value.id))
        path = client.app.state.settings.backups_root / profile_value.id / "pre-update-test.tar.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"backup")
        return path

    async def fake_stop(profile_id_value: str):
        calls.append(("stop", profile_id_value))
        snapshot = RuntimeSnapshot(profile_id=profile_id_value, state="stopped")
        runtime_manager._statuses[profile_id_value] = snapshot
        return snapshot

    async def fake_start(profile_value):
        calls.append(("start", profile_value.id))
        snapshot = RuntimeSnapshot(profile_id=profile_value.id, state="running")
        runtime_manager._statuses[profile_value.id] = snapshot
        return snapshot

    client.app.state.zomboid_service.run_command = fake_run_command  # type: ignore[method-assign]
    client.app.state.zomboid_service.create_backup = fake_create_backup  # type: ignore[method-assign]
    runtime_manager.stop_profile = fake_stop  # type: ignore[method-assign]
    runtime_manager.start_profile = fake_start  # type: ignore[method-assign]

    job = runtime_manager._create_job(
        kind="update",
        profile_id=profile.id,
        summary=f"Update {profile.display_name}",
        actor_user_id=None,
    )
    asyncio.run(
        runtime_manager._run_install_job(
            job.id,
            profile.id,
            InstallJobOptions(
                update=True,
                create_backup_before_update=True,
                stop_server_before_update=True,
                restart_after_completion=True,
            ),
        )
    )

    assert calls[:4] == [
        ("stop", profile_id),
        ("backup", profile_id),
        ("run", str(install_path)),
        ("start", profile_id),
    ]

    with client.app.state.session_factory() as db:
        saved_job = db.get(type(job), job.id)
        assert saved_job is not None
        assert saved_job.status == "succeeded"
        assert saved_job.progress_percent == 100
        assert "Pre-update backup: pre-update-test.tar.gz." in saved_job.detail
        assert "Managed runtime was stopped before the update." in saved_job.detail
        assert "Runtime state after job: running." in saved_job.detail


def test_sandbox_textareas_and_nested_sections_persist(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox",
        data={
            "csrf_token": csrf,
            "world_item_removal_list": 'Base.Hat, Base.CustomItem, Base.Toolbox"',
            "loot_item_removal_list": "Base.Banjo, Base.MugWhite",
            "allow_world_map": "on",
            "allow_mini_map": "on",
            "xp_global_multiplier": "2.5",
            "xp_use_global_multiplier": "on",
            "vehicle_easy_use": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    sandbox_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver_SandboxVars.lua"
    content = sandbox_path.read_text(encoding="utf-8")
    assert 'WorldItemRemovalList = "Base.Hat, Base.CustomItem, Base.Toolbox\\\"",' in content
    assert 'LootItemRemovalList = "Base.Banjo, Base.MugWhite",' in content
    assert "Map = {" in content
    assert "AllowWorldMap = true," in content
    assert "AllowMiniMap = true," in content
    assert "MultiplierConfig = {" in content
    assert "Global = 2.5," in content
    assert "GlobalToggle = true," in content
    assert "VehicleEasyUse = true," in content

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    assert "Base.Hat, Base.CustomItem, Base.Toolbox" in sandbox_page.text
    assert "Base.Banjo, Base.MugWhite" in sandbox_page.text


def test_sandbox_editor_preserves_unknown_entries(client) -> None:
    bootstrap_owner(client)
    profile_id = create_profile(client)

    sandbox_page = client.get(f"/profiles/{profile_id}/sandbox")
    sandbox_path = Path(client.app.state.settings.servers_root) / profile_id / "cache" / "Server" / "mainserver_SandboxVars.lua"
    sandbox_path.write_text(
        "\n".join(
            [
                "SandboxVars = {",
                "    VERSION = 5,",
                "    DayLength = 4,",
                "    CustomTopLevel = 77,",
                "    ZombieLore = {",
                "        Speed = 3,",
                "        MysteryToggle = true,",
                "    },",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    csrf = extract_csrf(sandbox_page.text)
    response = client.post(
        f"/profiles/{profile_id}/sandbox",
        data={
            "csrf_token": csrf,
            "day_length": "12",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    content = sandbox_path.read_text(encoding="utf-8")
    assert "DayLength = 12," in content
    assert "CustomTopLevel = 77," in content
    assert "MysteryToggle = true," in content
