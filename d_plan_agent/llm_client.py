"""OpenAI GPT 呼叫封裝，統一走 structured output（JSON Schema），減少格式錯誤。

model_provider 固定為 "openai"，對應 agent_metadata.model_provider。
model_version 預設讀 config.LLM_MODEL_VERSION（目前設定為 "gpt-6-astra"），
改了實際呼叫的模型字串，記得同步改 config.py 並更新 code_version（規格明文：
影響決策行為的參數和提示詞模板都算程式碼的一部分，改了就要換版本識別）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import config

ENV_FILE_PATH = config.ROOT_DIR / ".env"


def _load_env_file() -> None:
    """Bash工具每次呼叫是獨立的shell，export設定不會留到下一次呼叫，
    所以金鑰用專案根目錄的 .env 檔案持久化（KEY=VALUE，一行一個），
    不要把這個檔案加進版本控制。"""
    if not ENV_FILE_PATH.exists():
        return
    for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0


@dataclass
class LLMClient:
    """包一層，main.py 跑完整個流程後可以把 usage 塞進 agent_metadata。"""

    model: str = config.LLM_MODEL_VERSION
    usage: LLMUsage = field(default_factory=LLMUsage)
    _client: object | None = field(default=None, repr=False)

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise RuntimeError(
                    "需要先安裝 openai 套件：pip install openai，並設定 OPENAI_API_KEY 環境變數"
                ) from e
            _load_env_file()
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    f"請設定環境變數 OPENAI_API_KEY，或在 {ENV_FILE_PATH} 寫一行 OPENAI_API_KEY=sk-..."
                )
            # 明確設定逾時／重試次數：預設SDK的逾時設定在這個環境實測過會整個卡死超過580秒，
            # 與其讓main.py無限等待，寧可讓它在合理時間內失敗，問題才看得出來是什麼。
            # 120秒對inferences這種23檔個股一次產出的大輸出偶爾不夠，拉長到240秒。
            self._client = OpenAI(api_key=api_key, timeout=240.0, max_retries=1)
        return self._client

    def structured_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict,
        schema_name: str,
    ) -> dict:
        """呼叫 GPT，強制回傳符合 json_schema 的 JSON。回傳已 parse 好的 dict。"""
        client = self._get_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": True,
                },
            },
        )
        self.usage.call_count += 1
        if response.usage:
            self.usage.input_tokens += response.usage.prompt_tokens
            self.usage.output_tokens += response.usage.completion_tokens
        content = response.choices[0].message.content
        return json.loads(content)
