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
    沿用 unstructured_components.py 現有的 partition_and_clean + build_chunk_records，
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
    做 payload filter 就能查到鄰居，不需要預先算好存死的指標（Exp 6 真的需要時再視效能決定
    要不要加）。

檔名慣例（**必須**維持現有 eval_set.json 的 ground truth glob 能吃得下）：
  10-K → f"{ticker}_10K_{fiscal_year}.html"        例：NVDA_10K_2026.html
  10-Q → f"{ticker}_10Q_{yyyymm}.html"              例：AAPL_10Q_202603.html
  年份/期碼一律從 edgartools 的 XBRL entity_info（document_period_end_date /
  fiscal_year）算出，不是憑空編造——已用 AAPL/NVDA 驗證與現有 data/raw/*.html 檔名一致。

──────────────────────────────────────────────────────────────────────────────
2026-08-07：.txt 加 MD5 增量快取（hashes_edgar.json）
  News/Fundamentals/IncomeStatement 的 .txt 在 ingest 前先算 MD5，與上次寫入同一個
  collection 時的值相同就整檔跳過（連 partition/chunk/embed 都省）。**SEC filing 不做**：
  filing 申報後同一 accession 內容不再變動，且內容要 API 抓回來才存在、事前算不了 hash，
  加快取省不到 API 呼叫只省 embed。filing 維持原本「delete by source + 重新 upsert」。
  詳見 _load_hashes 上方註解（含快取失準的逃生口 --force-txt）。

2026-08-07：keep-latest 主動清除過期快照（修不變量破口）
  原本被判 [SKIP stale] 的 Fundamentals/IncomeStatement 只是「不再 ingest」，但它先前
  增量 run 寫進去的 chunk 沒人刪（delete-by-source 只對正在 ingest 的檔案執行）→ 增量
  模式會累積多份快照，正是 keep-latest 想避免的版本漂移。現在判 stale 時一併
  _delete_by_source。設計意圖：**靜態財報（10-K/10-Q）允許多期並存**供查歷史財務表現；
  **動態估值快照（Fundamentals）保證庫裡只有最新一份**供回答「目前估值」。

2026-08-07：ingest 入口單一化
  data_update_unstructure.py 改名 unstructured_components.py 並剝除 CLI/Qdrant 寫入/
  hash 增量，降級為純處理組件；本檔成為唯一 ingest 執行入口。

Usage:
  .venv/Scripts/python.exe data_update_edgar.py                 # 抓 7 家公司最新 10-K + 最近 2 份 10-Q
  .venv/Scripts/python.exe data_update_edgar.py --rebuild        # 全量重建新 collection
  .venv/Scripts/python.exe data_update_edgar.py --tickers NVDA   # 只跑單一公司（開發/驗證用）
  .venv/Scripts/python.exe data_update_edgar.py --force-txt      # 忽略 MD5 快取，.txt 全部重跑
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── 沿用 unstructured_components.py 現有邏輯（DRY，避免兩份 chunking/embedding 邏輯漂移）──
from unstructured_components import (
    BGEM3DenseEmbeddings,
    build_chunk_records,
    build_metadata_header,
    deterministic_uuid,
    partition_and_clean,
    table_element_to_text,
    _find_caption,
    _count_table_rows,
    _llm_summarize_table,
    _table_context,
    TABLE_SUMMARY_MIN_ROWS,
)
from rag_query import infer_source_type, make_qdrant_client

# ── Config ───────────────────────────────────────────────────────────────────────
RAW_DIR         = Path("data/raw")            # News/Fundamentals/IncomeStatement .txt 仍從這裡讀
PROCESSED_DIR   = Path("data/edgar_processed")  # 人眼驗證用：Item 結構 + 財報三表 markdown 傾印
HASHES_FILE     = Path("hashes_edgar.json")   # .txt 的 MD5 增量快取（見 _load_hashes 註解）
# SEC filing 的離線來源（2026-08-08 抓取／處理分離）。由 fetch_data.py 產生：
#   SEC_LOCAL_DIR  — edgartools 本機儲存，{...}/filings/{YYYYMMDD}/{accession}.nc 完整申報檔
#   SEC_MANIFEST   — 取件清單，本檔**只處理清單上列的 filing**，不自己去 SEC 查有什麼新的
SEC_LOCAL_DIR   = RAW_DIR / "sec_local"
SEC_MANIFEST    = RAW_DIR / "sec_manifest.json"

# data/raw 的分類子目錄（2026-08-07 語料重整）：Filings / Fundamentals / News，外加
# _archive_stale（刻意封存的過期快照，不 ingest）。掃描必須遞迴——重整後 data/raw 根目錄
# 已經沒有任何檔案，沿用非遞迴的 iterdir() 會一個 .txt 都掃不到（靜默 ingest 0 筆）。
RAW_EXCLUDE_DIRS = {"_archive_stale"}

# data/edgar_processed 的傾印輸出鏡像 data/raw 的三分類，方便逐類人眼對照。
CATEGORY_FILINGS      = "Filings"
CATEGORY_FUNDAMENTALS = "Fundamentals"
CATEGORY_NEWS         = "News"
CATEGORY_OTHER        = "Other"
COLLECTION_NAME = os.getenv("EDGAR_COLLECTION_NAME", "us_stock_rag_edgar_exp1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"
DENSE_DIM       = 1024

TICKERS = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"]
# 每家公司 1 份 10-K + 最近 N 份 10-Q。2026-08-08 抓取／處理分離後，**這個數字由抓取層
# 決定**（fetch_data.py 的 --quarters），本檔只照 sec_manifest.json 取件。保留常數僅供
# 文件與 _extract_extra_tables 之類的判讀參考，不再控制本檔行為。
N_RECENT_10Q = 2

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


# ══════════════════════════════════════════════════════════════════════════════
# .txt 的 MD5 增量快取（2026-08-07）
# ══════════════════════════════════════════════════════════════════════════════
# 只對 News/Fundamentals/IncomeStatement 的 .txt 做，**刻意不含 SEC filing**：
# filing 一經申報，同一 accession number 的內容在 SEC 端就不會再變（要改只能發 10-K/A
# 修正案，那是另一份 accession，本腳本 amendments=False 也不會抓），沒有「內容變了要重跑」
# 的情境；而且 filing 內容是 edgartools 呼叫 API 抓回來之後才存在，事前算不了 hash，
# 加快取只能省 embed、省不到 API 呼叫本身，划不來。.txt 則是本地檔案，讀檔前就能算。
#
# 快取以 collection 為第一層 key：不同 collection（exp3/exp4/實驗用）各自記帳，避免
# 「A collection 已 ingest 過」的 hash 讓 B collection 誤跳過而少資料。
# 已知限制：快取只反映「檔案內容有沒有變」，不驗證 Qdrant 裡的 point 是否真的還在。
# 若有人手動刪過 collection 的點，hash 會誤判為「未變」而跳過 → 用 --force-txt 或
# --rebuild 重來（--rebuild 會重建 collection，故該 collection 的快取一併作廢）。

def _category_for(doc_type: str) -> str:
    """doc_type（來自 infer_source_type，單一真相來源）→ data/raw 的三分類目錄名。
    Fundamentals 與 IncomeStatement 歸同一類，與 data/raw/Fundamentals/ 的實際擺法一致
    （兩者都是 yfinance 拉下來的數字快照，只是切面不同）。"""
    dt = (doc_type or "").lower()
    if dt in ("10-k", "10-q"):
        return CATEGORY_FILINGS
    if dt in ("fundamentals", "income_statement"):
        return CATEGORY_FUNDAMENTALS
    if dt == "news":
        return CATEGORY_NEWS
    return CATEGORY_OTHER


def _dump_to(doc_type: str, filename: str, text: str) -> Path:
    """把人眼驗證用的傾印寫進 data/edgar_processed/<分類>/，鏡像 data/raw 的結構。"""
    out_dir = PROCESSED_DIR / _category_for(doc_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _render_chunk_dump(source: str, doc_type: str, records: list[dict]) -> str:
    """.txt 來源沒有 filing 的 Item 結構可傾印，改傾印「實際寫進 Qdrant 的 chunk」。
    這比傾印清理後全文更有驗證價值：切壞（過碎/過大/切斷數字）一眼就看得出來。"""
    lines = [f"# source    : {source}",
             f"# doc_type  : {doc_type}",
             f"# chunks    : {len(records)}", ""]
    for i, r in enumerate(records):
        text = r.get("text", "")
        lines += ["=" * 78,
                  f"[chunk {i}] chunk_type={r.get('chunk_type', 'text')}  chars={len(text)}",
                  "=" * 78, text, ""]
    return "\n".join(lines)


def _compute_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()


def _load_hashes() -> dict:
    if not HASHES_FILE.exists():
        return {}
    try:
        with open(HASHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # 快取壞掉不該讓 ingest 失敗——最壞情況只是這次全部重跑一遍。
        print(f"[WARN] {HASHES_FILE} 讀取失敗（{e}），本次視為無快取、全部重新 ingest")
        return {}


def _save_hashes(data: dict) -> None:
    with open(HASHES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _number_set(text: str) -> set:
    return set(_NUMBER_RE.findall(text))


# ══════════════════════════════════════════════════════════════════════════════
# edgartools helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sanitize_item_id(item_name: str) -> str:
    """'Item 1A' -> 'Item_1A'；'Part I, Item 2' -> 'Part_I_Item_2'。"""
    s = item_name.replace(",", "").replace(".", "")
    return re.sub(r"\s+", "_", s.strip())


# ── 期間章節邊界（2026-08-08 加）─────────────────────────────────────────────────
# 病灶：10-Q 的 MD&A 把「單季比較」與「累計比較」寫成兩個相鄰章節，各自只在**章節標題**
# 標了期間，內文的每一句（「Microsoft 365 Commercial cloud revenue grew 19%」）都不重述。
# SemanticChunker 依語意切塊時，標題會被切到別的 chunk 去，於是內文 chunk 讀不出自己
# 屬於哪一期——生成器不是讀錯，是資訊根本不在 context 裡。
#
# 實測曝險（exp4 全庫，剝掉我們自己注入的 metadata 前綴後）：109 個含變動陳述的 filing
# text chunk 裡有 15 個（14%）讀不出期間，其中 9 個集中在 MSFT 10-Q（24 個裡佔 38%）。
# 集中的原因是這個 "X Compared with Y" 章節標題結構**只有 MSFT 在用**（全 12 份 10-Q
# 掃過：MSFT 24 處，其餘 6 家 0 處）；別家把期間寫在句子裡，所以切塊切不掉。
# 已知案例：col-11（19%/33%/12%/22% 單季 與 18%/29%/20% 九個月被當成互斥數值並陳）。
# ⚠ 原本這裡也列了 mix-03,2026-08-09 查清後移除：它的病不是期間而是**分部**
#   （報了 Intelligent Cloud 的 +$2.7B/24% 當成全公司總計）,修在下面的分部小標邊界。
#
# 修法是把這個標題當**硬邊界**：先依它切段再各自送進 SemanticChunker（同一個 chunk 不會
# 橫跨兩個期間——實測 MSFT #104 開頭是九個月數字、標題卻出現在它中段，只貼標不切段會標錯），
# 再把期間標籤前綴進每個 chunk 的文字（與既有 build_metadata_header 同一個機制）。
# 兩種標題寫法（2026-08-09 補第二種）：10-Q 用 "X Months Ended <date> Compared with …"，
# 10-K 用 "Fiscal Year 2026 Compared with Fiscal Year 2025"。補第二種的動機不是期間標籤本身
# （10-K 整份就是一個財年，`report_period_code` 已經夠了），而是**它是分部小標那層的開關**：
# 分部切段只在有期間標籤的章節內生效，不補這條，MSFT 10-K 的分部小標就永遠切不到
# （曝險 5 個 chunk 裡的 MSFT_10K_2026 #144 就是這個情況）。
# ⚠ **大小寫敏感是必要的**：MSFT 10-K 同一份裡還有一句句中小寫的
#   "… fiscal year 2026 compared with fiscal year 2025 included:"，加 re.I 會從句子中間切開。
_PERIOD_SECTION_RE = re.compile(
    r"((?:(?:Three|Six|Nine|Twelve)\s+Months\s+Ended\s+[A-Z][a-z]+\.?\s+\d{1,2},\s*\d{4}\s+"
    r"Compared\s+[Ww]ith\s+(?:Three|Six|Nine|Twelve)\s+Months\s+Ended\s+[A-Z][a-z]+\.?\s+\d{1,2},\s*\d{4})"
    r"|(?:Fiscal\s+Year\s+\d{4}\s+Compared\s+[Ww]ith\s+Fiscal\s+Year\s+\d{4}))")


def _compact_period_label(header: str) -> str:
    """'Three Months Ended March 31, 2026 Compared with Three Months Ended March 31, 2025'
    -> 'Three Months Ended March 31, 2026 vs March 31, 2025'。前綴會出現在每個 chunk 上，
    左右兩側跨度相同時砍掉重複的 'X Months Ended' 以免灌爆 embedding 文字。"""
    h = re.sub(r"\s+", " ", header).strip()
    m = re.match(r"((Three|Six|Nine|Twelve) Months Ended .+?) Compared [Ww]ith "
                 r"\2 Months Ended (.+)", h)
    if m:
        return f"{m.group(1)} vs {m.group(3)}"
    # 10-K 的財年寫法同樣砍掉重複的 'Fiscal Year'：'Fiscal Year 2026 vs 2025'
    m = re.match(r"(Fiscal Year \d{4}) Compared [Ww]ith Fiscal Year (\d{4})", h)
    return f"{m.group(1)} vs {m.group(2)}" if m else h


_TAB_NUM_ROW = re.compile(r"^[\d,.\$%\(\)\-\s]+$")
_TAB_THOUSAND = re.compile(r"\b\d{1,3},\d{3}\b")


def _tab_line_kind(line: str, table_nums: set) -> str:
    """把 item 純文字的一行分成三類（給 _strip_tabular_blocks 用）。

    'tab' 確定是表格行、'neu' 中性（空行/短標籤,可被表格區夾住但不主動觸發）、
    'txt' 敘述句（一律保留）。判「tab」只認兩種硬證據：整行只有數字與符號,
    或該行的千分位數字**全部**已出現在某個 table chunk 裡。
    """
    t = line.strip()
    if not t:
        return "neu"
    if _TAB_NUM_ROW.match(t):
        return "tab"
    nums = set(_TAB_THOUSAND.findall(t))
    if nums and nums <= table_nums:
        return "tab"
    if len(t) <= 60 and not t.endswith((".", ";", "!", "?")) and len(t.split()) <= 7:
        return "neu"
    return "txt"


def _strip_tabular_blocks(item_text: str, table_texts: list[str],
                          min_tab_lines: int = 6) -> tuple[str, int, int]:
    """把**已經被 table chunk 收錄**的表格區段從散文中移除。回傳 (新文字, 區段數, 字元數)。

    ⛔ **2026-08-09 起預設關閉**（`--strip-dup-tables` 才啟用）。理由不是「它有害」,而是
    「**量不出好處,卻帶著一個無界的下行風險**」。100 題 RAGAS 單一變數對照（`period` vs
    `dedup`,同一批 21 份 filing、同一份修好的 gold）：recall +0.0176、correctness +0.0153
    是正的,faithfulness -0.0195、nv_context_relevance -0.0175 是負的,沒有明確勝方。而當初
    立案的核心指標 `context_precision` 量到 -0.0205——**這個數字不可解讀**：同一份結果檔
    重評兩次,precision 的 judge 噪音就有 0.0459（見 memory `ragas-judge-noise-floor`）。
    對上下面「已知缺口」那條「表格沒被 unstructured 抓成 table chunk 就真的刪掉資訊」,
    風險是不可逆的,收益是量不到的 → 預設關閉、程式保留。
    要重新立案,得先拿**確定性指標**（top-k 內重複對數、變動陳述存活數）驗收,別靠 RAGAS。

    病灶：表格走兩條路——edgartools 的 item 純文字接口把表格**壓平成文字**留在散文裡,
    Exp4 又額外用 unstructured 抓一份結構化的存成 table chunk。「額外」是關鍵：原本壓平
    那份從來沒被扣掉,於是同一張表同時存在兩處。實測 MSFT 10-Q 有 10/17 個 table chunk
    的數字在散文裡完整重現。

    代價是實打實的：100 題 × top-8 檢索,**33 題（33%）的候選池裡出現同一張表的兩個版本**,
    共 150 對。最糟的「Meta FY2025 Diluted EPS」top-8 裡有 6 個位置被同一份損益表佔掉
    （4 個 table chunk ＋ 2 個壓平版）。這是 context_precision 的直接損耗。
    附帶病灶：壓平表格的欄位標頭/列標籤（Revenue、Gross margin、Percentage、Change）
    在純文字裡與章節標題無法區分,是「按標題切塊」方案失準的主因（MSFT Item 2 的 171 個
    標題候選,移除後降到 121）。

    安全性由「變動陳述數量不變」把關（`increased/decreased $X billion`、`or Y%` 這類句子）。
    2026-08-08 對完整重建後的 collection 逐 source 比對（`us_stock_rag_edgar_period` vs
    `us_stock_rag_edgar_dedup`,21 份 filing 的 filing text chunk）：
    **變動陳述 426→426、`or N%` 252→252,每一份都逐一相同,零損失**。
    效果最大的 MSFT_10Q_202512 砍掉 22% 字元（234738→182603）、9 個 chunk,97 條變動陳述
    一條沒少——刪掉的確實只有壓平表格。
    ⚠ **效果差異的真因是 edgartools 的壓平風格不同,不是 MD&A 文體**（2026-08-08 查清,
    先前寫「MSFT 純數字行多」是不完整的歸因）。同一張 12 列 x 3 欄的表:
      MSFT 一個儲存格一行  -> '     81,273' / '     $' / '     69,632' …約 100 個 tab 行
      META 一整列一行      -> 'Revenue$200,966\xa0$164,501\xa0$134,902\xa0' …只有 12 個
    所以 min_tab_lines=6 對 MSFT 幾乎必中,對 META 只擋得住最大的表:實測 META Item 8 損益表
    一帶 13 個區段只刪掉 3 個（其餘是 1~5 個 tab 行的小表,全部低於門檻）。
    量化落差:META_10K 掉 8 段/5364 字元（-1.5%）vs MSFT_10Q 掉 65 段/41654 字元（-22%）。
    → 要提升覆蓋率,門檻該改成「數 cell 不數 line」,不是把 6 調小。

    ⚠ **第二個缺口:`_TAB_THOUSAND` 要求千分位逗號,看不見 <1000 的數值**。
    'Diluted$23.49\xa0$23.86\xa0$14.87\xa0' 匹配不到任何數字 -> 規則 2 的 `if nums` 不成立
    -> 判成 neu;而區段尾端回縮到最後一個 tab 行,EPS 那幾列剛好落在區段外而留在散文裡。
    評測題「Meta FY2025 Diluted EPS」問的就是這一列,也是殘留重複對最多的一題。

    ⚠ **殘留的重複對不全是 pipeline 造成的**:實測 AMZN Part_I_Item_1（財務報表）與
    Part_I_Item_2（MD&A）共享 7 個數字——SEC 申報書本身就會在兩處重述同一批分部數字。
    移除那個等於刪真內容。所以「殘留 68 對」高估了可修復的部分。
    ⚠ 移除文字會改變 SemanticChunker 的邊界位置,chunk 數**不是單調遞減**
    （實測 TSLA_10Q_202606 是 67→68）。要驗證正確性看變動陳述數,不要看 chunk 數。

    ⚠ **已知缺口**：`_tab_line_kind` 的第一條規則（整行只有數字與符號）不檢查這張表是否
    真的被收成 table chunk。所以一張 unstructured 沒抓到的純數字表格,只要夠大也會被刪,
    那份資訊就真的消失。目前擋這件事的只有上面的整體量測,不是逐表對帳。
    """
    table_nums: set = set()
    for t in table_texts:
        table_nums |= set(_TAB_THOUSAND.findall(t))
    if not table_nums:
        return item_text, 0, 0
    lines = item_text.split("\n")
    kinds = [_tab_line_kind(l, table_nums) for l in lines]
    keep = [True] * len(lines)
    i = nblk = nchar = 0
    while i < len(lines):
        if kinds[i] != "tab":
            i += 1
            continue
        j = i
        while j < len(lines) and kinds[j] in ("tab", "neu"):
            j += 1
        # 區段尾端回縮到最後一個 tab 行,避免把後面的章節標題一起吃掉
        end = max(k for k in range(i, j) if kinds[k] == "tab") + 1
        if sum(1 for k in range(i, end) if kinds[k] == "tab") >= min_tab_lines:
            for k in range(i, end):
                keep[k] = False
                nchar += len(lines[k])
            nblk += 1
        i = end
    return "\n".join(l for l, k in zip(lines, keep) if k), nblk, nchar


def _split_by_period_section(item_text: str) -> list[tuple[Optional[str], str]]:
    """依期間章節標題把 Item 內文硬切成 [(期間標籤 or None, 段落文字), ...]。
    標題本身留在段落開頭（它是真實來源文字，人眼傾印時要看得到）。沒有任何標題時
    回傳單一 [(None, item_text)]，行為與加這層之前完全相同。"""
    parts = _PERIOD_SECTION_RE.split(item_text)
    if len(parts) == 1:
        return [(None, item_text)]
    out: list[tuple[Optional[str], str]] = []
    if parts[0].strip():
        out.append((None, parts[0]))
    # split 帶一個 capture group → [前言, 標題1, 內文1, 標題2, 內文2, ...]
    for i in range(1, len(parts), 2):
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out.append((_compact_period_label(parts[i]), parts[i] + body))
    return out


# ── 分部小標邊界（2026-08-09 加，期間章節之下的第三層）───────────────────────────
# 病灶與期間那層**同形、低一層**：MSFT 的 MD&A 在每個期間章節裡再依分部寫小節，分部名
# 只出現在獨立成行的小標上，內文每句都不重述。SemanticChunker 併掉小標後，續段 chunk
# 就**主動把自己冒充成合併總計**——這比「讀不出期間」更嚴重：讀不出期間至少是資訊缺失，
# 冒充總計會直接產生一個看起來有憑有據的錯數字。
#
# 確診案例 mix-03（2026-08-09 端到端查清）：
#   問「Microsoft 最新一季整體營業利益成長多少」→ 系統答 +$2.7B / 24%。
#   那句原文在 chunk #111,開頭就是 "Operating income increased $2.7 billion or 24%.",
#   帶了期間標籤卻沒有分部標籤。用合併損益表驗算:合併單季 38,398-32,000=+6,398(+20%),
#   而 Intelligent Cloud 分部 13,753-11,095=+2,658(+24%)——#111 是 Intelligent Cloud。
#   正解那句其實**在語料裡**（#104 "Operating income increased $6.4 billion or 20%"）,
#   只是沒被檢索到:#111 開頭就是那個句型,字面與語意都比「數字埋在段中」的 #104 更像答案。
#   → 貼上分部標籤同時修兩邊:生成端不再讀成總計,檢索端 #111 對「全公司」問題的相似度下降。
#
# 曝險（us_stock_rag_edgar_period 全庫）：32 個含 "X increased $Y or Z%" 的 filing text
# chunk 裡有 5 個（16%）不含任何分部名稱＝讀起來像合併總計,全部在 MSFT（10Q 4 個、10K 1 個）。
#
# **刻意不用硬編碼分部名單**（見 memory `llm-vs-python-task-split`:硬編碼詞表＝在用字串
# 比對做感知,詞表外的寫法會靜默漏抓）。改用兩個結構訊號＋自我佐證:
#   ① 獨立成行的短標題（無數字、不以句號結尾、字數少、字首大寫）
#   ② **下一個非空行是一句變動陳述**——這是分部小標與「壓平表格的列標籤」的分野。
#      少了 ② 會把表格列標籤（'Revenue'、'Total'、'Percentage'、'Three Months Ended'）
#      全當標題,照那樣切會把散文切碎（2026-08-09 實測第一版就是這樣）。
#   ③ 該字串在同一份 filing 內以獨立行出現 ≥2 次（真小標會在單季段與累計段各出現一次）。
# 離線驗證（21 份 10-K/10-Q 的真實 SEC 原文,零網路,`eval/verify_segment_split.py`,可重跑）：
#   受影響：MSFT 兩份 10-Q（各 2 個期間章節）＋ MSFT 10-K Item 7（1 個）＝ 5 個章節,
#           每個都切成「合併層 + 3 分部」,分部名恰好是 Productivity and Business Processes
#           / Intelligent Cloud / More Personal Computing,零誤報。
#   未受影響：其餘 18 份 filing 全部 0 段（逐字不變）。
#   目標句 'Operating income increased $2.7 billion or 24%' 正確落進 Intelligent Cloud 段。
#   表格未被切開：切段前後每個分部名在該章節的出現次數完全相同（分部名同時是 SEGMENT
#   RESULTS 表的列標籤,靠「按行號切、不按字串切」保住,見下方函式的 ⚠）。
# → 曝險的 5 個 chunk（MSFT 10-Q 4 個、10-K 1 個）全部涵蓋。
#
# ⚠ **只在「有期間標籤」的章節內生效**（`period_label` 為 None 的整份 Item 不套用）。
#   這不是為了省事,是實測出來的必要收斂:把同一個偵測器套到沒有期間章節的 Item 上,
#   GOOGL/META/TSLA 會抓到真分部（Google Search & other、Family of Apps、Reality Labs、
#   Automotive & Services and Other Segment——這部分確實有價值）,但**同時產生真誤報**:
#     META 'NM — not meaningful'（表格圖例）、META 'Other Actions'（出現在 Part I Item 1
#     法律訴訟,吃掉 16~58k 字元）、TSLA 'Cash Flows from Operating Activities'（是真標題
#     但不是分部）、GOOGL 'Other Bets' 因為後面沒有下一個小標而吞掉 20k 字元的 Item 尾巴。
#   而曝險量測顯示受害 chunk **5/5 全在 MSFT**,MSFT 又正是唯一有期間章節的公司,所以收到
#   期間章節內即可覆蓋已知病例。要擴大適用範圍是獨立的下一步,需要先解掉上面四類誤報。
#   ⚠ 別被「先前探針顯示其餘 6 家零誤報」誤導——那個探針**跳過了沒有期間章節的檔案**,
#     等於根本沒測到那些情境（2026-08-09 差點據此把範圍開太大）。
_HEAD_MAX_CHARS = 60
_HEAD_MAX_WORDS = 6

# **導航殘渣**：SEC 申報檔的頁面家具,每頁重複一次。它形狀完全像標題（短、字首大寫、
# 無數字、不以標點結尾）,而「下一行是散文」也擋不住它（它後面就是該頁的正文）,所以
# 只能列清單。**列清單在這裡是正當的**,判準見 memory `llm-vs-python-task-split`:
# 這是**格式定義的封閉集合**（每份 SEC 申報檔都用這幾個字,不會有新寫法),與分部名／
# 指標名那種開放集合不同——同 VALID_*_ITEMS 的理由。
# ⚠ 2026-08-09 原本想用「同檔重複次數 >= 3 判為殘渣」取代清單,**實測推翻**:
#   Table of Contents 在 GOOGL 出現 20 次、META 5 次、TSLA 只有 1 次（連自己都不穩定）,
#   而真標題 Intelligent Cloud 7 次、More Personal Computing 8 次、
#   Energy Generation and Storage 8 次——完全重疊。照那個門檻做會把 F 的修復砸掉。
_HEAD_FURNITURE = {"table of contents", "page", "index", "index to consolidated "
                   "financial statements", "form 10-k", "form 10-q",
                   # 簽名頁：同樣是格式定義的封閉集合。列進來是為了不讓簽名區塊的
                   # 內容被貼上 [section: SIGNATURES]（無害但無意義的標籤）。
                   "signatures", "signature", "power of attorney"}

# 標籤最短長度。實測抓到 'X'（11 次,來自封面／展覽表的勾選欄）與 'N/A'——單字母／縮寫
# 通過了「短、字首大寫、無數字、不以標點結尾」全部條件,只能用長度擋。4 是實測分界：
# 真標題最短是 'Xbox'(4)、'Risks'(5)、'Taxes'(5)。
_HEAD_MIN_CHARS = 4

# **下一個非空行必須像散文**——這是真標題與「壓平表格的列標籤」的分野,取代原本
# 「下一行是變動陳述」那個過窄的條件（見上方 ② 的演進說明）。
# 少了它會把 Total(31 次)／Revenue(11)／Amount／Period／Fair Value／Numerator／
# Denominator／Assets／Equity 全當標題,而這些是**開放集合,列不完**,只能用結構判。
def _next_line_is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    words = [w for w in re.split(r"\s+", s) if re.search(r"[A-Za-z]", w)]
    return len(words) >= 8 or (bool(words) and s[-1] in ".?!")


def _looks_like_heading(line: str) -> bool:
    """條件①：獨立成行的短標題（無數字、不以標點結尾、字數少、字首大寫）。"""
    s = line.strip()
    if not s or not (_HEAD_MIN_CHARS <= len(s) <= _HEAD_MAX_CHARS):
        return False
    # '/' 是簽名行的起首（'/S/    SUNDAR PICHAI'）,其餘是項目符號與表格框線
    if s[0] in "•-–*|/":
        return False
    if re.search(r"\d", s) or s[-1] in ".,:;":
        return False
    if s.lower() in _HEAD_FURNITURE:
        return False
    words = [w for w in re.split(r"\s+", s) if re.search(r"[A-Za-z]", w)]
    if not words or len(words) > _HEAD_MAX_WORDS:
        return False
    # 允許 and / of / & 之類的小寫虛詞（'Productivity and Business Processes'）
    return sum(1 for w in words if w[0].isupper()) >= max(1, len(words) - 2)


def _standalone_line_counts(texts) -> "Counter":
    """整份 filing 的「獨立行 → 出現次數」。2026-08-09 通用化後不再是切段條件（見
    _split_by_subheading 的演進說明）,保留給診斷／驗收腳本統計用。"""
    from collections import Counter
    return Counter(l.strip() for t in texts for l in t.split("\n") if l.strip())


def _split_by_subheading(section_text: str) -> list[tuple[Optional[str], str]]:
    """把一段文字依**通用小標**再切成 [(小標 or None, 段落文字), ...]。
    第一個小標之前的部分標籤為 None（那是章節層敘述,例如合併總計,不貼標才是對的）。
    抓不到任何小標時回傳單一 [(None, section_text)],行為與加這層之前完全相同。

    **2026-08-09 由「分部小標」通用化而來**,並在通用化時**刪掉兩個條件**:
      舊 ②「下一行是變動陳述」→ 換成 `_next_line_is_prose`（保住它防表格列標籤的作用,
         但不再限定必須是變動陳述）。
      舊 ③「同檔以獨立行出現 >= 2 次」→ **刪除**。它是為了「專門找分部」而設,對通用
         標題是過度收斂;而重複次數本身已被證明無法當判準（見 _HEAD_FURNITURE 的 ⚠）。
    通用化的依據（2026-08-09 實測,只用條件① 掃 21 份申報檔原文）:散文型 Item 上抓到的
    是真標題——META Item 7 抓 50 個（Ad Revenue／Non-Ad Revenue／Investment Philosophy…,
    區間中位數 789 字元）、TSLA Item 1 抓 56 個、GOOGL Item 7 抓 62 個（Google Cloud／
    Other Bets／Cost of Revenues…）。原先記錄的四類「誤報」有三類其實是**真標題**,只是
    對「分部」偵測器而言算誤報（TSLA 'Cash Flows from Operating Activities'、
    GOOGL 'Other Bets'）——通用化後它們是正確的標籤。剩下兩類各有對策:
      表格列標籤 → `_next_line_is_prose`;導航殘渣 → `_HEAD_FURNITURE`。
    表格為主體的 Item（Item 8／15 等）**整層不套用**,見 _TABLE_DOMINATED_ITEMS。

    ⚠ **必須用行號判定,不能用字串比對**:同一個標題（尤其分部名）在同一章節裡還會作為
    表格的列標籤出現（SEGMENT RESULTS 表）。用字串比對會連表格那次一起切,把表格切成
    兩半。`_next_line_is_prose` 讓表格那次不會被認成標題（後面接的是儲存格不是句子）,
    所以只要記住「是第幾行」就不會誤切。"""
    lines = section_text.split("\n")
    nonempty = [i for i, l in enumerate(lines) if l.strip()]
    order = {i: k for k, i in enumerate(nonempty)}
    head_idx: dict[int, str] = {}
    for i in nonempty:
        if not _looks_like_heading(lines[i]):
            continue
        k = order[i]
        nxt = lines[nonempty[k + 1]] if k + 1 < len(nonempty) else ""
        if _next_line_is_prose(nxt):
            head_idx[i] = lines[i].strip()
    if not head_idx:
        return [(None, section_text)]
    out: list[tuple[Optional[str], str]] = []
    label: Optional[str] = None
    buf: list[str] = []
    for i, line in enumerate(lines):
        if i in head_idx:
            if "\n".join(buf).strip():
                out.append((label, "\n".join(buf)))
            label, buf = head_idx[i], [line]
        else:
            buf.append(line)
    if "\n".join(buf).strip():
        out.append((label, "\n".join(buf)))
    # 開頭無標籤的短區段併進下一段。實測 80 個,幾乎都是 Item 的標題行本身
    # （'ITEM 1. BUSINESS'、16~26 字元）——它本來就是下一段的抬頭,獨立成段只會製造一個
    # 十幾字元的 chunk,等於這層自己生產它要消滅的碎片。
    # ⚠ **有標籤的短區段刻意保留**（實測 81 個）:它短但自帶 scope,前綴 [section: X] 已經
    #   把指涉補上,與 mix-08 那種沒有 scope 的孤兒句性質不同。往前併反而會讓它被貼上
    #   前一段的標籤＝貼錯標,比短更糟。中段無標籤的短區段實測 0 個,不需處理。
    if len(out) > 1 and out[0][0] is None and len(out[0][1].strip()) < _MIN_SECTION_CHARS:
        out[1] = (out[1][0], out[0][1] + "\n" + out[1][1])
        out.pop(0)
    return out


_MIN_SECTION_CHARS = 200

# SEC 表格結構上以財報表格／清單為主體的 Item——通用標題層**整層不套用**。
# 依據（2026-08-09 實測）:TSLA 10-K Item 15（展覽表）只用條件① 會抓到 27 個候選、
# 區間中位數 **10 字元**,等於把表格切成碎片;Item 8（財報附註）抓到 150 個,混雜
# 'Page'／'/s/ PricewaterhouseCoopers LLP' 這類非標題。這些 Item 的表格本來就走
# chunk_type=table 另一條路,散文殘渣不需要標題層。
# 這是**格式定義的封閉集合**（SEC 表格結構固定）,列清單正當,同 VALID_*_ITEMS 的理由。
_TABLE_DOMINATED_ITEMS = {"Item 6", "Item 8", "Item 15", "Part I, Item 1"}

# 語意切塊的**下界**護欄。此前只有上界（RCTS_THRESHOLD=1200 token 補切）而沒有下界,
# 是不對稱的漏洞:2026-08-09 實測 us_stock_rag_edgar_period 的 2955 個 filing 散文
# chunk 裡,剝掉自注入前綴後 328 個 (11%) 不到 200 字元,極端尾巴是純頁碼（'63'、'18'）
# 與失去指涉對象的孤兒句。後者確實進了生成端 context——mix-08 撈到
# 'The increases were almost entirely driven by advertising revenue.'（65 字元,
# 「The increases」指什麼、增加多少全被切掉了）。
# ⚠ 不刪 bare-digit 獨立行:MSFT 10-K 有 502 個,遠多於頁數 → 多數是壓平表格的儲存格,
#   刪掉會毀損表格數字。改由合併吸收（併進鄰居後數字仍在,只是不再自成一個 chunk）。
_MIN_CHUNK_CHARS = 200


def _merge_small_chunks(chunks: list[str], min_chars: int = _MIN_CHUNK_CHARS) -> list[str]:
    """把過短的 chunk 併進鄰居。**呼叫端必須逐「硬邊界內」呼叫**（同一個期間／小標
    章節）,合併才不會跨越邊界把兩個期間或兩個分部黏在一起。
    往前併優先（保住上文指涉:'The increases…' 要接在講 increases 的那句後面）;
    首塊沒有前一個,往後併。整段只有一個 chunk 時原樣返回——那可能是真的很短的 Item
    （'ITEM 9. … Not applicable.'）,沒有鄰居可併也不該丟。"""
    if not chunks:
        return chunks
    out: list[str] = []
    for c in chunks:
        if out and len(c) < min_chars:
            out[-1] = out[-1] + "\n\n" + c
        else:
            out.append(c)
    if len(out) > 1 and len(out[0]) < min_chars:
        out[1] = out[0] + "\n\n" + out[1]
        out.pop(0)
    return out


def _period_prefix(record: dict) -> str:
    """chunk 文字的期間／小標前綴；兩者都沒有就回空字串（維持舊行為）。"""
    out = ""
    if record.get("period_context"):
        out += f"[{record['period_context']}] "
    if record.get("heading_context"):
        out += f"[section: {record['heading_context']}] "
    return out


# ── 離線取件（2026-08-08 抓取／處理分離）────────────────────────────────────────
# 在此之前本檔自己呼叫 `company.get_filings()` 去 SEC 查「現在最新的是哪幾份」，於是
# **每次重新切塊都會重抓、而且可能悄悄換到不同版本的文件**——這讓 chunking 實驗不可重現
# （改了切塊規則跑出來的差異，分不清是規則造成還是換版造成）。而且 fetch_data.py 本來就
# 已經抓過同一批 filing 了，等於抓兩次。
#
# 現在改成：fetch_data.py 抓 + 落地 + 寫 manifest；本檔只照 manifest 取件、零網路。
#
# ⚠ **guard 必須自己寫，不能依賴 edgartools**：`Filing.sgml()` 在本機找不到檔案時會
# 靜默 fallback 去下載（實地讀過原始碼確認）。少了下面的 guard，manifest 或 .nc 缺一份
# 就會無聲地變回線上抓取，而且結果看起來完全正常。
def _enable_local_storage() -> None:
    import edgar
    if not SEC_LOCAL_DIR.exists():
        raise RuntimeError(
            f"SEC 本機儲存不存在：{SEC_LOCAL_DIR}\n"
            f"處理層不自己抓 SEC。請先跑：python fetch_data.py --tickers <...> --skip-news --skip-fundamentals")
    edgar.set_local_storage_path(SEC_LOCAL_DIR)
    edgar.use_local_storage(True)
    _install_offline_html_patch()
    print(f"[INFO] SEC local storage: {SEC_LOCAL_DIR.resolve()}（處理層不連網）")


def _install_offline_html_patch() -> None:
    """堵住 edgartools 最後一個網路呼叫。

    `Filing.html()` 對「開頭是 `<?xml ...?>` 的文件」會**丟掉已經從本機讀到的 HTML、
    重新 download 一次 primary document**（edgar/_filings.py 的 `html.startswith("<?xml")`
    分支）。而 SEC 的 inline-XBRL 財報全都是這個開頭，所以本機儲存對 10-K/10-Q 幾乎沒
    生效——這也是舊管線每次重新切塊都在重抓的原因之一，只是它從來沒吵過。

    繞過是安全的，已實測比對：`sgml.html()` 與下載版**內容 MD5 完全相同**（MSFT 10-Q、
    NVDA 10-K 各驗一份，只差一個結尾換行）。這裡刻意讓「sgml 給不出 HTML」直接報錯，
    而不是回頭抓——寧可停下來也不要靜默連網。"""
    from edgar._filings import Filing, is_probably_html

    if getattr(Filing, "_offline_html_patched", False):
        return

    def _offline_html(self):
        html = self.sgml().html()
        if not html:
            raise RuntimeError(
                f"本機申報檔取不出 HTML：accession={self.accession_no}。"
                f"處理層不回頭抓 SEC——請重跑 fetch_data.py 補齊該份 .nc。")
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="replace")
        if html.endswith("</PDF>"):
            return None
        if is_probably_html(html):
            return html
        return f"<html><body><div>{html.replace('<PAGE>', '')}</div></body></html>"

    Filing.html = _offline_html
    Filing._offline_html_patched = True


def _load_sec_manifest() -> list[dict]:
    if not SEC_MANIFEST.exists():
        raise RuntimeError(
            f"取件清單不存在：{SEC_MANIFEST}\n"
            f"處理層只處理 manifest 上列的 filing。請先跑 fetch_data.py 產生它。")
    entries = json.loads(SEC_MANIFEST.read_text(encoding="utf-8"))
    if not entries:
        raise RuntimeError(f"{SEC_MANIFEST} 是空的，沒有任何 filing 可處理。")
    return entries


def _filings_from_manifest(ticker: str, allow_fetch: bool = False) -> list[tuple[str, "object"]]:
    """照 manifest 重建這家公司的 Filing 物件清單，回傳 [(form, filing), ...]。

    每一份都先確認本機 .nc 存在；缺檔就中止（除非 --allow-fetch）——寧可吵著停下來，
    也不要靜默地變成線上抓取。"""
    import edgar
    from edgar.storage import local_filing_path

    rows = [e for e in _load_sec_manifest() if e["ticker"] == ticker]
    if not rows:
        raise RuntimeError(f"manifest 裡沒有 {ticker} 的任何 filing。先跑 fetch_data.py --tickers {ticker}")

    out = []
    for e in sorted(rows, key=lambda r: (r["form"], r["period_tag"])):
        nc = Path(local_filing_path(e["filing_date"], e["accession_no"]))
        if not nc.exists():
            msg = (f"{ticker} {e['form']} {e['period_tag']} 的完整申報檔不在本機：{nc}\n"
                   f"       accession={e['accession_no']}  filing_date={e['filing_date']}")
            if not allow_fetch:
                raise RuntimeError(
                    msg + "\n處理層拒絕回頭抓 SEC。請重跑 fetch_data.py 補齊，"
                          "或明知後果時加 --allow-fetch。")
            print(f"  [WARN] {msg}\n         --allow-fetch 已開，將線上抓取（結果可能與 manifest 記載的版本不同）")
        filing = edgar.Filing(cik=e["cik"], company=e["company"], form=e["form"],
                              filing_date=e["filing_date"], accession_no=e["accession_no"])
        out.append((e["form"], filing))
    return out


def _filing_meta_from_xbrl(entity_info: dict, ticker: str, accession_no: str) -> dict:
    """對映到與 unstructured_components.extract_filing_metadata() 相同的欄位語意：
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
    unstructured_components.build_chunk_records 對 Table element 的處理完全一致），
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
            caption = _llm_summarize_table(
                body, context=_table_context(elements, i), item_id="notes_table")
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
                          rcts_threshold: int = RCTS_THRESHOLD,
                          strip_dup_tables: bool = False) -> tuple[dict, list[dict], str]:
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

    # 移除已被 table chunk 收錄的壓平表格（見 _strip_tabular_blocks）。必須在期間切段與
    # SemanticChunker 之前做：一旦切成 chunk 就分不出哪一段是表格複本了。
    # 預設關閉（2026-08-09）：效益量不出來、風險不可逆,理由見 _strip_tabular_blocks docstring。
    _tbl_texts = [r["text"] for r in records if r["chunk_type"] == "table"] if strip_dup_tables else []
    if _tbl_texts:
        _tb_blk = _tb_chr = 0
        for _iname in list(item_texts):
            _new, _b, _c = _strip_tabular_blocks(item_texts[_iname], _tbl_texts)
            if _b:
                item_texts[_iname] = _new
                _tb_blk += _b
                _tb_chr += _c
                dump_lines.append(f"[DEDUP-TABLE] item='{_iname}' 移除 {_b} 個已被 table chunk "
                                  f"收錄的表格區段（{_c} 字元）\n")
        if _tb_blk:
            print(f"  [DEDUP-TABLE] {form}: 從散文移除 {_tb_blk} 個表格區段（{_tb_chr} 字元）")

    for item_name, item_text in item_texts.items():
        item_id = _sanitize_item_id(item_name)
        dump_lines.append(f"--- [ITEM] {item_name} (len={len(item_text)} chars) ---\n"
                           f"{item_text[:400]}...\n")
        # 先收集這個 Item 最終要落地的所有文字片段（可能因 RCTS fallback 而比
        # SemanticChunker 原始輸出更多），最後統一 enumerate，讓 item_chunk_index
        # 維持連續（Neighbor Expansion 依賴這個序號查前後 chunk）。
        # (文字, 期間標籤, 小標) 三元組收集。切塊的層級結構是 Item → 期間章節 → 通用小標
        # → SemanticChunker → RCTS 上界補切 → min-size 下界合併。
        # 前三層都是**硬邊界**（規則、確定性、有唯一正確答案）,只決定「哪裡不准切」;
        # 「40k 字的敘述該在哪裡斷」沒有唯一正確答案,才交給 SemanticChunker。
        # 兩層都抓不到標題的 filing 只會得到單一段,行為與加這些層之前完全相同。
        final_texts: list[tuple[str, Optional[str], Optional[str]]] = []
        sections = _split_by_period_section(item_text)
        if len(sections) > 1:
            dump_lines.append(
                f"[PERIOD] item={item_id} 依期間章節標題切成 {len(sections)} 段："
                + " | ".join(lbl or "(標題前)" for lbl, _ in sections) + "\n")
        # 表格／清單為主體的 Item 不套通用標題層（見 _TABLE_DOMINATED_ITEMS）。
        use_heading = item_name not in _TABLE_DOMINATED_ITEMS
        for period_label, section_text in sections:
            # 期間章節之下再依通用小標切（第三層硬邊界，見 _split_by_subheading）。
            subsecs = (_split_by_subheading(section_text)
                       if use_heading else [(None, section_text)])
            if len(subsecs) > 1:
                dump_lines.append(
                    f"[SECTION] item={item_id} period={period_label} 依小標切成 "
                    f"{len(subsecs)} 段：" + " | ".join(s or "(小標前=章節層)" for s, _ in subsecs) + "\n")
            for head_label, sub_text in subsecs:
                # min-size 合併必須**逐硬邊界章節**做（在這個迴圈內），否則會把兩個期間
                # 或兩個小標的內容黏成一個 chunk——那正是這幾層要防的事。
                raw = [d.page_content.strip()
                       for d in semantic_chunker.create_documents([sub_text])]
                raw = [c for c in raw if len(c) > 20]
                merged = _merge_small_chunks(raw)
                if len(merged) < len(raw):
                    dump_lines.append(
                        f"[MIN-MERGE] item={item_id} section={head_label} "
                        f"{len(raw)} → {len(merged)} 個 chunk（< {_MIN_CHUNK_CHARS} 字元併進鄰居）\n")
                for content in merged:
                    # 上界檢查放在合併之後：合併可能把 chunk 推過 RCTS 門檻，順序反了
                    # 就會漏掉那次補切、讓 chunk 在 rerank 階段被截斷。
                    if rcts_splitter is not None and token_len_fn is not None \
                            and token_len_fn(content) > rcts_threshold:
                        sub_texts = [t.strip() for t in rcts_splitter.split_text(content) if t.strip()]
                        dump_lines.append(f"[RCTS] item={item_id} 原 chunk {token_len_fn(content)} "
                                           f"token > {rcts_threshold} → 補切成 {len(sub_texts)} 份\n")
                        final_texts.extend((t, period_label, head_label) for t in sub_texts)
                    else:
                        final_texts.append((content, period_label, head_label))

        # item_chunk_index 跨期間／小標章節連續編號：Neighbor Expansion 靠 ±1 查鄰居，
        # 若每段各自從 0 起算會有重號、查到錯的鄰居。
        for idx, (content, period_label, head_label) in enumerate(final_texts):
            rec = {"text": content, "chunk_type": "text",
                   "item_id": item_id, "item_chunk_index": idx}
            if period_label:
                rec["period_context"] = period_label
            if head_label:
                rec["heading_context"] = head_label
            records.append(rec)

    return filing_meta, records, "\n".join(dump_lines)


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant collection setup（本檔是唯一實作；schema 沿襲舊 unstructured 管線：
# dense+sparse 雙向量 + source/doc_type 兩個 keyword payload index）
# ══════════════════════════════════════════════════════════════════════════════

def _delete_by_source(client, collection_name: str, source: str) -> None:
    """刪掉某個 source 檔案在 collection 裡的所有 point。
    兩種用途：① 重寫前的冪等清除 ② 清掉已被 keep-latest 判為過期的舊快照。
    source 不存在時 Qdrant 視為 no-op，不必先查存在與否。"""
    from qdrant_client import models
    client.delete(
        collection_name=collection_name,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(
                key="source", match=models.MatchValue(value=source))])
        ),
    )


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
        # mentioned_tickers 索引（Gap 2）：news 硬 filter 改用「內文提及公司陣列包含 X」取代 owner
        # ticker，讓市場級新聞對被提及的其他公司可見（見 rag_query 的 _is_news_q 改寫）。KEYWORD
        # 索引對陣列欄位即「包含即命中」。
        client.create_payload_index(collection_name=collection_name, field_name="mentioned_tickers",
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
    """把 records 編碼成 dense+sparse 並 upsert，回傳 chunk 數。沿襲舊 unstructured
    管線的寫入約定（前綴 metadata header、deterministic UUID），額外疊加
    item_id / item_chunk_index / accession_number。本檔是唯一的 upsert 實作。"""
    from qdrant_client import models

    if not records:
        return 0

    meta_header = build_metadata_header(filing_meta)
    # 期間前綴接在申報層 metadata 之後：'[MSFT 10-Q period 202603] [Three Months Ended
    # March 31, 2026 vs March 31, 2025] <原文>'。跟 metadata header 同一個動機——把只存在
    # 章節標題裡、chunk 內文不重述的期間資訊帶進**被 embed 的文字**，檢索與生成才看得到。
    chunk_texts = [meta_header + _period_prefix(r) + r["text"] for r in records]
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
            "document":         meta_header + _period_prefix(record) + record["text"],
            "item_id":          record.get("item_id", "n/a"),
            "item_chunk_index": record.get("item_chunk_index", 0),
        }
        if record.get("period_context"):
            # 也存成獨立欄位（不只前綴進文字）：供日後期間硬 filter / 診斷用，
            # 且能直接查「這個 chunk 到底歸屬哪一期」而不必再解析 document 前綴。
            payload["period_context"] = record["period_context"]
        if record.get("heading_context"):
            # 同上理由。額外用途：一致性 validator 的 scope 判定（分部 vs 合併）可以直接
            # 讀這個欄位，不必再靠 LLM 從文字猜——mix-03 就是猜錯 scope 造成的。
            payload["heading_context"] = record["heading_context"]
        _basis = _period_basis_for(doc_type)
        if _basis:
            payload["period_basis"] = _basis   # 供 rag_query TTM 硬 filter 路由（見 _period_basis_for）
        payload.update(filing_meta)

        if doc_type == "news":
            # Gap 2：news chunk 標記內文實際提及的所有公司（mentioned_tickers 陣列），供 rag_query
            # 對 news 用「陣列包含 X」硬篩取代 owner ticker——市場級新聞（Magnificent Seven 全跌等）
            # 才不會對內文提到的其他公司隱形。deterministic 別名偵測（複用 rag_query._find_all_ticker_aliases）
            # ∪ owner ticker；與一次性 migrate_add_mentioned_tickers.py 同邏輯（未來重建語料自帶）。
            import rag_query as _rq
            _doc = payload["document"]
            _mentioned = set(_rq._find_all_ticker_aliases(_doc.lower(), _doc))
            _owner = str(payload.get("ticker") or "").upper()
            if _owner:
                _mentioned.add(_owner)
            payload["mentioned_tickers"] = sorted(_mentioned)

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
    parser.add_argument("--skip-filings", action="store_true",
                        help="跳過 10-K/10-Q，只處理 .txt。用於「只改了 .txt 內容」的增量重建"
                             "（如 eval/migrate_fundamentals_pct.py 之後）——SEC filing 不走 MD5 快取、"
                             "每次都會全部重切（21 份約 1 小時），且表格抽取有跑次間變異，"
                             "沒必要時重跑只會引入無關差異。")
    parser.add_argument("--force-txt", action="store_true",
                        help=f"忽略 {HASHES_FILE} 的 MD5 快取，強制重新 ingest 所有 .txt"
                             "（快取與 Qdrant 實際內容不同步時的逃生口）")
    parser.add_argument("--allow-fetch", action="store_true",
                        help="本機缺 .nc 時允許線上抓取（預設拒絕並中止）。⚠ 開了就可能拿到與 "
                             "sec_manifest.json 記載不同版本的文件，chunking 實驗的可重現性會斷掉。")
    parser.add_argument("--rcts-fallback", action="store_true",
                        help="Exp 3：SemanticChunker 輸出的 chunk 若 reranker token 數超過 "
                             f"{RCTS_THRESHOLD} 時，改用 RCTS（{RCTS_CHUNK_SIZE}/{RCTS_CHUNK_OVERLAP}，"
                             "length_function=reranker tokenizer）補切。不加此旗標則行為與 Exp 1/2 "
                             "完全一致（純 SemanticChunker，無 size cap）。")
    parser.add_argument("--strip-dup-tables", action="store_true",
                        help="把已被 table chunk 收錄的壓平表格從散文移除（_strip_tabular_blocks）。"
                             "⚠ 預設關閉：100 題 RAGAS 對照無明確勝方，而 context_precision 的差異"
                             "落在 judge 噪音（0.046）內量不出來；反之若某張表 unstructured 沒抓成 "
                             "table chunk，這層會把它從散文刪掉、資訊真的消失。")
    args = parser.parse_args()

    import edgar
    from FlagEmbedding import BGEM3FlagModel
    from langchain_experimental.text_splitter import SemanticChunker

    sec_identity = os.getenv("SEC_IDENTITY")
    if not sec_identity:
        raise RuntimeError("SEC_IDENTITY 未設定（.env），edgartools 需要合法聯絡資訊才能呼叫 SEC EDGAR。")
    edgar.set_identity(sec_identity)
    _enable_local_storage()

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
    print(f"[INFO] 壓平表格去重 (--strip-dup-tables) : {'ON' if args.strip_dup_tables else 'OFF（預設）'}")

    client = make_qdrant_client()
    ensure_collection(client, args.collection, recreate=args.rebuild)

    total_chunks = 0
    total_filings = 0
    txt_skipped_unchanged = 0

    # ── 10-K + 10-Q（照 manifest 離線取件）─────────────────────────────────────
    # 「抓哪幾份」由 fetch_data.py 決定並寫進 manifest（含 amendments=False 的排除邏輯，
    # TSLA 2026-04-30 那份 10-K/A 就是在抓取層被擋掉的）。本檔不再自己查 SEC，因此
    # 重新切塊永遠針對同一批文件，chunking 實驗的差異可以歸因到規則本身。
    for ticker in (() if args.skip_filings else args.tickers):
        print(f"\n[TICKER] {ticker}")
        for form, filing in _filings_from_manifest(ticker, allow_fetch=args.allow_fetch):
            print(f"  [LOCAL] {form} | accession={filing.accession_no} | filed={filing.filing_date}")

            filing_meta, records, dump_text = _fetch_filing_records(
                ticker, form, filing, semantic_chunker,
                rcts_splitter=prose_rcts_splitter, token_len_fn=prose_token_len_fn,
                strip_dup_tables=args.strip_dup_tables)
            source = _build_filename(filing_meta, ticker)
            doc_type = infer_source_type(source)

            n_table = sum(1 for r in records if r["chunk_type"] == "table")
            n_text  = sum(1 for r in records if r["chunk_type"] == "text")
            print(f"    -> source={source} | {len(records)} chunks ({n_table} table + {n_text} text)")

            _dump_to(doc_type, Path(source).stem + ".txt", dump_text)

            if not args.rebuild:
                _delete_by_source(client, args.collection, source)

            n = upsert_records(client, args.collection, bge_m3, source, filing_meta, records, doc_type)
            total_chunks += n
            total_filings += 1

    # ── News / Fundamentals / IncomeStatement .txt ──────────────────────────────
    # News：單純 SemanticChunker + RCTS fallback（_build_news_records，本次修正）。
    # Fundamentals / IncomeStatement：沿用舊 partition_and_clean + build_chunk_records
    # （這兩種是 key:value 純文字，本來就沒有 HTML 表格可偵測，維持原行為）。
    if not args.skip_txt:
        print("\n[INFO] Ingesting News/Fundamentals/IncomeStatement .txt ...")
        # --rebuild 會重建 collection，舊快取對這個 collection 全部作廢，從空的開始記。
        all_hashes = _load_hashes()
        txt_hashes: dict = {} if args.rebuild else dict(all_hashes.get(args.collection, {}))
        if args.force_txt:
            print(f"[INFO] --force-txt：忽略 {HASHES_FILE} 快取，所有 .txt 一律重新 ingest")
        elif txt_hashes:
            print(f"[INFO] MD5 快取：{HASHES_FILE}（{args.collection} 已記錄 {len(txt_hashes)} 個 .txt）")
        # 遞迴掃描：語料已按 Filings/Fundamentals/News 分目錄擺放，根目錄不再有檔案。
        # _archive_stale/ 是刻意封存的過期快照，整個目錄排除（不只靠 keep-latest 擋）。
        txt_files = sorted(
            f for f in RAW_DIR.rglob("*")
            if f.is_file()
            and f.suffix.lower() in {".txt", ".md"}
            and not (RAW_EXCLUDE_DIRS & set(f.relative_to(RAW_DIR).parts))
            and any(f.stem.startswith(t + "_") for t in args.tickers)
        )
        print(f"[INFO] 掃到 {len(txt_files)} 個 .txt/.md"
              f"（已排除 {'/'.join(sorted(RAW_EXCLUDE_DIRS))}）")
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
                    # 不能只是「跳過」：舊快照可能在**先前的增量 run** 已經寫進 collection，
                    # 而 delete-by-source 只對「這次要 ingest 的檔案」執行，被跳過的檔案沒人清
                    # → 增量模式下會累積多份 Fundamentals/IncomeStatement 快照，正是
                    # keep-latest 要避免的版本漂移（靜態財報 10-K/10-Q 允許多期並存查歷史；
                    # 動態估值快照則必須「庫裡永遠只有最新一份」）。這裡主動清掉殘留。
                    # --rebuild 不需要做：collection 剛重建，本來就是空的。
                    if not args.rebuild:
                        _delete_by_source(client, args.collection, filepath.name)
                    txt_hashes.pop(str(filepath), None)   # 一併撤銷快取，重新變成最新時會重跑
                    print(f"  [SKIP stale] {filepath.name} (keep {_latest_stamp.get(key)}；已清除殘留 chunk)")
                    continue

            # MD5 增量：內容與上次寫入這個 collection 時相同就整檔跳過，連 partition/
            # chunk/embed 都不做（最貴的是 embed）。放在 keep-latest 之後——被判 stale 的
            # 檔案本來就不該 ingest，不必為它算 hash。
            file_md5 = _compute_md5(filepath)
            if not args.force_txt and txt_hashes.get(str(filepath)) == file_md5:
                print(f"  [SKIP unchanged] {filepath.name}")
                txt_skipped_unchanged += 1
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

            # 傾印到 data/edgar_processed/<分類>/，與 filing 一樣可人眼驗證（先前只有
            # filing 有傾印，News/Fundamentals 切成什麼樣完全看不到）。
            _dump_to(doc_type, filepath.stem + ".txt",
                     _render_chunk_dump(filepath.name, doc_type, chunk_records))

            m_ticker = re.match(r"^([A-Z]+)_", filepath.stem)
            filing_meta = {"ticker": m_ticker.group(1) if m_ticker else ""}

            if not args.rebuild:
                _delete_by_source(client, args.collection, filepath.name)

            n = upsert_records(client, args.collection, bge_m3, filepath.name, filing_meta,
                              chunk_records, doc_type)
            total_chunks += n
            print(f"  [{filepath.name}] {n} chunks")

            # 寫入成功才記 hash——中途丟例外時不會留下「已處理」的假紀錄。
            txt_hashes[str(filepath)] = file_md5

        all_hashes[args.collection] = txt_hashes
        _save_hashes(all_hashes)

    total_in_db = client.count(collection_name=args.collection, exact=True).count
    print(f"\n{'─'*60}")
    print(f"[DONE] Filings processed : {total_filings}")
    print(f"[DONE] Chunks this run   : {total_chunks}")
    print(f"[DONE] .txt skipped (MD5): {txt_skipped_unchanged}")
    print(f"[DONE] Total in Qdrant   : {total_in_db}")
    print(f"[DONE] Collection        : {args.collection}")
    print(f"[DONE] Validation dumps  : {PROCESSED_DIR}/")


if __name__ == "__main__":
    main()
