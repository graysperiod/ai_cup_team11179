"""L2 inferences：個股推論。

設計重點：D-Plan 正式規格的 inference 物件（additionalProperties:false）不允許放
ticker / 建議權重 這種欄位——ticker 與權重的綁定是在下一層 decisions 才出現。
但 decision_engine 需要知道「這筆推論是講哪一檔、該檔該怎麼動」才能生成 decisions，
所以這裡讓 GPT 多回傳兩個「內部欄位」(ticker, action_hint, target_weight_hint)，
decision_engine 用完之後，組裝最終 D-Plan JSON 時只取公開欄位（見 to_public_inference）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm_client import LLMClient

SYSTEM_PROMPT_TEMPLATE = """你是「{etf_name}」的個股研究員。

本ETF投資理念：
{etf_philosophy}

今天的整體市場判斷（market_view）：
regime={regime}, stance={stance}, net_exposure_intent={net_exposure_intent}

⚠️ 比賽硬性規定（違反會導致整份D-Plan被判違規，不是建議而是規則）：
- 執行後持股檔數必須落在 20～30 檔之間，不能更多也不能更少
- 目前持有 {held_count} 檔
- 即使你判斷整體應該轉趨保守，也不能把太多檔建議成 SELL_ALL 導致剩餘檔數低於20檔；
  如果想降低曝險，優先用 TRIM（減碼但保留部位）而不是 SELL_ALL（全數出清），
  或是從候選新標的裡找幾檔基本面夠支撐的建議BUY來補足檔數
- 這份清單裡的候選新標的（原本沒持有的）：{candidate_tickers}，這些是用來補檔數、也是用來抓新機會的，
  如果你讓太多既有持股SELL_ALL，請確保從這些候選裡選出足夠的BUY來補上缺口

任務：針對提供的個股與相關事實，逐檔產出推論：事實 + 策略規則 → 結論，並給出你建議的動作方向與目標權重。

規則：
1. premise_refs 只能填被提供的 obs_id 清單裡存在的值。
2. logic 要清楚寫出推導過程，且要能呼應投資理念（抽檢時會比對一致性），不是空話。
3. counter_evidence：找不到才填 null，能找到就具體寫。
4. action_hint：
   - BUY = 建議新建倉（目前無此持股）
   - ADD = 建議加碼既有持股
   - TRIM = 建議減碼但保留部位
   - SELL_ALL = 建議全數出清（請節制使用，見上方硬性規定）
   - HOLD = 建議維持不動（會被放進 no_trade_decisions）
