"""
data_update_edgar.py — Exp 1: edgartools 資料抽取來源替換（chunking coordinates.md 實驗計畫）

Exp 1 範圍（刻意窄）：只換「filing 內容從哪裡抽取」，不動 chunking 演算法本身，也不動檢索：
  - 散文：改用 edgartools 的 SEC Item 邊界（Item 1A / Item 7 / Part I,Item 2 ...）取代舊有
    unstructured 的啟發式 section-header 偵測（_is_section_header）。Item 邊界只負責「限制
    章節範圍」（避免不同 Item 被語意相近而併成同一 chunk），Item 內文字仍照舊交給
    SemanticChunker 做語意切割，threshold 與現行生產一致（percentile/90），**尚不加大小
    上限**——Item 1A/7 過大導致 rerank 截斷的問題留到 Exp 2（純量測）與 Exp 3（RCTS fallback）
    處理，Exp 1 只驗證「資料內容/數字/章節結構是否正確」。
  - 財報三表：改用 edgartools Statement.to_markdown()（income_statement / balance_sheet /
    cash_flow_statement），每表整份保留為一個原子 chunk，不切、不摘要——validated 手動比對
    NVDA 10-K 數字與 SEC 原文一致（Revenue $215,938M FY2026 等）。這解決「複雜 HTML 財報表格
    被 unstructured 誤判成 text」的問題（三表不再經過 unstructured 表格偵測）。
  - News / Fundamentals / IncomeStatement 的 .txt 檔案不是 SEC filing，edgartools 不適用，
    沿用 data_update_unstructure.py 現有的 partition_and_clean + build_chunk_records，
    確保新 collection 對這些 doc_type 的內容與舊 collection 完全一致（公平比較的前提）。

──────────────────────────────────────────────────────────────────────────────
2026-07-23 修正（Exp2/Exp3 RAGAS 退步根因分析後的三個修正，見對話紀錄）：
  1. 表格混合抽取 + 去重：財報三表以外的表格（分部營收、股東權益變動表、稅率調節、
     負債到期表…）過去只走 edgartools 的 Item 文字接口，表格結構會被壓平成純文字
     （實測舊 unstructured collection 有 744 個 table chunk，edgar-only 只剩 63 個）。
     現在額外對整份 filing 原始 HTML 跑 unstructured 的表格偵測（_extract_extra_tables），
     取回財報附註內的表格，並用「大數字集合重疊度」與三大表去重（避免 MD&A 段落重複
     貼一份損益表被算兩次）。
  2. Item 邊界誤判修正：edgartools 對單一份文件（實測：NVDA fiscal-2026 10-K）的
     item 邊界偵測會出錯，把本該屬於 Item 8（財報附註）的內容錯置到 Item 14/15
     （NVDA Item 15 實測 105,394 字元，其他 6 家同期 10-K 僅 7,005~14,834）。
     ITEM_10K_ADMIN_CEILING 用 7 家公司實測值校準門檻，超過門檻視為邊界誤判溢出，
     併回 Item 8 而非當獨立 item 保留錯置內容。
  3. 新聞改回單純 SemanticChunker + RCTS fallback（_build_news_records），拿掉
     unstructured 的 Table 偵測與 2026-07-14 才加入的 section 硬邊界
     （_split_text_elements_into_sections，是為財報 MD&A 稀釋型大 chunk 設計的，
     新聞是短文章不需要，套用後同一份文字反而碎成 2.5~3 倍 chunk）。

寫入獨立的新 collection（預設 us_stock_rag_edgar_exp1），**不覆蓋**生產 collection
us_stock_rag_unstructured，方便兩者在 Qdrant 裡並存、直接跑 eval 對照。

Payload 新增欄位（供 Exp 6 Neighbor Expansion 不必重新 ingest）：
  - accession_number：filing 的 SEC accession number，唯一定位來源文件
  - item_id：sanitized 的 Item 識別碼（例如 "Item_1A"、"Part_I_Item_2"），財報三表固定用
    "income_statement"/"balance_sheet"/"cash_flow_statement"
  - item_chunk_index：同一個 item_id 內的序號（0-based），供之後查詢「同 Item 前/後一個
    chunk」；不額外存 previous_chunk_id/next_chunk_id——用 source+item_id+item_chunk_index±1
    做 payload filter 就能查到鄰居，不需要预先算好存死的指標（Exp 6 真的需要時再視效能決定
    要不要加）。

檔名慣例（**必須**維持現有 eval_set.json 的 ground truth glob 能吃得下）：
  10-K → f"{ticker}_10K_{fiscal_year}.html"        例：NVDA_10K_2026.html
  10-Q → f"{ticker}_10Q_{yyyymm}.html"              例：AAPL_10Q_202603.html
  年份/期碼一律從 edgartools 的 XBRL entity_info（document_period_end_date /
  fiscal_year）算出，不是憑空編造——已用 AAPL/NVDA 驗證與現有 data/raw/*.html 檔名一致。

Usage:
  .venv/Scripts/python.exe data_update_edgar.py                 # 抓 7 家公司最新 10-K + 最近 2 份 10-Q
  .venv/Scripts/python.exe data_update_edgar.py --rebuild        # 全量重建新 collection
  .venv/Scripts/python.exe data_update_edgar.py --tickers NVDA   # 只跑單一公司（開發/驗證用）
"""

