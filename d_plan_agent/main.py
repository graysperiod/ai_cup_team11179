"""串起整條 D-Plan 生成流程。

用法：
    python -m d_plan_agent.main --datapack path/to/DataPack.json --team-id TEAM_042 --output-dir .

目前 fetch_market_data.py 裡的 MOPS／三大法人／期貨夜盤還是 stub（見該檔案的 TODO），
所以 sources 目前只有：TAIEX大盤指數、SOX、台幣匯率（Yahoo Finance）、鉅亨網(Anue)新聞。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import config, etf_profile
from .build_sources import build_sources
from .decision_engine import DecisionEngineError, build_decisions
from .drift_tracker import update_drift_state
from .fetch_datapack import DataPackNotReady, load_datapack
from .fetch_market_data import (
    RawDataItem,
    fetch_news,
    fetch_prev_close_prices,
    fetch_sox_index,
    fetch_taiex_index,
    fetch_usd_twd_rate,
)
from .llm_client import LLMClient
from .llm_inferences import build_inferences
from .llm_market_view import build_market_view
from .llm_observations import build_observations
from .order_derivation import OrderDerivationError, derive_orders
from .theme_classification import load_theme_classification, theme_relevant_tickers
from .universe import UniverseStock, load_universe
from .validate_dplan import validate_dplan, validate_filename

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))


def run(*, datapack_path: str, team_id: str, output_dir: str) -> int:
    run_started_at = dt.datetime.now(TAIPEI_TZ)

    try:
        datapack = load_datapack(datapack_path)
    except DataPackNotReady as e:
        print(f"[main] {e}")
        return 1

    held = datapack.holdings_by_code()
    held_tickers = {code for code, h in held.items() if h.shares > 0}
    prev_nav_tradable = datapack.nav_tradable or config.INITIAL_CAPITAL
    prev_cash = datapack.cash if datapack.cash is not None else config.INITIAL_CAPITAL

    universe = load_universe()

    raw_items: list[RawDataItem] = []
    for label, fetcher in (
        ("大盤指數", fetch_taiex_index),
        ("SOX", fetch_sox_index),
        ("台幣匯率", fetch_usd_twd_rate),
    ):
        try:
            raw_items.append(fetcher())
        except Exception as e:
            print(f"[main] {label}抓取失敗，先跳過：{e}")

    # 候選新標的的「資格」看主題分類（150檔池裡哪些屬於AI伺服器/半導體供應鏈），
    # 不是看今天有沒有新聞——這樣第一天0檔持股也能一次選出足夠的候選評估建倉，
    # 新聞只用來排序「優先評估哪些」。
    # with_archive=False：archive.org 在目前環境實測幾乎必定逾時（見 fetch_market_data.try_archive_url
    # 的實測筆記），等你在實際部署環境確認連得上之後再打開。
    theme_news: list[RawDataItem] = []
    try:
        theme_news = fetch_news(keywords=config.NEWS_THEME_KEYWORDS, limit_per_query=6, with_archive=False)
    except Exception as e:
        print(f"[main] 主題新聞抓取失敗，先跳過：{e}")

    mention_counts = _count_ticker_mentions(theme_news, universe)
    theme_pool = theme_relevant_tickers()
    candidate_pool = theme_pool - held_tickers
    candidates_needed = max(0, config.TARGET_SCOPE_SIZE - len(held_tickers))
    candidate_new_tickers = _pick_candidate_tickers_from_theme_pool(candidate_pool, mention_counts, limit=candidates_needed)
    tickers_in_scope = sorted(held_tickers | candidate_new_tickers)
    print(
        f"[main] 主題候選池{len(theme_pool)}檔（扣除已持有{len(candidate_pool)}檔可選），"
        f"今天評估範圍共{len(tickers_in_scope)}檔（持股{len(held_tickers)}＋候選{len(candidate_new_tickers)}）"
    )

    # 既有持股裡，主題新聞完全沒提到的，額外補搜一次，確保每檔今天都至少有機會拿到個股層級事實
    # （no_trade_decisions 仍然需要能引用到至少一筆有依據的推論）
    uncovered_held = sorted(held_tickers - set(mention_counts))
    dedicated_news: list[RawDataItem] = []
    if uncovered_held:
        try:
            dedicated_news = fetch_news(keywords=uncovered_held, limit_per_query=2, with_archive=False)
        except Exception as e:
            print(f"[main] 既有持股個股新聞補搜失敗，先跳過：{e}")

    news_items = _dedup_by_url(theme_news + dedicated_news)
    raw_items.extend(news_items)
    raw_items = _cap_raw_items(raw_items, max_total=config.MAX_SOURCES)
    print(f"[main] 新聞去重後{len(news_items)}則，sources總數{len(raw_items)}筆（上限{config.MAX_SOURCES}）")

    # TODO：接上 fetch_market_data.py 裡的三大法人/MOPS/期貨夜盤之後，把結果加進 raw_items

    sources = build_sources(raw_items)
    raw_payload_by_source_id = {s["source_id"]: item.payload for s, item in zip(sources, raw_items)}

    client = LLMClient()

    print(f"[main] 呼叫GPT產生observations...（{len(sources)}筆sources）")
    observations = build_observations(client, sources=sources, raw_payload_by_source_id=raw_payload_by_source_id)
    if not observations:
        print("[main] 沒有產出任何 observations，流程中止（schema 要求 observations 至少1筆）")
        return 1
    print(f"[main] observations完成，{len(observations)}筆，累計用量{client.usage}")

    print("[main] 呼叫GPT產生market_view...")
    market_view = build_market_view(
        client,
        etf_name=etf_profile.ETF_NAME,
        etf_philosophy=etf_profile.ETF_PHILOSOPHY,
        market_level_observations=observations,
        current_cash_ratio=datapack.cash_ratio_tradable if datapack.cash_ratio_tradable is not None else 1.0,
        holdings_count=len(held_tickers),
    )
    print(f"[main] market_view完成：regime={market_view['regime']} stance={market_view['stance']}，累計用量{client.usage}")

    print(f"[main] 呼叫GPT產生inferences...（{len(tickers_in_scope)}檔個股）")
    public_inferences, signals = build_inferences(
        client,
        etf_name=etf_profile.ETF_NAME,
        etf_philosophy=etf_profile.ETF_PHILOSOPHY,
        regime=market_view["regime"],
        stance=market_view["stance"],
        net_exposure_intent=market_view["posture"]["net_exposure_intent"],
        stock_level_observations=observations,
        tickers_in_scope=tickers_in_scope,
        held_tickers=held_tickers,
        sector_by_ticker={code: c.sector for code, c in load_theme_classification().items()},
    )
    print(f"[main] LLM用量（到inferences為止）：{client.usage}")

    tradable_weights = {code: h.tradable_weight for code, h in held.items() if h.tradable_weight is not None}
    from .decision_engine import weight_cap_for

    drift_statuses = update_drift_state(
        trade_date=datapack.trade_date,
        tradable_weights=tradable_weights,
        weight_cap_for=weight_cap_for,
    )

    try:
        decision_result = build_decisions(
            signals=signals,
            held_tickers=held_tickers,
            current_weights=tradable_weights,
            target_cash_pct_range=tuple(market_view["posture"]["target_cash_pct_range"]),
            drift_statuses=drift_statuses,
        )
    except DecisionEngineError as e:
        print(f"[main] 決策引擎無法產生合規決策：{e}")
        return 1

    all_decision_tickers = {d["ticker"] for d in decision_result.decisions}
    try:
        prev_close_prices = _resolve_prev_close_prices(all_decision_tickers, held)
    except Exception as e:
        print(f"[main] 無法取得前日收盤價：{e}")
        return 1

    current_shares = {code: h.shares for code, h in held.items()}
    try:
        orders = derive_orders(
            decisions=decision_result.decisions,
            prev_nav_tradable=prev_nav_tradable,
            prev_close_prices=prev_close_prices,
            current_shares=current_shares,
        )
    except OrderDerivationError as e:
        print(f"[main] 委託推導失敗：{e}")
        return 1

    run_completed_at = dt.datetime.now(TAIPEI_TZ)

    dplan = {
        "schema_version": config.SCHEMA_VERSION,
        "doc_type": "D-Plan",
        "team_id": team_id,
        "trade_date": datapack.trade_date,
        "sources": sources,
        "observations": observations,
        "market_view": market_view,
        "inferences": public_inferences,
        "decisions": decision_result.decisions,
        "no_trade_decisions": decision_result.no_trade_decisions,
        "orders": orders,
        "agent_metadata": {
            "model_provider": config.LLM_PROVIDER,
            "model_version": client.model,
            "run_started_at": run_started_at.isoformat(timespec="seconds"),
            "run_completed_at": run_completed_at.isoformat(timespec="seconds"),
            "code_version": _resolve_code_version(),
            "input_tokens": client.usage.input_tokens,
            "output_tokens": client.usage.output_tokens,
            "tool_use_count": client.usage.call_count,
        },
    }

    filename = f"D-Plan_{team_id}_{datapack.trade_date}.json"
    output_path = Path(output_dir) / filename

    # 不管驗證過不過都先存一份debug檔，驗證失敗時才能直接檢查中間結果，不用為了除錯
    # 又重新花錢呼叫一次GPT。正式送出的檔案只有驗證通過才會寫（見下面的 output_path）。
    debug_path = Path(output_dir) / "_debug_last_run.json"
    debug_path.write_text(json.dumps(dplan, ensure_ascii=False, indent=2), encoding="utf-8")

    validation = validate_dplan(
        dplan,
        prev_holdings=current_shares,
        prev_nav_tradable=prev_nav_tradable,
        prev_cash=prev_cash,
        prev_close_prices=prev_close_prices,
    )
    filename_validation = validate_filename(filename, dplan)
    validation.errors.extend(filename_validation.errors)

    if not validation.ok:
        print("[main] 驗證未通過，不會輸出檔案：")
        for err in validation.errors:
            print(f"  - {err}")
        return 1

    output_path.write_text(json.dumps(dplan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[main] 驗證通過，已輸出 {output_path}")
    return 0


def _dedup_by_url(items: list[RawDataItem]) -> list[RawDataItem]:
    """主題新聞和既有持股補搜可能抓到同一篇文章，用url去重（保留第一次出現的）。"""
    seen: set[str] = set()
    deduped = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(item)
    return deduped


def _cap_raw_items(items: list[RawDataItem], *, max_total: int) -> list[RawDataItem]:
    """D-Plan schema的sources上限是50筆。非新聞類（大盤/SOX/匯率等）固定保留，
    新聞類超過預算時只留最新的，因為新聞的時效性遠高於早一點抓到的舊新聞。"""
    non_news = [i for i in items if i.authority != "media"]
    news = [i for i in items if i.authority == "media"]
    news_sorted = sorted(news, key=lambda i: i.content_as_of, reverse=True)
    budget = max(max_total - len(non_news), 0)
    return non_news + news_sorted[:budget]


def _count_ticker_mentions(news_items: list[RawDataItem], universe: dict[str, UniverseStock]) -> dict[str, int]:
    """掃描新聞標題＋內文，比對150檔池裡的公司代號／名稱有沒有被提到，回傳 {ticker: 提及次數}。
    這是純文字比對，不呼叫LLM，幾乎不花錢；候選新標的的篩選依據是「今天新聞真的有講」，
    不是任意的代號排序，也不需要事先知道要查哪幾檔（新聞抓取不依賴tickers_in_scope，
    解開了之前「要查新聞才知道候選股、但候選股又要靠新聞查」的雞生蛋問題）。
    """
    mention_counts: dict[str, int] = {}
    for item in news_items:
        payload = item.payload or {}
        text = f"{payload.get('title', item.name or '')} {payload.get('content', '')}"
        for code, stock in universe.items():
            if code in text or stock.name in text:
                mention_counts[code] = mention_counts.get(code, 0) + 1
    return mention_counts


def _pick_candidate_tickers_from_theme_pool(
    candidate_pool: set[str], mention_counts: dict[str, int], *, limit: int
) -> set[str]:
    """候選新標的的資格來自主題分類（candidate_pool），today's新聞只用來排序「優先評估哪些」
    （今天有新聞的排前面），不是用來決定誰夠格進candidate_pool——這樣即使完全沒有相關新聞，
    也能從主題分類裡選出足夠的候選，不會卡在「候選太少湊不滿20檔」。
    """
    ranked = sorted(candidate_pool, key=lambda t: (-mention_counts.get(t, 0), t))
    return set(ranked[:limit])


def _resolve_prev_close_prices(tickers: set[str], held: dict) -> dict[str, float]:
    """優先用 DataPack 裡既有持股的 close_price（已經是 as_of_date 收盤價）；
    新標的（目前沒持有）才額外呼叫交易所 API 查收盤價。"""
    prices: dict[str, float] = {}
    missing: list[str] = []
    for t in tickers:
        if t in held and held[t].close_price is not None:
            prices[t] = held[t].close_price
        else:
            missing.append(t)
    if missing:
        prices.update(fetch_prev_close_prices(missing))
    return prices


def _resolve_code_version() -> str:
    import subprocess

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT_DIR, stderr=subprocess.DEVNULL
        ).decode().strip()
        return f"git:{commit}"
    except Exception:
        return "unversioned"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成當日 D-Plan")
    parser.add_argument("--datapack", required=True, help="DataPack JSON 檔路徑")
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()
    return run(datapack_path=args.datapack, team_id=args.team_id, output_dir=args.output_dir)


if __name__ == "__main__":
    sys.exit(main())
