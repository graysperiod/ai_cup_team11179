"""針對 decision_engine._scale_to_cash_target 的「買進額度不夠就收回賣出力道」邏輯，
單獨用假訊號測試，不用真的呼叫GPT——這是2026-10-26實測發現的真實bug：
risk_off情境下GPT建議大量減碼，但候選新標的只有1檔、個股上限卡住買不了那麼多，
導致現金比衝到29.3%（超過比賽規定的25%上限）。

跑法：
    cd ai_cup && python3 tests/test_decision_engine_cash_scaling.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from d_plan_agent import config  # noqa: E402
from d_plan_agent.decision_engine import build_decisions  # noqa: E402
from d_plan_agent.llm_inferences import InferenceSignal  # noqa: E402


def main() -> None:
    # 22檔持股，每檔權重約3.9%（22*0.039≈0.864，現金約13.6%，貼近規格書範例的情境）
    held_tickers = {f"{1000+i:04d}" for i in range(22)}
    current_weights = {t: 0.039 for t in held_tickers}

    # GPT對20檔建議大幅TRIM到接近0（防禦），只對2檔HOLD，只有1檔新標的候選可以BUY
    signals = []
    sorted_held = sorted(held_tickers)
    for t in sorted_held[:20]:
        signals.append(InferenceSignal(inf_id="I1", ticker=t, action_hint="TRIM", target_weight_hint=0.005))
    for t in sorted_held[20:]:
        signals.append(InferenceSignal(inf_id="I1", ticker=t, action_hint="HOLD", target_weight_hint=0.039))
    signals.append(InferenceSignal(inf_id="I1", ticker="5274", action_hint="BUY", target_weight_hint=0.10))

    target_cash_pct_range = (0.15, 0.22)  # 跟實測那次一樣的宣告區間

    result = build_decisions(
        signals=signals,
        held_tickers=held_tickers,
        current_weights=current_weights,
        target_cash_pct_range=target_cash_pct_range,
    )

    total_exposure = sum(d["target_weight"] for d in result.decisions) + sum(
        current_weights[nt["ticker"]] for nt in result.no_trade_decisions
    )
    achieved_cash = 1 - total_exposure
    print(f"達成現金比：{achieved_cash:.4f}（宣告區間 {target_cash_pct_range}，硬上限 {config.CASH_RATIO_MAX}）")

    assert achieved_cash <= config.CASH_RATIO_MAX + 1e-6, f"現金比 {achieved_cash} 超過比賽硬上限 {config.CASH_RATIO_MAX}！"
    lo, hi = target_cash_pct_range
    assert lo - 0.01 <= achieved_cash <= hi + 0.01, f"現金比 {achieved_cash} 離宣告區間 [{lo},{hi}] 太遠，縮放邏輯沒生效"

    sell_all_count = sum(1 for d in result.decisions if d["action"] == "SELL_ALL")
    trim_count = sum(1 for d in result.decisions if d["action"] == "TRIM")
    print(f"TRIM {trim_count} 筆、SELL_ALL {sell_all_count} 筆（賣出力道應該被部分收回，所以減碼幅度會比GPT原本建議的小）")

    original_trim = next(d for d in result.decisions if d["ticker"] == sorted_held[0])
    assert original_trim["target_weight"] > 0.005, "賣出力道應該被收回一部分，target_weight不該還停在GPT原本建議的0.005"

    print("[decision_engine cash scaling test] 全數通過：買進額度不夠時會收回賣出力道，現金比不會超標")


def test_trim_hint_above_current_weight_gets_clamped() -> None:
    """2026-10-26真實GPT輸出重現的bug：GPT標action=TRIM，卻給了一個比目前權重還高的
    target_weight_hint（等於沒有真的減碼，兩者自相矛盾）。decision_engine要能自己修正，
    不能照單全收GPT的數字，不然_scale_to_cash_target算出來的總曝險會被悄悄墊高，
    導致現金比超過比賽規定的25%上限。SELL_ALL也要強制鎖定target_weight=0，
    不管GPT給的hint數字是多少。
    """
    # _check_holdings_count 要求20-30檔，這裡用19檔HOLD湊數，只在1111/2222這兩檔上
    # 製造矛盾訊號，其餘跟本測試的重點無關。padding權重刻意設高一點（0.05），
    # 讓整體曝險已經達標、不需要_scale_to_cash_target的「收回賣出力道」介入，
    # 才能單純驗證語意一致性clamp本身，不被另一個機制的交互作用干擾判讀。
    padding_tickers = {f"{9000+i:04d}" for i in range(19)}
    held_tickers = {"1111", "2222"} | padding_tickers
    current_weights = {"1111": 0.05, "2222": 0.05, **{t: 0.05 for t in padding_tickers}}

    signals = [
        # 矛盾訊號：標TRIM卻給了比目前權重(0.05)還高的hint(0.08)
        InferenceSignal(inf_id="I1", ticker="1111", action_hint="TRIM", target_weight_hint=0.08),
        # SELL_ALL卻給了非零的hint，應該被強制歸零
        InferenceSignal(inf_id="I1", ticker="2222", action_hint="SELL_ALL", target_weight_hint=0.03),
    ] + [
        InferenceSignal(inf_id="I1", ticker=t, action_hint="HOLD", target_weight_hint=0.05) for t in padding_tickers
    ]

    result = build_decisions(
        signals=signals,
        held_tickers=held_tickers,
        current_weights=current_weights,
        target_cash_pct_range=(0.05, 0.10),  # 曝險已經達標，不觸發收回賣出力道的邏輯
    )

    by_ticker = {d["ticker"]: d for d in result.decisions}
    assert by_ticker["1111"]["target_weight"] <= current_weights["1111"] + 1e-9, (
        f"TRIM的target_weight({by_ticker['1111']['target_weight']})不該超過目前權重"
        f"({current_weights['1111']})，GPT給矛盾數字時要被修正"
    )
    assert by_ticker["2222"]["target_weight"] == 0.0, (
        f"SELL_ALL的target_weight應該強制為0，不管GPT給的hint是什麼，實際是"
        f"{by_ticker['2222']['target_weight']}"
    )
    print("[decision_engine 語意一致性 test] 全數通過：TRIM不會超過目前權重，SELL_ALL強制歸零")


if __name__ == "__main__":
    main()
    test_trim_hint_above_current_weight_gets_clamped()
