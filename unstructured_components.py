"""
unstructured_components.py — unstructured 解析／chunking 處理組件（**函式庫，非執行入口**）

前身是 data_update_unstructure.py（自帶 CLI，寫入現已棄用的 collection
us_stock_rag_unstructured）。2026-08-07 改造：ingest 入口統一收斂到
data_update_edgar.py 一支，本檔只保留可被重用的「原始檔案 → elements → chunk records」
處理組件，不再自帶 CLI、Qdrant 寫入、MD5 增量。

被移除的部分不是消失，是各有歸屬（DRY：同一件事只留一份實作，避免兩份邏輯漂移）：
  - main() / argparse CLI          → data_update_edgar.py main()
  - ensure_collection              → data_update_edgar.py ensure_collection（帶 collection 參數）
  - delete_points_by_source        → data_update_edgar.py 內聯 delete by source
  - compute_md5 / load / save_hashes → data_update_edgar.py 的 .txt MD5 快取（hashes_edgar.json）

提供的組件：
  - 解析清理   partition_and_clean / render_processed
  - 表格處理   html_table_to_markdown / table_element_to_text / _find_caption /
               _count_table_rows / _table_context / _compact_table_markdown /
               _llm_summarize_table（無 caption 的大表用 Gemini 補摘要）
  - 分段切塊   _is_section_header / _split_text_elements_into_sections / build_chunk_records
  - filing 中繼資料  extract_filing_metadata / build_metadata_header
  - 其他       BGEM3DenseEmbeddings（SemanticChunker 的 BGE-M3 adapter）、deterministic_uuid

用法：由 data_update_edgar.py import，不直接執行（沒有 main，執行了也不會做任何事）。
"""

import os          # _llm_summarize_table 讀 GEMINI_API_KEY
import re
import time        # _llm_summarize_table 的 429 退避
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 本檔可被單獨 import 當函式庫用，故自己載入 .env，不依賴呼叫端先載過。
load_dotenv()


# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def deterministic_uuid(name: str) -> str:
    """Qdrant point IDs must be int or UUID. Deterministic UUID lets us replay
    --rebuild and get the same IDs for the same chunk_id string."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


# ══════════════════════════════════════════════════════════════════════════════
# Filing metadata extraction (filing_type / fiscal_year / fiscal_period / report_label_year)
#
# 與 data_update.py 的 extract_filing_metadata() 邏輯相同：優先讀 XBRL dei:
# cover-page tags 取得文件「自己宣稱」的會計期間，缺漏的欄位用檔名規則補齊。
#
# report_label_year 額外永遠從檔名抽取（不受 dei: 是否存在影響）。動機：檔名
# 年份（例如 AAPL_10K_2026.html）代表「使用者口語/資料集收錄時間點」語意，
# 跟 XBRL 的 fiscal_year（AAPL 該檔案實際是 FY2025）是兩個不同座標系——
# AAPL/META 的會計年度結束月跟日曆年度不同調，兩者常常差 1。query-filter
# 從使用者問題抽出來的年份通常對應檔名語意，因此額外保留這個欄位讓
# rag_query.py 的 hard filter 可以同時比對兩種年份語意。
# ══════════════════════════════════════════════════════════════════════════════

DEI_FIELD_MAP = {
    "dei:documenttype":              "filing_type",
    "dei:documentfiscalyearfocus":   "fiscal_year",
    "dei:documentfiscalperiodfocus": "fiscal_period",
}
_IX_NONNUMERIC_RE = re.compile(r"ix:nonnumeric", re.I)
_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Z]+)_(?P<filing>10K|10Q)_(?P<yyyymm>\d{4,6})")


def extract_filing_metadata(path: Path) -> dict:
    """回傳 {"filing_type": "10-K", "fiscal_year": "2025", "fiscal_period": "FY",
    "report_label_year": "2026"}。優先讀 XBRL dei: cover-page tags，缺漏的欄位
    用檔名規則補齊（例如 NVDA_10K_2026.html -> filing_type=10-K, fiscal_year=2026）。
    report_label_year 永遠取自檔名（與 fiscal_year 是兩個獨立座標系，見上方說明）。"""
    meta: dict = {}
    try:
        from bs4 import BeautifulSoup
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(_IX_NONNUMERIC_RE):
            key = DEI_FIELD_MAP.get((tag.get("name") or "").lower())
            if key and key not in meta:
                value = tag.get_text(strip=True)
                if value:
                    meta[key] = value
    except Exception as e:
        print(f"  [WARN] dei: extraction failed for {path.name}: {e!r}")

    m = _FILENAME_RE.match(path.stem)
    if m:
        meta.setdefault("filing_type", "10-K" if m.group("filing") == "10K" else "10-Q")
        meta.setdefault("fiscal_year", m.group("yyyymm")[:4])
        meta["report_label_year"]  = m.group("yyyymm")[:4]
        meta["report_period_code"] = m.group("yyyymm")        # 完整期間碼（e.g. "202605"）
        meta["ticker"]             = m.group("ticker")         # e.g. "AAPL", "NVDA"

    return meta


def build_metadata_header(filing_meta: dict) -> str:
    """把 ticker / 期別前綴進被 embed 的文字。

    動機：fiscal_year / report_period_code 等期別資訊只存在 payload，從不出現在
    chunk 原文裡（例如 10-Q 正文不會寫「這是 202512 那一期」）——導致同一 ticker
    不同期的 chunk 文字幾乎相同，dense/sparse/BM25 任何文字檢索都無法區分期別，
    只能仰賴 query-understanding 的 hard filter。把這些 metadata 前綴進文字後，
    檢索本身也具備期別感知，hard filter 失準或未觸發時仍有救。"""
    parts = []
    if filing_meta.get("ticker"):
        parts.append(filing_meta["ticker"])
    if filing_meta.get("filing_type"):
        parts.append(filing_meta["filing_type"])
    period_code = filing_meta.get("report_period_code")
    if period_code:
        parts.append(f"period {period_code}")
    elif filing_meta.get("fiscal_year"):
        period_str = f"FY{filing_meta['fiscal_year']}"
        if filing_meta.get("fiscal_period"):
            period_str += f" {filing_meta['fiscal_period']}"
        parts.append(period_str)
    if not parts:
        return ""
    return "[" + " ".join(parts) + "] "


def html_table_to_markdown(html: str) -> Optional[str]:
    """把 unstructured 提供的 text_as_html 轉成 Markdown 表格語法。

    動機：原始 <table><tr><td> 標記對 embedding 模型來說雜訊很重（一堆標籤、
    樣式屬性，數字訊號被稀釋），Markdown 表格語法（| a | b |）保留相同的
    列/欄結構，但更精簡、更接近模型訓練語料中常見的純文字形式，embedding
    訊號應該更乾淨。轉換失敗時回傳 None，由呼叫端退回原始內容。
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return None

        rows = []
        for tr in table.find_all("tr"):
            cells = [
                " ".join(cell.get_text(separator=" ", strip=True).split())
                for cell in tr.find_all(["td", "th"])
            ]
            if any(cells):
                rows.append(cells)

        if not rows:
            return None

        n_cols = max(len(r) for r in rows)
        rows = [r + [""] * (n_cols - len(r)) for r in rows]

        def fmt_row(cells):
            return "| " + " | ".join(c.replace("|", "/") for c in cells) + " |"

        lines = [fmt_row(rows[0]), fmt_row(["---"] * n_cols)]
        lines += [fmt_row(r) for r in rows[1:]]
        return "\n".join(lines)
    except Exception:
        return None


def table_element_to_text(el) -> str:
    """取得表格元素的最終文字內容：優先把 text_as_html 轉成 Markdown，
    轉換失敗則退回原始 text_as_html，再不行就退回 unstructured 的 el.text。"""
    html = getattr(el.metadata, "text_as_html", None)
    if html:
        md = html_table_to_markdown(html)
        if md:
            return md
        return html
    return el.text


# ══════════════════════════════════════════════════════════════════════════════
# unstructured: partition + clean
# ══════════════════════════════════════════════════════════════════════════════

def partition_and_clean(path: Path) -> Optional[list]:
    """用 unstructured 把檔案解析成帶角色的 elements，並逐一清理空白字元。"""
    from unstructured.partition.html import partition_html
    from unstructured.partition.text import partition_text
    from unstructured.cleaners.core import clean_extra_whitespace

    ext = path.suffix.lower()
    try:
        if ext in {".html", ".htm"}:
            elements = partition_html(filename=str(path))
        elif ext in {".txt", ".md"}:
            elements = partition_text(filename=str(path))
        else:
            return None
    except Exception as e:
        print(f"  [WARN] unstructured partition failed: {e}")
        return None

    for el in elements:
        el.apply(clean_extra_whitespace)

    return elements


