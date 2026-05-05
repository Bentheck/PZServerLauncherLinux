from __future__ import annotations

import importlib.metadata
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


@dataclass(frozen=True)
class LauncherUpdateStatus:
    state: str
    current_version: str
    latest_version: str | None
    release_title: str | None
    release_page_url: str | None
    published_at_utc: datetime | None
    checked_at_utc: datetime
    status_message: str

    @property
    def state_label(self) -> str:
        if self.state == "update_available":
            return "Update available"
        if self.state == "up_to_date":
            return "Up to date"
        return "Check unavailable"

    @property
    def published_label(self) -> str:
        if self.published_at_utc is None:
            return "Unavailable"
        return self.published_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def checked_label(self) -> str:
        return self.checked_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class ReleaseCheckService:
    _REPOSITORY_OWNER = "Bentheck"
    _REPOSITORY_NAME = "PZServerLauncherLinux"
    _SUCCESS_CACHE_DURATION = timedelta(minutes=30)
    _FAILURE_CACHE_DURATION = timedelta(minutes=5)

    def __init__(
        self,
        package_name: str = "pzserverlauncherlinux",
        pyproject_path: Path | None = None,
        current_version_override: str | None = None,
    ) -> None:
        self._package_name = package_name
        self._pyproject_path = pyproject_path or Path(__file__).resolve().parents[2] / "pyproject.toml"
        self._current_version_override = current_version_override
        self._cached_status: LauncherUpdateStatus | None = None
        self._cached_until_utc: datetime | None = None

    def get_status(self, force_refresh: bool = False) -> LauncherUpdateStatus:
        now_utc = datetime.now(timezone.utc)
        if (
            not force_refresh
            and self._cached_status is not None
            and self._cached_until_utc is not None
            and now_utc < self._cached_until_utc
        ):
            return self._cached_status

        current_version = self._normalize_version(self._current_version_override or self._resolve_current_version())

        try:
            with httpx.Client(
                timeout=10.0,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"PZServerLauncherLinux/{current_version}",
                },
            ) as client:
                response = client.get(
                    f"https://api.github.com/repos/{self._REPOSITORY_OWNER}/{self._REPOSITORY_NAME}/releases/latest"
                )
                response.raise_for_status()
                payload = response.json()

            latest_version = self._normalize_version(str(payload.get("tag_name") or payload.get("name") or ""))
            if not latest_version:
                raise ValueError("GitHub latest release did not include a recognizable version.")

            state = (
                "update_available"
                if self._compare_versions(current_version, latest_version) < 0
                else "up_to_date"
            )
            published_at_raw = payload.get("published_at")
            published_at = (
                datetime.fromisoformat(str(published_at_raw).replace("Z", "+00:00"))
                if published_at_raw
                else None
            )
            status = LauncherUpdateStatus(
                state=state,
                current_version=current_version,
                latest_version=latest_version,
                release_title=(str(payload.get("name") or payload.get("tag_name") or "") or None),
                release_page_url=(str(payload.get("html_url") or "") or self._build_releases_page_url()),
                published_at_utc=published_at,
                checked_at_utc=now_utc,
                status_message=(
                    f"Version {latest_version} is available on GitHub. You're on {current_version}."
                    if state == "update_available"
                    else f"You're on {current_version}. The latest stable release is {latest_version}."
                ),
            )
            self._store_cached_status(status, self._SUCCESS_CACHE_DURATION)
            return status
        except (httpx.HTTPError, ValueError, TypeError):
            status = LauncherUpdateStatus(
                state="unavailable",
                current_version=current_version,
                latest_version=None,
                release_title=None,
                release_page_url=self._build_releases_page_url(),
                published_at_utc=None,
                checked_at_utc=now_utc,
                status_message="Unable to check GitHub releases right now.",
            )
            self._store_cached_status(status, self._FAILURE_CACHE_DURATION)
            return status

    def _store_cached_status(self, status: LauncherUpdateStatus, duration: timedelta) -> None:
        self._cached_status = status
        self._cached_until_utc = status.checked_at_utc + duration

    def _resolve_current_version(self) -> str:
        try:
            return importlib.metadata.version(self._package_name)
        except importlib.metadata.PackageNotFoundError:
            if self._pyproject_path.exists():
                content = self._pyproject_path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'^\s*version\s*=\s*"([^"]+)"\s*$', content, re.MULTILINE)
                if match:
                    return match.group(1)
        return "0.0.0"

    @staticmethod
    def _normalize_version(value: str) -> str:
        normalized = value.strip()
        if normalized.startswith(("v", "V")):
            normalized = normalized[1:]

        if "-" in normalized:
            normalized = normalized.split("-", 1)[0]
        if "+" in normalized:
            normalized = normalized.split("+", 1)[0]

        return normalized.strip() or "0.0.0"

    @classmethod
    def _compare_versions(cls, current_version: str, latest_version: str) -> int:
        current_parts = cls._parse_version_parts(current_version)
        latest_parts = cls._parse_version_parts(latest_version)
        if current_parts == latest_parts:
            return 0
        return -1 if current_parts < latest_parts else 1

    @classmethod
    def _parse_version_parts(cls, value: str) -> tuple[int, int, int, int]:
        parts: list[int] = []
        for segment in cls._normalize_version(value).split("."):
            digits = "".join(character for character in segment if character.isdigit())
            parts.append(int(digits or "0"))
            if len(parts) == 4:
                break

        while len(parts) < 4:
            parts.append(0)

        return tuple(parts[:4])

    @classmethod
    def _build_releases_page_url(cls) -> str:
        return f"https://github.com/{cls._REPOSITORY_OWNER}/{cls._REPOSITORY_NAME}/releases"
