"""第二個管線測試：TRIM一檔、用賣出資金BUY一檔新標的，驗證最容易出錯的三塊：
官方公式委託推導、funding_for缺口檢核、posture(淨曝險/現金區間)一致性檢核。

這裡跳過 decision_engine 的自動縮放，直接手寫 decisions，專注驗證
order_derivation 與 validate_dplan 本身邏輯正確（照規格公式算、照規格邏輯驗）。

跑法：
    cd ai_cup && python3 tests/test_pipeline_trim_buy.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from d_plan_agent import config  # noqa: E402
from d_plan_agent.fetch_datapack import parse_datapack  # noqa: E402
from d_plan_agent.order_derivation import derive_orders, estimate_net_flow_and_cash  # noqa: E402
from d_plan_agent.validate_dplan import validate_dplan, validate_filename  # noqa: E402

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def main() -> None:
    raw = json.loads((FIXTURES_DIR / "datapack_example.json").read_text(encoding="utf-8"))
    datapack = parse_datapack(raw)
    held = datapack.holdings_by_code()
    held_tickers = {code for code, h in held.items() if h.shares > 0}
    current_shares = {code: h.shares for code, h in held.items()}
    prev_close_prices = {code: h.close_price for code, h in held.items()}

    NEW_TICKER = "5274"
    NEW_TICKER_PRICE = 800.0
    prev_close_prices[NEW_TICKER] = NEW_TICKER_PRICE

    decisions = [
        {
            "decision_id": "D1",
            "ticker": "2049",
            "action": "TRIM",
            "target_weight": 0.01,
            "inference_refs": ["I1"],
            "funding_for": ["D2"],
        },
        {
            "decision_id": "D2",
            "ticker": NEW_TICKER,
            "action": "BUY",
            "target_weight": 0.03,
            "inference_refs": ["I2"],
        },
    ]

    orders = derive_orders(
        decisions=decisions,
        prev_nav_tradable=datapack.nav_tradable,
        prev_close_prices=prev_close_prices,
        current_shares=current_shares,
    )
    orders_by_ticker = {o["ticker"]: o for o in orders}
    assert orders_by_ticker["2049"]["side"] == "SELL", orders
    assert orders_by_ticker[NEW_TICKER]["side"] == "BUY", orders
    assert orders_by_ticker["2049"]["shares"] % config.ORDER_LOT_SIZE == 0
    assert orders_by_ticker[NEW_TICKER]["shares"] % config.ORDER_LOT_SIZE == 0
    print(f"[orders] 2049 SELL {orders_by_ticker['2049']['shares']}股；{NEW_TICKER} BUY {orders_by_ticker[NEW_TICKER]['shares']}股")

    net_flow, estimated_cash_ratio = estimate_net_flow_and_cash(
        orders=orders,
        prev_close_prices=prev_close_prices,
        prev_cash=datapack.cash,
        prev_nav_tradable=datapack.nav_tradable,
    )

    remaining_orders = [o for o in orders if o["decision_ref"] != "D1"]
    buy_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in remaining_orders if o["side"] == "BUY")
    sell_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in remaining_orders if o["side"] == "SELL")
    ratio_without_trim_sell = (datapack.cash - (buy_value - sell_value)) / datapack.nav_tradable
    print(f"[funding_for] 拿掉D1的賣出後，估計現金比={ratio_without_trim_sell:.4f}；含D1時={estimated_cash_ratio:.4f}")

    lo = ratio_without_trim_sell + 0.005
    hi = max(estimated_cash_ratio + 0.01, lo + 0.02)
    tolerance = config.NET_EXPOSURE_HOLD_TOLERANCE * datapack.nav_tradable
    if abs(net_flow) <= tolerance:
        intent = "hold"
    elif net_flow > 0:
        intent = "increase"
    else:
        intent = "reduce"
    print(f"[posture] net_flow={net_flow:.0f}（容差{tolerance:.0f}）→ intent={intent}；cash區間=[{lo:.4f},{hi:.4f}]")

    no_trade_decisions = [
        {"ticker": t, "reason_refs": ["I3"]} for t in sorted(held_tickers) if t != "2049"
    ]

    observations = [
        {"obs_id": "O1", "source_ref": ["S1"], "statement": "2049近期量縮價平", "values": {"x": 1}},
        {"obs_id": "O2", "source_ref": ["S1"], "statement": f"{NEW_TICKER}法說會展望優於同業", "values": {"y": 1}},
        {"obs_id": "O3", "source_ref": ["S1"], "statement": "其餘持股無重大變化", "values": {"z": 1}},
    ]
    inferences = [
        {"inf_id": "I1", "premise_refs": ["O1"], "logic": "2049動能轉弱且非核心主題持股，減碼騰出資金", "counter_evidence": None},
        {"inf_id": "I2", "premise_refs": ["O2"], "logic": f"{NEW_TICKER}基本面轉強，符合供應鏈主題，新建倉", "counter_evidence": None},
        {"inf_id": "I3", "premise_refs": ["O3"], "logic": "其餘持股基本面與動能無顯著變化，維持原部位", "counter_evidence": None},
    ]

    run_time = dt.datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    dplan = {
        "schema_version": "4.3",
        "doc_type": "D-Plan",
        "team_id": "TEAM_042",
        "trade_date": datapack.trade_date,
        "sources": [
            {
                "source_id": "S1",
                "authority": "media",
                "url": "https://example-news.test/article",
                "content_as_of": "2026-10-27T18:00:00+08:00",
            }
        ],
        "observations": observations,
        "market_view": {
            "basis_refs": ["O3"],
            "logic": "整體市場無重大變化，個股面有2049轉弱、5274轉強的訊號，小幅換股。",
            "regime": "neutral",
            "stance": "neutral",
            "posture": {"net_exposure_intent": intent, "target_cash_pct_range": [round(lo, 6), round(hi, 6)]},
            "counter_evidence": None,
        },
        "inferences": inferences,
        "decisions": decisions,
        "no_trade_decisions": no_trade_decisions,
        "orders": orders,
        "agent_metadata": {
            "model_provider": "openai",
            "model_version": "gpt-6-astra",
            "run_started_at": run_time,
            "run_completed_at": run_time,
            "code_version": "test:trim_buy",
        },
    }

    validation = validate_dplan(
        dplan,
        prev_holdings=current_shares,
        prev_nav_tradable=datapack.nav_tradable,
        prev_cash=datapack.cash,
        prev_close_prices=prev_close_prices,
    )
    if not validation.ok:
        print("驗證失敗：")
        for err in validation.errors:
            print(f"  - {err}")
        raise SystemExit(1)

    filename_check = validate_filename(f"D-Plan_TEAM_042_{datapack.trade_date}.json", dplan)
    assert filename_check.ok, filename_check.errors

    # 反向測試：TRIM幅度縮小、BUY也縮小，讓「即使不靠D1的賣出，現金也還是很充裕」，
    # 缺口不存在，但decisions仍宣稱funding_for——這應該要被攔下。
    # 用很寬的現金區間，確保唯一會失敗的理由是funding_for，不是posture。
    bad_decisions = json.loads(json.dumps(decisions))
    bad_decisions[0]["target_weight"] = 0.034  # 小減一點，仍會產生非零SELL單
    bad_decisions[1]["target_weight"] = 0.005  # BUY也縮小，單靠帳上現金就綽綽有餘
    bad_orders = derive_orders(
        decisions=bad_decisions,
        prev_nav_tradable=datapack.nav_tradable,
        prev_close_prices=prev_close_prices,
        current_shares=current_shares,
    )
    bad_sell_order = next(o for o in bad_orders if o["decision_ref"] == "D1")
    assert bad_sell_order["shares"] > 0, "測試前提不成立：D1應該仍要產生非零SELL單"

    bad_net_flow, bad_estimated_cash_ratio = estimate_net_flow_and_cash(
        orders=bad_orders,
        prev_close_prices=prev_close_prices,
        prev_cash=datapack.cash,
        prev_nav_tradable=datapack.nav_tradable,
    )
    bad_dplan = dict(dplan)
    bad_dplan["decisions"] = bad_decisions
    bad_dplan["orders"] = bad_orders
    bad_dplan["market_view"] = {
        **dplan["market_view"],
        "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.02, 0.24]},
    }
    assert abs(bad_net_flow) <= config.NET_EXPOSURE_HOLD_TOLERANCE * datapack.nav_tradable, (
        "測試前提不成立：這個情境下net_flow應該在hold容差內，否則posture檢核會先報錯干擾判讀"
    )

    bad_validation = validate_dplan(
        bad_dplan,
        prev_holdings=current_shares,
        prev_nav_tradable=datapack.nav_tradable,
        prev_cash=datapack.cash,
        prev_close_prices=prev_close_prices,
    )
    assert not bad_validation.ok, "缺口不存在時還標記funding_for，應該要被validate_dplan攔下，但沒有"
    assert any("funding_for" in e for e in bad_validation.errors), bad_validation.errors
    print("[反向測試] 缺口不存在卻標記funding_for，成功被攔下：", [e for e in bad_validation.errors if "funding_for" in e])

    print("[trim_buy test] 全數通過：orders公式、funding_for缺口檢核、posture一致性")


if __name__ == "__main__":
    main()
