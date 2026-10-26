"""讀取150檔標的池（玉山挑戰賽投資組合及交易標的-個股清單）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from . import config


@dataclass(frozen=True)
class UniverseStock:
    code: str
    name: str
    market: str  # "TWSE" | "TPEX"


@lru_cache(maxsize=1)
def load_universe() -> dict[str, UniverseStock]:
    """回傳 {證券代號: UniverseStock}，150檔，key 為字串代號。"""
    with open(config.UNIVERSE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    stocks = {
        item["code"]: UniverseStock(code=item["code"], name=item["name"], market=item["market"])
        for item in data["universe"]
    }
    if len(stocks) != 150:
        raise ValueError(f"標的池應為150檔，實際讀到 {len(stocks)} 檔，請檢查 {config.UNIVERSE_PATH}")
    return stocks


def is_in_universe(ticker: str) -> bool:
    return ticker in load_universe()
