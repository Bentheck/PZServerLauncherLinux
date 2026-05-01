from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_root() -> Path:
    if os.name == "nt":
        return Path.cwd() / ".localdata" / "pzserverlauncher"

    return Path(os.environ.get("PZSL_DATA_ROOT", "/var/lib/pzserverlauncher"))


def _default_logs_root() -> Path:
    if os.name == "nt":
        return _default_data_root() / "logs"

    return Path(os.environ.get("PZSL_LOGS_ROOT", "/var/log/pzserverlauncher"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PZSL_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "PZServerLauncherLinux"
    environment: str = "development"
    secret_key: str = Field(
        default="change-this-before-production-pzserverlauncherlinux",
        description="Session signing key.",
    )
    bind_host: str = "127.0.0.1"
    bind_port: int = 48231
    session_https_only: bool = False
    data_root: Path = Field(default_factory=_default_data_root)
    logs_root: Path = Field(default_factory=_default_logs_root)
    steamcmd_path: str = "/usr/games/steamcmd"
    default_server_user: str = "pzlauncher"

    @property
    def database_path(self) -> Path:
        return self.data_root / "app.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def backups_root(self) -> Path:
        return self.data_root / "backups"

    @property
    def servers_root(self) -> Path:
        return self.data_root / "servers"

    @property
    def templates_root(self) -> Path:
        return Path(__file__).parent / "templates"

    @property
    def static_root(self) -> Path:
        return Path(__file__).parent / "static"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.logs_root,
            self.backups_root,
            self.servers_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