def render_processed(elements: list) -> str:
    """把 elements 轉成角色標記格式的純文字，寫進 unstructure_processed/，
    方便人眼檢查與舊版 processed/*.txt（純文字流，無角色資訊）對照。"""
    from unstructured.documents.elements import Table

    blocks = []
    for el in elements:
        role = type(el).__name__
        if isinstance(el, Table):
            body = table_element_to_text(el).strip()
            blocks.append(f"[Table]\n{body}")
        else:
            text = el.text.strip()
            if text:
                blocks.append(f"[{role}] {text}")
    return "\n\n".join(blocks)


# ══════════════════════════════════════════════════════════════════════════════
# Chunking：Table 獨立成 chunk、其餘文字走語意切割
# ══════════════════════════════════════════════════════════════════════════════

# 把表格前最近的標題/敘述當 caption prepend 進 table chunk 時，最多取多少字元。
# 限制長度是為了補回脈絡的同時，不讓一大段敘述稀釋掉表格本身的數字訊號。
TABLE_CAPTION_MAXLEN = 240

# LLM 摘要的觸發門檻：表格至少要有這麼多列才值得摘要。
# 列數 < 閾值的通常是封面 checkbox / IRS ID 之類的行政雜訊，不值得花 LLM call。
TABLE_SUMMARY_MIN_ROWS = 4

# caption 至少要有這麼多個英文字母，否則視為頁碼/編號雜訊（見 _is_meaningful_caption）。
CAPTION_MIN_LETTERS = 3

# 餵給 _llm_summarize_table 的表格 markdown 上限（壓掉分隔列之後才算）。
TABLE_SUMMARY_MAX_CHARS = 2500

# 摘要生成的 token 上限。⚠ 別再設成 60：gemini-2.5-flash 預設開 thinking，而 thinking token
# 與正文**共用**這個額度，60 會讓正文剛開頭就撞頂。實測 `us_stock_rag_edgar_head` 走到這條路
# 的 11 筆裡 10 筆被砍在 2~3 個字（`This table details`／`This table presents`／`This table`），
# 等於這條路整條沒有產出。現在同時 thinking_budget=0（見 _llm_summarize_table）。
TABLE_SUMMARY_MAX_TOKENS = 200

# 生成 caption 時往前收多少個 element 當脈絡。比 TABLE_CAPTION_LOOKBACK 大，因為這裡不是
# 「挑一個 caption」而是「給 LLM 看夠不夠判斷這是什麼表」，寧可多給幾段。
TABLE_CONTEXT_LOOKBACK = 8
TABLE_CONTEXT_MAXLEN = 900

# 429 退避。⚠ Gemini free tier 對 gemini-2.5-flash 是 **5 requests/min**（實測 429 訊息裡的
# `quotaValue: 5`），而這裡原本沒有任何重試 —— 撞到就靜默回傳 ""，表格變成完全沒 caption。
# 重建時要跑上百張表，沒有退避等於大半都拿不到摘要，而且**失敗不會留下痕跡**。
TABLE_SUMMARY_MAX_RETRIES = 4
TABLE_SUMMARY_BACKOFF = 45.0     # 秒；free tier 是「每分鐘」額度，退避要跨過整個窗口才有用


def _is_meaningful_caption(text: str) -> bool:
    """判斷一段前文能不能當表格 caption：要有實際文字內容，排除 unstructured 在
    封面表格周圍吐出的純標點/空白雜訊（例如 ', ,' 或孤立的 'OR'）。

    ⚠ 2026-08-11 加「至少 CAPTION_MIN_LETTERS 個英文字母」：原本只要求「≥3 字元且含任一
    alnum」，**頁碼會通過**。實測 `us_stock_rag_edgar_head` 有 4 個 table chunk 的 caption
    就是頁碼（META `3228`／`790`／`3215`／`3285`），其中 `META_10Q_202603 #12` 是真的
    ARPP 指標時間序列（`| ARPP: | $11.20 | $11.89 | …`）、表格本身沒有任何期間標頭 —— 而
    因為「3228」被判為有效 caption，`_llm_summarize_table` **根本沒被呼叫**。最需要摘要
    的表被一個頁碼擋在門外。
    """
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    return sum(1 for ch in stripped if ch.isalpha()) >= CAPTION_MIN_LETTERS


