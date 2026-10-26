"""抓取大盤與個股原始資料。

已驗證可用（2026-10-26 本機測試過）：
- TWSE 個股收盤價：openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL（公開，不需金鑰）
- TPEX 個股收盤價：www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes（公開，不需金鑰）
- TWSE 大盤指數：openapi.twse.com.tw/v1/exchangeReport/MI_INDEX（公開，不需金鑰）
- SOX／台幣匯率：Yahoo Finance（經 yfinance 套件，免金鑰，authority=vendor）
- 新聞：鉅亨網(Anue) api.cnyes.com 的新聞列表／搜尋端點（免金鑰，authority=media；
  非官方文件化API，格式可能變動，已依規格書建議接上 archive.org 存檔備援）

尚未實作（需要你補金鑰/確認端點後接上，見各函式 docstring）：
- MOPS 月營收／重大訊息
- 三大法人買賣超（TWSE 有開放資料，但本機測試時端點回傳302，需要確認正式路徑）
- 期貨夜盤（TAIFEX）

回傳格式統一為 RawDataItem，供 build_sources.py 組裝成 D-Plan 的 sources[]。
"""

from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Any, Literal

import requests

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))

TWSE_STOCK_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_MI_INDEX = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
TPEX_DAILY_CLOSE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
ANUE_NEWSLIST_URL = "https://api.cnyes.com/media/api/v1/newslist/category/{category}"
ANUE_SEARCH_URL = "https://api.cnyes.com/media/api/v1/search/news"
ANUE_ARTICLE_URL = "https://news.cnyes.com/news/id/{news_id}"
ARCHIVE_SAVE_URL = "https://web.archive.org/save/{url}"

Authority = Literal["twse", "tpex", "taifex", "mops", "fininst", "media", "vendor", "other"]


@dataclass
class RawDataItem:
    """一筆原始資料，對應未來 sources[] 裡的一個 source_id。"""

    authority: Authority
    url: str
    content_as_of: dt.datetime  # 內容描述的時點（非抓取時間）
    name: str | None = None
    archive_url: str | None = None
    published_at: dt.datetime | None = None
    fetched_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(TAIPEI_TZ))
    payload: Any = None  # 原始資料本體，供 LLM 做事實摘要（observations）用


def _roc_date_to_iso(roc_date: str) -> str:
    """'1151022' -> '2026-10-22'（民國年轉西元年）。"""
    year = int(roc_date[:3]) + 1911
    month = roc_date[3:5]
    day = roc_date[5:7]
    return f"{year}-{month}-{day}"


def fetch_twse_close_prices() -> tuple[dict[str, float], dt.datetime]:
    """回傳 {證券代號: 收盤價}（上市，最近一個交易日全市場）與該資料對應的交易日（content_as_of）。

    用途：order_derivation.py 需要的「前日收盤價」，上市股查這裡。
    """
    resp = requests.get(TWSE_STOCK_DAY_ALL, timeout=15)
    resp.raise_for_status()
    rows = resp.json()
    prices: dict[str, float] = {}
    content_date_iso = None
    for row in rows:
        code = row["Code"]
        closing = row["ClosingPrice"]
        if closing in ("", "--", None):
            continue
        try:
            prices[code] = float(closing)
        except ValueError:
            continue
        if content_date_iso is None:
            content_date_iso = _roc_date_to_iso(row["Date"])
    content_as_of = dt.datetime.fromisoformat(f"{content_date_iso}T13:30:00+08:00")
    return prices, content_as_of


