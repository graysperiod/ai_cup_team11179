"""管線接線測試（不呼叫真實 OpenAI/網路）。

跑法：
    cd ai_cup && python3 tests/test_pipeline_smoke.py

驗證：fixtures/datapack_example.json（規格書§3.1範例）經過
sources → observations → market_view → inferences → decisions → orders
全部走完一輪、全檔「HOLD不動」的情境下，validate_dplan 應該全數通過。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from d_plan_agent import config  # noqa: E402
from d_plan_agent.build_sources import build_sources  # noqa: E402
from d_plan_agent.decision_engine import build_decisions, weight_cap_for  # noqa: E402
from d_plan_agent.drift_tracker import update_drift_state  # noqa: E402
from d_plan_agent.fetch_datapack import parse_datapack  # noqa: E402
from d_plan_agent.fetch_market_data import RawDataItem  # noqa: E402
from d_plan_agent.llm_inferences import build_inferences  # noqa: E402
from d_plan_agent.llm_market_view import build_market_view  # noqa: E402
from d_plan_agent.llm_observations import build_observations  # noqa: E402
from d_plan_agent.order_derivation import derive_orders  # noqa: E402
from d_plan_agent.validate_dplan import validate_dplan, validate_filename  # noqa: E402

from mock_llm import MockLLMClient  # noqa: E402

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def main() -> None:
    raw = json.loads((FIXTURES_DIR / "datapack_example.json").read_text(encoding="utf-8"))
    datapack = parse_datapack(raw)
    held = datapack.holdings_by_code()
    held_tickers = {code for code, h in held.items() if h.shares > 0}
    assert len(held_tickers) == 22, held_tickers

    raw_items = [
        RawDataItem(
            authority="twse",
            url="https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX",
            content_as_of=dt.datetime(2026, 10, 27, 13, 30, tzinfo=TAIPEI_TZ),
            name="發行量加權股價指數",
            payload={"收盤指數": "23850.12", "漲跌百分比": "-0.3"},
        ),
        RawDataItem(
            authority="vendor",
            url="https://example-vendor.test/sox",
            content_as_of=dt.datetime(2026, 10, 26, 22, 0, tzinfo=TAIPEI_TZ),
            name="費城半導體指數(SOX)",
            payload={"SOX_chg_pct": -0.5},
        ),
    ]
    sources = build_sources(raw_items)
    assert [s["source_id"] for s in sources] == ["S1", "S2"]

    mock_observations_response = {
        "observations": [
            {
                "source_ref": ["S1"],
                "statement": "加權指數小跌0.3%，盤勢持平",
                "values": [{"key": "taiex_chg_pct", "value": -0.3}],
            },
            {
                "source_ref": ["S2"],
                "statement": "SOX小跌0.5%，半導體權值股夜盤偏弱",
                "values": [{"key": "SOX_chg_pct", "value": -0.5}],
            },
        ]
    }

    mock_inferences_response = {
        "inferences": [
            {
                "ticker": ticker,
                "premise_refs": ["O1", "O2"],
                "logic": f"{ticker} 近期走勢與大盤及SOX同步、無個股異常訊號，維持原部位不變動。",
                "conclusion": "維持持股",
                "counter_evidence": None,
                "confidence": 0.6,
                "action_hint": "HOLD",
                "target_weight_hint": held[ticker].tradable_weight or 0.0,
            }
            for ticker in sorted(held_tickers)
        ]
    }

    client = MockLLMClient(
        responses_by_schema={
            "observations_response": mock_observations_response,
            "market_view_response": {
                "basis_refs": ["O1", "O2"],
                "logic": "大盤小跌且SOX同步走弱，但跌幅有限，判定中性震盪，維持既有配置。",
                "regime": "neutral",
                "stance": "neutral",
                "posture": {
                    "net_exposure_intent": "hold",
                    "target_cash_pct_range": [0.10, 0.20],
                },
                "counter_evidence": None,
            },
            "inferences_response": mock_inferences_response,
        }
    )

    observations = build_observations(client, sources=sources, raw_payload_by_source_id={})
    assert [o["obs_id"] for o in observations] == ["O1", "O2"]

    market_view = build_market_view(
        client,
        etf_name="測試ETF",
        etf_philosophy="測試用投資理念",
        market_level_observations=observations,
        current_cash_ratio=datapack.cash_ratio_tradable,
        holdings_count=len(held_tickers),
    )
    assert market_view["regime"] == "neutral"

    public_inferences, signals = build_inferences(
        client,
        etf_name="測試ETF",
        etf_philosophy="測試用投資理念",
        regime=market_view["regime"],
        stance=market_view["stance"],
        net_exposure_intent=market_view["posture"]["net_exposure_intent"],
        stock_level_observations=observations,
        tickers_in_scope=sorted(held_tickers),
        held_tickers=held_tickers,
    )
    assert len(public_inferences) == 22
    assert all("ticker" not in inf for inf in public_inferences), "公開inference不應該含內部欄位ticker"

    tradable_weights = {code: h.tradable_weight for code, h in held.items() if h.tradable_weight is not None}
    drift_statuses = update_drift_state(
        trade_date=datapack.trade_date,
        tradable_weights=tradable_weights,
        weight_cap_for=weight_cap_for,
    )
    assert drift_statuses == [], f"範例資料不應該有超限持股，但追蹤到：{drift_statuses}"

    decision_result = build_decisions(
        signals=signals,
        held_tickers=held_tickers,
        current_weights=tradable_weights,
        target_cash_pct_range=tuple(market_view["posture"]["target_cash_pct_range"]),
        drift_statuses=drift_statuses,
    )
    assert decision_result.decisions == [], "全部HOLD時decisions應為空陣列"
    assert len(decision_result.no_trade_decisions) == 22

    prev_close_prices = {code: h.close_price for code, h in held.items()}
    current_shares = {code: h.shares for code, h in held.items()}
    orders = derive_orders(
        decisions=decision_result.decisions,
        prev_nav_tradable=datapack.nav_tradable,
        prev_close_prices=prev_close_prices,
        current_shares=current_shares,
    )
    assert orders == [], "沒有decisions時orders應為空陣列"

    run_time = dt.datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    dplan = {
        "schema_version": "4.3",
        "doc_type": "D-Plan",
        "team_id": "TEAM_042",
        "trade_date": datapack.trade_date,
        "sources": sources,
        "observations": observations,
        "market_view": market_view,
        "inferences": public_inferences,
        "decisions": decision_result.decisions,
        "no_trade_decisions": decision_result.no_trade_decisions,
        "orders": orders,
        "agent_metadata": {
            "model_provider": "openai",
            "model_version": "gpt-6-astra",
            "run_started_at": run_time,
            "run_completed_at": run_time,
            "code_version": "test:smoke",
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

    print("[smoke test] 全數通過：schema / 引用鏈 / orders重算 / 覆蓋度 / 標的池 / 權重上限 / posture一致性 / 檔名")


if __name__ == "__main__":
    main()
