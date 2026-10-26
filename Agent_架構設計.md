# 每日 D-Plan Agent — 架構設計

對應 ETF 主題：[ETF_公開說明書_草稿.md](ETF_公開說明書_草稿.md)（AI/半導體供應鏈）
LLM：OpenAI GPT（`agent_metadata.model_provider = "openai"`）
資料來源：twse / tpex / mops openapi ＋ 財經資料 API（Yahoo Finance、CMoney、富果 Fugle）

## 設計總原則：LLM 負責「理解與推論」，程式碼負責「計算與驗證」

D-Plan 最容易被退件的地方不是「判斷錯」，而是「鏈斷掉」或「委託跟決策對不上」。
所以分工定死：

| 層 | 誰做 | 原因 |
|---|---|---|
| sources / observations | 程式碼抓資料 + GPT 做事實摘要 | GPT 擅長把原始資料/新聞濃縮成一句話＋數字，但抓取與存檔要程式碼保證可重現 |
| market_view / inferences | GPT 推論，程式碼做欄位合法性檢查 | 這是「判斷」的核心價值所在，交給 GPT；但 enum/範圍由程式碼擋 |
| decisions | GPT 建議 target_weight，程式碼 clamp 到風險上限 | 避免 GPT 算出不合規的權重（現金<0%或>25%、個股超過上限） |
| orders | **純程式碼公式，禁止 GPT 碰數字** | 規格明文：「須與推導演算法結果一致」，GPT 算數字有 hallucinate 風險，直接用官方公式 `floor(target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000` |
| 提交前驗證 | 純程式碼（jsonschema + 自訂規則） | 不能靠 GPT 自己說「我檢查過了」 |

---

## 每日流程（Pipeline）

```
00) 下載 DataPack（文件繳交頁）
     ↓
01) 抓市場與個股原始資料（twse/tpex/mops openapi + Yahoo Finance/CMoney/Fugle）
     ↓  （全部存 raw cache，含抓取時間戳，供事後重抓比對）
02) 組裝 sources[]（每個原始資料來源 → 一筆 source_id，標 authority/url/content_as_of）
     ↓
03) GPT：raw data + 新聞 → observations[]（只寫事實＋數字，禁止推論語句）
     ↓
04) GPT：市場級 observations → market_view（regime/stance/posture/counter_evidence）
     ↓
05) GPT：個股級 observations（+ market_view）→ inferences[]（對 150 檔池逐檔或變動標的產生推論）
     ↓
06) 決策引擎（程式碼）：inferences → decisions[] / no_trade_decisions[]
     - 套用風險 clamp：現金 0–25%、2330 上限 25%、其餘個股上限 10%、持股 20–30 檔、150 檔池
     - 覆蓋檢查：昨日每一檔持股必須落在 decisions 或 no_trade_decisions 其中之一，不重不漏
     ↓
07) 官方公式（程式碼）：decisions + DataPack(前日淨值/持股) + 前日收盤價 → orders[]
     ↓
08) agent_metadata：model_provider=openai、model_version、run_started_at/run_completed_at、
     code_version（當日程式/prompt 版本識別）、token 用量
     ↓
09) 本地驗證（見下節），全部通過才輸出
     ↓
10) 輸出 D-Plan_<team_id>_<trade_date>.json
```

---

## 模組拆分建議

```
d_plan_agent/
  fetch_datapack.py        # 下載/解析當日 DataPack，判斷 missing/as_of_date 情境
  fetch_market_data.py     # 呼叫 twse/tpex/mops openapi + Yahoo Finance/CMoney/Fugle
                            # 統一存成 raw cache（含 fetched_at、content_as_of）
  build_sources.py         # raw cache → sources[]，流水號 S1.. 從 1 開始每天重編
  llm_observations.py      # GPT structured output：raw data/新聞 → observations[]
  llm_market_view.py       # GPT：市場級 observations → market_view
  llm_inferences.py        # GPT：個股 observations → inferences[]
  decision_engine.py       # 規則引擎：inferences → decisions/no_trade_decisions，套風險 clamp
  order_derivation.py      # 官方公式計算 orders，不經過 LLM
  validate_dplan.py        # jsonschema(D-Plan.schema_v4.3.json) + 自訂一致性檢查
  main.py                  # 串起 00~10，失敗就中止不送出
```

