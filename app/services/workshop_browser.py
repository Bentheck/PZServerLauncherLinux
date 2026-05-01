from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import httpx

from app.models import ServerProfile


STEAM_QUERY_FILES_ENDPOINT = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
STEAM_DETAILS_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAM_COLLECTION_DETAILS_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
PROJECT_ZOMBOID_APP_ID = 108600


@dataclass(frozen=True, slots=True)
class SteamWorkshopRemoteItem:
    workshop_id: str
    title: str
    description: str
    preview_url: str | None
    tags: list[str]
    kind: str
    child_workshop_ids: list[str]


@dataclass(frozen=True, slots=True)
class WorkshopBrowserItem:
    workshop_id: str
    title: str
    description: str
    preview_url: str | None
    source: str
    source_label: str
    kind: str
    kind_label: str
    is_installed_locally: bool
    is_queued: bool
    mod_ids: list[str]
    map_ids: list[str]
    child_workshop_ids: list[str]
    collection_item_count: int
    tags: list[str]
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkshopBrowserPreviewChild:
    workshop_id: str
    title: str
    is_installed_locally: bool
    is_queued: bool


@dataclass(frozen=True, slots=True)
class WorkshopBrowserPreview:
    item: WorkshopBrowserItem
    workshop_ids_to_add: list[str]
    mod_ids_to_add: list[str]
    map_ids_to_add: list[str]
    collection_children: list[WorkshopBrowserPreviewChild]


@dataclass(frozen=True, slots=True)
class WorkshopBrowserSearchResult:
    query: str
    has_api_key: bool
    items: list[WorkshopBrowserItem]
    diagnostics: list[str]


