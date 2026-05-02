from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.config import Settings
from app.models import ServerProfile
from app.services.config_files import FlatIniDocument


@dataclass(slots=True)
class LaunchPlan:
    working_directory: Path
    command: list[str]
    notes: str
    environment: dict[str, str] | None = None
    redactions: tuple[str, ...] = ()
    blocked: bool = False


@dataclass(slots=True)
class WorldResetResult:
    world_directory: Path
    world_directory_existed: bool
    backup_path: Path | None
    reset_id: int | None
    seed: str | None
    ini_updated: bool


@dataclass(slots=True)
class BackupRestoreResult:
    source_backup_path: Path
    safety_backup_path: Path | None
    install_restored: bool
    cache_restored: bool


@dataclass(slots=True)
class ProfileRetirementResult:
    removed_managed_install: bool
    removed_managed_cache: bool
    removed_backups: bool
    removed_logs: bool
    deleted_profile: bool
    message: str


class ZomboidService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def managed_install_directory(self, profile_id: str) -> Path:
        return self.settings.servers_root / profile_id / "install"

    def managed_cache_directory(self, profile_id: str) -> Path:
        return self.settings.servers_root / profile_id / "cache"

    def is_managed_install_directory(self, profile: ServerProfile) -> bool:
        return self._paths_match(Path(profile.install_directory), self.managed_install_directory(profile.id))

    def is_managed_cache_directory(self, profile: ServerProfile) -> bool:
        return self._paths_match(Path(profile.cache_directory), self.managed_cache_directory(profile.id))

    def profile_log_path(self, profile_id: str) -> Path:
        return self.settings.logs_root / "profiles" / f"{profile_id}.log"

    def resolve_install_command(self, profile: ServerProfile) -> list[str]:
        command = [
            self.settings.steamcmd_path,
            "+force_install_dir",
            profile.install_directory,
            "+login",
            "anonymous",
            "+app_update",
            "380870",
        ]

        if profile.branch == "unstable":
            command.extend(["-beta", "unstable"])

        command.extend(["validate", "+quit"])
        return command

    def build_launch_plan(self, profile: ServerProfile) -> LaunchPlan:
        install_directory = Path(profile.install_directory)
        admin_username, admin_password = self._load_launch_secrets(profile)
        candidates = [
            install_directory / "start-server.sh",
            install_directory / "StartServer.sh",
        ]

        script = next((candidate for candidate in candidates if candidate.exists()), None)
        if script is None:
            return LaunchPlan(
                working_directory=install_directory,
                command=[],
                notes=f"Launch blocked. Could not find start-server.sh under {install_directory}. Install or update the server first.",
                blocked=True,
            )

        if not admin_password:
            return LaunchPlan(
                working_directory=install_directory,
                command=[],
                notes=(
                    "Launch blocked. Configure a bootstrap admin password on the Network page before the first server start. "
                    "Project Zomboid asks for this interactively otherwise, which is not compatible with a web-managed VPS service."
                ),
                blocked=True,
            )

        runtime_home, runtime_home_note = self._prepare_runtime_home(profile)
        memory_note = self._apply_memory_profile(install_directory, profile.preferred_memory_gb)
        command = [
            "bash",
            script.name,
            "-servername",
            profile.server_name,
        ]

        if not profile.use_steam:
            command.append("-nosteam")

        if admin_username:
            command.extend(["-adminusername", admin_username])

        if admin_password:
            command.extend(["-adminpassword", admin_password])

        bind_ip = profile.bind_ip.strip()
        if bind_ip and bind_ip not in {"0.0.0.0", "::"}:
            command.extend(["-ip", bind_ip])

        notes = f"Launching {profile.server_name} through {script.name}. Memory profile target is {profile.preferred_memory_gb} GB."
        if runtime_home_note:
            notes = f"{notes} {runtime_home_note}"
        if memory_note:
            notes = f"{notes} {memory_note}"
        if admin_username:
            notes = f"{notes} Bootstrap admin '{admin_username}' is configured for launch."

        return LaunchPlan(
            working_directory=install_directory,
            command=command,
            notes=notes,
            environment={
                "HOME": str(runtime_home),
                "JAVA_TOOL_OPTIONS": f"-Duser.home={runtime_home}",
            },
            redactions=(admin_password,),
        )

    def _load_launch_secrets(self, profile: ServerProfile) -> tuple[str, str]:
        paths = [
            Path(profile.cache_directory) / "Server" / f"{profile.server_name}_LauncherSecrets.ini",
            Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini",
        ]
        for path in paths:
            if not path.exists():
                continue

            document = FlatIniDocument.parse(path.read_text(encoding="utf-8"))
            admin_username = (document.get("AdminUsername", "") or "").strip()
            admin_password = (document.get("AdminPassword", "") or "").strip()
            if admin_username or admin_password:
                return admin_username, admin_password

        return "", ""

    def _prepare_runtime_home(self, profile: ServerProfile) -> tuple[Path, str]:
        runtime_home = self.settings.servers_root / profile.id / "runtime-home"
        cache_directory = Path(profile.cache_directory)
        runtime_home.mkdir(parents=True, exist_ok=True)
        cache_directory.mkdir(parents=True, exist_ok=True)

        zomboid_link = runtime_home / "Zomboid"
        if os.name == "nt":
            return runtime_home, f"Runtime HOME is {runtime_home}; cache root is {cache_directory}."

        try:
            if zomboid_link.is_symlink():
                if zomboid_link.resolve(strict=False) != cache_directory.resolve(strict=False):
                    zomboid_link.unlink()
                    zomboid_link.symlink_to(cache_directory, target_is_directory=True)
            elif not zomboid_link.exists():
                zomboid_link.symlink_to(cache_directory, target_is_directory=True)
            elif zomboid_link.resolve(strict=False) != cache_directory.resolve(strict=False):
                return runtime_home, f"Runtime HOME is {runtime_home}; existing Zomboid path prevented cache link to {cache_directory}."
        except OSError as exc:
            return runtime_home, f"Runtime HOME is {runtime_home}; could not link Zomboid cache: {exc}."

        return runtime_home, f"Runtime HOME is {runtime_home}; Zomboid cache is linked to {cache_directory}."

    def _apply_memory_profile(self, install_directory: Path, preferred_memory_gb: int) -> str:
        memory_gb = max(1, int(preferred_memory_gb or 1))
        changed_files: list[str] = []

        for config_name in ("ProjectZomboid64.json", "ProjectZomboid32.json"):
            config_path = install_directory / config_name
            if not config_path.exists():
                continue

            try:
                document = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue

            vm_args = document.get("vmArgs")
            if not isinstance(vm_args, list):
                continue

            desired_arg = f"-Xmx{memory_gb}g"
            next_args: list[object] = []
            replaced = False
            for arg in vm_args:
                if isinstance(arg, str) and arg.startswith("-Xmx"):
                    if not replaced:
                        next_args.append(desired_arg)
                        replaced = True
                    continue
                next_args.append(arg)

            if not replaced:
                next_args.append(desired_arg)

            if next_args == vm_args:
                continue

            document["vmArgs"] = next_args
            config_path.write_text(json.dumps(document, indent=4) + "\n", encoding="utf-8")
            changed_files.append(config_name)

        if not changed_files:
            return ""
        return f"Applied -Xmx{memory_gb}g to {', '.join(changed_files)}."

    async def run_command(
        self,
        command: list[str],
        *,
        working_directory: Path,
        on_output,
    ) -> int:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(working_directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_stream(stream) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await on_output(line.decode("utf-8", errors="replace").rstrip())

        await asyncio.gather(read_stream(process.stdout), read_stream(process.stderr))
        return await process.wait()

    def create_backup(self, profile: ServerProfile, *, prune: bool = True) -> Path:
        profile_backup_root = self.settings.backups_root / profile.id
        profile_backup_root.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = profile_backup_root / f"{profile.server_name}-{stamp}.tar.gz"

        with tarfile.open(backup_path, "w:gz") as archive:
            install_directory = Path(profile.install_directory)
            cache_directory = Path(profile.cache_directory)

            if install_directory.exists():
                archive.add(install_directory, arcname=f"install-{install_directory.name}")
            if cache_directory.exists():
                archive.add(cache_directory, arcname=f"cache-{cache_directory.name}")

        if prune:
            self.prune_backups(profile)
        return backup_path

    def list_backups(self, profile: ServerProfile) -> list[Path]:
        profile_backup_root = self.settings.backups_root / profile.id
        if not profile_backup_root.exists():
            return []

        return sorted(profile_backup_root.glob("*.tar.gz"), reverse=True)

    def resolve_backup_path(self, profile: ServerProfile, backup_name: str) -> Path:
        normalized = backup_name.strip()
        if normalized == "":
            raise ValueError("Backup selection is required.")
        if Path(normalized).name != normalized:
            raise ValueError("Backup name is invalid.")

        profile_backup_root = (self.settings.backups_root / profile.id).resolve()
        candidate = (profile_backup_root / normalized).resolve()
        if candidate.parent != profile_backup_root:
            raise ValueError("Backup name is invalid.")
        if not candidate.exists() or not candidate.is_file():
            raise ValueError("The selected backup could not be found.")
        if not candidate.name.endswith(".tar.gz"):
            raise ValueError("Only managed .tar.gz backups can be restored.")
        return candidate

    def prune_backups(self, profile: ServerProfile) -> None:
        backups = self.list_backups(profile)
        for backup in backups[profile.backup_retention_count :]:
            backup.unlink(missing_ok=True)

    def restore_backup(
        self,
        profile: ServerProfile,
        *,
        backup_name: str,
        create_backup_before_restore: bool,
    ) -> BackupRestoreResult:
        backup_path = self.resolve_backup_path(profile, backup_name)
        safety_backup_path = self.create_backup(profile, prune=False) if create_backup_before_restore else None
        profile_backup_root = self.settings.backups_root / profile.id
        temp_root = profile_backup_root / f".restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            with tarfile.open(backup_path, "r:gz") as archive:
                members = archive.getmembers()
                self._validate_backup_members(members)
                archive.extractall(temp_root, members=members)
        except (OSError, tarfile.TarError, ValueError) as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise ValueError(f"Backup '{backup_path.name}' could not be restored: {exc}.") from exc

        try:
            install_source = self._find_restore_directory(temp_root, "install")
            cache_source = self._find_restore_directory(temp_root, "cache")
            if install_source is None and cache_source is None:
                raise ValueError(f"Backup '{backup_path.name}' does not contain a managed install or cache payload.")

            install_directory = Path(profile.install_directory)
            cache_directory = Path(profile.cache_directory)

            install_restored = self._restore_directory(install_source, install_directory) if install_source is not None else False
            cache_restored = self._restore_directory(cache_source, cache_directory) if cache_source is not None else False

            return BackupRestoreResult(
                source_backup_path=backup_path,
                safety_backup_path=safety_backup_path,
                install_restored=install_restored,
                cache_restored=cache_restored,
            )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def reset_world(self, profile: ServerProfile, *, create_backup_before_reset: bool) -> WorldResetResult:
        backup_path = self.create_backup(profile) if create_backup_before_reset else None
        world_directory = Path(profile.cache_directory) / "Saves" / "Multiplayer" / profile.server_name
        world_directory_existed = world_directory.exists()
        if world_directory_existed:
            shutil.rmtree(world_directory)

        ini_path = Path(profile.cache_directory) / "Server" / f"{profile.server_name}.ini"
        reset_id: int | None = None
        seed: str | None = None
        ini_updated = False

        if ini_path.exists():
            document = FlatIniDocument.parse(ini_path.read_text(encoding="utf-8"))
            current_reset = document.get("ResetID", "")
            try:
                parsed_reset = int(current_reset) if current_reset not in {None, ""} else 0
                reset_id = parsed_reset + 1 if parsed_reset >= 0 else 1
            except ValueError:
                reset_id = 1

            seed = self._generate_seed()
            document.set("ResetID", str(reset_id))
            document.set("Seed", seed)
            ini_path.write_text(document.to_text(), encoding="utf-8")
            ini_updated = True

        return WorldResetResult(
            world_directory=world_directory,
            world_directory_existed=world_directory_existed,
            backup_path=backup_path,
            reset_id=reset_id,
            seed=seed,
            ini_updated=ini_updated,
        )

    def uninstall_server(self, profile: ServerProfile) -> ProfileRetirementResult:
        if not self.is_managed_install_directory(profile):
            raise ValueError(
                "Uninstall only removes launcher-managed installs under the default servers root. "
                "This profile is using a custom install directory."
            )

        removed_managed_install = self._delete_path_if_exists(Path(profile.install_directory))
        return ProfileRetirementResult(
            removed_managed_install=removed_managed_install,
            removed_managed_cache=False,
            removed_backups=False,
            removed_logs=False,
            deleted_profile=False,
            message=(
                f"Uninstalled the managed server files for {profile.display_name}. "
                "The profile, backups, and profile data were left intact."
            ),
        )

    def delete_profile_files(self, profile: ServerProfile) -> ProfileRetirementResult:
        removed_managed_install = self.is_managed_install_directory(profile) and self._delete_path_if_exists(Path(profile.install_directory))
        removed_managed_cache = self.is_managed_cache_directory(profile) and self._delete_path_if_exists(Path(profile.cache_directory))
        removed_backups = self._delete_path_if_exists(self.settings.backups_root / profile.id)
        removed_logs = self._delete_path_if_exists(self.profile_log_path(profile.id))
        return ProfileRetirementResult(
            removed_managed_install=bool(removed_managed_install),
            removed_managed_cache=bool(removed_managed_cache),
            removed_backups=bool(removed_backups),
            removed_logs=bool(removed_logs),
            deleted_profile=True,
            message=(
                f"Deleted profile {profile.display_name}. Launcher-managed files, backups, logs, and runtime artifacts were cleaned up. "
                "External install or cache folders were left alone."
            ),
        )

    @staticmethod
    def _generate_seed() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(16))

    @staticmethod
    def _validate_backup_members(members: list[tarfile.TarInfo]) -> None:
        for member in members:
            path = PurePosixPath(member.name)
            if member.name.strip() == "":
                raise ValueError("Backup archive contains an empty path entry.")
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Backup archive contains an unsafe path.")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("Backup archive contains unsupported link or device entries.")

    @staticmethod
    def _find_restore_directory(root: Path, prefix: str) -> Path | None:
        directories = [
            path
            for path in root.iterdir()
            if path.is_dir() and (path.name == prefix or path.name.startswith(f"{prefix}-"))
        ]
        if not directories:
            return None
        return sorted(directories, key=lambda item: (0 if item.name == prefix else 1, item.name.lower()))[0]

    @staticmethod
    def _restore_directory(source: Path, destination: Path) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.copytree(source, destination)
        return True

    @staticmethod
    def _delete_path_if_exists(path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False

        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True

    @staticmethod
    def _paths_match(left: Path, right: Path) -> bool:
        left_text = os.path.normcase(str(left.resolve())).rstrip("\\/")
        right_text = os.path.normcase(str(right.resolve())).rstrip("\\/")
        return left_text == right_text