### GPT 呼叫建議

- 全部用 **structured output / function calling** 硬性要求 JSON schema 輸出（對應 observation/inference 等 `$defs`），不要用自由文字後再自己 parse，減少格式錯誤。
- `observations` 這層的 system prompt 要明確禁止推論語句，只能是「可核對的事實＋數字」，否則抽檢時會被挑出「L1 寫了判斷」。
- `inferences`/`market_view` 的 prompt 裡要把 ETF 投資理念全文餵進去，確保 `logic` 文字持續呼應主題（供應鏈地位、AI運算需求、總體敏感度），抽檢會對照公開說明書審一致性。
- `counter_evidence` 不要讓模型偷懶填 null：prompt 裡要求「找不到才填 null，否則必須具體寫出」。

### 風險 clamp（decision_engine.py 要硬編的規則）

- 持股檔數：20–30 檔
- 標的池：僅限 150 檔名單內
- 現金比：0–25%（market_view.posture.target_cash_pct_range 上限不得超過 0.25）
- 個股權重：2330 上限 25%，其他個股上限 10%
- `net_exposure_intent` 與委託加總方向要一致（increase→淨買超／reduce→淨賣超／hold→誤差 ≤2% 淨值），這段用前日收盤價先估算一次，不一致就退回決策引擎重調
- `funding_for` 只在真的有資金缺口時才標記：拿掉該筆賣出重算，現金比要跌破宣告下限才算數，程式碼直接算一次驗證，不要讓 GPT 自稱

### 新聞/資料來源分類（配合你選的 API 來源）

- twse/tpex openapi → `authority: "twse"` / `"tpex"`（指數、個股價量、三大法人）
- mops → `authority: "mops"`（月營收、重大訊息、法說會公告）
- Yahoo Finance/CMoney/Fugle 的**行情數據** → `authority: "vendor"`
- 這些平台若附帶的是**新聞報導/評論內容** → `authority: "media"`，建議仍備份原始回應 JSON＋送 archive_url（即使走 API，新聞內容事後可能下架或改版，抽檢時沒有存檔佐證的話關鍵來源會被列為合理性審查優先對象）

---

## 提交前驗證清單（validate_dplan.py 要覆蓋）

1. `jsonschema` 驗證整份檔案符合 `D-Plan.schema_v4.3.json`
2. 引用鏈：orders→decisions→inferences→observations→sources 逐一比對 id 存在
3. `orders` 重新用官方公式算一次，比對是否與程式輸出一致（防止中途被手改）
4. 覆蓋度：DataPack 裡昨日 `holdings`（`shares>0`）每一檔都出現在 decisions 或 no_trade_decisions，且不重複
5. `posture` 一致性：用前日收盤價重算委託淨流向與預估現金比，核對是否落在宣告範圍
6. 個股權重 / 現金比 / 持股檔數上下限
7. `funding_for` 缺口存在性驗證
8. 檔名 `D-Plan_<team_id>_<trade_date>.json` 與 JSON 內 `team_id`/`trade_date` 一致
9. `agent_metadata.run_completed_at` ≤ 目前時間（避免程式時區算錯）

任何一項沒過，`main.py` 應該直接中止、印出失敗原因，不要送出——整份退回的代價遠高於晚一點送。

---

## 比賽規則補充（來自主辦方簡報，2026-10-26 確認）

### 標的池與交易方式

- 基準日 2026-07-31：上市市值前100大 ＋ 上櫃市值前50大，共150檔
- 僅限台股現股交易，不可融資、融券、借券、放空、當沖
- 僅限整股交易（1張＝1,000股），不進行零股交易

### 投資組合限制（與風險 clamp 一致）

| 項目 | 限制 |
|---|---|
| 持股檔數 | 每日 20–30 檔 |
| 單一個股權重 | 2330 ≤ 淨值25%；其餘個股 ≤ 淨值10% |
| 現金部位 | ≤ 淨值25%，不得為負 |