import argparse
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── 沿用 data_update_unstructure.py 現有邏輯（DRY，避免兩份 chunking/embedding 邏輯漂移）──
from data_update_unstructure import (
    BGEM3DenseEmbeddings,
    build_chunk_records,
    build_metadata_header,
    deterministic_uuid,
    partition_and_clean,
    table_element_to_text,
    _find_caption,
    _count_table_rows,
    _llm_summarize_table,
    TABLE_SUMMARY_MIN_ROWS,
)
from rag_query import infer_source_type, make_qdrant_client

# ── Config ───────────────────────────────────────────────────────────────────────
RAW_DIR         = Path("data/raw")            # News/Fundamentals/IncomeStatement .txt 仍從這裡讀
PROCESSED_DIR   = Path("data/edgar_processed")  # 人眼驗證用：Item 結構 + 財報三表 markdown 傾印
COLLECTION_NAME = os.getenv("EDGAR_COLLECTION_NAME", "us_stock_rag_edgar_exp1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"
DENSE_DIM       = 1024

TICKERS = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"]
N_RECENT_10Q = 2   # 與現有 data/raw/*.html 覆蓋範圍對齊（每家公司 1 份 10-K + 最近 2 份 10-Q）

SEC_FORM_10K = "10-K"
SEC_FORM_10Q = "10-Q"

STATEMENT_ATTRS = ["income_statement", "balance_sheet", "cash_flow_statement"]

# Exp 3：Semantic Chunking 產出的 chunk 若 reranker token 數超過此門檻，改用 RCTS
# （RecursiveCharacterTextSplitter）補切。800/80 為使用者定案的候選方案參數；門檻用
# 1200（而非 2048 的實際截斷點）留出安全餘裕——1200 是「該不該補切」的判斷線，
# 2048 是「補切失敗會被截斷」的真正代價，兩者刻意不是同一個數字。
RCTS_THRESHOLD       = 1200
RCTS_CHUNK_SIZE      = 800
RCTS_CHUNK_OVERLAP   = 80

# SEC Form 10-K / 10-Q 官方表格結構的合法 Item 清單。用途：edgartools 的 obj.items 偶爾會
# 吐出不屬於表格結構的偽 Item（實測 6/7 家公司的 10-Q 都會多出一個 "Part I, Item 8"——但
# 10-Q 表格 Part I 官方只到 Item 4，這個「Item 8」內容跟 Part I Item 1/2 大幅重疊卻不完全
# 相同，是 edgartools 對這份文件邊界偵測的雜訊區塊，不是真的獨立章節）。白名單過濾比「內容
# 完全相同才去重」更可靠，因為這類偽 Item 常是部分重疊、非逐字相同，內容比對抓不乾淨。
VALID_10K_ITEMS = {
    "Item 1", "Item 1A", "Item 1B", "Item 1C", "Item 2", "Item 3", "Item 4", "Item 5",
    "Item 6", "Item 7", "Item 7A", "Item 8", "Item 9", "Item 9A", "Item 9B", "Item 9C",
    "Item 10", "Item 11", "Item 12", "Item 13", "Item 14", "Item 15", "Item 16",
}
VALID_10Q_ITEMS = {
    "Part I, Item 1", "Part I, Item 2", "Part I, Item 3", "Part I, Item 4",
    "Part II, Item 1", "Part II, Item 1A", "Part II, Item 2", "Part II, Item 3",
    "Part II, Item 4", "Part II, Item 5", "Part II, Item 6",
}

# Item 8 之後的行政性/引用性 item（Part III/IV：審計費用、董事資訊、Exhibit Index…），
# SEC 慣例內容都很短。若 edgartools 對某份文件的 item 邊界誤判，Item 8（財報附註）的
# 內容可能被錯誤含括進後面某個 admin item——門檻用 7 家公司 2026 財年 10-K 實測值校準
# （NVDA 是唯一離群值：Item 14 實測 18,137 字元 vs 其他 6 家 187~416；Item 15 實測
# 105,394 字元 vs 其他 6 家 7,005~14,834），超過門檻視為邊界誤判造成的內容溢出。
ITEM_10K_ADMIN_CEILING = {
    "Item 9": 3000, "Item 9A": 15000, "Item 9B": 5000, "Item 9C": 3000,
    "Item 10": 5000, "Item 11": 3000, "Item 12": 3000, "Item 13": 3000,
    "Item 14": 3000, "Item 15": 25000, "Item 16": 8000,
}

# 表格去重：unstructured 從整份 filing HTML 抓到的表格，若其中出現的大數字與
# edgartools 三大表（income/balance/cash_flow）高度重疊，視為同一張表的重複來源
# （MD&A 段落常見直接複製一份損益表小節），只保留 edgartools 版本。
TABLE_DEDUP_NUMBER_OVERLAP = 0.5
_NUMBER_RE = re.compile(r"\d[\d,]{3,}")


def _number_set(text: str) -> set:
    return set(_NUMBER_RE.findall(text))


# ══════════════════════════════════════════════════════════════════════════════
# edgartools helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sanitize_item_id(item_name: str) -> str:
    """'Item 1A' -> 'Item_1A'；'Part I, Item 2' -> 'Part_I_Item_2'。"""
    s = item_name.replace(",", "").replace(".", "")
    return re.sub(r"\s+", "_", s.strip())


def _filing_meta_from_xbrl(entity_info: dict, ticker: str, accession_no: str) -> dict:
    """對映到與 data_update_unstructure.extract_filing_metadata() 相同的欄位語意：
    filing_type / fiscal_year / fiscal_period / report_label_year / report_period_code，
    但資料來源是 edgartools 的 XBRL entity_info（比舊版手動解析 ix:nonnumeric 更可靠），
    另外疊加 accession_number。"""
    document_type = entity_info.get("document_type", "")
    period_end     = entity_info.get("document_period_end_date", "")  # "YYYY-MM-DD"
    fiscal_year    = str(entity_info.get("fiscal_year", ""))
    fiscal_period  = entity_info.get("fiscal_period", "")

    is_10k = document_type.upper() == SEC_FORM_10K
    if is_10k:
        # 10-K 檔名慣例只用年份（e.g. NVDA_10K_2026.html），期碼＝年份
        report_label_year  = fiscal_year or period_end[:4]
        report_period_code = report_label_year
    else:
        # 10-Q 檔名慣例用 yyyymm（e.g. AAPL_10Q_202603.html）
        yyyymm = period_end.replace("-", "")[:6]
        report_period_code = yyyymm
        report_label_year  = yyyymm[:4]

    return {
        "filing_type":        SEC_FORM_10K if is_10k else SEC_FORM_10Q,
        "fiscal_year":        fiscal_year,
        "fiscal_period":      fiscal_period,
        "report_label_year":  report_label_year,
        "report_period_code": report_period_code,
        "ticker":             ticker,
        "accession_number":   accession_no,
    }


def _build_filename(filing_meta: dict, ticker: str) -> str:
    """對齊現有 data/raw/*.html 命名慣例，讓 eval_set.json 的 glob ground truth 吃得下。"""
    if filing_meta["filing_type"] == SEC_FORM_10K:
        return f"{ticker}_10K_{filing_meta['report_label_year']}.html"
    return f"{ticker}_10Q_{filing_meta['report_period_code']}.html"


def _extract_extra_tables(filing, core_stmt_mds: list[str]) -> list[dict]:
    """混合 unstructured 的表格偵測：從整份 filing 原始 HTML 抓出 edgartools 三大表
    以外的表格（分部營收、股東權益變動表、稅率調節、負債到期表…），沿用舊
    unstructured 管線抓表格的邏輯（table_element_to_text + caption 策略，與
    data_update_unstructure.build_chunk_records 對 Table element 的處理完全一致），
    但排除與 income_statement/balance_sheet/cash_flow_statement 數字高度重疊的
    重複表格（MD&A 常見直接複製一份損益表小節）。

    回傳 [{"text", "chunk_type": "table", "item_id": "notes_table",
           "item_chunk_index": i}, ...]（依 filing 內出現順序編號）。
    """
    from unstructured.partition.html import partition_html
    from unstructured.cleaners.core import clean_extra_whitespace
    from unstructured.documents.elements import Table

    try:
        html = filing.html()
    except Exception as e:
        print(f"  [WARN] filing.html() failed: {e!r}")
        return []
    if not html:
        return []

    try:
        elements = partition_html(text=html)
    except Exception as e:
        print(f"  [WARN] partition_html failed for extra-table extraction: {e!r}")
        return []
    for el in elements:
        el.apply(clean_extra_whitespace)

    core_numsets = [_number_set(md) for md in core_stmt_mds]

    records: list[dict] = []
    for i, el in enumerate(elements):
        if not isinstance(el, Table):
            continue
        body = table_element_to_text(el).strip()
        if not body:
            continue

        ns = _number_set(body)
        is_dup = any(
            ns and cs and len(ns & cs) / max(1, min(len(ns), len(cs))) >= TABLE_DEDUP_NUMBER_OVERLAP
            for cs in core_numsets
        )
        if is_dup:
            continue

        caption = _find_caption(elements, i)
        if not caption and _count_table_rows(body) >= TABLE_SUMMARY_MIN_ROWS:
            caption = _llm_summarize_table(body)
        text = f"{caption}\n\n{body}" if caption else body
        records.append({"text": text, "chunk_type": "table",
                         "item_id": "notes_table", "item_chunk_index": len(records)})

    return records


def _build_news_records(text: str, semantic_chunker, rcts_splitter, token_len_fn,
                         rcts_threshold: int = RCTS_THRESHOLD) -> list[dict]:
    """新聞單純用 SemanticChunker + RCTS fallback，不跑 unstructured 的 Table 偵測、
    也不套用 2026-07-14 才加進 build_chunk_records 的 section 硬邊界
    （_split_text_elements_into_sections，是為財報 MD&A 稀釋型大 chunk 設計的機制）。
    新聞是純文字短文章，沒有表格，套用 section 硬邊界只會把同一篇文章切得更碎
    （實測：同一批新聞檔案套用後 chunk 數從 87 暴增到 286，平均長度 1095→350 字元）。

    行為與財報散文（Item 邊界）chunking 策略一致：SemanticChunker 切完，chunk 若
    reranker token 數超過 rcts_threshold 才用 RCTS 補切，全站維持同一套「何時該補切」
    的判斷邏輯。
    """
    final_texts: list[str] = []
    for doc in semantic_chunker.create_documents([text]):
        content = doc.page_content.strip()
        if len(content) <= 20:
            continue
        if token_len_fn(content) > rcts_threshold:
            sub_texts = [t.strip() for t in rcts_splitter.split_text(content) if t.strip()]
            final_texts.extend(sub_texts)
        else:
            final_texts.append(content)
    return [{"text": t, "chunk_type": "text"} for t in final_texts]


def _fetch_filing_records(ticker: str, form: str, filing, semantic_chunker,
                          rcts_splitter=None, token_len_fn=None,
                          rcts_threshold: int = RCTS_THRESHOLD) -> tuple[dict, list[dict], str]:
    """回傳 (filing_meta, chunk_records, validation_dump_text)。
    chunk_records: [{"text", "chunk_type", "item_id", "item_chunk_index"}, ...]

    rcts_splitter/token_len_fn 皆非 None 時啟用 Exp 3 的 RCTS fallback：SemanticChunker
    切出的每個 chunk，若 reranker token 數超過 rcts_threshold，改用 RCTS（800/80，
    length_function=reranker tokenizer）再切一次，取代原本那個過大的 chunk。財報三表
    （原子 chunk，不切）不受影響——三表本身已驗證過不會超標（Exp 2：max 1290 token）。
    """
    xb = filing.xbrl()
    entity_info = xb.entity_info
    filing_meta = _filing_meta_from_xbrl(entity_info, ticker, filing.accession_no)

    obj = filing.obj()
    records: list[dict] = []
    dump_lines = [
        f"=== {ticker} {form} | accession={filing.accession_no} | "
        f"fiscal_year={filing_meta['fiscal_year']} fiscal_period={filing_meta['fiscal_period']} "
        f"period_code={filing_meta['report_period_code']} ===\n"
    ]

    # ── 財報三表：原子 chunk，不切、不摘要 ──────────────────────────────────
    for attr in STATEMENT_ATTRS:
        stmt = getattr(obj, attr, None)
        if stmt is None:
            continue
        try:
            md = stmt.to_markdown()
        except Exception as e:
            dump_lines.append(f"[WARN] {attr}.to_markdown() failed: {e!r}\n")
            continue
        if not md or not md.strip():
            continue
        records.append({"text": md.strip(), "chunk_type": "table",
                         "item_id": attr, "item_chunk_index": 0})
        dump_lines.append(f"--- [TABLE] {attr} ---\n{md}\n")

    # ── 財報附註等三大表以外的表格：混合 unstructured 表格偵測，與三大表去重 ──
    core_stmt_mds = [r["text"] for r in records if r["chunk_type"] == "table"]
    extra_tables = _extract_extra_tables(filing, core_stmt_mds)
    records.extend(extra_tables)
    for r in extra_tables:
        dump_lines.append(f"--- [TABLE extra #{r['item_chunk_index']}] ---\n{r['text'][:400]}...\n")
    dump_lines.append(f"[INFO] extra tables kept={len(extra_tables)} (去重後，來自 unstructured)\n")

    # ── 散文：Item 邊界 → SemanticChunker（沿用現行生產 threshold，無 size cap）──
    # dict.fromkeys 去重但保留順序：edgartools 的 obj.items 偶爾會重複列同一個 Item
    # （實測 META 10-K 2025 的 'Item 8' 出現兩次），不去重會讓該 Item 的內容被完整
    # 複製成兩組一模一樣的 chunk，佔用候選池與儲存空間卻沒有新資訊。
    #
    # 先收集全部 item_name -> item_text（保留 filing 原始順序），再做 admin-item
    # 邊界誤判修正，最後才統一切 chunk——因為修正動作（把誤判內容併回 Item 8）要在
    # chunking 之前完成，否則錯置內容已經被切成獨立 chunk 就來不及了。
    valid_items = VALID_10K_ITEMS if form == SEC_FORM_10K else VALID_10Q_ITEMS
    item_texts: dict[str, str] = {}
    for item_name in dict.fromkeys(obj.items):
        if item_name not in valid_items:
            # 實測 6/7 家公司的 10-Q 都會多出一個不屬於表格結構的 "Part I, Item 8"，
            # 內容跟 Part I Item 1/2 大幅重疊（非逐字相同，內容比對去重抓不到）——
            # 用白名單直接排除，不進 records。
            dump_lines.append(f"[SKIP] '{item_name}' 不屬於 {form} 官方表格結構，略過\n")
            continue
        try:
            item_text = obj[item_name]
        except Exception as e:
            dump_lines.append(f"[WARN] item '{item_name}' extraction failed: {e!r}\n")
            continue
        if not item_text or len(item_text.strip()) < 20:
            continue
        item_texts[item_name] = item_text.strip()

    if form == SEC_FORM_10K and "Item 8" in item_texts:
        for admin_item, ceiling in ITEM_10K_ADMIN_CEILING.items():
            text = item_texts.get(admin_item)
            if text is not None and len(text) > ceiling:
                dump_lines.append(
                    f"[MERGE] '{admin_item}' 內容 {len(text)} 字元 > 門檻 {ceiling}，"
                    f"判定為 edgartools 邊界誤判（財報附註溢出），併回 Item 8\n")
                item_texts["Item 8"] = item_texts["Item 8"] + "\n\n" + text
                del item_texts[admin_item]

    for item_name, item_text in item_texts.items():
        item_id = _sanitize_item_id(item_name)
        dump_lines.append(f"--- [ITEM] {item_name} (len={len(item_text)} chars) ---\n"
                           f"{item_text[:400]}...\n")
        # 先收集這個 Item 最終要落地的所有文字片段（可能因 RCTS fallback 而比
        # SemanticChunker 原始輸出更多），最後統一 enumerate，讓 item_chunk_index
        # 維持連續（Neighbor Expansion 依賴這個序號查前後 chunk）。
        final_texts: list[str] = []
        for doc in semantic_chunker.create_documents([item_text]):
            content = doc.page_content.strip()
            if len(content) <= 20:
                continue
            if rcts_splitter is not None and token_len_fn is not None \
                    and token_len_fn(content) > rcts_threshold:
                sub_texts = [t.strip() for t in rcts_splitter.split_text(content) if t.strip()]
                dump_lines.append(f"[RCTS] item={item_id} 原 chunk {token_len_fn(content)} "
                                   f"token > {rcts_threshold} → 補切成 {len(sub_texts)} 份\n")
                final_texts.extend(sub_texts)
            else:
                final_texts.append(content)

        for idx, content in enumerate(final_texts):
            records.append({"text": content, "chunk_type": "text",
                             "item_id": item_id, "item_chunk_index": idx})

    return filing_meta, records, "\n".join(dump_lines)


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant collection setup（與 data_update_unstructure.py 相同 schema，獨立 collection 名）
# ══════════════════════════════════════════════════════════════════════════════

def ensure_collection(client, collection_name: str, recreate: bool = False) -> None:
    from qdrant_client import models

    exists = client.collection_exists(collection_name)
    if exists and recreate:
        client.delete_collection(collection_name)
        print(f"[INFO] Deleted collection '{collection_name}'")
        exists = False

    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(index=models.SparseIndexParams(on_disk=False)),
            },
        )
        client.create_payload_index(collection_name=collection_name, field_name="source",
                                     field_schema=models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(collection_name=collection_name, field_name="doc_type",
                                     field_schema=models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(collection_name=collection_name, field_name="item_id",
                                     field_schema=models.PayloadSchemaType.KEYWORD)
        # period_basis 索引：TTM 口徑題對 period_basis 做硬 filter（rag_query._detect_period_basis）。
        client.create_payload_index(collection_name=collection_name, field_name="period_basis",
                                     field_schema=models.PayloadSchemaType.KEYWORD)
        print(f"[INFO] Created collection '{collection_name}' (dense={DENSE_DIM}D cosine, sparse=BGE-M3 lexical)")


def _period_basis_for(doc_type: str) -> Optional[str]:
    """依 doc_type 決定數字口徑（period_basis）：
      - fundamentals            → "TTM"（yfinance-style 滾動十二個月快照：Revenue(TTM)/margins/YoY）
      - 10-K / 10-Q / income_statement → "fiscal_year"（會計期間結算數字）
      - news / other            → None（質性內容，無單一數字口徑，不標）
    供 rag_query 的 period_basis 硬 filter 把「最近十二個月(TTM)」題確定性導向 Fundamentals，
    避免抓到年度(fiscal_year)結算數字（口徑歧義）。doc_type 來自 infer_source_type，單一真相來源。"""
    dt = (doc_type or "").lower()
    if dt == "fundamentals":
        return "TTM"
    if dt in ("10-k", "10-q", "income_statement"):
        return "fiscal_year"
    return None


def upsert_records(client, collection_name: str, bge_m3, source: str, filing_meta: dict,
                    records: list[dict], doc_type: str) -> int:
    """把 records 編碼成 dense+sparse 並 upsert，回傳 chunk 數。與
    data_update_unstructure.py 的 upsert 邏輯一致（前綴 metadata header、
    deterministic UUID），額外疊加 item_id / item_chunk_index / accession_number。"""
    from qdrant_client import models

    if not records:
        return 0

    meta_header = build_metadata_header(filing_meta)
    chunk_texts = [meta_header + r["text"] for r in records]
    encoded = bge_m3.encode(chunk_texts, batch_size=8,
                            return_dense=True, return_sparse=True, return_colbert_vecs=False)
    dense_vecs  = encoded["dense_vecs"]
    lex_weights = encoded["lexical_weights"]

    points = []
    for i, (record, dense_vec, weights) in enumerate(zip(records, dense_vecs, lex_weights)):
        chunk_id_str = f"{Path(source).stem}_c{i}"
        indices = [int(tok) for tok in weights.keys()]
        values  = [float(w) for w in weights.values()]

        payload = {
            "chunk_id":         chunk_id_str,
            "source":           source,
            "stem":             Path(source).stem,
            "chunk_index":      i,
            "chunk_type":       record["chunk_type"],
            "doc_type":         doc_type,
            "document":         meta_header + record["text"],
            "item_id":          record.get("item_id", "n/a"),
            "item_chunk_index": record.get("item_chunk_index", 0),
        }
        _basis = _period_basis_for(doc_type)
        if _basis:
            payload["period_basis"] = _basis   # 供 rag_query TTM 硬 filter 路由（見 _period_basis_for）
        payload.update(filing_meta)

        points.append(models.PointStruct(
            id=deterministic_uuid(chunk_id_str),
            vector={
                DENSE_VECTOR_NAME:  dense_vec.tolist(),
                SPARSE_VECTOR_NAME: models.SparseVector(indices=indices, values=values),
            },
            payload=payload,
        ))

    client.upsert(collection_name=collection_name, points=points)
    return len(points)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="刪除並重建新 collection")
    parser.add_argument("--tickers", nargs="+", default=TICKERS)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--skip-txt", action="store_true",
                        help="跳過 News/Fundamentals/IncomeStatement .txt（開發時只驗證 filing 部分用）")
    parser.add_argument("--rcts-fallback", action="store_true",
                        help="Exp 3：SemanticChunker 輸出的 chunk 若 reranker token 數超過 "
                             f"{RCTS_THRESHOLD} 時，改用 RCTS（{RCTS_CHUNK_SIZE}/{RCTS_CHUNK_OVERLAP}，"
                             "length_function=reranker tokenizer）補切。不加此旗標則行為與 Exp 1/2 "
                             "完全一致（純 SemanticChunker，無 size cap）。")
    args = parser.parse_args()

    import edgar
    from FlagEmbedding import BGEM3FlagModel
    from langchain_experimental.text_splitter import SemanticChunker

    sec_identity = os.getenv("SEC_IDENTITY")
    if not sec_identity:
        raise RuntimeError("SEC_IDENTITY 未設定（.env），edgartools 需要合法聯絡資訊才能呼叫 SEC EDGAR。")
    edgar.set_identity(sec_identity)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Embedding model : {EMBEDDING_MODEL}")
    print(f"[INFO] Collection      : {args.collection}  (新 collection，不影響生產 us_stock_rag_unstructured)")
    print("[INFO] Loading BGE-M3 (dense+sparse)...")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
    embed_adapter = BGEM3DenseEmbeddings(bge_m3)
    semantic_chunker = SemanticChunker(embed_adapter, breakpoint_threshold_type="percentile",
                                       breakpoint_threshold_amount=90)

    # RCTS 元件一律建構（新聞 _build_news_records 無條件需要它），但是否套用到
    # 財報散文（Item 邊界）仍由 --rcts-fallback 旗標控制，維持 Exp 1/2/3 的實驗
    # 可比較性——不加旗標：財報散文純 SemanticChunker（Exp1/2 行為不變），
    # 新聞一律 SemanticChunker+RCTS（本次修正的新預設行為，見 _build_news_records）。
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from transformers import AutoTokenizer

    # 用 reranker 自己的 tokenizer 當「800 tokens」的尺——確保 RCTS 切出來的
    # chunk 真的落在 rag_query.RERANK_MAX_LENGTH(2048) 之內不被截斷，而不是用
    # 字元數或另一個 tokenizer 的「800」，兩者換算不到位（見架構決策：
    # RCTS 的 length_function 必須用 reranker tokenizer）。
    rerank_model_name = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    _tok = AutoTokenizer.from_pretrained(rerank_model_name)

    def token_len_fn(text: str) -> int:
        return len(_tok(text, add_special_tokens=False)["input_ids"])

    rcts_splitter = RecursiveCharacterTextSplitter(
        chunk_size=RCTS_CHUNK_SIZE, chunk_overlap=RCTS_CHUNK_OVERLAP,
        length_function=token_len_fn,
    )
    print(f"[INFO] RCTS fallback (新聞，一律套用) : threshold={RCTS_THRESHOLD}, "
          f"chunk_size={RCTS_CHUNK_SIZE}, overlap={RCTS_CHUNK_OVERLAP}, "
          f"length_function={rerank_model_name}")

    prose_rcts_splitter = rcts_splitter if args.rcts_fallback else None
    prose_token_len_fn  = token_len_fn if args.rcts_fallback else None
    print(f"[INFO] RCTS fallback (財報散文/Item) : {'ON' if args.rcts_fallback else 'OFF（純 SemanticChunker）'}")

    client = make_qdrant_client()
    ensure_collection(client, args.collection, recreate=args.rebuild)

    total_chunks = 0
    total_filings = 0

    # ── 10-K + 10-Q（edgartools）──────────────────────────────────────────────
    for ticker in args.tickers:
        print(f"\n[TICKER] {ticker}")
        company = edgar.Company(ticker)

        # amendments=False：排除 10-K/A、10-Q/A 修正案。TSLA 曾在 2026-04-30 提交過一份
        # 10-K/A，若不排除，get_filings(form="10-K") 會把它當成「最新一份 10-K」選中——
        # 修正案常缺完整 XBRL（income/balance/cash flow 三表全部抽取失敗），且
        # entity_info["document_type"] 回傳 "10-K/A" 不等於 "10-K"，會讓
        # _filing_meta_from_xbrl() 的 is_10k 判斷失準，誤標成 10-Q 檔名。
        jobs = [(SEC_FORM_10K, company.get_filings(form=SEC_FORM_10K, amendments=False).head(1))]
        q_filings = company.get_filings(form=SEC_FORM_10Q, amendments=False).head(N_RECENT_10Q)
        for i in range(len(q_filings)):
            jobs.append((SEC_FORM_10Q, q_filings[i:i+1]))

        for form, filing_slice in jobs:
            if len(filing_slice) == 0:
                print(f"  [WARN] No {form} filing found for {ticker}")
                continue
            filing = filing_slice[0]
            print(f"  [FETCH] {form} | accession={filing.accession_no} | filed={filing.filing_date}")

            filing_meta, records, dump_text = _fetch_filing_records(
                ticker, form, filing, semantic_chunker,
                rcts_splitter=prose_rcts_splitter, token_len_fn=prose_token_len_fn)
            source = _build_filename(filing_meta, ticker)
            doc_type = infer_source_type(source)

            n_table = sum(1 for r in records if r["chunk_type"] == "table")
            n_text  = sum(1 for r in records if r["chunk_type"] == "text")
            print(f"    -> source={source} | {len(records)} chunks ({n_table} table + {n_text} text)")

            (PROCESSED_DIR / (Path(source).stem + ".txt")).write_text(dump_text, encoding="utf-8")

            if not args.rebuild:
                from qdrant_client import models
                client.delete(collection_name=args.collection, points_selector=models.FilterSelector(
                    filter=models.Filter(must=[models.FieldCondition(
                        key="source", match=models.MatchValue(value=source))])))

            n = upsert_records(client, args.collection, bge_m3, source, filing_meta, records, doc_type)
            total_chunks += n
            total_filings += 1

    # ── News / Fundamentals / IncomeStatement .txt ──────────────────────────────
    # News：單純 SemanticChunker + RCTS fallback（_build_news_records，本次修正）。
    # Fundamentals / IncomeStatement：沿用舊 partition_and_clean + build_chunk_records
    # （這兩種是 key:value 純文字，本來就沒有 HTML 表格可偵測，維持原行為）。
    if not args.skip_txt:
        print("\n[INFO] Ingesting News/Fundamentals/IncomeStatement .txt ...")
        txt_files = sorted(
            f for f in RAW_DIR.iterdir()
            if f.suffix.lower() in {".txt", ".md"}
            and any(f.stem.startswith(t + "_") for t in args.tickers)
        )
        # 去重守門（Item 2，2026-07-30）：Fundamentals / IncomeStatement 只保留每個 ticker 最新一份
        # 快照（如 0508/0519/0612 → 只留 0612），避免舊快照與最新版在檢索時互相競爭、放大版本漂移。
        # News 不去重——多篇不同日期新聞是正當時間序列。
        def _txt_stamp(p) -> str:
            m = re.search(r"_(\d{8})(?=[_.]|$)", p.stem)
            return m.group(1) if m else ""
        _latest_stamp: dict[tuple[str, str], str] = {}
        for f in txt_files:
            dt = infer_source_type(f.name)
            if dt not in ("fundamentals", "income_statement"):
                continue
            mt = re.match(r"^([A-Z]+)_", f.stem)
            key = (mt.group(1) if mt else "", dt)
            st = _txt_stamp(f)
            if st and (key not in _latest_stamp or st > _latest_stamp[key]):
                _latest_stamp[key] = st
        for filepath in txt_files:
            doc_type = infer_source_type(filepath.name)

            # keep-latest：非最新快照的 Fundamentals/IncomeStatement 直接跳過（不 ingest）。
            if doc_type in ("fundamentals", "income_statement"):
                mt = re.match(r"^([A-Z]+)_", filepath.stem)
                key = (mt.group(1) if mt else "", doc_type)
                st = _txt_stamp(filepath)
                if st and st != _latest_stamp.get(key):
                    print(f"  [SKIP stale] {filepath.name} (keep {_latest_stamp.get(key)})")
                    continue

            if doc_type == "news":
                text = filepath.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    print(f"  [SKIP] {filepath.name}: empty")
                    continue
                chunk_records = _build_news_records(text, semantic_chunker, rcts_splitter, token_len_fn)
            else:
                elements = partition_and_clean(filepath)
                if not elements:
                    print(f"  [SKIP] {filepath.name}: partition failed")
                    continue
                chunk_records = build_chunk_records(elements, semantic_chunker)

            # 這些檔案沒有 filing_type/fiscal_year/Item 結構；item_id/item_chunk_index
            # 沿用全域 chunk_index 當替代（沒有 SEC Item 語意可對映，Neighbor Expansion
            # 本來就只針對 filing 生效）。
            for i, r in enumerate(chunk_records):
                r["item_id"] = "n/a"
                r["item_chunk_index"] = i

            m_ticker = re.match(r"^([A-Z]+)_", filepath.stem)
            filing_meta = {"ticker": m_ticker.group(1) if m_ticker else ""}

            if not args.rebuild:
                from qdrant_client import models
                client.delete(collection_name=args.collection, points_selector=models.FilterSelector(
                    filter=models.Filter(must=[models.FieldCondition(
                        key="source", match=models.MatchValue(value=filepath.name))])))

            n = upsert_records(client, args.collection, bge_m3, filepath.name, filing_meta,
                              chunk_records, doc_type)
            total_chunks += n
            print(f"  [{filepath.name}] {n} chunks")

    total_in_db = client.count(collection_name=args.collection, exact=True).count
    print(f"\n{'─'*60}")
    print(f"[DONE] Filings processed : {total_filings}")
    print(f"[DONE] Chunks this run   : {total_chunks}")
    print(f"[DONE] Total in Qdrant   : {total_in_db}")
    print(f"[DONE] Collection        : {args.collection}")
    print(f"[DONE] Validation dumps  : {PROCESSED_DIR}/")


if __name__ == "__main__":
    main()
