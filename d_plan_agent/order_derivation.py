"""L4 orders：純公式計算，禁止 LLM 碰這段數字（見 D-Plan 撰寫指南 §⑥）。

目標股數 = floor( target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000 ) × 1000
委託股數 = 目標股數 − 目前持有股數（正=BUY，負=SELL）
"""

from __future__ import annotations

import math

from . import config


class OrderDerivationError(Exception):
    pass


def derive_orders(
    *,
    decisions: list[dict],
    prev_nav_tradable: float,
    prev_close_prices: dict[str, float],
    current_shares: dict[str, int],
) -> list[dict]:
    """decisions: D-Plan 的 decisions[]（需含 decision_id/ticker/action/target_weight）。
    prev_close_prices: {ticker: 前日收盤價}，交易所公布的前一交易日收盤價（非 LLM 估計）。
    current_shares: {ticker: 目前庫存股數}，來自 DataPack 的 holdings（查無則視為0）。
    """
    orders = []
    for d in decisions:
        ticker = d["ticker"]
        target_weight = d["target_weight"]

        if ticker not in prev_close_prices:
            raise OrderDerivationError(f"{ticker} 缺前日收盤價，無法推導委託（規格：缺價且不在標的池內的委託應作廢）")
        prev_close = prev_close_prices[ticker]
        if prev_close <= 0:
            raise OrderDerivationError(f"{ticker} 前日收盤價為 {prev_close}，無法計算")

        target_shares = math.floor(target_weight * prev_nav_tradable / prev_close / config.ORDER_LOT_SIZE) * config.ORDER_LOT_SIZE
        held_shares = current_shares.get(ticker, 0)
        order_shares = target_shares - held_shares

        if order_shares == 0:
            continue

        side = "BUY" if order_shares > 0 else "SELL"
        shares = abs(order_shares)

        if side == "SELL" and shares > held_shares:
            raise OrderDerivationError(
                f"{ticker} 推導出賣出 {shares} 股，超過目前持有 {held_shares} 股——"
                "檢查 target_weight 或 current_shares 是否用了過期的資料"
            )

        orders.append(
            {
                "ticker": ticker,
                "side": side,
                "shares": shares,
                "decision_ref": d["decision_id"],
            }
        )
    return orders


def estimate_net_flow_and_cash(
    *,
    orders: list[dict],
    prev_close_prices: dict[str, float],
    prev_cash: float,
    prev_nav_tradable: float,
) -> tuple[float, float]:
    """用前日收盤價估算委託後的淨流向（買-賣金額）與預估現金比，供 posture 一致性檢核使用。
    注意：這是撰寫當下的估計值，實際成交以當日均價為準，會有小幅偏移（撰寫指南已提醒）。
    """
    buy_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in orders if o["side"] == "BUY")
    sell_value = sum(o["shares"] * prev_close_prices[o["ticker"]] for o in orders if o["side"] == "SELL")
    net_flow = buy_value - sell_value
    estimated_cash = prev_cash - net_flow
    estimated_cash_ratio = estimated_cash / prev_nav_tradable
    return net_flow, estimated_cash_ratio
