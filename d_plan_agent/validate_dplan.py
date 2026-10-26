"""提交前驗證。全部用程式碼判斷，不依賴 LLM 自稱「我檢查過了」。

對應 D-Plan_撰寫指南_v4.3.md「提交前自我檢查」8項 + D-Plan 檢核規則簡報。
任何一項沒過，main.py 應該中止、不送出。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

import jsonschema

from . import config
from .order_derivation import derive_orders, estimate_net_flow_and_cash
from .universe import is_in_universe


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)


def load_schema() -> dict:
    with open(config.DPLAN_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate_dplan(
    dplan: dict,
    *,
    prev_holdings: dict[str, int],
    prev_nav_tradable: float,
    prev_cash: float,
    prev_close_prices: dict[str, float],
) -> ValidationResult:
    result = ValidationResult()

    _validate_schema(dplan, result)
    if not result.ok:
        return result  # schema 都不過，後面的檢查沒有意義

    _validate_reference_chain(dplan, result)
    _validate_orders_match_official_formula(dplan, result, prev_nav_tradable, prev_close_prices, prev_holdings)
    _validate_coverage(dplan, result, prev_holdings)
    _validate_universe(dplan, result)
    _validate_weight_caps(dplan, result)
    _validate_funding_for(dplan, result, prev_nav_tradable, prev_cash, prev_close_prices, prev_holdings)
    _validate_posture_consistency(dplan, result, prev_nav_tradable, prev_cash, prev_close_prices)
    _validate_filename_fields(dplan, result)
    _validate_timestamps(dplan, result)

    return result


def _validate_schema(dplan: dict, result: ValidationResult) -> None:
    schema = load_schema()
    # jsonschema預設不會檢查 format（例如 url 的 "format": "uri"），一定要帶 format_checker
    # 才會真的驗；而且"uri"這個format還需要額外裝 rfc3987 套件，沒裝的話會把所有字串都當成
    # 「格式不明、視為合法」直接放行（之前就是這樣漏掉了一個yfinance代號^SOX沒做URL編碼的bug）。
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for err in validator.iter_errors(dplan):
        result.errors.append(f"[schema] {'/'.join(str(p) for p in err.absolute_path)}: {err.message}")


def _validate_reference_chain(dplan: dict, result: ValidationResult) -> None:
    source_ids = {s["source_id"] for s in dplan["sources"]}
    obs_ids = {o["obs_id"] for o in dplan["observations"]}
    inf_ids = {i["inf_id"] for i in dplan["inferences"]}
    decision_ids = {d["decision_id"] for d in dplan["decisions"]}

    for o in dplan["observations"]:
        bad = [r for r in o["source_ref"] if r not in source_ids]
        result.add(not bad, f"observation {o['obs_id']} 引用不存在的 source_id：{bad}")

    mv = dplan["market_view"]
    bad = [r for r in mv["basis_refs"] if r not in obs_ids]
    result.add(not bad, f"market_view 引用不存在的 obs_id：{bad}")

    for i in dplan["inferences"]:
        bad = [r for r in i["premise_refs"] if r not in obs_ids]
        result.add(not bad, f"inference {i['inf_id']} 引用不存在的 obs_id：{bad}")

    for d in dplan["decisions"]:
        bad = [r for r in d["inference_refs"] if r not in inf_ids]
        result.add(not bad, f"decision {d['decision_id']} 引用不存在的 inf_id：{bad}")
        for ref in d.get("funding_for", []):
            result.add(ref in decision_ids, f"decision {d['decision_id']} 的 funding_for 引用不存在的 decision_id：{ref}")

    for nt in dplan["no_trade_decisions"]:
        bad = [r for r in nt["reason_refs"] if r not in inf_ids]
        result.add(not bad, f"no_trade_decision {nt['ticker']} 引用不存在的 inf_id：{bad}")

    for o in dplan["orders"]:
        result.add(o["decision_ref"] in decision_ids, f"order {o['ticker']} 引用不存在的 decision_id：{o['decision_ref']}")


def _validate_orders_match_official_formula(
    dplan: dict,
    result: ValidationResult,
    prev_nav_tradable: float,
    prev_close_prices: dict[str, float],
    prev_holdings: dict[str, int],
) -> None:
    try:
        expected_orders = derive_orders(
            decisions=dplan["decisions"],
            prev_nav_tradable=prev_nav_tradable,
            prev_close_prices=prev_close_prices,
            current_shares=prev_holdings,
        )
    except Exception as e:
        result.errors.append(f"orders 重算失敗：{e}")
        return

    expected_set = {(o["ticker"], o["side"], o["shares"]) for o in expected_orders}
    actual_set = {(o["ticker"], o["side"], o["shares"]) for o in dplan["orders"]}
    result.add(
        expected_set == actual_set,
        f"orders 與官方公式重算結果不一致。預期：{expected_set}，實際：{actual_set}",
    )


def _validate_coverage(dplan: dict, result: ValidationResult, prev_holdings: dict[str, int]) -> None:
    held_tickers = {t for t, shares in prev_holdings.items() if shares > 0}
    decided = {d["ticker"] for d in dplan["decisions"]}
    not_traded = {nt["ticker"] for nt in dplan["no_trade_decisions"]}

    overlap = decided & not_traded
    result.add(not overlap, f"以下個股同時出現在 decisions 與 no_trade_decisions：{overlap}")

    covered = decided | not_traded
    missing = held_tickers - covered
    result.add(not missing, f"昨日持股未被交代（既不在decisions也不在no_trade_decisions）：{missing}")

    extra_no_trade = not_traded - held_tickers
    result.add(not extra_no_trade, f"no_trade_decisions 出現非昨日持股的個股：{extra_no_trade}")


def _validate_universe(dplan: dict, result: ValidationResult) -> None:
    for d in dplan["decisions"]:
        if d["action"] in ("BUY", "ADD"):
            result.add(is_in_universe(d["ticker"]), f"decision {d['decision_id']} 的 {d['ticker']} 不在150檔標的池內")


def _validate_weight_caps(dplan: dict, result: ValidationResult) -> None:
    for d in dplan["decisions"]:
        cap = config.WEIGHT_CAP_2330 if d["ticker"] == config.TICKER_2330 else config.WEIGHT_CAP_DEFAULT
        result.add(
            d["target_weight"] <= cap + 1e-9,
            f"decision {d['decision_id']}（{d['ticker']}）target_weight={d['target_weight']} 超過上限 {cap}",
        )

    lo, hi = dplan["market_view"]["posture"]["target_cash_pct_range"]
    result.add(0 <= lo <= hi <= config.CASH_RATIO_MAX, f"target_cash_pct_range 不合法：[{lo}, {hi}]")


def _validate_funding_for(
    dplan: dict,
    result: ValidationResult,
    prev_nav_tradable: float,
    prev_cash: float,
    prev_close_prices: dict[str, float],
    prev_holdings: dict[str, int],
) -> None:
    decisions_by_id = {d["decision_id"]: d for d in dplan["decisions"]}
    funding_decisions = [d for d in dplan["decisions"] if d.get("funding_for")]
    if not funding_decisions:
        return

    try:
        all_orders = derive_orders(
            decisions=dplan["decisions"],
            prev_nav_tradable=prev_nav_tradable,
            prev_close_prices=prev_close_prices,
            current_shares=prev_holdings,
        )
    except Exception as e:
        result.errors.append(f"funding_for 檢核時 orders 重算失敗：{e}")
        return

    lo, _hi = dplan["market_view"]["posture"]["target_cash_pct_range"]

    for d in funding_decisions:
        for target_id in d["funding_for"]:
            result.add(
                target_id in decisions_by_id and decisions_by_id[target_id]["action"] in ("BUY", "ADD"),
                f"decision {d['decision_id']} 的 funding_for 必須指向同日的 BUY/ADD 決策，實際指向 {target_id}",
            )

        remaining_orders = [o for o in all_orders if o["decision_ref"] != d["decision_id"]]
        buy_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in remaining_orders if o["side"] == "BUY")
        sell_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in remaining_orders if o["side"] == "SELL")
        cash_without_this_sell = prev_cash - (buy_value - sell_value)
        ratio_without_this_sell = cash_without_this_sell / prev_nav_tradable
        result.add(
            ratio_without_this_sell < lo,
            f"decision {d['decision_id']} 標記 funding_for，但拿掉這筆賣出後現金比估計為 "
            f"{ratio_without_this_sell:.4f}，並未跌破宣告下限 {lo}（缺口不存在，不該標記 funding_for）",
        )


def _validate_posture_consistency(
    dplan: dict,
    result: ValidationResult,
    prev_nav_tradable: float,
    prev_cash: float,
    prev_close_prices: dict[str, float],
) -> None:
    """用 T-1 收盤價重算委託淨流向與預估現金比，核對是否落在 market_view.posture 的宣告內。"""
    try:
        orders_with_price = [o for o in dplan["orders"] if o["ticker"] in prev_close_prices]
        missing_price = [o["ticker"] for o in dplan["orders"] if o["ticker"] not in prev_close_prices]
        result.add(not missing_price, f"orders 裡以下個股沒有前日收盤價可供 posture 檢核：{missing_price}")

        net_flow, estimated_cash_ratio = estimate_net_flow_and_cash(
            orders=orders_with_price,
            prev_close_prices=prev_close_prices,
            prev_cash=prev_cash,
            prev_nav_tradable=prev_nav_tradable,
        )
    except Exception as e:
        result.errors.append(f"posture 一致性檢核失敗：{e}")
        return

    posture = dplan["market_view"]["posture"]
    intent = posture["net_exposure_intent"]
    tolerance = config.NET_EXPOSURE_HOLD_TOLERANCE * prev_nav_tradable

    if intent == "increase":
        result.add(net_flow > 0, f"宣告 increase（應淨買超）但估計淨流向為 {net_flow:.0f}")
    elif intent == "reduce":
        result.add(net_flow < 0, f"宣告 reduce（應淨賣超）但估計淨流向為 {net_flow:.0f}")
    else:  # hold
        result.add(
            abs(net_flow) <= tolerance,
            f"宣告 hold 但估計淨流向 {net_flow:.0f} 超過容差 {tolerance:.0f}（2% NAV）",
        )

    lo, hi = posture["target_cash_pct_range"]
    result.add(
        lo <= estimated_cash_ratio <= hi,
        f"以T-1收盤價估計委託後現金比為 {estimated_cash_ratio:.4f}，未落在宣告區間 [{lo}, {hi}] 內",
    )


_FILENAME_RE = re.compile(r"^D-Plan_([A-Za-z0-9_-]{1,32})_(\d{4}-\d{2}-\d{2})\.json$")


def validate_filename(filename: str, dplan: dict) -> ValidationResult:
    result = ValidationResult()
    m = _FILENAME_RE.match(filename)
    result.add(bool(m), f"檔名格式不符 D-Plan_<team_id>_<trade_date>.json：{filename}")
    if m:
        team_id, trade_date = m.groups()
        result.add(team_id == dplan["team_id"], f"檔名的 team_id={team_id} 與 JSON 內的 {dplan['team_id']} 不一致")
        result.add(trade_date == dplan["trade_date"], f"檔名的日期={trade_date} 與 JSON 內的 {dplan['trade_date']} 不一致")
    return result


def _validate_filename_fields(dplan: dict, result: ValidationResult) -> None:
    result.add(bool(re.match(r"^[A-Za-z0-9_-]{1,32}$", dplan["team_id"])), f"team_id 格式不合法：{dplan['team_id']}")


def _validate_timestamps(dplan: dict, result: ValidationResult) -> None:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    completed_at = dt.datetime.fromisoformat(dplan["agent_metadata"]["run_completed_at"])
    result.add(completed_at <= now, f"agent_metadata.run_completed_at ({completed_at}) 晚於目前時間 ({now})")
