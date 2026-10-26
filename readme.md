# AI CUP — D-Plan Agent

玉山AI CUP「主動式ETF」挑戰賽的每日D-Plan生成Agent。每個交易日讀取DataPack（昨日結算部位），
抓新聞與市場資料，用GPT產出符合規格的D-Plan JSON並送出。

## 文件

- [ETF_公開說明書_草稿.md](ETF_公開說明書_草稿.md) — 本隊ETF名稱／投資主題／投資理念
- [Agent_架構設計.md](Agent_架構設計.md) — 整體架構、每日pipeline、已知限制與待補事項（**改程式前請先看這份**）
- [DataPack_規格書_v4.3.md](DataPack_規格書_v4.3.md) / [D-Plan_撰寫指南_v4.3.md](D-Plan_撰寫指南_v4.3.md) / [D-Plan.schema_v4.3.json](D-Plan.schema_v4.3.json) — 主辦方規格文件
- [標的池_150檔.json](標的池_150檔.json) — 交易標的池（上市100＋上櫃50）
- [主題分類_AI半導體供應鏈.json](主題分類_AI半導體供應鏈.json) — 150檔裡哪些屬於本ETF主題的靜態分類（候選新標的的篩選依據）

## 環境設定

```bash
pip install -r requirements.txt
```

在專案根目錄建立 `.env`（已加入`.gitignore`，不會進版控）：

```
OPENAI_API_KEY=sk-...
```

## 每日執行

```bash
python3 -m d_plan_agent.main --datapack <DataPack檔案路徑> --team-id <隊伍代號> --output-dir output
```

- `--team-id` 要跟DataPack檔案裡的 `team_id` 欄位一致
- 全程約3-5分鐘、約15-25k GPT token（視當天候選股/持股檔數而定)
- 驗證通過才會輸出 `output/D-Plan_<team_id>_<trade_date>.json`；不論成功失敗都會存一份
  `output/_debug_last_run.json` 供除錯（不用重跑GPT就能檢查中間結果）

DataPack檔案請放進 [datapacks/](datapacks/)。

## 測試（不呼叫真實OpenAI/網路，免費）

```bash
python3 tests/test_pipeline_smoke.py
python3 tests/test_pipeline_trim_buy.py
python3 tests/test_decision_engine_cash_scaling.py
```

## 專案結構

```
d_plan_agent/
  config.py              規則常數（持股數、權重上限、現金範圍、初始本金...）
  universe.py             讀150檔標的池
  theme_classification.py 讀主題分類（候選新標的資格）
  fetch_datapack.py       解析DataPack
  fetch_market_data.py    抓TWSE/TPEX/SOX/匯率/新聞
  build_sources.py        組裝D-Plan的sources[]
  llm_client.py           OpenAI呼叫封裝（structured output）
  llm_observations.py     L1 observations
  llm_market_view.py      L2M market_view
  llm_inferences.py       L2 inferences
  decision_engine.py      L3 decisions/no_trade_decisions（風險clamp、現金縮放）
  order_derivation.py     L4 orders（官方公式，不經LLM）
  drift_tracker.py        價格波動超限的5日緩衝追蹤
  validate_dplan.py       提交前驗證（schema + 一致性檢查）
  main.py                 串起整條流程的CLI
tests/                    測試（含mock LLM，不花錢不用網路）
datapacks/                放DataPack檔案
output/                   D-Plan輸出與debug dump（已gitignore）
state/                    本地持久化狀態，例如drift_state.json（已gitignore）
```

## 已知限制

三大法人／MOPS月營收與重大訊息／期貨夜盤尚未接上，候選股篩選與現金縮放邏輯都還是簡化版本。
完整清單見 [Agent_架構設計.md](Agent_架構設計.md) 的「已知簡化」與「待補事項」章節。