5. target_weight_hint：你建議的目標權重（0~0.25），HOLD/SELL_ALL 時可填目前權重或0，之後會由程式碼套用風險上限，不用自己算到精確值。
6. 每一檔持股今天都要有交代，不要漏掉提供給你的任何一檔。
"""

_INFERENCES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["inferences"],
    "properties": {
        "inferences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # strict mode 要求每個 property 都在 required 裡，optional 欄位
                # （conclusion/confidence）改用 nullable type 表示「可以不填」。
                "required": [
                    "ticker",
                    "premise_refs",
                    "logic",
                    "conclusion",
                    "counter_evidence",
                    "confidence",
                    "action_hint",
                    "target_weight_hint",
                ],
                "properties": {
                    "ticker": {"type": "string", "pattern": "^[0-9]{4}$"},
                    "premise_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
                    "logic": {"type": "string", "minLength": 20, "maxLength": 1000},
                    "conclusion": {"type": ["string", "null"], "maxLength": 300},
                    "counter_evidence": {"type": ["string", "null"], "maxLength": 500},
                    "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "action_hint": {"enum": ["BUY", "ADD", "TRIM", "SELL_ALL", "HOLD"]},
                    "target_weight_hint": {"type": "number", "minimum": 0, "maximum": 0.25},
                },
            },
        }
    },
}


@dataclass
class InferenceSignal:
    """decision_engine 用的內部信號：一檔股票、一筆 GPT 建議。"""

    inf_id: str
    ticker: str
    action_hint: str
    target_weight_hint: float


def build_inferences(
    client: LLMClient,
    *,
    etf_name: str,
    etf_philosophy: str,
    regime: str,
    stance: str,
    net_exposure_intent: str,
    stock_level_observations: list[dict],
    tickers_in_scope: list[str],
    held_tickers: set[str],
    sector_by_ticker: dict[str, str] | None = None,
) -> tuple[list[dict], list[InferenceSignal]]:
    """回傳 (public_inferences, signals)。
    public_inferences：可直接放進最終 D-Plan JSON 的 inferences[]（已依序編 I1, I2, ...）。
    signals：decision_engine 用來決定 decisions 的內部資訊。
    held_tickers 只用來在prompt裡提醒GPT目前檔數與哪些是候選新標的，不影響引用鏈驗證。
    sector_by_ticker：每檔的產業分類（例如「晶圓代工」「IC設計」），讓GPT知道候選新標的
    為什麼會被列入評估——就算今天沒有個股新聞，也看得出它跟ETF主題的關聯，不是亂塞的。
    """
    valid_obs_ids = {o["obs_id"] for o in stock_level_observations}
    candidate_tickers = sorted(set(tickers_in_scope) - held_tickers)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        etf_name=etf_name,
        etf_philosophy=etf_philosophy,
        regime=regime,
        stance=stance,
        net_exposure_intent=net_exposure_intent,
        held_count=len(held_tickers),
        candidate_tickers=", ".join(candidate_tickers) if candidate_tickers else "（今天沒有新的候選標的）",
    )
    user_prompt = _build_user_prompt(stock_level_observations, tickers_in_scope, sector_by_ticker or {})

    result = client.structured_call(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=_INFERENCES_SCHEMA,
        schema_name="inferences_response",
    )

    public_inferences = []
    signals = []
    for i, inf in enumerate(result["inferences"], start=1):
        bad_refs = [r for r in inf["premise_refs"] if r not in valid_obs_ids]
        if bad_refs:
            raise ValueError(f"LLM 產生的 inference 引用了不存在的 obs_id：{bad_refs}")

        inf_id = f"I{i}"
        public_inferences.append(to_public_inference(inf_id, inf))
        signals.append(
            InferenceSignal(
                inf_id=inf_id,
                ticker=inf["ticker"],
                action_hint=inf["action_hint"],
                target_weight_hint=inf["target_weight_hint"],
            )
        )

    missing_tickers = set(tickers_in_scope) - {s.ticker for s in signals}
    if missing_tickers:
        raise ValueError(f"LLM 的 inferences 漏掉了這些應該交代的持股：{sorted(missing_tickers)}")

    return public_inferences, signals


def to_public_inference(inf_id: str, inf: dict) -> dict:
    """從內部格式（含 ticker/action_hint/target_weight_hint）投影成官方 D-Plan inference schema。"""
    public = {
        "inf_id": inf_id,
        "premise_refs": inf["premise_refs"],
        "logic": inf["logic"],
        "counter_evidence": inf["counter_evidence"],
    }
    if inf.get("conclusion"):
        public["conclusion"] = inf["conclusion"]
    if inf.get("confidence") is not None:
        public["confidence"] = inf["confidence"]
    return public


def _build_user_prompt(
    observations: list[dict], tickers_in_scope: list[str], sector_by_ticker: dict[str, str]
) -> str:
    lines = [f"今天需要交代的個股（共 {len(tickers_in_scope)} 檔）：\n"]
    for t in tickers_in_scope:
        sector = sector_by_ticker.get(t)
        lines.append(f"- {t}" + (f"（{sector}）" if sector else ""))
    lines.append(
        "\n候選新標的若今天沒有個股專屬新聞，可以根據上面列出的產業分類判斷是否符合ETF主題"
        "（不是只能等新聞才能判斷），但premise_refs仍然至少要引用一筆事實（例如可以引用大盤"
        "或總經層級的observation），沒有具體事實支撐新建倉的就給HOLD，不要硬掰理由。\n"
    )
    lines.append("相關事實：")
    for o in observations:
        lines.append(f"- obs_id={o['obs_id']}: {o['statement']} values={o['values']}")
    return "\n".join(lines)