def fetch_tpex_close_prices() -> tuple[dict[str, float], dt.datetime]:
    """回傳 {證券代號: 收盤價}（上櫃，最近一個交易日全市場）與對應交易日。"""
    resp = requests.get(TPEX_DAILY_CLOSE, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    prices: dict[str, float] = {}
    content_date_iso = None
    for row in rows:
        code = row["SecuritiesCompanyCode"]
        closing = row["Close"]
        if closing in ("", "--", None):
            continue
        try:
            prices[code] = float(closing)
        except ValueError:
            continue
        if content_date_iso is None:
            content_date_iso = _roc_date_to_iso(row["Date"])
    content_as_of = dt.datetime.fromisoformat(f"{content_date_iso}T14:30:00+08:00")
    return prices, content_as_of


def fetch_prev_close_prices(tickers: list[str]) -> dict[str, float]:
    """合併 TWSE + TPEX 收盤價，只回傳 tickers 需要的部分。

    注意：這裡拿到的是「最近一個公開交易日」收盤價，請在呼叫端核對它確實是
    你要送出的 D-Plan 對應的「前一交易日」，不要假設抓到的就一定是 T-1
    （例如連假、抓取時機差一天都可能對不上）。
    """
    twse_prices, _ = fetch_twse_close_prices()
    tpex_prices, _ = fetch_tpex_close_prices()
    merged = {**twse_prices, **tpex_prices}
    return {t: merged[t] for t in tickers if t in merged}


def fetch_taiex_index() -> RawDataItem:
    """大盤發行量加權股價指數（TWSE MI_INDEX）。"""
    resp = requests.get(TWSE_MI_INDEX, timeout=15)
    resp.raise_for_status()
    rows = resp.json()
    taiex_row = next(r for r in rows if r["指數"] == "發行量加權股價指數")
    content_as_of = dt.datetime.fromisoformat(f"{_roc_date_to_iso(taiex_row['日期'])}T13:30:00+08:00")
    return RawDataItem(
        authority="twse",
        url=TWSE_MI_INDEX,
        content_as_of=content_as_of,
        name="發行量加權股價指數",
        payload=taiex_row,
    )


def fetch_institutional_investors() -> RawDataItem:
    """三大法人買賣超。TODO：TWSE開放資料平台上有此資料集，但本機測試
    openapi.twse.com.tw/v1/fund/T86 回傳302，請上
    https://openapi.twse.com.tw/ 的資料集清單確認正式路徑後補上。"""
    raise NotImplementedError("三大法人端點待確認，見本函式 docstring")


def fetch_monthly_revenue(ticker: str) -> RawDataItem:
    """公開資訊觀測站（MOPS）月營收。TODO：MOPS 無公開 JSON API，通常需解析
    mopsov.twse.com.tw 的查詢頁面或使用第三方整理過的資料集，需要你確認要用
    哪一種方式（直接解析 MOPS 頁面 or 用 vendor 整理好的資料再標 authority=mops
    是不對的，MOPS 原始公告才能標 mops，用 vendor 整理的要標 vendor）。"""
    raise NotImplementedError("MOPS 月營收抓取方式待確認，見本函式 docstring")


def fetch_material_news(ticker: str) -> list[RawDataItem]:
    """MOPS 重大訊息公告。TODO：同上，需確認抓取方式。"""
    raise NotImplementedError("MOPS 重大訊息抓取方式待確認，見本函式 docstring")


def _fetch_yahoo_last_close(yahoo_symbol: str, *, name: str) -> RawDataItem:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        hist = yf.Ticker(yahoo_symbol).history(period="5d")
    if hist.empty:
        raise RuntimeError(f"Yahoo Finance 查無 {yahoo_symbol} 的資料")

    last_row = hist.iloc[-1]
    prev_row = hist.iloc[-2] if len(hist) >= 2 else last_row
    last_close = float(last_row["Close"])
    chg_pct = (last_close - float(prev_row["Close"])) / float(prev_row["Close"]) * 100 if len(hist) >= 2 else 0.0

    content_as_of = hist.index[-1].to_pydatetime()
    # yfinance 回傳的是該交易所當地時區（SOX是美東、TWD=X是UTC），規格要求一律台北時區，
    # 要用 astimezone 轉換，不能只是直接貼上 +08:00（那樣時間點本身就錯了）。
    if content_as_of.tzinfo is None:
        content_as_of = content_as_of.replace(tzinfo=TAIPEI_TZ)
    else:
        content_as_of = content_as_of.astimezone(TAIPEI_TZ)

    return RawDataItem(
        authority="vendor",
        # yahoo_symbol可能含^這類URI保留字元（例如^SOX），沒編碼會讓url不符合RFC3986的uri格式
        url=f"https://finance.yahoo.com/quote/{urllib.parse.quote(yahoo_symbol, safe='')}",
        content_as_of=content_as_of,
        name=name,
        payload={"close": last_close, "chg_pct": round(chg_pct, 4)},
    )


def fetch_sox_index() -> RawDataItem:
    """費城半導體指數（SOX），Yahoo Finance ^SOX。"""
    return _fetch_yahoo_last_close("^SOX", name="費城半導體指數(SOX)")


def fetch_usd_twd_rate() -> RawDataItem:
    """台幣兌美元匯率，Yahoo Finance TWD=X。"""
    return _fetch_yahoo_last_close("TWD=X", name="美元兌新台幣匯率")


def fetch_futures_night_session() -> RawDataItem:
    """台指期夜盤。authority 應填 "taifex"（期交所）或 "vendor"（若是行情商整理的）。
    TODO：期交所官網有夜盤行情，請確認要直接解析期交所頁面還是走 vendor。"""
    raise NotImplementedError("期貨夜盤來源待確認，見本函式 docstring")


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw_html)
    return html.unescape(text).strip()