# 往表格前方最多回溯幾個 element 找 caption。
# 設為 2 是為了同時抓到「Title + NarrativeText → Table」這種兩層結構，
# 又不會跨越章節邊界拿到不相關的遠端文字。
TABLE_CAPTION_LOOKBACK = 2


def _count_table_rows(markdown: str) -> int:
    """從 Markdown 表格字串數出資料列數（排除 header 和分隔線）。"""
    lines = [l for l in markdown.splitlines()
             if l.strip().startswith("|") and not set(l.replace("|", "").replace("-", "").replace(" ", "")) == set()]
    # 第一列是 header，第二列是 ---, 所以資料列 = max(0, 總列數 - 2)
    return max(0, len(lines) - 2)


_TABLE_SUMMARY_SYSTEM = (
    "You are a financial document analyst. "
    "Given a Markdown table from a SEC filing (plus the document text that "
    "immediately precedes it, if provided), write ONE concise sentence "
    "(max 30 words) describing what the table contains: the type of data, "
    "which section/segment it belongs to, and the time period it covers. "
    "Use the preceding text to identify the subject — do NOT guess. "
    "If something is not stated, leave it out rather than inferring it. "
    "Do NOT repeat specific numbers. Output only the sentence, no preamble."
)

_SEPARATOR_ROW = re.compile(r"^[\s|:-]*$")


def _compact_table_markdown(markdown: str) -> str:
    """壓掉壓平表格裡的分隔列與連續空格子，讓字元上限裝的是內容而不是 `| --- |`。

    動機（實測）：走到 LLM 這條路的 11 張表，前 1500 字元裡平均 **41%** 是 `|`／`-`／空白，
    最糟的 `TSLA_10Q_202606 #13` 是 66% —— 名目上給了 1500 字元，實際內容只有幾百字元。
    只影響餵給 LLM 的副本，寫進 chunk 的 body 不變（數字訊號要留給檢索）。
    """
    lines = [l for l in markdown.splitlines() if not _SEPARATOR_ROW.match(l)]
    out = []
    for l in lines:
        l = re.sub(r"(\|\s*)+\|", "|", l)     # `| | | |` → `|`
        l = re.sub(r"[ \t]{2,}", " ", l).strip()
        if l and not _SEPARATOR_ROW.match(l):
            out.append(l)
    return "\n".join(out)


def _table_context(elements: list, table_idx: int) -> str:
    """往前收最多 TABLE_CONTEXT_LOOKBACK 個 element 的原文，當作表格的脈絡。

    與 _find_caption 的差別，也是為什麼要有這個函式：_find_caption 是在「挑**一個**能直接
    當 caption 的前文」，被 _is_meaningful_caption 擋掉的一律不要；這裡是在「湊足夠讓 LLM
    判斷這是什麼表的材料」，所以**照收**（頁碼之類的雜訊留給 LLM 自己忽略，比讓它完全看不到
    上下文好）。遇到前一張 Table 就停 —— 跨過去就是別的區塊了，同 _find_caption。

    ⚠ 這一段是**確定性取材**（Python），只有「概括成一句話」交給 LLM，判準見 memory
    `llm-vs-python-task-split`。舊版只餵表格本身，而 caption 要還原的資訊（這是什麼表、
    屬於哪一節、哪一期）本來就寫在表格**上方** —— 等於輸入裡沒有答案，換多大的模型都補不回來。
    """
    from unstructured.documents.elements import Table

    picked: list[str] = []
    for j in range(table_idx - 1, -1, -1):
        if len(picked) >= TABLE_CONTEXT_LOOKBACK:
            break
        prev = elements[j]
        if isinstance(prev, Table):
            break
        txt = (prev.text or "").strip()
        if txt:
            picked.append(txt)
    ctx = " ".join(reversed(picked))
    return ctx[-TABLE_CONTEXT_MAXLEN:] if len(ctx) > TABLE_CONTEXT_MAXLEN else ctx


