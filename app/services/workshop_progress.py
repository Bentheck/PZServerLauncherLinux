from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

NUMERIC_TOKEN_RE = re.compile(r"(?<!\d)(?P<value>\d{8,12})(?!\d)")
COMPLETION_KEYWORDS = (
    "complete",
    "completed",
    "finished",
    "done",
    "success",
    "succeeded",
    "installed",
    "ready",
)


@dataclass(frozen=True, slots=True)
class WorkshopDownloadProgress:
    current_item_index: int
    total_item_count: int
    current_workshop_id: str | None
    last_raw_line: str
    is_complete: bool
    updated_at: datetime

    @property
    def status_label(self) -> str:
        if self.is_complete:
            return f"Workshop download complete ({self.total_item_count}/{self.total_item_count})"
        return f"Downloading workshop item {self.current_item_index}/{self.total_item_count}"

    @property
    def detail_label(self) -> str:
        if not self.current_workshop_id:
            return self.status_label
        return f"{self.status_label} | Workshop ID {self.current_workshop_id}"


class WorkshopDownloadProgressTracker:
    def __init__(self, configured_workshop_ids: list[str]) -> None:
        unique_ids: list[str] = []
        seen: set[str] = set()
        for workshop_id in configured_workshop_ids:
            normalized = workshop_id.strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            unique_ids.append(normalized)

        self._ordered_workshop_ids = unique_ids
        self._index_lookup = {
            workshop_id.lower(): index + 1
            for index, workshop_id in enumerate(self._ordered_workshop_ids)
        }
        self.current: WorkshopDownloadProgress | None = None

    @property
    def has_configured_items(self) -> bool:
        return bool(self._ordered_workshop_ids)

    def observe(self, line: str, updated_at: datetime) -> WorkshopDownloadProgress | None:
        if not self.has_configured_items or not line.strip():
            return self.current

        matched_workshop_id = self._find_configured_workshop_id(line)
        if matched_workshop_id is None:
            if (
                self.current is not None
                and not self.current.is_complete
                and self.current.current_item_index == self.current.total_item_count
                and self._looks_like_completion(line)
            ):
                self.current = WorkshopDownloadProgress(
                    current_item_index=self.current.current_item_index,
                    total_item_count=self.current.total_item_count,
                    current_workshop_id=self.current.current_workshop_id,
                    last_raw_line=line,
                    is_complete=True,
                    updated_at=updated_at,
                )
            return self.current

        observed_index = self._index_lookup[matched_workshop_id.lower()]
        current_index = observed_index if self.current is None else max(self.current.current_item_index, observed_index)
        effective_workshop_id = (
            self.current.current_workshop_id
            if self.current is not None and observed_index < self.current.current_item_index
            else matched_workshop_id
        )
        is_complete = current_index == len(self._ordered_workshop_ids) and self._looks_like_completion(line)

        self.current = WorkshopDownloadProgress(
            current_item_index=current_index,
            total_item_count=len(self._ordered_workshop_ids),
            current_workshop_id=effective_workshop_id,
            last_raw_line=line,
            is_complete=is_complete,
            updated_at=updated_at,
        )
        return self.current

    def _find_configured_workshop_id(self, line: str) -> str | None:
        for match in NUMERIC_TOKEN_RE.finditer(line):
            value = match.group("value")
            if value.lower() in self._index_lookup:
                return value
        return None

    @staticmethod
    def _looks_like_completion(line: str) -> bool:
        lowered = line.lower()
        return any(keyword in lowered for keyword in COMPLETION_KEYWORDS)
