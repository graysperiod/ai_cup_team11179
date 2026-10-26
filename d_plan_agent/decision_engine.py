"""L3 decisions / no_trade_decisions：把 GPT 的個股信號轉成合規的決策。

職責劃分：GPT（llm_inferences）負責「這檔股票該怎麼動、大概多重」，這裡負責把它榨進
比賽的硬規則裡（持股20-30檔、2330上限25%/其餘10%、現金0-25%、只能買150檔池內的股票），
並處理 GPT 不會知道的機制：價格波動5日緩衝超限的強制減碼。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .drift_tracker import DriftStatus
from .llm_inferences import InferenceSignal
from .universe import is_in_universe


class DecisionEngineError(Exception):
    """無法在不捏造交易訊號的前提下滿足規則（例如持股數不足20檔），需要回頭擴大
    llm_inferences 的候選範圍，不應該由 decision_engine 自己編一筆沒有依據的決策。"""


def weight_cap_for(ticker: str) -> float:
    return config.WEIGHT_CAP_2330 if ticker == config.TICKER_2330 else config.WEIGHT_CAP_DEFAULT


@dataclass
class DecisionResult:
    decisions: list[dict]
    no_trade_decisions: list[dict]


def build_decisions(
    *,
    signals: list[InferenceSignal],
    held_tickers: set[str],
    current_weights: dict[str, float],
    target_cash_pct_range: tuple[float, float],
    drift_statuses: list[DriftStatus] | None = None,
) -> DecisionResult:
    """signals 必須涵蓋 held_tickers 的每一檔（llm_inferences.build_inferences 已經強制檢查過）。
    current_weights：{ticker: 目前tradable_weight}，只需要涵蓋held_tickers，
    給 _scale_to_cash_target 計算「賣出力道最多能收回多少」用。
    """
    drift_by_ticker = {d.ticker: d for d in (drift_statuses or [])}

    by_ticker: dict[str, list[InferenceSignal]] = {}
    for s in signals:
        by_ticker.setdefault(s.ticker, []).append(s)

    uncovered = held_tickers - set(by_ticker)
    if uncovered:
        raise DecisionEngineError(f"以下既有持股沒有對應的推論，無法決定動作：{sorted(uncovered)}")

    plan: dict[str, dict] = {}
    for ticker, sigs in by_ticker.items():
        if not is_in_universe(ticker) and ticker not in held_tickers:
            raise DecisionEngineError(f"{ticker} 不在150檔標的池內，不能新建倉")

        primary = sigs[0]

        if primary.action_hint == "HOLD" and ticker not in held_tickers:
            # 候選新標的被GPT評估後決定「不買」：這檔目前沒有部位，
            # 不該出現在no_trade_decisions（那是給既有持股「不動」用的），整個跳過不留痕跡。
            continue

        inference_refs = [s.inf_id for s in sigs]
        cap = weight_cap_for(ticker)
        action = primary.action_hint
        target_weight = min(max(primary.target_weight_hint, 0.0), cap)
        risk_check = None

        # 語意一致性檢查：GPT有時候標了action=TRIM，卻給了一個比目前權重還高的
        # target_weight_hint（等於沒有真的減碼），兩者矛盾——這裡強制依動作定義修正，
        # 不要照單全收GPT的數字，不然_scale_to_cash_target算出來的曝險會被悄悄墊高
        # （這是2026-10-26實測發現的真實bug：現金比因此超過比賽規定的25%上限）。
        current_weight = current_weights.get(ticker)
        if action == "SELL_ALL":
            target_weight = 0.0
        elif action == "TRIM" and current_weight is not None:
            target_weight = min(target_weight, current_weight)

        drift = drift_by_ticker.get(ticker)
        if drift and drift.is_urgent and action in ("HOLD", "ADD", "BUY"):
            # 價格波動被動超限已到第5個交易日：今天是避免第6天被記警告的最後機會，
            # 強制轉成 TRIM 並把目標權重壓到上限以下，蓋過 GPT 原本的建議。
            action = "TRIM"
            target_weight = cap * 0.9
            risk_check = (
                f"因股價波動已連續{drift.trading_days_in_breach}個交易日超過權重上限，"
                "系統強制減碼以避免第6個交易日被記警告"
            )

        plan[ticker] = {
            "action": action,
            "target_weight": target_weight,
            "inference_refs": inference_refs,
            "risk_check": risk_check,
        }

    _scale_to_cash_target(plan, target_cash_pct_range, current_weights)

    decisions = []
    no_trade_decisions = []
    decision_id_counter = 1
    for ticker, info in plan.items():
        if info["action"] == "HOLD":
            no_trade_decisions.append({"ticker": ticker, "reason_refs": info["inference_refs"]})
            continue
        decision: dict = {
            "decision_id": f"D{decision_id_counter}",
            "ticker": ticker,
            "action": info["action"],
            "target_weight": round(info["target_weight"], 6),
            "inference_refs": info["inference_refs"],
        }
        if info["risk_check"]:
            decision["risk_check"] = info["risk_check"]
        decisions.append(decision)
        decision_id_counter += 1

    _check_holdings_count(decisions, no_trade_decisions, held_tickers)

    return DecisionResult(decisions=decisions, no_trade_decisions=no_trade_decisions)


def _scale_to_cash_target(
    plan: dict[str, dict], target_cash_pct_range: tuple[float, float], current_weights: dict[str, float]
) -> None:
    """讓總曝險大致落在宣告的現金區間中點，分兩步：

    1. 先把 BUY/ADD 等比例縮放去填補缺口（原本的做法）。
    2. 如果縮放後仍然不夠（例如防禦性訊號太多，但新標的的候選只有一兩檔、
       個股上限又卡住買不了那麼多）——不能放著讓現金超標，比賽規則20-30檔＋
       現金上限25%的組合，代表不管多看空都不能把部位空出來變現金，只能靠
       既有持股的分散去消化防禦性調整。所以改成按比例「收回」TRIM/SELL_ALL
       的減碼幅度（往原本權重的方向拉回去一部分），直到缺口補滿或賣出力道用盡。

    這仍然是簡化版本（線性、一次到位，不是疊代最佳化），縮放後若仍不在範圍內，
    交由 validate_dplan 的 posture 檢核攔截，提醒人工調整。
    """
    lo, hi = target_cash_pct_range
    target_exposure = 1 - (lo + hi) / 2

    buyish = {t: p for t, p in plan.items() if p["action"] in ("BUY", "ADD")}
    sellish = {t: p for t, p in plan.items() if p["action"] in ("TRIM", "SELL_ALL")}
    fixed_exposure = sum(p["target_weight"] for t, p in plan.items() if p["action"] not in ("BUY", "ADD"))
    buyish_exposure = sum(p["target_weight"] for p in buyish.values())

    if buyish and buyish_exposure > 0:
        headroom = target_exposure - fixed_exposure
        scale = max(headroom / buyish_exposure, 0.0)
        for ticker, p in buyish.items():
            cap = weight_cap_for(ticker)
            p["target_weight"] = min(p["target_weight"] * scale, cap)

    achieved_exposure = sum(p["target_weight"] for p in plan.values())
    shortfall = target_exposure - achieved_exposure

    if shortfall > 1e-9 and sellish:
        # 賣出力道最多能收回多少：每檔「原本權重」與「GPT建議的減碼後權重」的差距
        max_recoverable = {
            t: max(current_weights.get(t, p["target_weight"]) - p["target_weight"], 0.0) for t, p in sellish.items()
        }
        sellish_room = sum(max_recoverable.values())
        if sellish_room > 0:
            undo_fraction = min(shortfall / sellish_room, 1.0)
            for ticker, p in sellish.items():
                original_weight = current_weights.get(ticker, p["target_weight"])
                p["target_weight"] += undo_fraction * max_recoverable[ticker]
                p["target_weight"] = min(p["target_weight"], original_weight)
                if p["action"] == "SELL_ALL" and p["target_weight"] > 1e-9:
                    # 部分收回出清的話，就不再是「全數出清」了，改記為減碼
                    p["action"] = "TRIM"


def _check_holdings_count(decisions: list[dict], no_trade_decisions: list[dict], held_tickers: set[str]) -> None:
    sell_all = {d["ticker"] for d in decisions if d["action"] == "SELL_ALL"}
    new_buys = {d["ticker"] for d in decisions if d["action"] == "BUY"}
    resulting_holdings = (held_tickers - sell_all) | new_buys
    count = len(resulting_holdings)
    if not (config.MIN_HOLDINGS_COUNT <= count <= config.MAX_HOLDINGS_COUNT):
        raise DecisionEngineError(
            f"委託執行後持股檔數將是 {count} 檔，不在規定的 "
            f"{config.MIN_HOLDINGS_COUNT}-{config.MAX_HOLDINGS_COUNT} 檔範圍內。"
            "請調整 llm_inferences 傳入的 tickers_in_scope（納入更多候選新標的，"
            "或讓更多弱勢持股得到 TRIM/SELL_ALL 的推論），不要在 decision_engine 裡憑空捏造決策。"
        )