def _llm_summarize_table(markdown: str, context: str = "", item_id: str = "") -> str:
    """用 Gemini 為找不到 caption 的大型表格生成一行摘要。
    摘要只描述「這是什麼表、涵蓋哪些指標」，不重複表格數字（數字已在 body 裡）。
    LLM call 失敗時靜默回傳空字串，表格仍以純 Markdown 寫入。
    走 GEMINI_API_KEY（與 rag_query.py 的 Gemini 路徑一致），而非 LiteLLM（額度/認證
    不穩定，曾在批量 ingest 時整批 401 失敗）。

    `context`（表格前方原文，由 _table_context 取）與 `item_id` 都是 2026-08-11 加的：
    公司與期間**不需要**靠這裡補（chunk 文字已被 `[TSLA 10-Q period 202606]` 前綴注入），
    缺的是「這張表在講什麼、屬於哪一節」。

    `temperature=0` ＋ `thinking_budget=0` 讓它變確定性：未固定 temperature 是
    CLAUDE.md「重建不會逐字重現舊 collection」第 ② 條記載的變異來源之一。
    """
    try:
        from google import genai
        from google.genai import types

        body = _compact_table_markdown(markdown)[:TABLE_SUMMARY_MAX_CHARS]
        parts = []
        if item_id:
            parts.append(f"Filing section: {item_id}")
        if context:
            parts.append(f"Text immediately preceding the table:\n{context}")
        parts.append(f"Table (Markdown):\n{body}")

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        config = types.GenerateContentConfig(
            system_instruction=_TABLE_SUMMARY_SYSTEM,
            max_output_tokens=TABLE_SUMMARY_MAX_TOKENS,
            temperature=0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        for attempt in range(TABLE_SUMMARY_MAX_RETRIES):
            try:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents="\n\n".join(parts),
                    config=config,
                )
                return (resp.text or "").strip()
            except Exception as e:
                # 只對額度類錯誤退避重試；其他錯誤（認證、參數）重試也不會好，直接往外拋。
                if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                    raise
                if attempt == TABLE_SUMMARY_MAX_RETRIES - 1:
                    raise
                wait = TABLE_SUMMARY_BACKOFF * (attempt + 1)
                print(f"  [WARN] table summary 429，{wait:.0f}s 後重試 "
                      f"({attempt + 1}/{TABLE_SUMMARY_MAX_RETRIES - 1})")
                time.sleep(wait)
        return ""
    except Exception as e:
        print(f"  [WARN] LLM table summary failed: {e!r}")
        return ""


def _find_caption(elements: list, table_idx: int) -> str:
    """往前回溯最多 TABLE_CAPTION_LOOKBACK 個 element 找 caption。

    停止條件（任一成立即停止，不繼續往前）：
    - 遇到另一張 Table（代表跨表格了，不同區塊）
    - 已回溯超過 TABLE_CAPTION_LOOKBACK 個 element

    回傳第一個（最近的）通過 _is_meaningful_caption() 的文字；
    若找不到則回傳空字串（= 不加 caption）。
    """
    from unstructured.documents.elements import Table

    lookback = 0
    for j in range(table_idx - 1, -1, -1):
        if lookback >= TABLE_CAPTION_LOOKBACK:
            break
        prev = elements[j]
        if isinstance(prev, Table):
            break  # 跨過另一張表，停止
        txt = (prev.text or "").strip()
        lookback += 1
        if _is_meaningful_caption(txt):
            return txt[:TABLE_CAPTION_MAXLEN]
    return ""


# 累積到這個大小才允許在下一個 section header 硬切段。動機：純用 header 當硬邊界會把
# 「header + 一句話」切成孤立小 chunk，cross-encoder 對無上下文的孤立段落評分結構性偏低
# （見 CHANGELOG「孤立段落」機制型死路）；設一個 soft-min 讓太小的小節往後併，只有累積到
# 夠份量時才切，兼顧「拆開稀釋型大 chunk（sem-08）」與「不製造孤兒 chunk」。
SECTION_SOFT_MIN_CHARS = 350


def _is_section_header(el) -> bool:
    """判斷一個文字 element 是否為小節標題（section header），用來當 chunk 硬邊界。

    signal：unstructured 把 10-K MD&A 的小節標題（'Operating Income'、'Interest Income and
    Expense'、'Income Taxes' …）歸類為 Title 或**通用 Text**（body 段落是 NarrativeText，
    是 Text 的子類但 __name__ 不同）。故 Title 一律視為 header；通用 Text 若「短、字數少、
    不以句末標點結尾」也視為 header。"""
    from unstructured.documents.elements import Title

    txt = (el.text or "").strip()
    if not txt:
        return False
    if isinstance(el, Title):
        return True
    if type(el).__name__ == "Text":
        return (len(txt) <= 60 and len(txt.split()) <= 8
                and not txt.endswith((".", "。", ":", "：", ";", "；", ",", "，")))
    return False


