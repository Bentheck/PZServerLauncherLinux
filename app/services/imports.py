from __future__ import annotations

import os
import re
from hashlib import sha1
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.config import Settings
from app.models import ServerProfile
from app.security import slugify
from app.services.config_files import FlatIniDocument, ProjectZomboidConfigService


def _parse_list(value: str | None, *, allow_comma_fallback: bool = False) -> list[str]:
    if not value:
        return []

    separator = ";"
    if allow_comma_fallback and "," in value and ";" not in value:
        separator = ","

    seen: set[str] = set()
    values: list[str] = []
    for raw_item in value.split(separator):
        item = raw_item.strip()
        if not item:
            continue

        lowered = item.lower()
        if lowered in seen:
            continue

        seen.add(lowered)
        values.append(item)

    return values


def _parse_int(value: str | None, fallback: int) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return fallback


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _iter_dirs(path: Path) -> list[Path]:
    try:
        return [item for item in path.iterdir() if item.is_dir()]
    except OSError:
        return []


def _glob_files(path: Path, pattern: str) -> list[Path]:
    try:
        return list(path.glob(pattern))
    except OSError:
        return []


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    candidate_id: str
    display_name: str
    server_name: str
    cache_directory: str
    install_directory: str | None
    branch: str
    workshop_ids: list[str]
    mod_ids: list[str]
    map_ids: list[str]
    diagnostics: list[str]
    is_already_imported: bool
    can_import: bool
    matching_profile_id: str | None
    matching_profile_display_name: str | None
    default_port: int
    udp_port: int
    max_players: int
    preferred_memory_gb: int
    bind_ip: str
    start_with_host: bool
    auto_restart_on_crash: bool
    public_name: str


@dataclass(frozen=True, slots=True)
class InstallProbe:
    path: Path
    branch: str


