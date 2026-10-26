"""競賽規則常數。數字皆來自 DataPack 規格書、D-Plan 撰寫指南與主辦方簡報（2026-10-26 確認）。"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# -- 帳戶 --
INITIAL_CAPITAL = 1_000_000_000.0  # 新台幣10億元

# -- 投資組合限制 --
MIN_HOLDINGS_COUNT = 20
MAX_HOLDINGS_COUNT = 30
TICKER_2330 = "2330"
WEIGHT_CAP_2330 = 0.25
WEIGHT_CAP_DEFAULT = 0.10
CASH_RATIO_MIN = 0.0
CASH_RATIO_MAX = 0.25

# -- market_view.posture 檢核容差 --
NET_EXPOSURE_HOLD_TOLERANCE = 0.02  # |淨流向| / NAV ≤ 2% 視為 hold

# -- 價格波動被動超限的緩衝天數（第6個交易日仍超限才記警告）--
DRIFT_GRACE_TRADING_DAYS = 5

# -- 委託規則 --
ORDER_LOT_SIZE = 1000  # 僅限整股：1張=1000股

# -- 標的池 --
UNIVERSE_PATH = ROOT_DIR / "標的池_150檔.json"

# -- D-Plan schema --
DPLAN_SCHEMA_PATH = ROOT_DIR / "D-Plan.schema_v4.3.json"
SCHEMA_VERSION = "4.3"

# -- 本地持久化狀態（價格波動超限追蹤用）--
STATE_DIR = ROOT_DIR / "state"
DRIFT_STATE_PATH = STATE_DIR / "drift_state.json"

# -- 候選新標的篩選 --
# 候選池的「資格」看主題分類（見 theme_classification.py / 主題分類_AI半導體供應鏈.json），
# 不是看今天有沒有新聞——新聞只用來排序「優先評估哪些」，不是決定誰夠格。
NEWS_THEME_KEYWORDS = ["AI伺服器", "半導體供應鏈", "晶圓代工", "先進封裝"]
TARGET_SCOPE_SIZE = 33  # 每天inferences要評估的個股總數上限（持股+候選新標的），控制單次呼叫規模／成本
MAX_SOURCES = 45  # D-Plan schema限制 sources 最多50筆，留一點緩衝給大盤/SOX/匯率等固定來源

# -- LLM --
LLM_PROVIDER = "openai"  # agent_metadata.model_provider 的合法值之一
LLM_MODEL_VERSION = "gpt-6-astra"  # agent_metadata.model_version；改模型要同步更新 code_version

# -- 團隊 --
TEAM_ID_ENV_VAR = "AI_CUP_TEAM_ID"  # 建議從環境變數讀，不要寫死進程式碼
