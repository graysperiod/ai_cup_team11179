"""讀取150檔標的池的靜態主題分類（見 主題分類_AI半導體供應鏈.json）。

這份分類決定「哪些股票屬於ETF投資主題的基本候選池」，不受當天有沒有新聞影響；
新聞/法人動向只用來決定候選池裡「今天要買多少、加碼哪個」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from . import config

THEME_CLASSIFICATION_PATH = config.ROOT_DIR / "主題分類_AI半導體供應鏈.json"


@dataclass(frozen=True)
class ThemeClassification:
    code: str
    name: str
    theme_relevant: bool
    sector: str
    confidence: str = "normal"  # "normal" | "low"（低信心代表建議人工覆核）


@lru_cache(maxsize=1)
def load_theme_classification() -> dict[str, ThemeClassification]:
    with open(THEME_CLASSIFICATION_PATH, encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for item in data["tickers"]:
        result[item["code"]] = ThemeClassification(
            code=item["code"],
            name=item["name"],
            theme_relevant=item["theme_relevant"],
            sector=item["sector"],
            confidence=item.get("confidence", "normal"),
        )
    return result


def theme_relevant_tickers() -> set[str]:
    """回傳屬於ETF投資主題的基本候選池（不含是否持有的判斷）。"""
    return {t.code for t in load_theme_classification().values() if t.theme_relevant}