class WorkshopBrowserService:
    def __init__(self, config_service) -> None:
        self.config_service = config_service

    def search(
        self,
        profile: ServerProfile,
        *,
        current_workshop_ids: list[str],
        current_mod_ids: list[str],
        current_map_ids: list[str],
        query: str,
        api_key: str | None,
        take: int = 24,
    ) -> WorkshopBrowserSearchResult:
        normalized_query = query.strip()
        has_api_key = bool((api_key or "").strip())
        diagnostics: list[str] = []
        local_items = self._build_local_items(profile, current_workshop_ids, current_mod_ids, current_map_ids)
        results: dict[str, WorkshopBrowserItem] = {}

        filtered_local = self._search_local(local_items, normalized_query, take)
        for item in filtered_local:
            results[item.workshop_id] = item

        if normalized_query and has_api_key:
            try:
                for remote_item in self._search_remote_items(api_key or "", normalized_query, take):
                    merged = self._merge_browser_items(
                        results.get(remote_item.workshop_id),
                        self._build_remote_item(remote_item, current_workshop_ids, current_mod_ids, current_map_ids),
                    )
                    results[merged.workshop_id] = merged

                for remote_item in self._search_remote_collections(api_key or "", normalized_query, take):
                    merged = self._merge_browser_items(
                        results.get(remote_item.workshop_id),
                        self._build_remote_item(remote_item, current_workshop_ids, current_mod_ids, current_map_ids),
                    )
                    results[merged.workshop_id] = merged

                diagnostics.append("Steam Workshop search is enabled with the configured Web API key.")
            except httpx.HTTPError as exc:
                diagnostics.append(f"Steam search failed: {exc}. Local cache results are still available.")
        elif normalized_query:
            diagnostics.append("Steam search is unavailable until a Steam Web API key is configured. Local cache search and manual Workshop ID / URL lookup still work.")

        manual_lookup_id = self._normalize_workshop_lookup(normalized_query)
        if manual_lookup_id:
            try:
                preview = self.get_preview(
                    profile,
                    current_workshop_ids=current_workshop_ids,
                    current_mod_ids=current_mod_ids,
                    current_map_ids=current_map_ids,
                    workshop_id=manual_lookup_id,
                    api_key=api_key,
                )
            except httpx.HTTPError as exc:
                diagnostics.append(f"Manual Workshop lookup failed: {exc}.")
                preview = None
            if preview is not None:
                merged = self._merge_browser_items(results.get(preview.item.workshop_id), preview.item)
                results[merged.workshop_id] = merged

        if local_items:
            diagnostics.append(f"Indexed {len(local_items)} local workshop item(s) under the managed server paths.")
        else:
            diagnostics.append("No local workshop content is available under the managed server paths yet.")

        items = sorted(
            results.values(),
            key=lambda item: (
                0 if item.is_queued else 1,
                0 if item.is_installed_locally else 1,
                0 if item.kind == "collection" else 1,
                item.title.lower(),
            ),
        )[: max(1, min(take, 50))]

        return WorkshopBrowserSearchResult(
            query=normalized_query,
            has_api_key=has_api_key,
            items=items,
            diagnostics=diagnostics,
        )

    def get_preview(
        self,
        profile: ServerProfile,
        *,
        current_workshop_ids: list[str],
        current_mod_ids: list[str],
        current_map_ids: list[str],
        workshop_id: str,
        api_key: str | None,
    ) -> WorkshopBrowserPreview | None:
        current_workshop_lookup = {value.lower() for value in current_workshop_ids}
        current_mod_lookup = {value.lower() for value in current_mod_ids}
        current_map_lookup = {value.lower() for value in current_map_ids}

        local_items = self._build_local_items(profile, current_workshop_ids, current_mod_ids, current_map_ids)
        local_lookup = {item.workshop_id.lower(): item for item in local_items}
        local_item = local_lookup.get(workshop_id.lower())

        try:
            collection = self._get_collection(workshop_id)
        except httpx.HTTPError:
            collection = None
        if collection is not None:
            try:
                child_details = {
                    item.workshop_id.lower(): item
                    for item in self._get_details(collection.child_workshop_ids)
                }
            except httpx.HTTPError:
                child_details = {}

            child_items: list[WorkshopBrowserItem] = []
            for child_workshop_id in collection.child_workshop_ids:
                local_child = local_lookup.get(child_workshop_id.lower())
                remote_child = child_details.get(child_workshop_id.lower())
                merged_child = self._merge_browser_items(
                    local_child,
                    self._build_remote_item(remote_child, current_workshop_ids, current_mod_ids, current_map_ids) if remote_child is not None else None,
                )
                if merged_child is None:
                    merged_child = WorkshopBrowserItem(
                        workshop_id=child_workshop_id,
                        title=f"Workshop item {child_workshop_id}",
                        description="",
                        preview_url=None,
                        source="steam-details",
                        source_label="Steam details",
                        kind="item",
                        kind_label="Workshop item",
                        is_installed_locally=False,
                        is_queued=child_workshop_id.lower() in current_workshop_lookup,
                        mod_ids=[],
                        map_ids=[],
                        child_workshop_ids=[],
                        collection_item_count=0,
                        tags=[],
                    )
                child_items.append(merged_child)

            aggregated_mod_ids = self._unique_list(mod_id for item in child_items for mod_id in item.mod_ids)
            aggregated_map_ids = self._unique_list(map_id for item in child_items for map_id in item.map_ids)
            preview_item = self._merge_browser_items(
                local_item,
                self._build_remote_item(collection, current_workshop_ids, current_mod_ids, current_map_ids),
            )
            if preview_item is None:
                return None

            preview_item = WorkshopBrowserItem(
                workshop_id=preview_item.workshop_id,
                title=preview_item.title,
                description=preview_item.description,
                preview_url=preview_item.preview_url,
                source=preview_item.source,
                source_label=preview_item.source_label,
                kind="collection",
                kind_label="Collection",
                is_installed_locally=all(item.is_installed_locally for item in child_items) if child_items else False,
                is_queued=all(child.workshop_id.lower() in current_workshop_lookup for child in child_items) if child_items else False,
                mod_ids=aggregated_mod_ids,
                map_ids=aggregated_map_ids,
                child_workshop_ids=self._unique_list(item.workshop_id for item in child_items),
                collection_item_count=len(child_items),
                tags=preview_item.tags,
                source_path=preview_item.source_path,
            )

            return WorkshopBrowserPreview(
                item=preview_item,
                workshop_ids_to_add=[
                    item.workshop_id
                    for item in child_items
                    if item.workshop_id.lower() not in current_workshop_lookup
                ],
                mod_ids_to_add=[
                    mod_id
                    for mod_id in aggregated_mod_ids
                    if mod_id.lower() not in current_mod_lookup
                ],
                map_ids_to_add=[
                    map_id
                    for map_id in aggregated_map_ids
                    if map_id.lower() not in current_map_lookup
                ],
                collection_children=[
                    WorkshopBrowserPreviewChild(
                        workshop_id=item.workshop_id,
                        title=item.title,
                        is_installed_locally=item.is_installed_locally,
                        is_queued=item.is_queued,
                    )
                    for item in child_items
                ],
            )

        try:
            remote_detail = self._get_detail(workshop_id)
        except httpx.HTTPError:
            remote_detail = None
        preview_item = self._merge_browser_items(
            local_item,
            self._build_remote_item(remote_detail, current_workshop_ids, current_mod_ids, current_map_ids) if remote_detail is not None else None,
        )
        if preview_item is None:
            return None

        workshop_ids_to_add = []
        if preview_item.workshop_id.lower() not in current_workshop_lookup:
            workshop_ids_to_add.append(preview_item.workshop_id)

        return WorkshopBrowserPreview(
            item=preview_item,
            workshop_ids_to_add=workshop_ids_to_add,
            mod_ids_to_add=[mod_id for mod_id in preview_item.mod_ids if mod_id.lower() not in current_mod_lookup],
            map_ids_to_add=[map_id for map_id in preview_item.map_ids if map_id.lower() not in current_map_lookup],
            collection_children=[],
        )

    def _build_local_items(
        self,
        profile: ServerProfile,
        current_workshop_ids: list[str],
        current_mod_ids: list[str],
        current_map_ids: list[str],
    ) -> list[WorkshopBrowserItem]:
        queued_workshop_lookup = {value.lower() for value in current_workshop_ids}
        queued_mod_lookup = {value.lower() for value in current_mod_ids}
        queued_map_lookup = {value.lower() for value in current_map_ids}
        items = self.config_service.list_installed_workshop_catalog(profile)
        return [
            WorkshopBrowserItem(
                workshop_id=item.workshop_id,
                title=item.title,
                description="",
                preview_url=None,
                source="local",
                source_label="Local cache",
                kind="item",
                kind_label="Workshop item",
                is_installed_locally=True,
                is_queued=(
                    item.workshop_id.lower() in queued_workshop_lookup or
                    any(mod_id.lower() in queued_mod_lookup for mod_id in item.mod_ids) or
                    any(map_id.lower() in queued_map_lookup for map_id in item.map_ids)
                ),
                mod_ids=list(item.mod_ids),
                map_ids=list(item.map_ids),
                child_workshop_ids=[],
                collection_item_count=0,
                tags=[],
                source_path=item.source_path,
            )
            for item in items
        ]

    @staticmethod
    def _search_local(items: list[WorkshopBrowserItem], query: str, take: int) -> list[WorkshopBrowserItem]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return items[: max(1, min(take, 50))]

        results: list[WorkshopBrowserItem] = []
        for item in items:
            haystacks = [item.workshop_id, item.title, item.description, *item.mod_ids, *item.map_ids, *item.tags]
            if any(normalized_query in haystack.lower() for haystack in haystacks if haystack):
                results.append(item)
        return results[: max(1, min(take, 50))]

    def _search_remote_items(self, api_key: str, query: str, take: int) -> list[SteamWorkshopRemoteItem]:
        payload = {
            "key": api_key,
            "appid": str(PROJECT_ZOMBOID_APP_ID),
            "creator_appid": str(PROJECT_ZOMBOID_APP_ID),
            "search_text": query,
            "numperpage": str(max(1, min(take, 50))),
            "cursor": "*",
            "query_type": "12",
            "return_short_description": "true",
            "return_previews": "true",
            "return_tags": "true",
            "strip_description_bbcode": "true",
        }
        return self._query_remote_search(STEAM_QUERY_FILES_ENDPOINT, payload, explicit_kind="item")

    def _search_remote_collections(self, api_key: str, query: str, take: int) -> list[SteamWorkshopRemoteItem]:
        payload = {
            "key": api_key,
            "appid": str(PROJECT_ZOMBOID_APP_ID),
            "creator_appid": str(PROJECT_ZOMBOID_APP_ID),
            "search_text": query,
            "numperpage": str(max(1, min(take, 50))),
            "cursor": "*",
            "query_type": "12",
            "filetype": "1",
            "return_short_description": "true",
            "return_previews": "true",
            "return_tags": "true",
            "return_children": "true",
            "strip_description_bbcode": "true",
        }
        return self._query_remote_search(STEAM_QUERY_FILES_ENDPOINT, payload, explicit_kind="collection")

    def _query_remote_search(self, url: str, payload: dict[str, str], *, explicit_kind: str) -> list[SteamWorkshopRemoteItem]:
        with httpx.Client(timeout=10.0, headers={"User-Agent": "PZServerLauncherLinux/0.1"}) as client:
            response = client.get(url, params=payload)
            if response.status_code >= 400:
                response = client.post(url, data=payload)
            response.raise_for_status()
            body = response.json()

        items = body.get("response", {}).get("publishedfiledetails", [])
        if not isinstance(items, list):
            return []

        results: list[SteamWorkshopRemoteItem] = []
        for item in items:
            parsed = self._parse_remote_item(item, explicit_kind=explicit_kind)
            if parsed is not None:
                results.append(parsed)
        return results

    def _get_detail(self, workshop_id: str) -> SteamWorkshopRemoteItem | None:
        items = self._get_details([workshop_id])
        return items[0] if items else None

    def _get_details(self, workshop_ids: list[str]) -> list[SteamWorkshopRemoteItem]:
        normalized_ids = self._unique_list(workshop_id.strip() for workshop_id in workshop_ids if workshop_id.strip())
        if not normalized_ids:
            return []

        payload: dict[str, str] = {"itemcount": str(len(normalized_ids))}
        for index, workshop_id in enumerate(normalized_ids):
            payload[f"publishedfileids[{index}]"] = workshop_id

        with httpx.Client(timeout=10.0, headers={"User-Agent": "PZServerLauncherLinux/0.1"}) as client:
            response = client.post(STEAM_DETAILS_ENDPOINT, data=payload)
            response.raise_for_status()
            body = response.json()

        items = body.get("response", {}).get("publishedfiledetails", [])
        if not isinstance(items, list):
            return []

        results: list[SteamWorkshopRemoteItem] = []
        for item in items:
            parsed = self._parse_remote_item(item, explicit_kind=None)
            if parsed is not None:
                results.append(parsed)
        return results

    def _get_collection(self, workshop_id: str) -> SteamWorkshopRemoteItem | None:
        normalized_id = workshop_id.strip()
        if normalized_id == "":
            return None

        payload = {
            "collectioncount": "1",
            "publishedfileids[0]": normalized_id,
        }
        with httpx.Client(timeout=10.0, headers={"User-Agent": "PZServerLauncherLinux/0.1"}) as client:
            response = client.post(STEAM_COLLECTION_DETAILS_ENDPOINT, data=payload)
            response.raise_for_status()
            body = response.json()

        details = body.get("response", {}).get("collectiondetails", [])
        if not isinstance(details, list) or not details:
            return None

        detail = details[0]
        if detail.get("result") not in {1, "1"}:
            return None

        children = detail.get("children", [])
        child_ids = self._unique_list(
            str(child.get("publishedfileid", "")).strip()
            for child in children
            if isinstance(child, dict)
        )
        metadata = self._get_detail(normalized_id)
        if metadata is not None:
            return SteamWorkshopRemoteItem(
                workshop_id=metadata.workshop_id,
                title=metadata.title,
                description=metadata.description,
                preview_url=metadata.preview_url,
                tags=metadata.tags,
                kind="collection",
                child_workshop_ids=child_ids,
            )

        return SteamWorkshopRemoteItem(
            workshop_id=normalized_id,
            title=f"Collection {normalized_id}",
            description="",
            preview_url=None,
            tags=[],
            kind="collection",
            child_workshop_ids=child_ids,
        )

    def _build_remote_item(
        self,
        remote_item: SteamWorkshopRemoteItem | None,
        current_workshop_ids: list[str],
        current_mod_ids: list[str],
        current_map_ids: list[str],
    ) -> WorkshopBrowserItem | None:
        if remote_item is None:
            return None

        queued_workshop_lookup = {value.lower() for value in current_workshop_ids}
        queued_mod_lookup = {value.lower() for value in current_mod_ids}
        queued_map_lookup = {value.lower() for value in current_map_ids}
        mod_ids = self._extract_from_description(remote_item.description, "Mod ID")
        map_ids = self._unique_list(
            [
                *self._extract_from_description(remote_item.description, "Map Folder"),
                *self._extract_from_description(remote_item.description, "Map"),
            ]
        )
        return WorkshopBrowserItem(
            workshop_id=remote_item.workshop_id,
            title=remote_item.title,
            description=remote_item.description,
            preview_url=remote_item.preview_url,
            source="steam-search" if remote_item.kind == "item" else "steam-collection",
            source_label="Steam search" if remote_item.kind == "item" else "Steam collection",
            kind=remote_item.kind,
            kind_label="Collection" if remote_item.kind == "collection" else "Workshop item",
            is_installed_locally=False,
            is_queued=(
                remote_item.workshop_id.lower() in queued_workshop_lookup or
                any(mod_id.lower() in queued_mod_lookup for mod_id in mod_ids) or
                any(map_id.lower() in queued_map_lookup for map_id in map_ids)
            ),
            mod_ids=mod_ids,
            map_ids=map_ids,
            child_workshop_ids=list(remote_item.child_workshop_ids),
            collection_item_count=len(remote_item.child_workshop_ids),
            tags=list(remote_item.tags),
        )

    @staticmethod
    def _merge_browser_items(left: WorkshopBrowserItem | None, right: WorkshopBrowserItem | None) -> WorkshopBrowserItem | None:
        if left is None:
            return right
        if right is None:
            return left

        if left.source == "local" and right.source.startswith("steam"):
            source = "merged"
            source_label = "Local + Steam"
        else:
            source = left.source
            source_label = left.source_label

        kind = "collection" if left.kind == "collection" or right.kind == "collection" else "item"
        return WorkshopBrowserItem(
            workshop_id=left.workshop_id,
            title=left.title if left.title and not left.title.startswith("Workshop item ") else right.title,
            description=left.description or right.description,
            preview_url=left.preview_url or right.preview_url,
            source=source,
            source_label=source_label,
            kind=kind,
            kind_label="Collection" if kind == "collection" else "Workshop item",
            is_installed_locally=left.is_installed_locally or right.is_installed_locally,
            is_queued=left.is_queued or right.is_queued,
            mod_ids=WorkshopBrowserService._unique_list([*left.mod_ids, *right.mod_ids]),
            map_ids=WorkshopBrowserService._unique_list([*left.map_ids, *right.map_ids]),
            child_workshop_ids=WorkshopBrowserService._unique_list([*left.child_workshop_ids, *right.child_workshop_ids]),
            collection_item_count=max(left.collection_item_count, right.collection_item_count, len(left.child_workshop_ids), len(right.child_workshop_ids)),
            tags=WorkshopBrowserService._unique_list([*left.tags, *right.tags]),
            source_path=left.source_path or right.source_path,
        )

    @staticmethod
    def _parse_remote_item(payload: dict, *, explicit_kind: str | None) -> SteamWorkshopRemoteItem | None:
        workshop_id = str(payload.get("publishedfileid", "")).strip()
        if workshop_id == "":
            return None

        title = str(payload.get("title", "")).strip() or f"Workshop item {workshop_id}"
        description = (
            str(payload.get("short_description", "")).strip() or
            str(payload.get("file_description", "")).strip() or
            str(payload.get("description", "")).strip()
        )
        preview_url = str(payload.get("preview_url", "")).strip() or None
        tags_payload = payload.get("tags", [])
        tags = []
        if isinstance(tags_payload, list):
            tags = [
                str(tag.get("tag", "")).strip()
                for tag in tags_payload
                if isinstance(tag, dict) and str(tag.get("tag", "")).strip()
            ]
        children_payload = payload.get("children", [])
        child_workshop_ids = []
        if isinstance(children_payload, list):
            child_workshop_ids = [
                str(child.get("publishedfileid", "")).strip()
                for child in children_payload
                if isinstance(child, dict) and str(child.get("publishedfileid", "")).strip()
            ]
        kind = explicit_kind or ("collection" if child_workshop_ids else "item")
        return SteamWorkshopRemoteItem(
            workshop_id=workshop_id,
            title=title,
            description=description,
            preview_url=preview_url,
            tags=WorkshopBrowserService._unique_list(tags),
            kind=kind,
            child_workshop_ids=WorkshopBrowserService._unique_list(child_workshop_ids),
        )

    @staticmethod
    def _extract_from_description(description: str, label: str) -> list[str]:
        pattern = re.compile(rf"{re.escape(label)}\s*:\s*(?P<value>[^\r\n]+)", re.IGNORECASE)
        return WorkshopBrowserService._unique_list(
            match.group("value").strip()
            for match in pattern.finditer(description or "")
            if match.group("value").strip()
        )

    @staticmethod
    def _normalize_workshop_lookup(query: str) -> str | None:
        normalized_query = query.strip()
        if normalized_query == "":
            return None

        url_match = re.search(r"[?&]id=(?P<id>\d+)", normalized_query, re.IGNORECASE)
        if url_match:
            return url_match.group("id")

        if re.fullmatch(r"\d+", normalized_query):
            return normalized_query

        return None

    @staticmethod
    def _unique_list(values: Iterable[str]) -> list[str]:
        items: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            items.append(item)
        return items
