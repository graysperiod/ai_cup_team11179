"""L2M market_view：整體市場判斷（每天一份）。"""

from __future__ import annotations

from .llm_client import LLMClient

SYSTEM_PROMPT_TEMPLATE = """你是「{etf_name}」的市場判斷負責人。

本ETF投資理念：
{etf_philosophy}

任務：根據提供的市場級事實（大盤、費半SOX、匯率、期貨夜盤等 observations），
產出今天的 market_view：依據 → 走向研判(regime) → 操作姿態(stance) → 落實到部位的意思(posture)。

今天的起始狀態：目前現金比約為 {current_cash_ratio_pct}%（{holdings_count}檔持股）。

規則：
1. basis_refs 只能填被提供的 obs_id 清單裡存在的值，且必須是「市場級」事實（大盤/美股/夜盤/匯率/總經事件），不要引用個股層事實。
2. logic 要寫出從依據推到走向與姿態的完整過程，且要呼應上面的投資理念（抽檢時會對照公開說明書審一致性）。
3. target_cash_pct_range 的上下限必須落在 [0, 0.25] 區間內（規則：現金比不得為負、不得超過淨值25%），且建議留緩衝不要貼著0%或25%的邊界。
   區間本身也不要設太窄：委託股數是用官方公式無條件捨去到千股整數算出來的，捨去只會讓現金比比「理論權重」偏高，
   不會偏低，且同時異動的檔數越多（例如今天要一次新建20幾檔倉位），捨去累加起來的現金偏移就越明顯（實務上可達
   1~3個百分點）。區間寬度至少抓3個百分點，大量換股/建倉的日子建議抓5個百分點以上，且上限要比你心裡的目標值
   多留一些餘裕（因為實際現金只會比理論值更高，不會更低）。
4. counter_evidence：如果找得到與你研判相反的訊號就具體寫出來；真的找不到才填 null，不要偷懶。
5. net_exposure_intent 的承諾之後會被拿去跟當日委託加總比對（increase要淨買超、reduce要淨賣超、hold誤差要在2%淨值內），請確保你的部位語意前後一致。
   ⚠️ 特別注意起始狀態：目前現金比{current_cash_ratio_pct}%——如果目前幾乎是全現金（例如第一個交易日、0檔持股），
   手上根本沒有部位可以賣，net_exposure_intent 絕對不能選 reduce（沒東西可以淨賣超），就算你判斷市場
   risk_off／stance defensive，從零建倉的動作本質上仍是「增加曝險」（只是buildout幅度可以保守、
   target_cash_pct_range可以留高一點的現金），應該選 increase 或 hold，不要選 reduce。反過來，如果
   現金比已經很低（幾乎全部曝險），increase 也可能沒有多少空間，要注意現金上限25%的硬約束。
"""

_MARKET_VIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["basis_refs", "logic", "regime", "stance", "posture", "counter_evidence"],
    "properties": {
        "basis_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
        "logic": {"type": "string", "minLength": 20, "maxLength": 1000},
        "regime": {"enum": ["risk_on", "neutral", "risk_off"]},
        "stance": {"enum": ["aggressive", "neutral", "defensive"]},
        "posture": {
            "type": "object",
            "additionalProperties": False,
            "required": ["net_exposure_intent", "target_cash_pct_range"],
            "properties": {
                "net_exposure_intent": {"enum": ["increase", "hold", "reduce"]},
                "target_cash_pct_range": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0, "maximum": 0.25},
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        },
        "counter_evidence": {"type": ["string", "null"], "maxLength": 500},
    },
}


def build_market_view(
    client: LLMClient,
    *,
    etf_name: str,
    etf_philosophy: str,
    market_level_observations: list[dict],
    current_cash_ratio: float,
    holdings_count: int,
) -> dict:
    valid_obs_ids = {o["obs_id"] for o in market_level_observations}

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        etf_name=etf_name,
        etf_philosophy=etf_philosophy,
        current_cash_ratio_pct=round(current_cash_ratio * 100, 1),
        holdings_count=holdings_count,
    )
    user_prompt = _build_user_prompt(market_level_observations)

    market_view = client.structured_call(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=_MARKET_VIEW_SCHEMA,
        schema_name="market_view_response",
    )

    bad_refs = [r for r in market_view["basis_refs"] if r not in valid_obs_ids]
    if bad_refs:
        raise ValueError(f"LLM 產生的 market_view 引用了不存在的 obs_id：{bad_refs}")
    lo, hi = market_view["posture"]["target_cash_pct_range"]
    if lo > hi:
        raise ValueError(f"target_cash_pct_range 下限({lo}) > 上限({hi})")

    # 保險絲：就算prompt提醒過了，GPT偶爾還是會給邏輯上不可能的組合（0檔持股卻宣告reduce），
    # 這是機械上的矛盾（沒有部位可以賣），不是策略判斷，直接修正不需要重打一次昂貴的GPT呼叫。
    if holdings_count == 0 and market_view["posture"]["net_exposure_intent"] == "reduce":
        print("[llm_market_view] 0檔持股卻宣告reduce，邏輯矛盾（沒有部位可以賣），自動修正為increase")
        market_view["posture"]["net_exposure_intent"] = "increase"

    return market_view


def _build_user_prompt(observations: list[dict]) -> str:
    lines = ["今天的市場級事實如下：\n"]
    for o in observations:
        lines.append(f"- obs_id={o['obs_id']}: {o['statement']} values={o['values']}")
    return "\n".join(lines)
