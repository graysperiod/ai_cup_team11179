"""下載／解析當日 DataPack（見 DataPack_規格書_v4.3.md）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DataPackNotReady(Exception):
    """missing 含 prev_settlement：account/holdings 整段為 null，須等結算完成後重新下載。"""


@dataclass
class Holding:
    code: str
    shares: int
    receivable_shares: int
    close_price: float | None
    tradable_weight: float | None


@dataclass
class DataPack:
    raw: dict[str, Any]
    team_id: str
    trade_date: str
    as_of_date: str | None
    missing: list[str]
    cash: float | None
    cash_ratio_tradable: float | None
    nav_tradable: float | None
    aum: float | None
    holdings_count: int | None
    holdings: list[Holding] | None

    @property
    def is_first_trading_day(self) -> bool:
        """as_of_date 為 null 且 missing 不含 prev_settlement：第一個交易日，account 為真值全現金。"""
        return self.as_of_date is None and "prev_settlement" not in self.missing

    def holdings_by_code(self) -> dict[str, Holding]:
        if self.holdings is None:
            return {}
        return {h.code: h for h in self.holdings}


def load_datapack(path: str | Path) -> DataPack:
    """從本地檔案載入 DataPack JSON。實際比賽請先用參賽者後台「下載資料包」把檔案存到本地，
    再呼叫本函式；本函式不處理登入/下載，只負責解析。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return parse_datapack(raw)


def parse_datapack(raw: dict[str, Any]) -> DataPack:
    if raw.get("doc_type") != "DataPack":
        raise ValueError(f"doc_type 應為 DataPack，實際為 {raw.get('doc_type')!r}")

    missing = raw.get("missing", [])
    account = raw.get("account")
    holdings_raw = raw.get("holdings")

    if "prev_settlement" in missing:
        if account is not None or holdings_raw is not None:
            raise ValueError("missing 含 prev_settlement 時，account 與 holdings 應整段為 null")
        raise DataPackNotReady(
            f"trade_date={raw.get('trade_date')}：前一交易日結算尚未產生，"
            "account/holdings 為 null，請等結算完成後重新下載"
        )

    holdings: list[Holding] | None = None
    if holdings_raw is not None:
        holdings = [
            Holding(
                code=h["code"],
                shares=h["shares"],
                receivable_shares=h["receivable_shares"],
                close_price=h["close_price"],
                tradable_weight=h["tradable_weight"],
            )
            for h in holdings_raw
        ]

    return DataPack(
        raw=raw,
        team_id=raw["team_id"],
        trade_date=raw["trade_date"],
        as_of_date=raw.get("as_of_date"),
        missing=missing,
        cash=account["cash"] if account else None,
        cash_ratio_tradable=account["cash_ratio_tradable"] if account else None,
        nav_tradable=account["nav_tradable"] if account else None,
        aum=account["aum"] if account else None,
        holdings_count=account["holdings_count"] if account else None,
        holdings=holdings,
    )