權重分母為可交易淨值（`nav_tradable`），不含應收配息、配股。

### 違規與罰則

每日發生以下任一情形記警告一次（當日多項僅記一次）：

1. 當日未繳交 D-Plan
2. 當日結算現金為負或比例超過25%
3. 當日持股檔數非介於20–30檔
4. 單一個股持有比例超過上限（台積電25%，其餘10%）
5. 當日持有非150檔名單內之個股
6. 當日交易決策與交易行為不一致（抽檢）

違規當日交易視為無效，以前一日投組計算淨值。警告累積達3次，取消參賽資格。

⚠️ **兩種超限的處理時機不同，`decision_engine.py` 要分開處理**：

- **因當日加碼交易超限** → 當天交易直接視為無效並記警告（下單前 clamp 就要擋掉，不能讓這種委託產生）
- **因價格波動造成既有持股被動超限**（沒有新下單、股價漲上去把權重頂過上限）→ 給 **5 個交易日**緩衝自行減碼，第6個交易日仍超限才記警告。這代表 decision_engine 需要**逐日追蹤**每檔持股「是否處於價格波動超限中、已經第幾天」，並在緩衝期內主動排入減碼的 inference/decision，而不是等到第6天才處理

### 初始本金 ✅ 已確認

新台幣 **10億元**（主辦方確認，2026-10-26）。`d_plan_agent/config.py` 的 `INITIAL_CAPITAL` 已設為 `1_000_000_000.0`。

---

## 實作狀態（2026-10-26）

骨架已實作在 [d_plan_agent/](d_plan_agent/)，並用 [tests/test_pipeline_smoke.py](tests/test_pipeline_smoke.py)（全持股HOLD情境）與
[tests/test_pipeline_trim_buy.py](tests/test_pipeline_trim_buy.py)（TRIM+BUY+funding_for，含一個刻意製造「缺口不存在卻標記funding_for」的反向測試）
跑通驗證：schema、引用鏈、orders官方公式重算、覆蓋度、標的池限制、權重上限、funding_for缺口、posture（淨曝險/現金區間）一致性、檔名規則全數通過。

| 模組 | 狀態 | 說明 |
|---|---|---|
| `config.py` | ✅ 完成 | 所有規則常數（含初始本金、權重上限、5日緩衝天數） |
| `universe.py` | ✅ 完成 | 讀 `標的池_150檔.json`，已驗證150檔無重複 |
| `fetch_datapack.py` | ✅ 完成 | 解析DataPack，處理 missing/as_of_date 三種情境 |
| `fetch_market_data.py` | 🟡 部分完成 | TWSE/TPEX收盤價、TAIEX指數、SOX與台幣匯率(Yahoo Finance/yfinance)、新聞(鉅亨網Anue) **已實作並實測可用**；三大法人/MOPS/期貨夜盤仍是待確認端點的 stub |
| `build_sources.py` | ✅ 完成 | |
| `llm_client.py` / `llm_observations.py` / `llm_market_view.py` / `llm_inferences.py` | ✅ 完成（未接真實OpenAI金鑰測試） | 用OpenAI structured output強制JSON格式；inferences內部多帶 `ticker`/`action_hint`/`target_weight_hint` 給decision_engine用，組裝最終JSON時會投影掉這些非正式欄位 |
| `decision_engine.py` | ✅ 完成 | 含風險clamp、drift urgent強制TRIM覆蓋GPT建議、現金目標的簡化線性縮放、持股數20-30檢查 |
| `order_derivation.py` | ✅ 完成 | 純公式，已用真實範例數字測試正確 |
| `drift_tracker.py` | ✅ 完成 | 本地持久化於 `state/drift_state.json` |
| `validate_dplan.py` | ✅ 完成 | jsonschema + 8類自訂一致性檢查，已用正向/反向測試驗證 |
| `main.py` | 🟡 骨架完成，未端到端實跑 | sources 已接上 TAIEX/SOX/匯率/Anue新聞；三大法人/MOPS/期貨夜盤還是stub |

