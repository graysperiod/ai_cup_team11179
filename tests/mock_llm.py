"""測試用的假 LLM：不呼叫 OpenAI，直接回傳寫死的回應，用來驗證
build_sources → llm_* → decision_engine → order_derivation → validate_dplan
這條管線本身有沒有接好，不測試 GPT 判斷品質本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MockLLMClient:
    model: str = "mock-model"
    responses_by_schema: dict = field(default_factory=dict)

    class _Usage:
        input_tokens = 0
        output_tokens = 0
        call_count = 0

    usage = _Usage()

    def structured_call(self, *, system_prompt: str, user_prompt: str, json_schema: dict, schema_name: str) -> dict:
        if schema_name not in self.responses_by_schema:
            raise KeyError(f"MockLLMClient 沒有為 schema_name={schema_name} 準備回應")
        return self.responses_by_schema[schema_name]
