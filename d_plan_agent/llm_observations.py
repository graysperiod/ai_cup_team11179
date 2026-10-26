"""L1 observations：GPT 把原始資料濃縮成「事實＋數字」，禁止推論語句。"""

from __future__ import annotations

from .llm_client import LLMClient

SYSTEM_PROMPT = """你是台股投資團隊的資料分析助手。
任務：把提供的原始資料（行情數字、新聞、公告）逐條整理成「可核對的事實」。

鐵則：
1. 只寫事實，不寫判斷或推論。例如「SOX 跌 1.8%」可以，「所以半導體會轉弱」不行（這屬於下一層的推論，禁止在這裡出現）。
2. 每條 statement 要能讓人回頭核對原始資料，數字一律放進 values（只收數字，不放文字數字如"約"）。
3. source_ref 只能填你被提供的 source_id 清單裡存在的值，不能編造。
4. 同一個來源可以拆成多條 observation（例如一篇新聞裡有多個獨立事實）。
5. 不確定、模糊的內容不要硬掰成事實，寧可不寫。
"""

_OBSERVATIONS_SCHEMA = {
    # 注意：OpenAI 的 strict structured output 不支援「任意鍵名的dict」
    # （additionalProperties 給schema而非false）也不支援 minProperties，
    # 所以 values 這裡讓 GPT 輸出 key-value 陣列，組裝時再轉回規格要求的 dict。
    # strict mode 也要求每個 property 都要出現在 required 裡，optional 用
    # nullable type 表示，所以這裡沒有放可省略的 topic 欄位（反正是選填，不影響合規）。
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_ref", "statement", "values"],
                "properties": {
                    "source_ref": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "statement": {"type": "string", "minLength": 5, "maxLength": 500},
                    "values": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["key", "value"],
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "number"},
                            },
                        },
                    },
                },
            },
        }
    },
}


def build_observations(
    client: LLMClient,
    *,
    sources: list[dict],
    raw_payload_by_source_id: dict[str, object],
) -> list[dict]:
    """回傳符合 D-Plan observation schema 的 observations[]，obs_id 由本函式依序指派（O1, O2, ...）。"""
    valid_source_ids = {s["source_id"] for s in sources}

    user_prompt = _build_user_prompt(sources, raw_payload_by_source_id)
    result = client.structured_call(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_schema=_OBSERVATIONS_SCHEMA,
        schema_name="observations_response",
    )

    observations = []
    for i, obs in enumerate(result["observations"], start=1):
        bad_refs = [r for r in obs["source_ref"] if r not in valid_source_ids]
        if bad_refs:
            raise ValueError(f"LLM 產生的 observation 引用了不存在的 source_id：{bad_refs}")
        observations.append(
            {
                "obs_id": f"O{i}",
                "source_ref": obs["source_ref"],
                "statement": obs["statement"],
                "values": {kv["key"]: kv["value"] for kv in obs["values"]},
            }
        )
    return observations


def _build_user_prompt(sources: list[dict], raw_payload_by_source_id: dict[str, object]) -> str:
    lines = ["以下是今天蒐集到的原始資料，請依系統指示整理成 observations：\n"]
    for s in sources:
        lines.append(f"- source_id={s['source_id']} authority={s['authority']} content_as_of={s['content_as_of']}")
        payload = raw_payload_by_source_id.get(s["source_id"])
        if payload is not None:
            lines.append(f"  內容：{payload}")
    return "\n".join(lines)