### 新聞／總經資料來源決定（2026-10-26）

- **新聞內容** → 鉅亨網(Anue) `api.cnyes.com` 的新聞列表／搜尋端點（`fetch_market_data.fetch_news`），`authority="media"`，已實測可抓到AI伺服器/半導體供應鏈相關新聞。已寫好 `try_archive_url` 嘗試存檔到 web.archive.org 的邏輯，但**實測發現這個環境連web.archive.org幾乎必定逾時**（測過8秒、25秒timeout都失敗），導致 `main.py` 第一次端到端測試卡住超過240秒。已把 `fetch_news` 的 `with_archive` 預設改成 `False`，等你在實際部署環境確認連得上archive.org之後再打開（spec說的是「建議」存檔不是必要條件，不該讓它拖垮整條pipeline）。
  ⚠️ 這是鉅亨網網站/App自己在用的內部端點，**不是正式文件化、對外承諾穩定性的開發者API**，格式可能說變就變，要有這個心理準備並定期檢查。
- **SOX／台幣匯率** → Yahoo Finance（`yfinance`套件，`fetch_sox_index`/`fetch_usd_twd_rate`），`authority="vendor"`，已實測可用。
- 之所以沒選CMoney/Fugle：Fugle是行情/交易API沒有新聞端點、且台股收盤價已用TWSE/TPEX官方API免費拿到，功能重複；CMoney的「數據貓頭鷹」平台偏財務/選股數據，申請流程與新聞覆蓋度都不明確。

### 已知簡化，上線前建議再補強

1. **`decision_engine._scale_to_cash_target`** 只做一次線性縮放，沒有疊代最佳化；縮放後若剛好卡在個股上限會讓現金比偏離目標，交由 `validate_dplan` 的posture檢核攔截並提醒人工調整，不會悄悄送出不合規的檔案。
2. **候選新標的的篩選**（`main.py._pick_candidate_tickers`）目前只是按代號排序取前N檔，沒有實質邏輯；建議換成「法人買超排行」或「量能異常」之類有依據的篩選，篩選理由最好也留痕以備抽檢。
3. **新聞查詢關鍵字**（`main.py._build_news_keywords`）目前是理念關鍵字＋標的清單前6檔，數量上限是為了不要每天送太多次archive.org存檔（比較慢）；建議換成「今天比較需要新資訊的標的」（有法說會、法人買賣超異常的那幾檔）。
4. **三大法人／MOPS／期貨夜盤**：`fetch_market_data.py` 裡仍是 `NotImplementedError` 的 stub，三大法人的TWSE端點本機測試回傳302，需要確認正式路徑；MOPS無公開JSON API，需決定解析方式。
5. `main.py` 目前還沒有實際用 `OPENAI_API_KEY` 跑過（測試都用 `tests/mock_llm.py` 假資料），正式上線前建議先用真實金鑰跑一次完整流程，確認GPT的structured output實際回傳格式跟我們定義的json_schema吻合。

## 待補事項（需要你後續確認）

- `fetch_market_data.py` 裡的三大法人／MOPS／期貨夜盤來源，需要你確認端點或抓取方式
- `main.py._pick_candidate_tickers`、`_build_news_keywords` 的篩選邏輯（目前都只是合理的佔位，不是最終策略）
- `code_version` 的對帳機制：建議每天收盤前 `git commit` 一次，把 commit hash 填進 `agent_metadata.code_version`（`main.py._resolve_code_version` 已經會自動抓目前的 git commit hash，前提是這個資料夾要先 `git init` 並開始做版本控管）

## LLM 設定

- `agent_metadata.model_provider`: `openai`
- `agent_metadata.model_version`: `gpt-6-astra`（見 `config.LLM_MODEL_VERSION`；若之後換模型要同步更新這裡與 `code_version`，因為規格明文「影響決策行為的參數和提示詞模板都算程式碼的一部分」）
- 需要設定環境變數 `OPENAI_API_KEY` 才能真正呼叫（`llm_client.py`）
