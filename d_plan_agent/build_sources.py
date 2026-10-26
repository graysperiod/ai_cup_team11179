"""把 RawDataItem 組裝成 D-Plan 的 sources[]（L0 資料來源層）。"""

from __future__ import annotations

from .fetch_market_data import RawDataItem


def _iso(dt_obj) -> str:
    return dt_obj.isoformat(timespec="seconds")


def build_sources(items: list[RawDataItem]) -> list[dict]:
    """流水號從 S1 開始、連續遞增、不加前導零（見 D-Plan 撰寫指南 ID 命名規則）。"""
    sources = []
    for i, item in enumerate(items, start=1):
        source: dict = {
            "source_id": f"S{i}",
            "authority": item.authority,
            "url": item.url,
            "content_as_of": _iso(item.content_as_of),
        }
        if item.name:
            source["name"] = item.name
        if item.archive_url:
            source["archive_url"] = item.archive_url
        if item.published_at:
            source["published_at"] = _iso(item.published_at)
        if item.fetched_at:
            source["fetched_at"] = _iso(item.fetched_at)
        sources.append(source)
    return sources