def _split_text_elements_into_sections(text_elements: list) -> list[list]:
    """把文字 elements 依 section header 切成多段（section）。每遇到一個 header 且目前累積
    內容已達 SECTION_SOFT_MIN_CHARS 就開新 section（硬邊界）；否則繼續累積（小節往後併，
    避免孤兒 chunk）。回傳 list[list[element]]。"""
    sections: list[list] = []
    current: list = []
    cur_len = 0
    for el in text_elements:
        txt = (el.text or "").strip()
        if _is_section_header(el) and cur_len >= SECTION_SOFT_MIN_CHARS:
            sections.append(current)
            current, cur_len = [el], len(txt)
        else:
            current.append(el)
            cur_len += len(txt)
    if current:
        sections.append(current)
    return sections


def build_chunk_records(elements: list, semantic_chunker) -> list[dict]:
    """回傳 [{"text": ..., "chunk_type": "table" | "text"}, ...]

    Table 的 caption 取得策略（依序嘗試）：
      1. 向前回溯最多 TABLE_CAPTION_LOOKBACK 個 element，取最近的有意義前文。
      2. 若找不到（回傳 ""）且表格列數 >= TABLE_SUMMARY_MIN_ROWS，呼叫 LLM 生成一行摘要
         （只描述「這是什麼表」，不重複數字），**並把表格前方原文一起餵進去**
         （`_table_context`，2026-08-11 加 —— 只餵表格等於輸入裡沒有答案）。
      3. 兩者都失敗（太小的表格或 LLM call 失敗），直接用純 Markdown，不加 caption。

    閾值設計理由：
    - 列數 < TABLE_SUMMARY_MIN_ROWS 的表格通常是封面 checkbox / IRS 編號之類的行政
      雜訊，對任何 RAG query 都沒有貢獻，不值得花 LLM call。
    - 列數 >= 閾值 且找不到 caption 才是「真正的財務表格但結構上沒有前文」的情況，
      這裡 LLM 才能發揮作用。
    """
    from unstructured.documents.elements import Table

    records: list[dict] = []
    text_elements: list = []

    for i, el in enumerate(elements):
        if isinstance(el, Table):
            body = table_element_to_text(el).strip()
            if not body:
                continue
            caption = _find_caption(elements, i)
            if not caption and _count_table_rows(body) >= TABLE_SUMMARY_MIN_ROWS:
                caption = _llm_summarize_table(body, context=_table_context(elements, i))
            text = f"{caption}\n\n{body}" if caption else body
            records.append({"text": text, "chunk_type": "table"})
        else:
            txt = (el.text or "").strip()
            if txt:
                text_elements.append(el)

    # 依 section header 切段後，每段各自跑 SemanticChunker。等於在 SemanticChunker 之上加
    # 「小節硬邊界」：相鄰但主題不同的財務小節（Operating Income / Interest Income / Income
    # Taxes …）不再因語意相近被併成稀釋型大 chunk（見 sem-08：AWS 獲利句被埋在 5 個主題的
    # 5170 字大 chunk 裡，生成端選擇性忽略）。段內若仍過長，SemanticChunker 照常語意再切。
    for section in _split_text_elements_into_sections(text_elements):
        sec_blob = "\n\n".join(
            el.text.strip() for el in section if el.text and el.text.strip()
        )
        if not sec_blob.strip():
            continue
        for doc in semantic_chunker.create_documents([sec_blob]):
            content = doc.page_content.strip()
            if len(content) > 20:
                records.append({"text": content, "chunk_type": "text"})

    return records


# ══════════════════════════════════════════════════════════════════════════════
# BGE-M3 adapter for SemanticChunker (LangChain Embeddings interface)
# ══════════════════════════════════════════════════════════════════════════════

class BGEM3DenseEmbeddings:
    def __init__(self, model):
        self.model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = self.model.encode(
            texts, batch_size=8,
            return_dense=True, return_sparse=False, return_colbert_vecs=False,
        )
        return [vec.tolist() for vec in out["dense_vecs"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