class LocalServerImportService:
    def __init__(
        self,
        settings: Settings,
        config_service: ProjectZomboidConfigService,
        *,
        cache_roots: Iterable[Path] | None = None,
        install_directories: Iterable[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.config_service = config_service
        self._cache_roots_override = [Path(item) for item in cache_roots] if cache_roots is not None else None
        self._install_directories_override = [Path(item) for item in install_directories] if install_directories is not None else None

    def discover(self, existing_profiles: list[ServerProfile]) -> list[ImportCandidate]:
        existing_by_key = {
            self._existing_key(profile.server_name, Path(profile.cache_directory)): profile
            for profile in existing_profiles
        }
        existing_by_server_name = {
            profile.server_name.strip().lower(): profile
            for profile in existing_profiles
        }

        install_probes = self._discover_install_probes()
        candidates: list[ImportCandidate] = []
        for cache_root in self._discover_cache_roots():
            server_directory = cache_root / "Server"
            if not _path_exists(server_directory):
                continue

            for ini_path in sorted(_glob_files(server_directory, "*.ini"), key=lambda path: path.name.lower()):
                server_name = ini_path.stem.strip()
                if not server_name:
                    continue

                try:
                    document = FlatIniDocument.parse(ini_path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue

                public_name = (document.get("PublicName", "") or "").strip()
                workshop_ids = _parse_list(document.get("WorkshopItems", ""), allow_comma_fallback=True)
                mod_ids = _parse_list(document.get("Mods", ""))
                map_ids = _parse_list(document.get("Map", ""))
                exact_match = existing_by_key.get(self._existing_key(server_name, cache_root))
                matching_profile_id = exact_match.id if exact_match is not None else None
                matching_profile_display_name = exact_match.display_name if exact_match is not None else None
                can_import = exact_match is None
                diagnostics: list[str] = []

                if exact_match is not None:
                    diagnostics.append(f"Already managed by profile {exact_match.display_name}.")

                conflicting_profile = existing_by_server_name.get(server_name.lower())
                if conflicting_profile is not None and exact_match is None:
                    can_import = False
                    diagnostics.append(
                        f"Server name '{server_name}' is already used by managed profile {conflicting_profile.display_name}."
                    )

                install_probe, install_note = self._select_install_probe(cache_root, install_probes)
                install_directory = str(install_probe.path) if install_probe is not None else None
                branch = install_probe.branch if install_probe is not None else "stable"
                diagnostics.append(install_note)

                if workshop_ids or mod_ids or map_ids:
                    diagnostics.append(
                        f"Existing config includes {len(workshop_ids)} workshop ID(s), {len(mod_ids)} mod ID(s), and {len(map_ids)} map folder(s)."
                    )

                if install_directory is not None:
                    probe_profile = ServerProfile(
                        id="import-probe",
                        display_name=public_name or self._humanize_name(server_name),
                        server_name=server_name,
                        install_directory=install_directory,
                        cache_directory=str(cache_root),
                        branch=branch,
                    )
                    validation = self.config_service.build_mods_maps_validation(
                        probe_profile,
                        workshop_ids,
                        mod_ids,
                        map_ids,
                    )
                    diagnostics.extend(validation.diagnostics)

                default_port = _parse_int(document.get("DefaultPort", ""), 16261)
                fallback_udp_port = default_port + 1 if default_port < 65535 else 65534
                candidates.append(
                    ImportCandidate(
                        candidate_id=self._candidate_id(server_name, cache_root),
                        display_name=public_name or self._humanize_name(server_name),
                        server_name=server_name,
                        cache_directory=str(cache_root),
                        install_directory=install_directory,
                        branch=branch,
                        workshop_ids=workshop_ids,
                        mod_ids=mod_ids,
                        map_ids=map_ids,
                        diagnostics=diagnostics,
                        is_already_imported=exact_match is not None,
                        can_import=can_import,
                        matching_profile_id=matching_profile_id,
                        matching_profile_display_name=matching_profile_display_name,
                        default_port=default_port,
                        udp_port=_parse_int(document.get("UDPPort", ""), fallback_udp_port),
                        max_players=_parse_int(document.get("MaxPlayers", ""), 8),
                        preferred_memory_gb=_parse_int(document.get("PreferredMemoryInGigabytes", ""), 6),
                        bind_ip=(document.get("BindIP", "") or "0.0.0.0").strip() or "0.0.0.0",
                        start_with_host=(document.get("StartWithHost", "") or "").strip().lower() in {"1", "true", "yes", "on"},
                        auto_restart_on_crash=(document.get("AutoRestartOnCrash", "") or "").strip().lower() in {"1", "true", "yes", "on"},
                        public_name=public_name,
                    )
                )

        return sorted(candidates, key=lambda item: (item.display_name.lower(), item.server_name.lower(), item.cache_directory.lower()))

    def get_candidate(self, candidate_id: str, existing_profiles: list[ServerProfile]) -> ImportCandidate | None:
        normalized = candidate_id.strip()
        if normalized == "":
            return None

        for candidate in self.discover(existing_profiles):
            if candidate.candidate_id == normalized:
                return candidate

        return None

    def import_candidate(self, candidate_id: str, existing_profiles: list[ServerProfile]) -> ServerProfile:
        candidate = self.get_candidate(candidate_id, existing_profiles)
        if candidate is None:
            raise ValueError("The selected import candidate could not be found.")
        if candidate.is_already_imported:
            raise ValueError(f"{candidate.display_name} is already imported.")
        if not candidate.can_import:
            raise ValueError(f"{candidate.display_name} cannot be imported until the server name conflict is resolved.")

        existing_ids = {profile.id.lower() for profile in existing_profiles}
        profile_id = self._ensure_unique_profile_id(candidate.display_name or candidate.server_name, existing_ids)
        install_directory = candidate.install_directory or str(self.settings.servers_root / profile_id / "install")
        profile = ServerProfile(
            id=profile_id,
            display_name=candidate.display_name,
            server_name=candidate.server_name,
            install_directory=install_directory,
            cache_directory=candidate.cache_directory,
            branch=candidate.branch,
            preferred_memory_gb=candidate.preferred_memory_gb,
            max_players=candidate.max_players,
            default_port=candidate.default_port,
            udp_port=candidate.udp_port,
            bind_ip=candidate.bind_ip,
            use_steam=True,
            start_with_host=candidate.start_with_host,
            auto_restart_on_crash=candidate.auto_restart_on_crash,
        )
        return profile

    def _discover_cache_roots(self) -> list[Path]:
        if self._cache_roots_override is not None:
            return self._dedupe_paths(self._cache_roots_override)

        if os.name != "nt":
            return []

        roots: list[Path] = [Path.home() / "Zomboid"]

        servers_root = self.settings.servers_root
        if _path_exists(servers_root):
            for profile_root in sorted(_iter_dirs(servers_root), key=lambda path: path.name.lower()):
                roots.append(profile_root / "cache")

        return self._dedupe_paths(roots)

    def _discover_install_probes(self) -> list[InstallProbe]:
        if self._install_directories_override is not None:
            probes = [
                InstallProbe(path=path, branch=self._detect_branch(path))
                for path in self._install_directories_override
                if self._looks_like_install(path)
            ]
            return self._dedupe_install_probes(probes)

        if os.name != "nt":
            return []

        candidate_paths: list[Path] = []
        servers_root = self.settings.servers_root
        if _path_exists(servers_root):
            for profile_root in sorted(_iter_dirs(servers_root), key=lambda path: path.name.lower()):
                candidate_paths.append(profile_root / "install")

        candidate_paths.extend(self._common_install_paths())
        probes = [
            InstallProbe(path=path, branch=self._detect_branch(path))
            for path in candidate_paths
            if self._looks_like_install(path)
        ]
        return self._dedupe_install_probes(probes)

    def _common_install_paths(self) -> list[Path]:
        relative_paths = [
            Path("Steam") / "steamapps" / "common" / "Project Zomboid Dedicated Server",
            Path(".steam") / "steam" / "steamapps" / "common" / "Project Zomboid Dedicated Server",
            Path(".local") / "share" / "Steam" / "steamapps" / "common" / "Project Zomboid Dedicated Server",
        ]
        roots = [Path.home()]
        if os.name != "nt":
            homes_root = Path("/home")
            if _path_exists(homes_root):
                roots.extend(sorted(_iter_dirs(homes_root), key=lambda path: path.name.lower()))
            roots.append(Path("/root"))

        results: list[Path] = []
        for root in roots:
            for relative_path in relative_paths:
                results.append(root / relative_path)
        return results

    def _select_install_probe(self, cache_root: Path, global_probes: list[InstallProbe]) -> tuple[InstallProbe | None, str]:
        sibling_install = cache_root.parent / "install"
        if self._looks_like_install(sibling_install):
            probe = InstallProbe(path=sibling_install, branch=self._detect_branch(sibling_install))
            return probe, f"Detected a colocated dedicated server install at {probe.path}."

        if len(global_probes) == 1:
            probe = global_probes[0]
            return probe, f"Detected a dedicated server install at {probe.path}."

        if len(global_probes) > 1:
            return (
                None,
                "Multiple dedicated server installs were detected. Import will stage this profile in the managed install folder until you choose the correct install path.",
            )

        return (
            None,
            "No dedicated server install was detected. Import will point to the launcher's managed install folder until you run install or update.",
        )

    @staticmethod
    def _existing_key(server_name: str, cache_root: Path) -> str:
        return f"{LocalServerImportService._normalize_path(cache_root)}|{server_name.strip().lower()}"

    @staticmethod
    def _candidate_id(server_name: str, cache_root: Path) -> str:
        cache_token = LocalServerImportService._normalize_path(cache_root)
        digest = sha1(cache_token.encode("utf-8")).hexdigest()[:10]
        return f"{slugify(server_name)}-{digest}"

    @staticmethod
    def _ensure_unique_profile_id(base_name: str, existing_ids: set[str]) -> str:
        base_id = slugify(base_name)
        candidate = base_id
        suffix = 2
        while candidate.lower() in existing_ids:
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _normalize_path(path: Path) -> str:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError:
            resolved = path.expanduser()
        return os.path.normcase(str(resolved)).rstrip("\\/")

    @staticmethod
    def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
        results: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            normalized = LocalServerImportService._normalize_path(Path(path))
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(Path(path))
        return results

    @staticmethod
    def _dedupe_install_probes(probes: Iterable[InstallProbe]) -> list[InstallProbe]:
        results: list[InstallProbe] = []
        seen: set[str] = set()
        for probe in probes:
            normalized = LocalServerImportService._normalize_path(probe.path)
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(probe)
        return results

    @staticmethod
    def _looks_like_install(path: Path) -> bool:
        return _path_exists(path / "start-server.sh") or _path_exists(path / "StartServer.sh")

    @staticmethod
    def _detect_branch(install_directory: Path) -> str:
        manifest_candidates = [
            install_directory.parent.parent / "appmanifest_380870.acf",
            install_directory.parent / "appmanifest_380870.acf",
        ]
        for manifest_path in manifest_candidates:
            if not _path_exists(manifest_path):
                continue
            try:
                content = manifest_path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            if re.search(r'"betakey"\s+"unstable"', content):
                return "unstable"
        return "stable"

    @staticmethod
    def _humanize_name(server_name: str) -> str:
        parts = [part for part in re.split(r"[-_]+", server_name.strip()) if part]
        if not parts:
            return "Imported Server"
        return " ".join(part[:1].upper() + part[1:] for part in parts)
