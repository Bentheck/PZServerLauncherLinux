from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ServerProfile, WorkshopPreset


def _bool_to_ini(value: bool) -> str:
    return "true" if value else "false"


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _parse_list(value: str | None, *, allow_comma_fallback: bool = False) -> list[str]:
    if not value:
        return []

    separators = [";"]
    if allow_comma_fallback and "," in value and ";" not in value:
        separators = [","]

    current = value
    for separator in separators:
        items = [piece.strip() for piece in current.split(separator)]
        current = "\n".join(items)

    seen: set[str] = set()
    values: list[str] = []
    for raw in current.replace("\r\n", "\n").split("\n"):
        item = raw.strip()
        if not item:
            continue

        lowered = item.lower()
        if lowered in seen:
            continue

        seen.add(lowered)
        values.append(item)

    return values


def _join_list(values: Iterable[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue

        lowered = normalized.lower()
        if lowered in seen:
            continue

        seen.add(lowered)
        cleaned.append(normalized)

    return ";".join(cleaned)


def _parse_multiline_csv(value: str | None) -> str:
    if not value:
        return ""

    pieces = [piece.strip() for piece in value.replace(";", ",").split(",")]
    return "\n".join(piece for piece in pieces if piece)


def _join_multiline_csv(value: str | None) -> str:
    if not value:
        return ""

    pieces: list[str] = []
    seen: set[str] = set()
    for raw_line in value.replace("\r\n", "\n").split("\n"):
        for piece in raw_line.split(","):
            normalized = piece.strip()
            if not normalized:
                continue

            lowered = normalized.lower()
            if lowered in seen:
                continue

            seen.add(lowered)
            pieces.append(normalized)

    return ",".join(pieces)


def _decode_pz_multiline(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("<LINE>", "\n")


def _encode_pz_multiline(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\n", "<LINE>").strip()


@dataclass(slots=True)
class IniLine:
    kind: str
    raw: str = ""
    key: str = ""
    value: str = ""


class FlatIniDocument:
    def __init__(self, lines: list[IniLine]) -> None:
        self.lines = lines

    @classmethod
    def parse(cls, content: str) -> "FlatIniDocument":
        lines: list[IniLine] = []
        for raw_line in content.replace("\r\n", "\n").split("\n"):
            stripped = raw_line.strip()
            if stripped == "":
                lines.append(IniLine(kind="blank", raw=""))
                continue

            if stripped.startswith(("#", ";", "//")):
                lines.append(IniLine(kind="comment", raw=raw_line))
                continue

            if "=" in raw_line:
                key, value = raw_line.split("=", 1)
                lines.append(IniLine(kind="entry", key=key.strip(), value=value))
                continue

            lines.append(IniLine(kind="other", raw=raw_line))

        return cls(lines)

    def get(self, key: str, default: str | None = None) -> str | None:
        for line in self.lines:
            if line.kind == "entry" and line.key.lower() == key.lower():
                return line.value

        return default

    def set(self, key: str, value: str) -> None:
        for line in self.lines:
            if line.kind == "entry" and line.key.lower() == key.lower():
                line.key = key
                line.value = value
                return

        self.lines.append(IniLine(kind="entry", key=key, value=value))

    def to_text(self) -> str:
        rendered: list[str] = []
        for line in self.lines:
            if line.kind == "entry":
                rendered.append(f"{line.key}={line.value}")
            elif line.kind in {"comment", "other"}:
                rendered.append(line.raw)
            else:
                rendered.append("")

        return "\n".join(rendered).rstrip() + "\n"


@dataclass(slots=True)
class NamedPresetView:
    id: str
    name: str
    workshop_ids: list[str]
    mod_ids: list[str]
    map_ids: list[str]


@dataclass(slots=True)
class WorkshopScanResult:
    workshop_ids: list[str]
    mod_ids: list[str]
    map_ids: list[str]
    diagnostics: list[str]


@dataclass(slots=True)
class WorkshopCatalogItem:
    workshop_id: str
    title: str
    mod_ids: list[str]
    map_ids: list[str]
    mod_dependencies: dict[str, list[str]]
    source_path: str


@dataclass(slots=True)
class ModsMapsDiagnosticBucket:
    label: str
    tone: str
    items: list[str]


@dataclass(slots=True)
class ModsMapsValidationResult:
    catalog_count: int
    installed_mod_count: int
    installed_map_count: int
    resolved_workshop_ids: list[str]
    missing_workshop_ids: list[str]
    missing_mod_ids: list[str]
    missing_map_ids: list[str]
    diagnostics: list[str]
    buckets: list[ModsMapsDiagnosticBucket]


class ProjectZomboidConfigService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ensure_profile_files(self, profile: ServerProfile) -> None:
        paths = self.raw_file_paths(profile)
        paths["ini"].parent.mkdir(parents=True, exist_ok=True)
        for key, path in paths.items():
            if key == "ini" and not path.exists():
                path.write_text(self._default_ini_text(profile), encoding="utf-8")
                continue

            if key == "ini":
                self._repair_ini_defaults(path)
                continue

            if not path.exists() or path.stat().st_size == 0:
                path.write_text(self._default_lua_text(key), encoding="utf-8")

    def raw_file_paths(self, profile: ServerProfile) -> dict[str, Path]:
        server_root = Path(profile.cache_directory) / "Server"
        return {
            "ini": server_root / f"{profile.server_name}.ini",
            "sandbox": server_root / f"{profile.server_name}_SandboxVars.lua",
            "spawnregions": server_root / f"{profile.server_name}_spawnregions.lua",
            "spawnpoints": server_root / f"{profile.server_name}_spawnpoints.lua",
        }

    def launcher_secrets_path(self, profile: ServerProfile) -> Path:
        return Path(profile.cache_directory) / "Server" / f"{profile.server_name}_LauncherSecrets.ini"

    def load_general_settings(self, profile: ServerProfile) -> dict[str, object]:
        doc = self._load_ini(profile)
        default_port = _parse_int(doc.get("DefaultPort", ""), profile.default_port)
        fallback_udp_port = default_port + 1 if default_port < 65535 else 65534
        return {
            "public_name": doc.get("PublicName", profile.display_name) or profile.display_name,
            "public_description": doc.get("PublicDescription", "") or "",
            "public": _parse_bool(doc.get("Public"), True),
            "open": _parse_bool(doc.get("Open"), True),
            "max_players": str(_parse_int(doc.get("MaxPlayers", ""), profile.max_players)),
            "pvp": _parse_bool(doc.get("PVP"), True),
            "pause_empty": _parse_bool(doc.get("PauseEmpty"), True),
            "global_chat": _parse_bool(doc.get("GlobalChat"), True),
            "server_welcome_message": _decode_pz_multiline(doc.get("ServerWelcomeMessage", f"Welcome to {profile.display_name}") or ""),
            "spawn_items": _parse_multiline_csv(doc.get("SpawnItems", "") or ""),
            "loot_respawn_hours": doc.get("HoursForLootRespawn", "0") or "0",
            "loot_respawn_max_items": doc.get("MaxItemsForLootRespawn", "4") or "4",
            "construction_prevents_loot_respawn": _parse_bool(doc.get("ConstructionPreventsLootRespawn"), True),
            "sleep_allowed": _parse_bool(doc.get("SleepAllowed"), False),
            "sleep_needed": _parse_bool(doc.get("SleepNeeded"), False),
            "no_fire": _parse_bool(doc.get("NoFire"), False),
            "announce_death": _parse_bool(doc.get("AnnounceDeath"), True),
            "drop_whitelist_on_death": _parse_bool(doc.get("DropOffWhiteListAfterDeath"), False),
            "allow_sledgehammer_destruction": _parse_bool(doc.get("AllowDestructionBySledgehammer"), True),
            "respawn_with_self": _parse_bool(doc.get("PlayerRespawnWithSelf"), False),
            "respawn_with_other": _parse_bool(doc.get("PlayerRespawnWithOther"), False),
            "world_item_removal_hours": doc.get("HoursForWorldItemRemoval", "0.0") or "0.0",
            "world_item_removal_list": _parse_multiline_csv(doc.get("WorldItemRemovalList", "") or ""),
            "player_safehouse": _parse_bool(doc.get("PlayerSafehouse"), True),
            "admin_safehouse": _parse_bool(doc.get("AdminSafehouse"), False),
            "safehouse_allow_trespass": _parse_bool(doc.get("SafehouseAllowTrepass"), True),
            "safehouse_allow_fire": _parse_bool(doc.get("SafehouseAllowFire"), True),
            "safehouse_allow_loot": _parse_bool(doc.get("SafehouseAllowLoot"), True),
            "safehouse_allow_respawn": _parse_bool(doc.get("SafehouseAllowRespawn"), False),
            "safehouse_allow_non_residential": _parse_bool(doc.get("SafehouseAllowNonResidential"), False),
            "disable_safehouse_when_player_connected": _parse_bool(doc.get("DisableSafehouseWhenPlayerConnected"), False),
            "disable_safehouse_when_player_disconnected": _parse_bool(doc.get("DisableSafehouseWhenPlayerDisconnected"), False),
            "safehouse_days_to_claim": doc.get("SafehouseDaySurvivedToClaim", "0") or "0",
            "safehouse_removal_hours": doc.get("SafeHouseRemovalTime", "144") or "144",
            "faction_enabled": _parse_bool(doc.get("Faction"), True),
            "faction_days_to_create": doc.get("FactionDaySurvivedToCreate", "0") or "0",
            "faction_players_for_tag": doc.get("FactionPlayersRequiredForTag", "1") or "1",
            "allow_trade_ui": _parse_bool(doc.get("AllowTradeUI"), True),
            "rcon_port": doc.get("RCONPort", "27015") or "27015",
            "default_port": str(default_port),
            "udp_port": str(_parse_int(doc.get("UDPPort", ""), profile.udp_port or fallback_udp_port)),
            "preferred_memory_gb": str(_parse_int(doc.get("PreferredMemoryInGigabytes", ""), profile.preferred_memory_gb)),
            "start_with_host": _parse_bool(doc.get("StartWithHost"), profile.start_with_host),
            "auto_restart_on_crash": _parse_bool(doc.get("AutoRestartOnCrash"), profile.auto_restart_on_crash),
        }

    def save_general_settings(self, profile: ServerProfile, values: dict[str, object]) -> Path:
        doc = self._load_ini(profile)
        def normalized_text(key: str, default: str = "") -> str:
            raw = str(values[key]).strip()
            return raw if raw != "" else default

        doc.set("PublicName", str(values["public_name"]).strip())
        doc.set("PublicDescription", str(values["public_description"]).strip())
        doc.set("Public", _bool_to_ini(bool(values["public"])))
        doc.set("Open", _bool_to_ini(bool(values["open"])))
        doc.set("PVP", _bool_to_ini(bool(values["pvp"])))
        doc.set("PauseEmpty", _bool_to_ini(bool(values["pause_empty"])))
        doc.set("GlobalChat", _bool_to_ini(bool(values["global_chat"])))
        doc.set("ServerWelcomeMessage", _encode_pz_multiline(str(values["server_welcome_message"])))
        doc.set("SpawnItems", _join_multiline_csv(str(values["spawn_items"])))
        doc.set("HoursForLootRespawn", normalized_text("loot_respawn_hours", "0"))
        doc.set("MaxItemsForLootRespawn", normalized_text("loot_respawn_max_items", "4"))
        doc.set("ConstructionPreventsLootRespawn", _bool_to_ini(bool(values["construction_prevents_loot_respawn"])))
        doc.set("SleepAllowed", _bool_to_ini(bool(values["sleep_allowed"])))
        doc.set("SleepNeeded", _bool_to_ini(bool(values["sleep_needed"])))
        doc.set("NoFire", _bool_to_ini(bool(values["no_fire"])))
        doc.set("AnnounceDeath", _bool_to_ini(bool(values["announce_death"])))
        doc.set("DropOffWhiteListAfterDeath", _bool_to_ini(bool(values["drop_whitelist_on_death"])))
        doc.set("AllowDestructionBySledgehammer", _bool_to_ini(bool(values["allow_sledgehammer_destruction"])))
        doc.set("PlayerRespawnWithSelf", _bool_to_ini(bool(values["respawn_with_self"])))
        doc.set("PlayerRespawnWithOther", _bool_to_ini(bool(values["respawn_with_other"])))
        doc.set("HoursForWorldItemRemoval", normalized_text("world_item_removal_hours", "0.0"))
        doc.set("WorldItemRemovalList", _join_multiline_csv(str(values["world_item_removal_list"])))
        doc.set("PlayerSafehouse", _bool_to_ini(bool(values["player_safehouse"])))
        doc.set("AdminSafehouse", _bool_to_ini(bool(values["admin_safehouse"])))
        doc.set("SafehouseAllowTrepass", _bool_to_ini(bool(values["safehouse_allow_trespass"])))
        doc.set("SafehouseAllowFire", _bool_to_ini(bool(values["safehouse_allow_fire"])))
        doc.set("SafehouseAllowLoot", _bool_to_ini(bool(values["safehouse_allow_loot"])))
        doc.set("SafehouseAllowRespawn", _bool_to_ini(bool(values["safehouse_allow_respawn"])))
        doc.set("SafehouseAllowNonResidential", _bool_to_ini(bool(values["safehouse_allow_non_residential"])))
        doc.set("DisableSafehouseWhenPlayerConnected", _bool_to_ini(bool(values["disable_safehouse_when_player_connected"])))
        doc.set("DisableSafehouseWhenPlayerDisconnected", _bool_to_ini(bool(values["disable_safehouse_when_player_disconnected"])))
        doc.set("SafehouseDaySurvivedToClaim", normalized_text("safehouse_days_to_claim", "0"))
        doc.set("SafeHouseRemovalTime", normalized_text("safehouse_removal_hours", "144"))
        doc.set("Faction", _bool_to_ini(bool(values["faction_enabled"])))
        doc.set("FactionDaySurvivedToCreate", normalized_text("faction_days_to_create", "0"))
        doc.set("FactionPlayersRequiredForTag", normalized_text("faction_players_for_tag", "1"))
        doc.set("AllowTradeUI", _bool_to_ini(bool(values["allow_trade_ui"])))
        doc.set("DefaultPort", normalized_text("default_port", str(profile.default_port)))
        doc.set("UDPPort", normalized_text("udp_port", str(profile.udp_port)))
        doc.set("RCONPort", normalized_text("rcon_port", "27015"))
        doc.set("PreferredMemoryInGigabytes", normalized_text("preferred_memory_gb", str(profile.preferred_memory_gb)))
        doc.set("StartWithHost", _bool_to_ini(bool(values["start_with_host"])))
        doc.set("AutoRestartOnCrash", _bool_to_ini(bool(values["auto_restart_on_crash"])))
        doc.set("MaxPlayers", str(profile.max_players))
        return self._save_ini(profile, doc)

    def load_network_settings(self, profile: ServerProfile) -> dict[str, object]:
        doc = self._load_ini(profile)
        password_value = doc.get("Password", "") or ""
        rcon_password_value = doc.get("RCONPassword", "") or ""
        launcher_secrets = self._load_launcher_secrets(profile)
        admin_password_value = launcher_secrets.get("AdminPassword", doc.get("AdminPassword", "") or "")
        return {
            "bind_ip": doc.get("BindIP", profile.bind_ip) or profile.bind_ip,
            "rcon_port": doc.get("RCONPort", "27015") or "27015",
            "server_tag": doc.get("Tag", "") or "",
            "reset_id": doc.get("ResetID", "0") or "0",
            "upnp": _parse_bool(doc.get("UPnP"), False),
            "auto_create_user_in_whitelist": _parse_bool(doc.get("AutoCreateUserInWhiteList"), False),
            "do_lua_checksum": _parse_bool(doc.get("DoLuaChecksum"), True),
            "ping_limit": doc.get("PingLimit", "250") or "250",
            "steam_vac": _parse_bool(doc.get("SteamVAC"), True),
            "kick_fast_players": _parse_bool(doc.get("KickFastPlayers"), False),
            "deny_login_overloaded": _parse_bool(doc.get("DenyLoginOnOverloadedServer"), True),
            "client_command_filter": doc.get("ClientCommandFilter", "") or "",
            "save_world_every_minutes": doc.get("SaveWorldEveryMinutes", "0") or "0",
            "display_user_name": _parse_bool(doc.get("DisplayUserName"), True),
            "show_first_and_last_name": _parse_bool(doc.get("ShowFirstAndLastName"), False),
            "safety_system": _parse_bool(doc.get("SafetySystem"), True),
            "show_safety": _parse_bool(doc.get("ShowSafety"), True),
            "safety_toggle_timer": doc.get("SafetyToggleTimer", "100") or "100",
            "safety_cooldown_timer": doc.get("SafetyCooldownTimer", "120") or "120",
            "max_accounts_per_user": doc.get("MaxAccountsPerUser", "0") or "0",
            "allow_non_ascii_username": _parse_bool(doc.get("AllowNonAsciiUsername"), False),
            "player_save_on_damage": _parse_bool(doc.get("PlayerSaveOnDamage"), True),
            "mouse_over_display_name": _parse_bool(doc.get("MouseOverToSeeDisplayName"), True),
            "hide_players_behind_you": _parse_bool(doc.get("HidePlayersBehindYou"), True),
            "player_bump_player": _parse_bool(doc.get("PlayerBumpPlayer"), False),
            "map_remote_player_visibility": doc.get("MapRemotePlayerVisibility", "1") or "1",
            "use_tcp_for_map_traffic": _parse_bool(doc.get("UseTCPForMapTraffic"), False),
            "voice_enable": _parse_bool(doc.get("VoiceEnable"), True),
            "voice_3d": _parse_bool(doc.get("Voice3D"), True),
            "voice_min_distance": doc.get("VoiceMinDistance", "10.0") or "10.0",
            "voice_max_distance": doc.get("VoiceMaxDistance", "100.0") or "100.0",
            "minutes_per_page": doc.get("MinutesPerPage", "1") or "1",
            "admin_username": launcher_secrets.get("AdminUsername", doc.get("AdminUsername", "") or "") or "",
            "steam_mode": profile.use_steam,
            "has_server_password": bool(password_value.strip()),
            "has_rcon_password": bool(rcon_password_value.strip()),
            "has_admin_password": bool(admin_password_value.strip()),
        }

    def save_network_settings(self, profile: ServerProfile, values: dict[str, object]) -> Path:
        doc = self._load_ini(profile)
        existing_rcon_password = doc.get("RCONPassword", "") or ""
        launcher_secrets = self._load_launcher_secrets(profile)
        existing_admin_password = launcher_secrets.get("AdminPassword", doc.get("AdminPassword", "") or "")

        def normalized_text(key: str, default: str = "") -> str:
            raw = str(values[key]).strip()
            return raw if raw != "" else default

        submitted_password = str(values["server_password"]).strip()
        submitted_rcon_password = str(values["rcon_password"]).strip()
        submitted_admin_password = str(values["admin_password"]).strip()

        doc.set("Password", submitted_password)
        doc.set("RCONPassword", submitted_rcon_password if submitted_rcon_password else existing_rcon_password)
        self._save_launcher_secrets(
            profile,
            admin_username=normalized_text("admin_username"),
            admin_password=submitted_admin_password if submitted_admin_password else existing_admin_password,
        )
        doc.set("BindIP", normalized_text("bind_ip"))
        doc.set("RCONPort", normalized_text("rcon_port", "27015"))
        doc.set("Tag", normalized_text("server_tag"))
        doc.set("ResetID", normalized_text("reset_id", "0"))
        doc.set("UPnP", _bool_to_ini(bool(values["upnp"])))
        doc.set("AutoCreateUserInWhiteList", _bool_to_ini(bool(values["auto_create_user_in_whitelist"])))
        doc.set("DoLuaChecksum", _bool_to_ini(bool(values["do_lua_checksum"])))
        doc.set("PingLimit", normalized_text("ping_limit", "250"))
        doc.set("SteamVAC", _bool_to_ini(bool(values["steam_vac"])))
        doc.set("KickFastPlayers", _bool_to_ini(bool(values["kick_fast_players"])))
        doc.set("DenyLoginOnOverloadedServer", _bool_to_ini(bool(values["deny_login_overloaded"])))
        doc.set("ClientCommandFilter", normalized_text("client_command_filter"))
        doc.set("SaveWorldEveryMinutes", normalized_text("save_world_every_minutes", "0"))
        doc.set("DisplayUserName", _bool_to_ini(bool(values["display_user_name"])))
        doc.set("ShowFirstAndLastName", _bool_to_ini(bool(values["show_first_and_last_name"])))
        doc.set("SafetySystem", _bool_to_ini(bool(values["safety_system"])))
        doc.set("ShowSafety", _bool_to_ini(bool(values["show_safety"])))
        doc.set("SafetyToggleTimer", normalized_text("safety_toggle_timer", "100"))
        doc.set("SafetyCooldownTimer", normalized_text("safety_cooldown_timer", "120"))
        doc.set("MaxAccountsPerUser", normalized_text("max_accounts_per_user", "0"))
        doc.set("AllowNonAsciiUsername", _bool_to_ini(bool(values["allow_non_ascii_username"])))
        doc.set("PlayerSaveOnDamage", _bool_to_ini(bool(values["player_save_on_damage"])))
        doc.set("MouseOverToSeeDisplayName", _bool_to_ini(bool(values["mouse_over_display_name"])))
        doc.set("HidePlayersBehindYou", _bool_to_ini(bool(values["hide_players_behind_you"])))
        doc.set("PlayerBumpPlayer", _bool_to_ini(bool(values["player_bump_player"])))
        doc.set("MapRemotePlayerVisibility", normalized_text("map_remote_player_visibility", "1"))
        doc.set("UseTCPForMapTraffic", _bool_to_ini(bool(values["use_tcp_for_map_traffic"])))
        doc.set("VoiceEnable", _bool_to_ini(bool(values["voice_enable"])))
        doc.set("Voice3D", _bool_to_ini(bool(values["voice_3d"])))
        doc.set("VoiceMinDistance", normalized_text("voice_min_distance", "10.0"))
        doc.set("VoiceMaxDistance", normalized_text("voice_max_distance", "100.0"))
        doc.set("MinutesPerPage", normalized_text("minutes_per_page", "1"))
        profile.use_steam = bool(values["steam_mode"])
        profile.bind_ip = normalized_text("bind_ip", "0.0.0.0")
        return self._save_ini(profile, doc)

    def _load_launcher_secrets(self, profile: ServerProfile) -> dict[str, str]:
        path = self.launcher_secrets_path(profile)
        if not path.exists():
            return {}

        document = FlatIniDocument.parse(path.read_text(encoding="utf-8"))
        return {
            "AdminUsername": document.get("AdminUsername", "") or "",
            "AdminPassword": document.get("AdminPassword", "") or "",
        }

    def _save_launcher_secrets(self, profile: ServerProfile, *, admin_username: str, admin_password: str) -> Path:
        path = self.launcher_secrets_path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = FlatIniDocument.parse(path.read_text(encoding="utf-8") if path.exists() else "")
        document.set("AdminUsername", admin_username)
        document.set("AdminPassword", admin_password)
        path.write_text(document.to_text(), encoding="utf-8")
        return path

    def load_mods_maps(self, profile: ServerProfile) -> dict[str, object]:
        doc = self._load_ini(profile)
        workshop_ids = _parse_list(doc.get("WorkshopItems", ""), allow_comma_fallback=True)
        mod_ids = _parse_list(doc.get("Mods", ""))
        map_ids = _parse_list(doc.get("Map", ""))
        return {
            "workshop_ids": workshop_ids,
            "workshop_ids_text": "\n".join(workshop_ids),
            "mod_ids": mod_ids,
            "mod_ids_text": "\n".join(mod_ids),
            "map_ids": map_ids,
            "map_ids_text": "\n".join(map_ids),
        }

    def save_mods_maps(self, profile: ServerProfile, workshop_ids: list[str], mod_ids: list[str], map_ids: list[str]) -> Path:
        doc = self._load_ini(profile)
        doc.set("WorkshopItems", _join_list(workshop_ids))
        doc.set("Mods", _join_list(mod_ids))
        doc.set("Map", _join_list(map_ids))
        return self._save_ini(profile, doc)

    def resolve_workshop_ids_from_content(
        self,
        profile: ServerProfile,
        workshop_ids: list[str],
        mod_ids: list[str],
        map_ids: list[str],
    ) -> list[str]:
        resolved_ids = list(workshop_ids)
        seen = {item.lower() for item in resolved_ids}
        mod_lookup: dict[str, list[str]] = {}
        map_lookup: dict[str, list[str]] = {}

        for item in self.list_installed_workshop_catalog(profile):
            for mod_id in item.mod_ids:
                mod_lookup.setdefault(mod_id.lower(), []).append(item.workshop_id)
            for map_id in item.map_ids:
                map_lookup.setdefault(map_id.lower(), []).append(item.workshop_id)

        for lookup_values, lookup in ((mod_ids, mod_lookup), (map_ids, map_lookup)):
            for value in lookup_values:
                for workshop_id in lookup.get(value.lower(), []):
                    lowered = workshop_id.lower()
                    if lowered in seen:
                        continue
                    seen.add(lowered)
                    resolved_ids.append(workshop_id)

        return resolved_ids

    def build_mods_maps_validation(
        self,
        profile: ServerProfile,
        workshop_ids: list[str],
        mod_ids: list[str],
        map_ids: list[str],
    ) -> ModsMapsValidationResult:
        catalog = self.list_installed_workshop_catalog(profile)
        available_workshop = {item.workshop_id.lower(): item for item in catalog}
        available_mods: dict[str, str] = {}
        available_maps: dict[str, str] = {}
        for item in catalog:
            for mod_id in item.mod_ids:
                available_mods.setdefault(mod_id.lower(), item.workshop_id)
            for map_id in item.map_ids:
                available_maps.setdefault(map_id.lower(), item.workshop_id)

        missing_workshop_ids = [workshop_id for workshop_id in workshop_ids if workshop_id.lower() not in available_workshop]
        missing_mod_ids = [mod_id for mod_id in mod_ids if mod_id.lower() not in available_mods]
        missing_map_ids = [map_id for map_id in map_ids if map_id.lower() not in available_maps]
        resolved_workshop_ids = self.resolve_workshop_ids_from_content(profile, workshop_ids, mod_ids, map_ids)
        configured_workshop = {item.lower() for item in workshop_ids}
        auto_resolved_ids = [item for item in resolved_workshop_ids if item.lower() not in configured_workshop]

        buckets: list[ModsMapsDiagnosticBucket] = []
        diagnostics: list[str] = []
        if catalog:
            diagnostics.append(
                f"Indexed {len(catalog)} local workshop item(s), {sum(len(item.mod_ids) for item in catalog)} mod ID(s), and {sum(len(item.map_ids) for item in catalog)} map folder(s)."
            )
        else:
            diagnostics.append("No installed workshop content is available under the managed server paths yet.")

        if not missing_workshop_ids and not missing_mod_ids and not missing_map_ids and catalog:
            diagnostics.append("The live stack is fully represented in the local workshop cache.")

        if auto_resolved_ids:
            diagnostics.append(
                f"Local content can auto-map {len(auto_resolved_ids)} workshop ID(s) from the selected mods and maps."
            )

        matching_workshop_ids = [item for item in workshop_ids if item.lower() in available_workshop]
        if matching_workshop_ids:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Installed Workshop Matches",
                    tone="success",
                    items=[
                        f"{workshop_id} | {available_workshop[workshop_id.lower()].title}"
                        for workshop_id in matching_workshop_ids
                    ],
                )
            )

        if auto_resolved_ids:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Auto-Resolvable IDs",
                    tone="info",
                    items=[f"{workshop_id} can be inferred from the selected mod/map content." for workshop_id in auto_resolved_ids],
                )
            )

        if missing_workshop_ids:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Missing Workshop Downloads",
                    tone="warning",
                    items=[f"{workshop_id} is configured but not installed locally." for workshop_id in missing_workshop_ids],
                )
            )

        if missing_mod_ids:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Missing Mod IDs",
                    tone="danger",
                    items=[f"{mod_id} is configured but not present in the local workshop cache." for mod_id in missing_mod_ids],
                )
            )

        if missing_map_ids:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Missing Map Folders",
                    tone="danger",
                    items=[f"{map_id} is configured but not present in the local workshop cache." for map_id in missing_map_ids],
                )
            )

        if not buckets:
            buckets.append(
                ModsMapsDiagnosticBucket(
                    label="Local Cache Overview",
                    tone="info",
                    items=["Save or scan a stack to see local diagnostics and recovery hints."],
                )
            )

        installed_mod_ids = _parse_list(_join_list(mod_id for item in catalog for mod_id in item.mod_ids))
        installed_map_ids = _parse_list(_join_list(map_id for item in catalog for map_id in item.map_ids))
        return ModsMapsValidationResult(
            catalog_count=len(catalog),
            installed_mod_count=len(installed_mod_ids),
            installed_map_count=len(installed_map_ids),
            resolved_workshop_ids=resolved_workshop_ids,
            missing_workshop_ids=missing_workshop_ids,
            missing_mod_ids=missing_mod_ids,
            missing_map_ids=missing_map_ids,
            diagnostics=diagnostics,
            buckets=buckets,
        )

    def list_installed_workshop_catalog(self, profile: ServerProfile) -> list[WorkshopCatalogItem]:
        catalog_by_id: dict[str, WorkshopCatalogItem] = {}
        for root in self._candidate_workshop_roots(profile):
            if not root.exists():
                continue

            for item_dir in sorted(root.iterdir(), key=lambda path: path.name):
                if not item_dir.is_dir() or not item_dir.name.isdigit():
                    continue

                item = self._build_workshop_catalog_item(item_dir)
                existing = catalog_by_id.get(item.workshop_id)
                if existing is None:
                    catalog_by_id[item.workshop_id] = item
                    continue

                existing.mod_ids = _parse_list(_join_list([*existing.mod_ids, *item.mod_ids]))
                existing.map_ids = _parse_list(_join_list([*existing.map_ids, *item.map_ids]))
                existing.mod_dependencies.update(item.mod_dependencies)
                if existing.title == existing.workshop_id and item.title != item.workshop_id:
                    existing.title = item.title

        return sorted(catalog_by_id.values(), key=lambda item: (item.title.lower(), item.workshop_id))

    def search_installed_workshop_catalog(self, profile: ServerProfile, query: str) -> list[WorkshopCatalogItem]:
        catalog = self.list_installed_workshop_catalog(profile)
        normalized_query = query.strip().lower()
        if not normalized_query:
            return catalog

        results: list[WorkshopCatalogItem] = []
        for item in catalog:
            haystacks = [item.workshop_id, item.title, *item.mod_ids, *item.map_ids]
            if any(normalized_query in haystack.lower() for haystack in haystacks):
                results.append(item)
        return results

    def get_installed_workshop_item(self, profile: ServerProfile, workshop_id: str) -> WorkshopCatalogItem | None:
        for item in self.list_installed_workshop_catalog(profile):
            if item.workshop_id == workshop_id:
                return item
        return None

    def list_named_presets(self, db: Session, profile_id: str) -> list[NamedPresetView]:
        rows = db.scalars(
            select(WorkshopPreset)
            .where(WorkshopPreset.profile_id == profile_id)
            .order_by(WorkshopPreset.name.asc())
        ).all()
        return [
            NamedPresetView(
                id=row.id,
                name=row.name,
                workshop_ids=_parse_list(row.workshop_ids, allow_comma_fallback=True),
                mod_ids=_parse_list(row.mod_ids),
                map_ids=_parse_list(row.map_ids),
            )
            for row in rows
        ]

    def save_named_preset(
        self,
        db: Session,
        *,
        profile_id: str,
        name: str,
        workshop_ids: list[str],
        mod_ids: list[str],
        map_ids: list[str],
    ) -> WorkshopPreset:
        existing = db.scalar(
            select(WorkshopPreset)
            .where(WorkshopPreset.profile_id == profile_id, WorkshopPreset.name == name.strip())
        )
        if existing is None:
            existing = WorkshopPreset(
                profile_id=profile_id,
                name=name.strip(),
            )
            db.add(existing)

        existing.workshop_ids = _join_list(workshop_ids)
        existing.mod_ids = _join_list(mod_ids)
        existing.map_ids = _join_list(map_ids)
        db.commit()
        db.refresh(existing)
        return existing

    def delete_named_preset(self, db: Session, *, profile_id: str, preset_id: str) -> str | None:
        preset = db.scalar(
            select(WorkshopPreset)
            .where(WorkshopPreset.profile_id == profile_id, WorkshopPreset.id == preset_id)
        )
        if preset is None:
            return None

        name = preset.name
        db.delete(preset)
        db.commit()
        return name

    def get_named_preset(self, db: Session, *, profile_id: str, preset_id: str) -> NamedPresetView | None:
        preset = db.scalar(
            select(WorkshopPreset)
            .where(WorkshopPreset.profile_id == profile_id, WorkshopPreset.id == preset_id)
        )
        if preset is None:
            return None

        return NamedPresetView(
            id=preset.id,
            name=preset.name,
            workshop_ids=_parse_list(preset.workshop_ids, allow_comma_fallback=True),
            mod_ids=_parse_list(preset.mod_ids),
            map_ids=_parse_list(preset.map_ids),
        )

    def scan_installed_workshop(self, profile: ServerProfile) -> WorkshopScanResult:
        catalog = self.list_installed_workshop_catalog(profile)
        diagnostics: list[str] = []
        roots = self._candidate_workshop_roots(profile)
        existing_roots = [root for root in roots if root.exists()]
        if not existing_roots:
            diagnostics.append("No workshop content root was found under the managed install directory.")
            return WorkshopScanResult([], [], [], diagnostics)

        diagnostics.append(
            f"Scanned {len(existing_roots)} workshop root(s) and indexed {len(catalog)} local workshop item(s)."
        )
        for root in existing_roots:
            diagnostics.append(f"Scanned {root}.")

        return WorkshopScanResult(
            workshop_ids=[item.workshop_id for item in catalog],
            mod_ids=_parse_list(_join_list(mod_id for item in catalog for mod_id in item.mod_ids)),
            map_ids=_parse_list(_join_list(map_id for item in catalog for map_id in item.map_ids)),
            diagnostics=diagnostics,
        )

    def read_advanced_files(self, profile: ServerProfile) -> dict[str, dict[str, str]]:
        self.ensure_profile_files(profile)
        files: dict[str, dict[str, str]] = {}
        for key, path in self.raw_file_paths(profile).items():
            files[key] = {
                "path": str(path),
                "content": path.read_text(encoding="utf-8"),
            }
        return files

    def save_advanced_file(self, profile: ServerProfile, *, file_kind: str, content: str) -> Path:
        paths = self.raw_file_paths(profile)
        if file_kind not in paths:
            raise ValueError("Unknown file kind.")

        path = paths[file_kind]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
        return path

    def sync_profile_metadata_from_ini(self, profile: ServerProfile) -> None:
        doc = self._load_ini(profile)
        default_port = _parse_int(doc.get("DefaultPort", ""), profile.default_port)
        fallback_udp_port = default_port + 1 if default_port < 65535 else 65534
        profile.max_players = _parse_int(doc.get("MaxPlayers", ""), profile.max_players)
        profile.default_port = default_port
        profile.udp_port = _parse_int(doc.get("UDPPort", ""), fallback_udp_port)
        profile.preferred_memory_gb = _parse_int(doc.get("PreferredMemoryInGigabytes", ""), profile.preferred_memory_gb)
        profile.start_with_host = _parse_bool(doc.get("StartWithHost"), profile.start_with_host)
        profile.auto_restart_on_crash = _parse_bool(doc.get("AutoRestartOnCrash"), profile.auto_restart_on_crash)
        profile.bind_ip = (doc.get("BindIP", "") or profile.bind_ip).strip() or profile.bind_ip

    def _load_ini(self, profile: ServerProfile) -> FlatIniDocument:
        self.ensure_profile_files(profile)
        ini_path = self.raw_file_paths(profile)["ini"]
        return FlatIniDocument.parse(ini_path.read_text(encoding="utf-8"))

    def _save_ini(self, profile: ServerProfile, document: FlatIniDocument) -> Path:
        ini_path = self.raw_file_paths(profile)["ini"]
        ini_path.write_text(document.to_text(), encoding="utf-8")
        return ini_path

    def _default_ini_text(self, profile: ServerProfile) -> str:
        return "\n".join(
            [
                "# Managed by PZServerLauncherLinux",
                f"PublicName={profile.display_name}",
                "PublicDescription=",
                "Public=true",
                "Open=true",
                f"MaxPlayers={profile.max_players}",
                f"DefaultPort={profile.default_port}",
                f"UDPPort={profile.udp_port}",
                "PVP=true",
                "PauseEmpty=true",
                "GlobalChat=true",
                f"ServerWelcomeMessage=Welcome to {profile.display_name}",
                "SpawnItems=",
                "HoursForLootRespawn=0",
                "MaxItemsForLootRespawn=4",
                "ConstructionPreventsLootRespawn=true",
                "SleepAllowed=false",
                "SleepNeeded=false",
                "NoFire=false",
                "AnnounceDeath=true",
                "DropOffWhiteListAfterDeath=false",
                "AllowDestructionBySledgehammer=true",
                "PlayerRespawnWithSelf=false",
                "PlayerRespawnWithOther=false",
                "HoursForWorldItemRemoval=0.0",
                "WorldItemRemovalList=",
                "PlayerSafehouse=true",
                "AdminSafehouse=false",
                "SafehouseAllowTrepass=true",
                "SafehouseAllowFire=true",
                "SafehouseAllowLoot=true",
                "SafehouseAllowRespawn=false",
                "SafehouseAllowNonResidential=false",
                "DisableSafehouseWhenPlayerConnected=false",
                "DisableSafehouseWhenPlayerDisconnected=false",
                "SafehouseDaySurvivedToClaim=0",
                "SafeHouseRemovalTime=144",
                "Faction=true",
                "FactionDaySurvivedToCreate=0",
                "FactionPlayersRequiredForTag=1",
                "AllowTradeUI=true",
                "WorkshopItems=",
                "Mods=",
                "Map=Muldraugh, KY",
                "BindIP=0.0.0.0",
                "Password=",
                "RCONPort=27015",
                "RCONPassword=",
                "Tag=",
                "ResetID=0",
                "UPnP=false",
                "AutoCreateUserInWhiteList=false",
                "DoLuaChecksum=true",
                "PingLimit=250",
                "SteamVAC=true",
                "KickFastPlayers=false",
                "DenyLoginOnOverloadedServer=true",
                "ClientCommandFilter=",
                "SaveWorldEveryMinutes=0",
                "DisplayUserName=true",
                "ShowFirstAndLastName=false",
                "SafetySystem=true",
                "ShowSafety=true",
                "SafetyToggleTimer=100",
                "SafetyCooldownTimer=120",
                "MaxAccountsPerUser=0",
                "AllowNonAsciiUsername=false",
                "PlayerSaveOnDamage=true",
                "MouseOverToSeeDisplayName=true",
                "HidePlayersBehindYou=true",
                "PlayerBumpPlayer=false",
                "MapRemotePlayerVisibility=1",
                "UseTCPForMapTraffic=false",
                "VoiceEnable=true",
                "Voice3D=true",
                "VoiceMinDistance=10.0",
                "VoiceMaxDistance=100.0",
                "MinutesPerPage=1",
                f"PreferredMemoryInGigabytes={profile.preferred_memory_gb}",
                f"StartWithHost={_bool_to_ini(profile.start_with_host)}",
                f"AutoRestartOnCrash={_bool_to_ini(profile.auto_restart_on_crash)}",
                "AdminUsername=",
                "AdminPassword=",
                "",
            ]
        )

    def _repair_ini_defaults(self, path: Path) -> None:
        document = FlatIniDocument.parse(path.read_text(encoding="utf-8"))
        changed = False
        if not (document.get("Map", "") or "").strip():
            document.set("Map", "Muldraugh, KY")
            changed = True

        if changed:
            path.write_text(document.to_text(), encoding="utf-8")

    def _default_lua_text(self, key: str) -> str:
        if key == "sandbox":
            return "\n".join(
                [
                    'SandboxVars = require "Sandbox/Apocalypse"',
                    "",
                    "-- This is needed to add custom sandbox options to the SandboxVars table.",
                    "getSandboxOptions():initSandboxVars()",
                    "",
                ]
            )

        if key == "spawnregions":
            return "\n".join(
                [
                    "function SpawnRegions()",
                    "    return {",
                    '        { name = "Muldraugh, KY", file = "media/maps/Muldraugh, KY/spawnpoints.lua" },',
                    "    }",
                    "end",
                    "",
                ]
            )

        if key == "spawnpoints":
            return "\n".join(
                [
                    "function SpawnPoints()",
                    "    return {}",
                    "end",
                    "",
                ]
            )

        return ""

    def _candidate_workshop_roots(self, profile: ServerProfile) -> list[Path]:
        install_dir = Path(profile.install_directory)
        return [
            install_dir / "steamapps" / "workshop" / "content" / "108600",
            install_dir.parent / "steamapps" / "workshop" / "content" / "108600",
            install_dir.parent.parent / "steamapps" / "workshop" / "content" / "108600",
        ]

    @staticmethod
    def _read_simple_key_value_file(path: Path) -> dict[str, str]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        values: dict[str, str] = {}
        for raw_line in content.replace("\r\n", "\n").split("\n"):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(("#", ";", "//")) or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            normalized_key = key.strip().lower()
            if not normalized_key:
                continue
            values.setdefault(normalized_key, value.strip())

        return values

    def _build_workshop_catalog_item(self, item_dir: Path) -> WorkshopCatalogItem:
        workshop_meta = self._read_simple_key_value_file(item_dir / "workshop.txt")
        mod_ids: list[str] = []
        map_ids: list[str] = []
        mod_dependencies: dict[str, list[str]] = {}
        title_candidates: list[str] = []

        for mods_root in (item_dir / "mods", item_dir / "Contents" / "mods"):
            if not mods_root.exists():
                continue

            for mod_dir in sorted((path for path in mods_root.iterdir() if path.is_dir()), key=lambda path: path.name):
                mod_meta = self._read_simple_key_value_file(mod_dir / "mod.info")
                mod_id = mod_meta.get("id", "").strip() or mod_dir.name
                if mod_id:
                    mod_ids.append(mod_id)
                    dependency_values = _parse_list(
                        _join_list(
                            [
                                mod_meta.get("require", ""),
                                mod_meta.get("requires", ""),
                                mod_meta.get("depends", ""),
                                mod_meta.get("dependencies", ""),
                            ]
                        ),
                        allow_comma_fallback=True,
                    )
                    mod_dependencies[mod_id] = dependency_values

                mod_name = mod_meta.get("name", "").strip()
                if mod_name:
                    title_candidates.append(mod_name)

                mod_maps_root = mod_dir / "media" / "maps"
                if mod_maps_root.exists():
                    for map_dir in sorted((path for path in mod_maps_root.iterdir() if path.is_dir()), key=lambda path: path.name):
                        map_ids.append(map_dir.name)

        for maps_root in (item_dir / "media" / "maps", item_dir / "Contents" / "media" / "maps"):
            if not maps_root.exists():
                continue

            for map_dir in sorted((path for path in maps_root.iterdir() if path.is_dir()), key=lambda path: path.name):
                map_ids.append(map_dir.name)

        workshop_title = workshop_meta.get("title", "").strip()
        title = workshop_title or (title_candidates[0] if title_candidates else item_dir.name)
        return WorkshopCatalogItem(
            workshop_id=item_dir.name,
            title=title,
            mod_ids=_parse_list(_join_list(mod_ids)),
            map_ids=_parse_list(_join_list(map_ids)),
            mod_dependencies=mod_dependencies,
            source_path=str(item_dir),
        )

    def _read_mod_id(self, mod_info_path: Path) -> str | None:
        value = self._read_simple_key_value_file(mod_info_path).get("id", "").strip()
        if value:
            return value

        return None