def try_archive_url(url: str, *, timeout: int = 5) -> str | None:
    """嘗試送 web.archive.org 存檔，失敗（常見：archive.org很慢或暫時擋）就回傳 None，
    不讓存檔失敗擋住整條 pipeline——規格書說的是「建議」，不是必要條件。

    實測筆記（2026-10-26）：在目前這個環境，就算把 timeout 拉到25秒也幾乎必定逾時，
    research 環境裡存檔功能幾乎等於失效——所以 fetch_news 預設把 with_archive 關掉，
    等你在實際部署環境確認連得到 web.archive.org 之後再打開。"""
    try:
        resp = requests.get(ARCHIVE_SAVE_URL.format(url=url), timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return resp.url
    except requests.RequestException:
        pass
    return None


def fetch_news(
    *,
    keywords: list[str] | None = None,
    limit_per_query: int = 5,
    with_archive: bool = False,
) -> list[RawDataItem]:
    """鉅亨網(Anue)新聞：keywords 為空時抓台股要聞(tw_stock分類)，
    否則對每個關鍵字（例如個股代號、"AI伺服器"、"半導體供應鏈"）各搜尋一次。

    注意：api.cnyes.com 是鉅亨網網站/App自己在用的內部端點，不是正式文件化、對外承諾
    穩定性的開發者API，格式可能說變就變；已依規格書建議嘗試存檔到 archive_url 備援。
    """
    items: list[RawDataItem] = []
    seen_news_ids: set[int] = set()

    if keywords:
        queries = [(ANUE_SEARCH_URL, {"q": kw, "limit": limit_per_query}) for kw in keywords]
    else:
        queries = [(ANUE_NEWSLIST_URL.format(category="tw_stock"), {"page": 1, "limit": limit_per_query})]

    for url, params in queries:
        resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        # search 端點不一定會照 limit 參數回應（實測固定回 per_page=20），用這裡的切片保證上限
        rows = resp.json()["items"]["data"][:limit_per_query]
        for row in rows:
            news_id = row["newsId"]
            if news_id in seen_news_ids:
                continue
            seen_news_ids.add(news_id)

            article_url = ANUE_ARTICLE_URL.format(news_id=news_id)
            published_at = dt.datetime.fromtimestamp(row["publishAt"], tz=TAIPEI_TZ)
            archive_url = try_archive_url(article_url) if with_archive else None

            items.append(
                RawDataItem(
                    authority="media",
                    url=article_url,
                    content_as_of=published_at,
                    name=row["title"],
                    archive_url=archive_url,
                    published_at=published_at,
                    payload={"title": row["title"], "content": _strip_html(row.get("content", ""))[:1500]},
                )
            )
    return items
