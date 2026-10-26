"""追蹤「既有持股因價格波動被動超限」的天數（違規規則第4點的5日緩衝）。

主辦方規則：
- 因「當日加碼」導致超限 → 當天就無效並記警告（這種情況由 decision_engine 的下單前
  clamp 擋掉，不會進到這裡）。
- 因「股價波動」造成既有持股被動超過權重上限 → 給 5 個交易日緩衝自行減碼，
  第6個交易日仍超限才記警告。

DataPack 本身不會告訴你「這檔超限幾天了」，所以需要本地持久化狀態，每個交易日跑一次
D-Plan流程時更新一次。狀態檔：state/drift_state.json。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import config


@dataclass
class DriftStatus:
    ticker: str
    trading_days_in_breach: int
    breach_since_trade_date: str

    @property
    def is_urgent(self) -> bool:
        """已經到第5個交易日（含）還沒解決：今天是最後機會，第6天仍超限就記警告。"""
        return self.trading_days_in_breach >= config.DRIFT_GRACE_TRADING_DAYS


def load_state() -> dict:
    if not config.DRIFT_STATE_PATH.exists():
        return {}
    with open(config.DRIFT_STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.DRIFT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_drift_state(
    *,
    trade_date: str,
    tradable_weights: dict[str, float],
    weight_cap_for: callable,
) -> list[DriftStatus]:
    """每個交易日呼叫一次。

    tradable_weights: {ticker: 目前權重}（來自 DataPack 的 holdings[].tradable_weight）
    weight_cap_for: ticker -> 該檔的權重上限（2330 給 0.25，其餘給 0.10）

    回傳目前仍在「價格波動超限」狀態中的所有 DriftStatus，decision_engine 應該優先
    把 is_urgent=True 的那些排進今天的減碼決策，避免撐到第6個交易日被記警告。
    """
    state = load_state()
    updated_state: dict = {}

    for ticker, weight in tradable_weights.items():
        cap = weight_cap_for(ticker)
        if weight is not None and weight > cap:
            existing = state.get(ticker)
            if existing:
                updated_state[ticker] = {
                    "breach_since_trade_date": existing["breach_since_trade_date"],
                    "trading_days_in_breach": existing["trading_days_in_breach"] + 1,
                }
            else:
                updated_state[ticker] = {
                    "breach_since_trade_date": trade_date,
                    "trading_days_in_breach": 1,
                }
        # 權重未超限：不寫回 updated_state，等同清除追蹤紀錄（代表已自行解決）

    save_state(updated_state)

    return [
        DriftStatus(
            ticker=ticker,
            trading_days_in_breach=info["trading_days_in_breach"],
            breach_since_trade_date=info["breach_since_trade_date"],
        )
        for ticker, info in updated_state.items()
    ]
