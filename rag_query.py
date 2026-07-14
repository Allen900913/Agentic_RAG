"""
rag_query.py — US Stock Intelligence RAG (Qdrant Hybrid + Server-side RRF + Rerank)

檢索流程：
  query ─► BGE-M3 (dense + sparse 同時)
              │
              ▼
        Qdrant query_points (single call):
              Prefetch[dense] ──┐
                                ├─► FusionQuery(RRF, k=60) ─► top-10
              Prefetch[sparse] ─┘
              │
              ▼
        BGE-reranker v2-m3 (Cross-Encoder 精排)
              │
              ▼
        top-k → LLM 生成（附引用）

Usage:
  python rag_query.py
  python rag_query.py --query "NVIDIA 毛利率多少？"
  python rag_query.py --query "..." --top-k 3
  python rag_query.py --query "..." --model gemini-2.5-flash
"""

import argparse
import math
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
QDRANT_PATH      = os.getenv("QDRANT_PATH", "./qdrant_db")
QDRANT_URL       = os.getenv("QDRANT_URL", "")   # 若設定則走 server mode（Docker）
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL     = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
COLLECTION_NAME  = "us_stock_rag_unstructured"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"

DEFAULT_TOP_K      = 5
FETCH_N            = 60   # 每路 prefetch 取多少候選送進 server-side RRF（寬召回，Qdrant 取 60/30 幾乎同速）
RRF_TOP_N_PRIMARY  = 20   # server-side RRF 回傳候選數；sweep 顯示 20 > 40（reranker 信噪比最佳）
VARIANT_CAP        = 8    # 每個變體只注入 top-N（sweep 顯示 8 > 5 > 3/15）
RERANK_INPUT_N     = 50   # cross-encoder reranker 輸入上限（成本天花板）
RERANK_MAX_LENGTH  = 2048 # cross-encoder 每筆輸入截斷 token 數；預設(未設定時)是 8192 等於不截斷。
                           # ⚠ 512 曾一度試過但證實不安全：sem-11（TSLA 風險題）在未截斷時 rank-1
                           # 候選（chunk #58，6982 字元 ≈1745 token）在 512 截斷下直接掉出 top-5，
                           # 因為該候選相關內容不在前 512 token 內。全庫 2367 chunk 的 token 分布
                           # p99=2278、僅 1.6% 超過 2048 token；2048 在 sem-11 測試中與未截斷（8192）
                           # top-5 逐位分數完全一致，仍有 ~1.8 倍加速（見 CHANGELOG 2026-07-13）。
DEFAULT_MODEL   = "llama-3.3-70b-versatile"   # retrieval 側預設（filter/rewrite/translate）；預設走 Groq（避免 Gemini 免費額度 20/天 限制）
DEFAULT_GEN_MODEL = "openai/gpt-oss-120b"     # 生成答案側預設（見 CHANGELOG 2026-07-09 乾淨隔離 A/B：
                                               # retrieval 固定 70b、只換 gen model，120b 對 k=3 全量 11 題 semantic
                                               # 零回歸、mean 0.627→0.870 且更穩定，故轉正式預設；retrieval side 仍用 DEFAULT_MODEL）
GEN_TEMPERATURE = 0.3    # 生成答案用；檢索側（filter/rewrite）與 judge 一律 temp=0（call_llm 預設）。
                         # 實測：生成用 temp=0（greedy）會重複/mode collapse，反而漏 rubric 點（見 CHANGELOG 2026-07-06）。
# 生產預設（2026-07-13 轉正，見 CHANGELOG）：rewrite 擴召回 + translate_query_en 的
# 「雙 query 取最高分」rerank。k=3 全量驗證 sem-09 完全修好、sem-11 維持、lexical/mixed/
# colloquial 零新回歸。retrieve() 本身的參數預設維持 False（保持 eval 腳本向後相容），
# 只有生產入口（CLI / api_server）預設打開這兩個。
DEFAULT_ENABLE_REWRITE     = True
DEFAULT_TRANSLATE_QUERY_EN = True
# 檢索後句級抽取（contextual compression）：生成前用一次 LLM 呼叫把每個 top-k chunk 對
# 這個 query 的相關句子逐字抽出、放到 chunk 開頭當「重點摘錄」。
# **預設關（實驗性，未轉正）**——見 CHANGELOG 2026-07-14：原用來修 sem-08，實測 compressor
# 確實把目標句逐字拉到 chunk 最前面，但生成端仍 2/3 次不引用（scores=[1.0,0.5,0.5]），證實
# sem-08 病灶不是「訊號埋沒」而是「模型判定獲利與策略題無關 + judge/gen 雜訊」，此法解不了。
# 且它是全域生成端改動、加一次 LLM 呼叫（+延遲），未做跨類全量回歸，故不預設開。要轉正需先跑
# 全量 lexical/mixed/semantic/colloquial 確認無回歸（尤其 lexical 數字題的 citation 不被打亂）。
# 保留 --compress 開關供實驗；rewrite/translate 當初也是全量 k=3 驗證後才轉生產預設的。
DEFAULT_ENABLE_COMPRESS    = False
GROQ_BASE_URL   = "https://api.groq.com/openai/v1"
MAX_HISTORY     = 3

# ── Groq key rotation（支援最多 4 個 key，TPD 耗盡時自動切換）────────────────
_groq_keys: list[str] = []
_groq_key_idx: int = 0

def _get_groq_key() -> str:
    global _groq_keys
    if not _groq_keys:
        _groq_keys = [k for k in [
            os.getenv("GROQ_API_KEY"),
            os.getenv("GROQ_API_KEY2"),
            os.getenv("GROQ_API_KEY3"),
            os.getenv("GROQ_API_KEY4"),
        ] if k]
    return _groq_keys[_groq_key_idx % len(_groq_keys)]

def _rotate_groq_key(err_str: str) -> bool:
    global _groq_key_idx, _groq_keys
    if "tokens per day" not in err_str and "TPD" not in err_str:
        return False
    _get_groq_key()  # 確保 _groq_keys 已初始化
    next_idx = _groq_key_idx + 1
    if next_idx >= len(_groq_keys):
        return False
    _groq_key_idx = next_idx
    print(f"  [KEY ROTATION] TPD exhausted, switching to GROQ_API_KEY{_groq_key_idx + 1}")
    return True


def make_qdrant_client():
    """依 QDRANT_URL 決定走 Docker server 或 local path 模式。"""
    from qdrant_client import QdrantClient
    if QDRANT_URL:
        print(f"[INFO] Opening Qdrant server: {QDRANT_URL}")
        return QdrantClient(url=QDRANT_URL)
    print(f"[INFO] Opening Qdrant local: {QDRANT_PATH}")
    return QdrantClient(path=QDRANT_PATH)


SYSTEM_PROMPT = """\
You are a professional US stock market analyst and financial expert specializing \
in US technology stocks, including NVIDIA (NVDA), Microsoft (MSFT), Apple (AAPL), \
Amazon (AMZN), Alphabet/Google (GOOGL), Meta (META), and Tesla (TSLA).

Rules:
1. Answer ONLY based on the provided reference materials.
2. After EVERY factual claim, add a citation: [filename, chunk #N]
3. Only refuse if the materials contain NOTHING relevant to the question. If they \
contain partial or related information, answer with what IS available and briefly \
note what is missing — do NOT refuse outright. Prefer a partial answer over a \
refusal. Use "I don't have enough information in my knowledge base to answer this." \
ONLY when nothing in the materials relates to the question at all.
4. Structure every answer this way:
   (a) Lead with the single most important conclusion or headline number that \
directly answers the question — in the FIRST sentence.
   (b) Then provide supporting details and secondary figures.
   Do NOT open with background, caveats, or peripheral details.
5. If the question asks for a rate or figure (growth rate, margin, P/E, etc.), the \
headline number MUST appear first. If only component figures are present, compute \
or state the overall figure explicitly rather than saying "no explicit data".
6. Be precise and data-driven. When citing numbers (revenue, margins, etc.), always \
specify the time period.
7. For open-ended / qualitative questions (strategy, competitive positioning, moat, \
risks, growth outlook), do NOT stop at a single point. Cover the DISTINCT aspects the \
materials support, point by point (e.g. technology, market position, financials, \
forward strategy) — a thorough answer names several complementary angles rather than \
elaborating one. Aim for breadth of coverage, not depth on a single facet.
8. Never invent specific figures. If a precise number (a growth rate, percentage, \
dollar amount) is NOT stated in the materials, describe the trend qualitatively or say \
it is not disclosed — do NOT fabricate or estimate a number that the sources do not \
contain. Every number you state must be traceable to a cited chunk.
9. When the question concerns MORE THAN ONE company (e.g. "these companies", "compare \
A and B", or the materials clearly span multiple companies), you MUST attribute each \
point to a NAMED company in the prose itself — write "META faces X [cite]; Microsoft \
faces Y [cite]", NOT a generic "these companies face X and Y". Name each company \
explicitly and bind its specific risks/figures/strategy to it. A citation tag alone is \
NOT sufficient attribution — the company name must appear in the sentence. Cover at \
least the distinct companies the materials support.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def infer_source_type(source: str) -> str:
    source_upper = source.upper()
    if "_NEWS_" in source_upper:
        return "news"
    if "_10K_" in source_upper or "10-K" in source_upper:
        return "10-K"
    if "_10Q_" in source_upper or "10-Q" in source_upper:
        return "10-Q"
    if "FUNDAMENTALS" in source_upper:
        return "fundamentals"
    if "INCOMESTATEMENT" in source_upper:
        return "income_statement"
    return "other"


def looks_like_news_query(query: str) -> bool:
    query_lower = query.lower()
    keywords = (
        "news", "headline", "headlines", "favorable", "unfavorable",
        "新聞", "消息", "利多", "利空", "有利", "不利",
    )
    return any(keyword in query_lower for keyword in keywords)


# ══════════════════════════════════════════════════════════════════════════════
# Query-period understanding → hard payload filter
#
# 軟訊號（摘要裡帶 metadata）只能讓「期間正確」的候選排名往前移一點，無法保證
# 它能擠進最終 top-k（見 eval/build_and_compare_table_summary_meta.py 的實驗結
# 果：tbl-02 仍被期間錯誤的表格擠在 rank #5）。真正能保證不選錯期間的做法是
# 「結構化抽取 + payload 硬篩選」：把使用者問題中『明確』提到的 filing_type /
# fiscal_year / fiscal_period 抽取出來，連同「要/不要」這個極性，轉成 Qdrant
# 的 must（include）/ must_not（exclude）條件，在排序前就把不符合的候選排除。
# ══════════════════════════════════════════════════════════════════════════════

FILTERABLE_FIELDS = ("filing_type", "fiscal_year", "fiscal_period", "report_period_code", "ticker")

# deterministic 抽取：6 位數期間碼（yyyymm）和 ticker
_PERIOD_CODE_RE = re.compile(r"\b(20\d{2}(?:0[1-9]|1[0-2]))\b")
_COMPANY_TICKER: dict[str, str] = {
    "apple": "AAPL",   "aapl": "AAPL",   "蘋果": "AAPL",
    "microsoft": "MSFT", "msft": "MSFT", "微軟": "MSFT",
    "nvidia": "NVDA",  "nvda": "NVDA",   "輝達": "NVDA",
    "amazon": "AMZN",  "amzn": "AMZN",   "亞馬遜": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "googl": "GOOGL", "谷歌": "GOOGL",
    "meta": "META",    "臉書": "META",
    "tesla": "TSLA",   "tsla": "TSLA",   "特斯拉": "TSLA",
}

# 中文別名沒有天然的詞界（\b 對 CJK 字元無效），偵測時要跟英文別名分開處理：
# 英文別名用 \b 開頭詞界比對（避免 "meta" 誤配到 "metadata"）；
# 中文別名用直接子字串比對即可（公司簡稱在財經語境下不會是更長詞的子字串）。
_CJK_RE = re.compile(r"[一-鿿]")


def _find_ticker_alias(q_lower: str, query: str) -> str | None:
    """deterministic（無 LLM）：偵測第一個命中的公司別名（英文或中文），回傳對應 ticker。"""
    for name, symbol in _COMPANY_TICKER.items():
        if _CJK_RE.search(name):
            if name in query:
                return symbol
        elif re.search(r"\b" + re.escape(name) + r"\b", q_lower):
            return symbol
    return None


def _detect_ticker(query: str) -> str | None:
    """deterministic（無 LLM）：從問題偵測單一公司 ticker（含中文別名），與
    _extract_structural_filters 共用同一份 _COMPANY_TICKER 對照表。"""
    return _find_ticker_alias(query.lower(), query)


def _extract_structural_filters(query: str) -> list[dict]:
    """deterministic（無 LLM）：從問題抽出 yyyymm 期間碼和 ticker（含中文別名）。"""
    filters: list[dict] = []
    m = _PERIOD_CODE_RE.search(query)
    if m:
        filters.append({"field": "report_period_code", "value": m.group(1), "polarity": "include"})
    ticker = _find_ticker_alias(query.lower(), query)
    if ticker:
        filters.append({"field": "ticker", "value": ticker, "polarity": "include"})
    return filters

QUERY_FILTER_SYSTEM_PROMPT = """\
You are a query-understanding assistant for a financial RAG system whose knowledge \
base contains SEC filings (10-K annual reports and 10-Q quarterly reports) for \
NVIDIA (NVDA), Microsoft (MSFT), and Apple (AAPL).

Your job: read the user's question and decide whether it EXPLICITLY constrains \
which filing/period the answer must (or must not) come from. Extract those \
constraints as structured JSON — including whether each one is something the user \
WANTS (include) or does NOT want (exclude, e.g. "not the 2025 annual report", \
"exclude the 10-Q", "除了10-K以外", "不要2025年報").

Output ONLY a JSON object of this exact shape, nothing else:
{
  "filters": [
    {"field": "fiscal_year" | "fiscal_period" | "filing_type", "value": "<exact value>", "polarity": "include" | "exclude"}
  ]
}

Field value rules:
- "fiscal_year": a 4-digit year string, e.g. "2025"
- "fiscal_period": one of "FY" (annual/full-year), "Q1", "Q2", "Q3"
- "filing_type": one of "10-K", "10-Q"

Critical rules:
- Only extract a constraint when the question EXPLICITLY states it (e.g. "fiscal \
year 2025", "10-K", "annual report", "quarterly report", "Q2"). Do NOT guess a \
period from an implicit date you are not fully sure how to map (e.g. "as of \
January 25, 2026" — leave this alone, it needs fiscal-calendar knowledge you don't \
have here).
- "polarity" defaults to "include". Use "exclude" ONLY when the question contains \
clear negation aimed at that specific field ("not", "don't want", "exclude", \
"other than", "除了...以外", "不要", "不是").
- If nothing is explicitly constrained, return {"filters": []}.
- Output ONLY the JSON object — no markdown fences, no explanation, no preamble.
"""


def parse_query_filters(query: str, model_name: str = DEFAULT_MODEL) -> list[dict]:
    """用 LLM 把問題中『明確』提到的 filing 篩選條件抽取成結構化清單：
        [{"field": "fiscal_year", "value": "2025", "polarity": "include"}, ...]
    polarity 區分「要」(include -> must) 與「不要」(exclude -> must_not)。
    解析失敗或沒有明確條件時回傳 []（= 不套用篩選，行為等同原本）。"""
    import json
    import re as _re

    try:
        raw = call_llm(
            [
                {"role": "system", "content": QUERY_FILTER_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            model_name,
        )
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        parsed = json.loads(raw)
    except Exception as e:
        # LLM 掛掉（429/逾時/解析錯）時，至少保住不需 LLM 的 deterministic filter
        # （yyyymm 期碼 + ticker），而不是完全不過濾。
        print(f"WARN  - query-filter parsing failed ({e!r}); falling back to deterministic filter")
        return _extract_structural_filters(query)

    filters = parsed.get("filters", []) if isinstance(parsed, dict) else []
    cleaned = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        field    = f.get("field")
        value    = f.get("value")
        polarity = f.get("polarity", "include")
        if field not in FILTERABLE_FIELDS or not value:
            continue
        if polarity not in ("include", "exclude"):
            polarity = "include"
        cleaned.append({"field": field, "value": str(value), "polarity": polarity})
    # 疊加 deterministic structural filters（yyyymm 期間碼 + ticker），不覆蓋 LLM 已抽到的欄位
    existing_fields = {f["field"] for f in cleaned}
    for sf in _extract_structural_filters(query):
        if sf["field"] not in existing_fields:
            cleaned.append(sf)
    return cleaned


# ══════════════════════════════════════════════════════════════════════════════
# Query rewrite（口語 / 模糊 / 跨語言 → 標準金融術語）
#
# 注意：現有 eval/eval_set.json 的 query 本身就是標準金融用詞（如「gross margin」、
# 「核心技術護城河」），所以本功能對「現有 eval 指標幾乎沒有增益」，甚至可能因改寫
# 引入雜訊而略降——這是預期內的。它真正的價值在「真實使用者的口語化提問」（例如
# 「蘋果賺不賺錢」→「Apple net income / profitability」），而且因為改寫把詞彙對齊到
# 文件實際用語，對 hybrid 檢索中「字面比對」的 sparse lexical lane 幫助最大。
#
# 設計：multi-query（不是取代原 query）。原 query 照常跑完整檢索，改寫變體只在「勝出
# 的那層 filter」上額外擴召回、合併候選池；最終 rerank 仍用「原始 query」決定排序，
# 所以改寫失誤時原 query 仍是保底。預設關閉（--rewrite 才啟用）。
# ══════════════════════════════════════════════════════════════════════════════

_FIN_GLOSSARY = """\
- 賺錢 / 賺不賺錢 / 獲利能力 → profitability, net income, earnings
- 毛利 / 毛利率 → gross margin, gross profit
- 業績 / 賣多少 / 營收 → revenue, net sales, total revenue
- 護城河 / 競爭優勢 → competitive moat, competitive advantage
- 燒錢 / 花費 → cash burn, operating expenses
- 本益比 → P/E ratio, price-to-earnings
- 財報 / 年報 / 季報 → 10-K (annual report), 10-Q (quarterly report)
- 展望 / 預期 / 財測 → guidance, outlook, forward-looking statements
- 風險 → risk factors
- 研發 → R&D, research and development
"""

# 每家公司的實際產品/分部名稱，只從語料本身（10-K/10-Q 原文）grep 確認過的詞彙收錄，
# 不臆測（見 CHANGELOG 2026-07-08：sem-02 失敗是因為 rewrite 變體只猜得出抽象詞如
# "growth strategy"，猜不出語料實際用的具體產品/分部名如 "Copilot"/"segment"——
# rewrite 是盲猜任務，不該靠模型記憶硬猜，直接把答案表餵給它最便宜、最確定）。
# 查詢時偵測到單一 ticker 才注入對應清單，不是每次都把 7 家公司全塞進 prompt。
_COMPANY_PRODUCT_GLOSSARY: dict[str, list[str]] = {
    "MSFT": ["Copilot", "Microsoft 365 Copilot", "Azure", "Azure AI", "Azure OpenAI",
             "Dynamics 365", "Productivity and Business Processes segment",
             "Intelligent Cloud segment", "More Personal Computing segment"],
    "AMZN": ["AWS", "Alexa", "North America segment", "International segment"],
    "GOOGL": ["Google Search", "Google Cloud", "Google Network", "YouTube ads",
              "Gemini", "TPU", "Vertex AI", "Other Bets"],
    "META": ["Family of Apps", "Reality Labs", "Instagram", "WhatsApp", "Threads",
             "Reels", "Llama", "Meta AI"],
    "NVDA": ["Data Center segment", "Gaming segment", "Professional Visualization segment",
             "Automotive segment", "CUDA", "Blackwell", "H100", "H200", "DGX", "GPU"],
    "AAPL": ["iPhone", "Mac", "iPad", "Wearables", "Services segment", "App Store"],
    "TSLA": ["Full Self-Driving", "FSD", "Autopilot", "Optimus", "Cybertruck", "Robotaxi",
             "Powerwall", "Megapack", "automotive segment",
             "energy generation and storage segment"],
}


def _company_glossary_note(query: str) -> str:
    """偵測 query 是哪家公司，回傳該公司已知產品/分部詞彙清單（供 rewrite prompt 注入）。
    無命中或多命中歧義時回傳空字串，維持原行為（不硬塞）。"""
    ticker = _detect_ticker(query)
    terms = _COMPANY_PRODUCT_GLOSSARY.get(ticker or "")
    if not terms:
        return ""
    return (
        f"\nKnown product/segment names actually used in {ticker}'s filings: "
        f"{', '.join(terms)}.\n"
        f"For BROAD questions (strategy, direction, competitive positioning — not a single "
        f"metric like revenue/margin):\n"
        f"- If the question ITSELF already names distinct sub-topics (e.g. \"competition AND "
        f"regulation\", \"growth AND risks\"), each variant must stay anchored to ONE of those "
        f"sub-topics AND map it to EXACTLY ONE list item — the single most specific match (e.g. "
        f"a regulatory sub-topic about self-driving maps to FSD/Autopilot ONLY, not to an "
        f"unrelated segment like energy storage). Do NOT hedge by naming two-or-more list items "
        f"in one variant \"to be safe\" — that dilutes the retrieval signal for all of them. Do "
        f"NOT pick a list item just because it wasn't used yet if it doesn't fit the sub-topic.\n"
        f"- Only if the question names NO sub-topic of its own (a single open-ended ask), spread "
        f"the variants across DIFFERENT items from the list so they don't all name the same one "
        f"or two terms reworded (e.g. don't only ever mention Azure for Microsoft).\n"
    )


QUERY_REWRITE_SYSTEM_PROMPT = f"""\
You rewrite a user's question into standard US-financial / SEC-filing terminology so it \
better matches the vocabulary actually used in 10-K / 10-Q filings, earnings news, and \
financial statements. This improves both dense and (especially) sparse lexical retrieval.

CRITICAL — the indexed documents are ENGLISH SEC filings. Therefore:
- ALWAYS write the rewrites in ENGLISH, regardless of the question's language. A Chinese \
rewrite cannot match the English corpus and is useless.
- Translate the underlying CONCEPT into the financial TERM OF ART, do NOT do a literal \
word-for-word translation. Rewriting "蘋果賺不賺錢" into "Apple make money" or just \
"Apple" is wrong — the useful rewrite is "Apple net income / profitability / earnings". \
The goal is to surface the exact jargon a 10-K would use.

Glossary of colloquial → standard term mappings (extend with your own knowledge):
{_FIN_GLOSSARY}

REGISTER — separate from vocabulary, some questions are phrased as an open-ended STRATEGIC \
or QUALITATIVE ask (e.g. "what is X's future direction", "what problems does X face", "is X \
winning against competitors"). Merely translating this phrasing into English financial jargon \
is NOT enough if the phrasing itself stays strategic — a 10-K/10-Q MD&A section never answers \
"what's the direction" in the abstract; it reports concrete OPERATING RESULTS, PROFITABILITY, \
or RISK FACTORS (e.g. "segment operating income increased due to...", "increased competition \
could result in price reductions..."). A strategic-register query and its filing's actual \
answer can be worded so differently that even a perfect English translation still fails to \
retrieve it. For BROAD strategic/qualitative questions (future direction, outlook, "what \
problems/risks does X face", competitive positioning) — this applies EQUALLY to casual/colloquial \
phrasings of the same ask, not just formally-worded ones — at least one rewrite variant MUST \
recast the question into this accounting/performance-narrative register — ask about the \
segment's or company's operating income / profitability trend, or its risk factor disclosures, \
instead of asking about "direction" or "problems" in the abstract. This does not apply to \
questions that are already narrow/specific (e.g. a single metric like gross margin or EPS) — \
those need only the vocabulary rules above, not a register shift.

The register-shifted variant must use CONCRETE causal/quantifiable MD&A language, not just a \
section label. Prefer terms like: operating income, operating margin, profitability, gross \
margin, cost pressure, pricing pressure / price reductions, capital expenditures, revenue \
growth driven by [X] partially offset by [Y] — over abstract nouns like "direction", \
"strategy", "challenges", "concerns", or "positioning" alone. "Risk factors" by itself is too \
abstract — pair it with the concrete driver (e.g. "competitive pricing pressure and market \
share risk", not just "risk factors").

Rules:
- Produce 1-2 ALTERNATIVE phrasings that preserve the EXACT same intent, only swapping \
colloquial / vague / cross-lingual wording for standard ENGLISH financial terms.
- For BROAD strategic/qualitative questions, apply the REGISTER shift above to at least one variant.
- Keep company names (use the English name / ticker) and any explicit period / figure unchanged.
- Do NOT add new constraints, do NOT answer the question, do NOT broaden the scope.
- If the question is ALREADY in standard English financial terminology, return an empty list.
- Output ONLY a JSON object on a SINGLE line: {{"rewrites": ["...", "..."]}} — no markdown, \
no code fences, no explanation, no trailing text.
"""

# Few-shot 對話範例：教 8B 模型穩定輸出 JSON、且把白話概念轉成「英文金融術語」。
# 以 user/assistant 真實對話輪次注入（比塞在 system prompt 裡更能穩住格式）。
_QUERY_REWRITE_FEWSHOT = [
    {"role": "user", "content": "蘋果除了賣手機，還靠什麼一直在收錢？這塊以後會更賺嗎？"},
    {"role": "assistant", "content": '{"rewrites": ["Apple Services segment revenue growth and gross margin", "Apple Services business future growth potential and high-margin recurring revenue"]}'},
    {"role": "user", "content": "為什麼大家做 AI 都要買輝達的卡，別家一直追不上它？"},
    {"role": "assistant", "content": '{"rewrites": ["NVIDIA competitive moat and CUDA software ecosystem", "NVIDIA data center GPU competitive advantages and full-stack platform lock-in"]}'},
    {"role": "user", "content": "微軟賣東西，扣掉成本之後大概還能留下幾成？"},
    {"role": "assistant", "content": '{"rewrites": ["Microsoft gross margin and gross profit percentage", "Microsoft cost of revenue and gross profit ratio"]}'},
    {"role": "user", "content": "Google 在搜尋和 AI 領域接下來要怎麼佈局？"},
    {"role": "assistant", "content": '{"rewrites": ["Google Search and Google Cloud segment revenue and operating margin trends", "Google AI and Cloud investment spending, capital expenditures, and operating income performance"]}'},
    {"role": "user", "content": "NVDA gross margin"},
    {"role": "assistant", "content": '{"rewrites": []}'},
]


def rewrite_query(query: str, model_name: str = DEFAULT_MODEL, max_variants: int = 2) -> list[str]:
    """把口語 / 模糊 / 跨語言的問法改寫成標準金融術語，回傳 0..N 個等義改寫變體。
    若問題本身已是標準用詞（或解析失敗）回傳 []，呼叫端等同只用原 query。
    偵測到問題單一鎖定某家公司時，把該公司實際的產品/分部詞彙表（見
    _COMPANY_PRODUCT_GLOSSARY）注入 system prompt，讓 rewrite 不用盲猜語料的具體用詞
    （見 CHANGELOG 2026-07-08：這是 sem-02 rewrite 撈不到 Copilot chunk 的根因修法）。"""
    import json
    import re as _re

    system_prompt = QUERY_REWRITE_SYSTEM_PROMPT + _company_glossary_note(query)

    try:
        raw = call_llm(
            [
                {"role": "system", "content": system_prompt},
                *_QUERY_REWRITE_FEWSHOT,
                {"role": "user", "content": query},
            ],
            model_name,
        )
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        # 8B 模型常在合法 JSON 後面多吐解釋文字（"Extra data"）→ 只取第一個 {...} 區塊；
        # strict=False 容忍字串內的裸換行（"Invalid control character"）。
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        parsed = json.loads(m.group(0) if m else raw, strict=False)
    except Exception as e:
        print(f"WARN  - query rewrite failed ({e!r}); using original query only")
        return []

    rewrites = parsed.get("rewrites", []) if isinstance(parsed, dict) else []
    out: list[str] = []
    for r in rewrites:
        if isinstance(r, str) and r.strip() and r.strip() != query.strip():
            out.append(r.strip())
    return out[:max_variants]


def _looks_english(text: str) -> bool:
    """粗略判斷 query 是否已是英文——非 ASCII（CJK）字元佔比極低則視為英文，
    可省掉一次翻譯 LLM 呼叫。"""
    non_ascii = sum(1 for ch in text if ord(ch) > 0x2000)  # CJK/全形符號
    return non_ascii == 0


TRANSLATE_EN_SYSTEM_PROMPT = (
    "You are a financial-domain translator. Translate the user's question into concise, "
    "natural English using standard US-equities / SEC-filing terminology (e.g. '價格戰'→'price "
    "competition / price war', '監管風險'→'regulatory risk', '獲利引擎'→'profit driver'). "
    "Output ONLY the English translation, no quotes, no explanation. If the input is already "
    "English, return it unchanged."
)


def translate_query_to_english(query: str, model_name: str = DEFAULT_MODEL) -> str:
    """把 rerank 用的 query 翻成英文，讓 cross-encoder 與（幾乎全英文的）語料同語言配對。
    見 CHANGELOG 2026-07-07「cross-lingual rerank」發現：bge-reranker-v2-m3 對「繁中 query
    ↔ 英文文檔」評分嚴重失真（sem-11 命中 chunk raw 0.39→翻英文後 0.94）。
    query 已是英文則原樣返回；翻譯失敗時 fallback 回原 query（不讓翻譯成為單點故障）。"""
    if _looks_english(query):
        return query
    try:
        en = call_llm(
            [
                {"role": "system", "content": TRANSLATE_EN_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            model_name,
        ).strip()
        en = en.strip('"').strip()
        # 正規化特殊 unicode dash/hyphen（‑ non-breaking hyphen 等）→ ASCII '-'，
        # 避免 Windows cp950 終端 print 時 UnicodeEncodeError，也讓 tokenizer 輸入更乾淨。
        for ch in ("‐", "‑", "‒", "–", "—", "―"):
            en = en.replace(ch, "-")
        return en or query
    except Exception as e:
        print(f"WARN  - rerank query translation failed ({e!r}); using original query")
        return query


def build_qdrant_filter(filters: list[dict], strict: bool = False):
    """把 parse_query_filters() 的結果轉成 qdrant_client.models.Filter。

    strict=True（Tier 1）：include 條件完全不加 IsEmptyCondition，確保只撈到
      明確命中所有指定欄位的 chunk——用於 filing 識別碼明確的查找型 query。
      fiscal_year 仍然同時比對 report_label_year（雙座標系 OR）。

    strict=False（Tier 3，預設）：include 條件包成 should=[match OR IsEmpty(field)]，
      讓沒有 filing metadata 的 News/Fundamentals/IncomeStatement chunk 也能進候選。

    exclude 條件兩種模式都維持單純 must_not。
    """
    from qdrant_client import models

    must, must_not = [], []
    for f in filters:
        field, value = f["field"], f["value"]
        condition = models.FieldCondition(key=field, match=models.MatchValue(value=value))

        if f["polarity"] == "exclude":
            must_not.append(condition)
            if field == "fiscal_year":
                must_not.append(models.FieldCondition(
                    key="report_label_year", match=models.MatchValue(value=value),
                ))
            continue

        if strict:
            # Tier 1：精確命中，fiscal_year 加 report_label_year OR
            if field == "fiscal_year":
                must.append(models.Filter(should=[
                    condition,
                    models.FieldCondition(key="report_label_year", match=models.MatchValue(value=value)),
                ]))
            else:
                must.append(condition)
        else:
            # Tier 3：欄位不存在的 chunk 也放行（IsEmpty OR 值相符）
            should = [condition, models.IsEmptyCondition(is_empty=models.PayloadField(key=field))]
            if field == "fiscal_year":
                should.append(models.FieldCondition(
                    key="report_label_year", match=models.MatchValue(value=value),
                ))
            must.append(models.Filter(should=should))

    if not must and not must_not:
        return None
    return models.Filter(must=must or None, must_not=must_not or None)


def _build_tier2_filter(filters: list[dict]):
    """Tier 2：只保留 ticker + filing_type（放掉年份/期碼），strict 模式。
    讓「問 2026 但庫裡只有 2025」退回最新一份 filing，而不是拒答。"""
    tier2 = [f for f in filters
             if f["field"] in ("ticker", "filing_type") and f["polarity"] == "include"]
    return build_qdrant_filter(tier2, strict=True) if tier2 else None


def _build_fallback_note(filters: list[dict], fused_points: list) -> str:
    """Tier 2 命中後，偵測實際撈到的最新期間，回傳提示字串。"""
    year_f   = next((f for f in filters if f["field"] == "fiscal_year"        and f["polarity"] == "include"), None)
    period_f = next((f for f in filters if f["field"] == "report_period_code" and f["polarity"] == "include"), None)
    if not (year_f or period_f):
        return ""  # 沒有年份/期碼約束，不算 fallback

    requested = (year_f or period_f)["value"]
    ticker_f  = next((f for f in filters if f["field"] == "ticker"       and f["polarity"] == "include"), None)
    type_f    = next((f for f in filters if f["field"] == "filing_type"  and f["polarity"] == "include"), None)

    actual = set()
    for p in fused_points:
        pl = p.payload or {}
        for key in ("fiscal_year", "report_label_year", "report_period_code"):
            if pl.get(key):
                actual.add(pl[key])

    actual_str = ", ".join(sorted(actual, reverse=True)) if actual else "unknown"
    scope_parts = [f["value"] for f in [ticker_f, type_f] if f]
    scope = " ".join(scope_parts) or "the requested filing"

    return (
        f"[Note: The knowledge base does not contain {scope} for the requested period "
        f"({requested}). The following answer is based on the most recent available data "
        f"(period: {actual_str}).]"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Hybrid Retrieval via Qdrant (server-side RRF) + client-side rerank
# ══════════════════════════════════════════════════════════════════════════════

def retrieve(query: str, bge_m3, rerank_model, client, top_k: int = DEFAULT_TOP_K,
             model_name: str = DEFAULT_MODEL, enable_rewrite: bool = False,
             disable_filter: bool = False, rewrite_merge_top_n: int | None = None,
             rewrite_fusion: bool = False, return_pool: bool = False,
             rerank_multi_query: bool = False, translate_query_en: bool = False,
             sparse_translate_en: bool = False):
    """回傳 (chunks, fallback_note) 或 (chunks, fallback_note, pool)。
    fallback_note 為空字串表示正常命中；非空表示退回了次佳 tier，內含給使用者的說明。
    disable_filter=True 時完全跳過 query-understanding hard filter（不呼叫
    parse_query_filters），用於 eval 對照「filter vs nofilter」對檢索品質的影響——
    正常使用情境下不應該設成 True。
    return_pool=True 時額外回傳精排前的完整候選池（list of ScoredPoint），
    供兩階段 eval 量 pre-rerank recall@K。
    rerank_multi_query=True 時（需同時 enable_rewrite=True），cross-encoder 精排
    改用「原 query + 各 rewrite 變體」逐一評分、每個候選取跨 query 的最高分（實驗性，
    見 CHANGELOG sem-02/08 排序診斷——量化財務段落把質化策略敘述擠出 top-k 的問題，
    不是候選數不夠，而是原 query 字面對 reranker 不利；變體提供的策略/定位措辭框架
    可能救回這類候選，不需要動 RERANK_INPUT_N / top_k）。**已試無效**（sem-11 驗證：
    變體仍是中文，沒解決真正病灶 cross-lingual rerank，見下方 translate_query_en），
    保留參數但預設 False、不建議啟用。
    translate_query_en=True 時，在檢索最上游把 query 翻成英文一次（LLM temp=0，單一
    真相來源），**cross-encoder rerank 對每個候選同時用原始 query 與這個英文翻譯各
    評一次分、取逐候選最高分**——解決 cross-lingual reranker 失真（見 CHANGELOG
    2026-07-07：sem-11 決定性實驗 raw 0.39→0.94）。2026-07-12 前的舊行為是直接把
    評分 query「整組換成」翻譯版（全有全無），實測會讓 sem-11 進 top-5 但把 sem-09
    需要的一個純英文 10-K chunk 擠出去；改成逐候選取最高分後，兩題同時通過（見
    CHANGELOG 2026-07-12「雙 query 取最高分」）。
    dense/sparse 召回、query rewrite 的輸入仍用原始 query（實測踩坑，見 CHANGELOG
    2026-07-08）：① rewrite_query 若餵翻譯後英文會被其 prompt 自身的「已標準則不
    改寫」規則擋下，變體歸零；② dense/sparse 召回若也用翻譯後 query，sem-11 從
    OK(in top-k) 退步成 RANK——BGE-M3 dense 本身已是多語言、翻譯反而引入 paraphrase
    drift 改變候選池組成。query-understanding hard filter（parse_query_filters）與
    呼叫端的答案生成同樣用原始 query。目前預設 False，待全量 eval 驗證無回歸後再
    考慮轉正式預設。
    sparse_translate_en=True 時（實驗性，見 CHANGELOG 2026-07-08 sem-08/11 探針實驗），
    dense 召回仍用原始 query，但 sparse 召回改用翻譯後英文——動機：sparse 是純 lexical
    token 比對，中文 query 的 token 與英文語料幾乎零重疊，翻譯理論上該對 sparse 有幫助
    但對 dense 有害（dense 本身多語言，翻譯只引入 paraphrase drift，2026-07-08 已證實）。
    探針結果混合：sem-11 FSD chunk（#56）sparse 中文完全查無此 chunk（top-60 外），翻英文
    後進 rank 26——真實有幫助；sem-08 AWS chunk（#100）翻譯前後 rank 21~26 幾乎不動——
    該 chunk 是稀釋型大 chunk（混雜 FTC 訴訟/稅務/資產減損等其他主題），語言不是瓶頸。
    結論：sparse_translate_en 不是萬靈丹，對「語言是瓶頸」的 chunk 有效、對「內容稀釋」
    的 chunk 無效，需個案診斷。"""
    from qdrant_client import models

    def _empty():
        return ([], "", []) if return_pool else ([], "")

    # Sanity check
    try:
        count = client.count(collection_name=COLLECTION_NAME, exact=True).count
    except Exception:
        print(f"[ERROR] Collection '{COLLECTION_NAME}' not found. "
              f"Run: python data_update.py --rebuild")
        return _empty()
    if count == 0:
        return _empty()

    # ── 0. Query understanding → hard payload filter (filing_type/fiscal_year/period)
    # 用原始 query（結構化代碼抽取，語言無關——ticker/filing_type/fiscal_year 不管中英文
    # 問法都對應同一組代碼，翻譯與否不影響 filter 準確度，見 CHANGELOG 2026-07-08）。
    detected_filters = [] if disable_filter else parse_query_filters(query, model_name)
    if detected_filters:
        print(f"DEBUG - Detected filing filter(s): {detected_filters}")

    # ── 0b. 入口翻譯（單一真相來源，只呼叫一次）───────────────────────────────
    # translate_query_en=True 時只翻這一次，供下游需要英文的環節取用；但「翻了就該
    # 全部環節都用」實測是錯的（見 CHANGELOG 2026-07-08 兩個踩坑）：
    #   1. rewrite_query 的輸入若已是翻譯後的標準英文，會被其 prompt 自身的「已標準
    #      則回傳空列表」規則擋下，變體歸零——已改回餵原始 query（見下方 2b）。
    #   2. dense/sparse 召回若也用翻譯後 query，sem-11 從 OK(in top-k) 退步成
    #      RANK（pool 有但被擠出 top-5）——BGE-M3 dense 本身已是多語言、中文召回
    #      不需要翻譯，翻譯反而引入一次 paraphrase drift 改變候選池組成。
    #      故意只留 rerank_query 用 en_query，dense/sparse encode 仍用原始 query。
    en_query = translate_query_to_english(query, model_name) if translate_query_en else query
    if translate_query_en and en_query != query:
        # ascii-safe：Windows cp950 終端無法印某些字元，debug 訊息不值得為此崩潰
        _safe = en_query.encode("ascii", "replace").decode()
        print(f"DEBUG - Query translated to EN (rerank only): {_safe!r}")

    # ── 1. Encode query → dense + sparse（helper，供原 query 與改寫變體共用）────
    def _encode(text: str) -> dict:
        enc = bge_m3.encode(
            [text], return_dense=True, return_sparse=True, return_colbert_vecs=False,
        )
        sp = enc["lexical_weights"][0]
        return {
            "dense":   enc["dense_vecs"][0].tolist(),
            "indices": [int(tok) for tok in sp.keys()],
            "values":  [float(w)  for w   in sp.values()],
        }

    def _query_points(vecs: dict, active_filter):
        return client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=[
                models.Prefetch(
                    query=vecs["dense"],
                    using=DENSE_VECTOR_NAME,
                    limit=FETCH_N,
                    filter=active_filter,
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=vecs["indices"], values=vecs["values"]),
                    using=SPARSE_VECTOR_NAME,
                    limit=FETCH_N,
                    filter=active_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=RRF_TOP_N_PRIMARY,
            with_payload=True,
        ).points

    q_vecs = _encode(query)  # 召回一律用原始 query（見上方 0b 說明：翻譯對 dense/sparse 中性偏負）
    if sparse_translate_en:
        # dense 保留原始 query 的向量；sparse 換成翻譯後英文的向量（實驗性，見上方 docstring）。
        sparse_source = en_query if translate_query_en else translate_query_to_english(query, model_name)
        if sparse_source != query:
            sparse_vecs = _encode(sparse_source)
            q_vecs = {"dense": q_vecs["dense"], "indices": sparse_vecs["indices"], "values": sparse_vecs["values"]}

    # ── 2. 三層降級檢索 ───────────────────────────────────────────────────────
    # Tier 1：嚴格 filter（不加 IsEmpty）——filing 識別碼完整，精準命中目標 filing
    #
    # 特例：有 report_period_code 時，只用 ticker + report_period_code 做 Tier 1。
    # 原因：report_period_code（yyyymm）已足以唯一定位 filing，且 fiscal_year
    # 由 LLM 從問句抽出的往往是「曆年」，而 AAPL/META 的 XBRL fiscal_year 是
    # 「fiscal 年度」——兩者常差 1，若同時帶入 strict filter 反而衝突導致 0 命中。
    fallback_note = ""
    fused = []
    winning_filter = None  # 勝出那層實際使用的 filter，供 rewrite 變體沿用
    rerank_query = en_query  # debug 訊息與單一 query fallback 用
    # translate_query_en=True 時，rerank 對每個候選同時用「原始 query」與「翻譯後
    # query」各評一次分、取逐候選最高分——而不是直接把評分 query 換成翻譯版（舊行為）。
    # 見 CHANGELOG 2026-07-12「雙 query 取最高分」探針：舊的全有全無替換會讓 sem-11
    # 進 top-5，但同時把 sem-09 需要的一個純英文 10-K chunk（#139）擠出去；改成每個
    # 候選各自取「原句/翻譯句」較高分後，兩題同時通過（每個候選各自挑對自己有利的
    # 語言，不再是全域二選一的 trade-off）。
    if translate_query_en and en_query != query:
        rerank_queries = [query, en_query]
    else:
        rerank_queries = [rerank_query]  # rerank_multi_query=True 時，額外併入 rewrite 變體

    if detected_filters:
        has_period_code = any(f["field"] == "report_period_code" and f["polarity"] == "include"
                              for f in detected_filters)
        if has_period_code:
            # 只保留 ticker + report_period_code（唯一定位，排除衝突的年份欄位）
            tier1_key_filters = [f for f in detected_filters
                                 if f["field"] in ("ticker", "report_period_code")
                                 and f["polarity"] == "include"]
        else:
            tier1_key_filters = detected_filters

        tier1_filter = build_qdrant_filter(tier1_key_filters, strict=True)
        fused = _query_points(q_vecs, tier1_filter)
        if fused:
            winning_filter = tier1_filter
            print("DEBUG - Tier 1 (strict filter): hit")
        else:
            # Tier 2：放掉年份/期碼，只留 ticker+filing_type，取「最新一份」
            tier2_filter = _build_tier2_filter(detected_filters)
            if tier2_filter is not None:
                fused = _query_points(q_vecs, tier2_filter)
                if fused:
                    winning_filter = tier2_filter
                    fallback_note = _build_fallback_note(detected_filters, fused)
                    print(f"DEBUG - Tier 2 (drop year, keep ticker+type): hit | note: {fallback_note}")

    if not fused:
        # Tier 3：軟 filter（IsEmpty 放行 News/Fundamentals）或完全不過濾
        tier3_filter = build_qdrant_filter(detected_filters, strict=False) if detected_filters else None
        fused = _query_points(q_vecs, tier3_filter)
        if fused:
            winning_filter = tier3_filter
        elif tier3_filter is not None:
            print("WARN  - Tier 3 soft filter returned 0; retrying without any filter")
            fused = _query_points(q_vecs, None)
            winning_filter = None
        if fused:
            print("DEBUG - Tier 3 (soft filter / no filter): hit")

    if not fused:
        return _empty()

    # ── 2b. Query rewrite 擴召回（multi-query）────────────────────────────────
    # 用改寫變體在「勝出那層 filter」上額外檢索，合併進候選池（依 point id 去重，
    # 保留較高的 RRF 分數）。最終 rerank 仍用 rerank_query，故只擴召回、不改變意圖。
    #
    # 注意：這裡故意用「原始 query」而非 en_query 當 rewrite 輸入——實測踩坑（見
    # CHANGELOG 2026-07-08）：QUERY_REWRITE_SYSTEM_PROMPT 明文「若問題已是標準英文
    # 金融術語，回傳空列表」；translate_query_to_english() 的翻譯目標正是「標準英文
    # 金融術語」，兩者職責重疊，若餵 en_query（已翻譯）會被 rewrite 誤判「已標準」
    # 而回傳空變體，白白喪失多查詢擴召回（sem-11 pool 20→22 的來源）。原始 query
    # （通常是中文）永遠不會被判定為「已標準英文」，能穩定觸發變體生成。
    if enable_rewrite:
        variants = rewrite_query(query, model_name)
        if variants:
            # 變體只用來「擴召回」把更多候選撈進池；rerank_query 維持 en_query，
            # 讓它保底——變體漂移時不會污染送進 LLM 的最終排序（與上方設計說明一致）。
            print(f"DEBUG - Query rewrite variants (recall expansion only): {variants}")
            if rerank_multi_query:
                rerank_queries += variants
            if rewrite_fusion:
                # ── 跨變體 RRF-fusion（RAG-Fusion）─────────────────────────────
                # 把「原 query 的 fused」與「每個變體各自的候選 list」都當成一路排名，
                # 對每個 chunk 用 Σ 1/(k+rank) 累加。多個變體都排前面的 chunk 會被疊高、
                # 只在單一變體衝高的會被壓低 → 天然抑制單一變體灌水，取共識最高的前
                # RERANK_INPUT_N 進 reranker（不需 cap 這個魔術數字）。
                RRF_K = 60
                ranked_lists = [fused] + [_query_points(_encode(vq), winning_filter)
                                          for vq in variants]
                rrf_scores: dict = {}
                point_by_id: dict = {}
                for lst in ranked_lists:
                    for rank, p in enumerate(lst):
                        rrf_scores[p.id] = rrf_scores.get(p.id, 0.0) + 1.0 / (RRF_K + rank + 1)
                        point_by_id.setdefault(p.id, p)
                # 用「全域融合分數」排序後取前 RERANK_INPUT_N（≥ union，公平地與 cap/no-cap
                # 餵同寬的 reranker 輸入；不再用舊的 RRF_TOP_N=20 在精排前誤殺召回）
                top_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:RERANK_INPUT_N]
                before = len(fused)
                fused = [point_by_id[i] for i in top_ids]
                for p in fused:            # 把跨路累加分數寫回，供下方 debug/enriched 顯示
                    p.score = rrf_scores[p.id]
                print(f"DEBUG - Rewrite RRF-fusion: {before} → {len(fused)} candidates "
                      f"(fused across {len(ranked_lists)} query lists, top {RERANK_INPUT_N})")
            else:
                # 原 query 是最可信的一路，全部保留（RRF_TOP_N_PRIMARY 個，不 cap）
                by_id = {p.id: p for p in fused}
                # 變體只注入各自最有信心的 top-VARIANT_CAP（控噪閘門）
                # score 是各路各自的 RRF 分、跨路不可比，用 setdefault 去重即可；
                # 最終分數由 cross-encoder reranker 從頭重算，merge 階段不需比大小。
                cap = rewrite_merge_top_n if rewrite_merge_top_n is not None else VARIANT_CAP
                for vq in variants:
                    for p in _query_points(_encode(vq), winning_filter)[:cap]:
                        by_id.setdefault(p.id, p)
                before = len(fused)
                fused = list(by_id.values())
                print(f"DEBUG - Rewrite merge: {before} → {len(fused)} candidates (cap={cap}/variant)")
        else:
            print("DEBUG - Query rewrite: no variants (query already standard); original only")

    print(f"DEBUG - Qdrant returned {len(fused)} fused candidates (server-side RRF):")
    for i, p in enumerate(fused, start=1):
        payload = p.payload or {}
        print(f"  [{i}] {payload.get('source')} | chunk #{payload.get('chunk_index')} | "
              f"rrf_score={p.score:.6f}")

    # 精排前的候選池（供 return_pool=True 的呼叫端量 pre-rerank recall@K）
    pre_rerank_pool = list(fused)

    # ── 3. Cross-encoder rerank ──────────────────────────────────────────────
    docs = [(p.payload or {}).get("document", "") for p in fused]
    # batch_size=1：CPU 上消除 padding 浪費的免費加速（2026-07-13 實測 2.33x、top-5 不變）。
    # 預設 batch=32 會把整批 pad 到批內最長（常常是 2048 token），中位數 253 token 的
    # 短候選被迫多算數倍的 O(L²) attention；CPU 的矩陣運算單筆就能吃滿核心，批次
    # 平行沒有額外收益，逐筆跑純賺。若未來換 GPU，這個值應改回預設（GPU 吃批次平行）。
    if len(rerank_queries) > 1:
        # 逐一用「原 query + 各 rewrite 變體」評分，每個候選取跨 query 的最高分。
        # 只換評分用的 query 措辭，不動候選數（RERANK_INPUT_N/top_k 不變）。
        raw_scores = [float("-inf")] * len(docs)
        for rq in rerank_queries:
            cross_input = [[rq, doc] for doc in docs]
            scores = rerank_model.predict(cross_input, batch_size=1)
            scores = scores.tolist() if hasattr(scores, "tolist") else list(scores)
            raw_scores = [max(a, b) for a, b in zip(raw_scores, scores)]
    else:
        cross_input = [[rerank_query, doc] for doc in docs]
        raw_scores  = rerank_model.predict(cross_input, batch_size=1)
        raw_scores  = raw_scores.tolist() if hasattr(raw_scores, "tolist") else list(raw_scores)

    enriched = []
    for p, raw in zip(fused, raw_scores):
        payload = p.payload or {}
        source  = payload.get("source", "unknown")
        enriched.append({
            "content":          payload.get("document", ""),
            "source":           source,
            "source_type":      infer_source_type(source),
            "chunk_index":      payload.get("chunk_index", 0),
            "chunk_type":       payload.get("chunk_type", "n/a"),
            "rrf_score":        float(p.score),
            "raw_rerank_score": float(raw),
            "rerank_score":     float(1 / (1 + math.exp(-raw))),
        })

    enriched.sort(key=lambda x: x["raw_rerank_score"], reverse=True)

    print("DEBUG - Reranked top candidates:")
    for i, c in enumerate(enriched[:top_k], start=1):
        print(
            f"  [{i}] {c['source']} | chunk #{c['chunk_index']} | "
            f"type={c['source_type']} | rrf={c['rrf_score']:.6f} | "
            f"raw={c['raw_rerank_score']:.4f} | sigmoid={c['rerank_score']:.4f}"
        )

    if looks_like_news_query(query) and not any(c["source_type"] == "news" for c in enriched):
        print("WARN  - This looks like a news query, but no news chunks were retrieved.")

    if return_pool:
        return enriched[:top_k], fallback_note, pre_rerank_pool
    return enriched[:top_k], fallback_note


# ══════════════════════════════════════════════════════════════════════════════
# Contextual compression（檢索後句級抽取）
# ══════════════════════════════════════════════════════════════════════════════

COMPRESS_SYSTEM_PROMPT = """\
You are a precise evidence extractor for a financial RAG system. You receive a user \
QUESTION and several numbered REFERENCE passages. For EACH reference, extract the \
sentences (or minimal sentence fragments) that are directly relevant to answering the \
question — including any that state financial performance (operating income, profit \
contribution, revenue growth, margins) even when they are buried inside a passage that \
mostly discusses unrelated topics. This surfacing of buried but relevant sentences is \
the WHOLE POINT of your job.

Hard rules:
- Copy sentences VERBATIM from the reference. Do NOT paraphrase, summarize, translate, \
or invent. Every extracted string must appear character-for-character in that reference.
- If a reference has nothing relevant, return an empty list for it.
- Do NOT add commentary, numbers, or facts not present in the reference.

Output ONLY a JSON object mapping each reference number (as a string) to a list of \
verbatim extracted strings, e.g.:
{"1": ["<verbatim sentence>", "<verbatim sentence>"], "2": [], "3": ["<verbatim>"]}"""


def compress_chunks(query: str, chunks: list[dict], model_name: str = DEFAULT_MODEL) -> list[dict]:
    """檢索後句級抽取：一次 LLM 呼叫把每個 chunk 對 query 的相關句子逐字抽出，寫進該 chunk
    dict 的 'key_excerpts' 欄位（list[str]）。build_user_prompt() 會把有 key_excerpts 的 chunk
    把摘錄擺到全文前面（highlight 附加、不替換原文，最壞情況＝現狀）。

    設計（見 CHANGELOG 2026-07-14「方案 A」）：
    - 逐字抽取、禁止改寫 → 不在生成前引入幻覺（額外做一次 verbatim 驗證：抽出的字串必須真的
      是原 chunk 子字串，否則丟棄，防止抽取器自己幻覺）。
    - 一次呼叫打包全部 chunk（非逐 chunk）→ 延遲只 +1 次 LLM 呼叫。
    - temp=0、用 retrieval-side model（遵守溫度分工與 model_name 紀律）。
    - 任何失敗（LLM 掛掉 / JSON 解析失敗）→ 原樣回傳 chunks（不加 key_excerpts）＝現狀，
      不成為單點故障。"""
    import json
    import re as _re

    if not chunks:
        return chunks

    refs = "\n\n".join(
        f"[Reference {i+1}]\n{c['content'].strip()}" for i, c in enumerate(chunks)
    )
    user_msg = f"=== QUESTION ===\n{query}\n\n=== REFERENCES ===\n{refs}"

    try:
        raw = call_llm(
            [
                {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            model_name,
        )
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        parsed = json.loads(m.group(0) if m else raw, strict=False)
    except Exception as e:
        print(f"WARN  - contextual compression failed ({e!r}); using full chunks only")
        return chunks

    if not isinstance(parsed, dict):
        return chunks

    for i, c in enumerate(chunks):
        excerpts = parsed.get(str(i + 1), [])
        if not isinstance(excerpts, list):
            continue
        content = c["content"]
        # verbatim 防線：只留真的是原 chunk 子字串的抽取（抵禦抽取器自己改寫/幻覺）。
        kept = [s.strip() for s in excerpts
                if isinstance(s, str) and s.strip() and s.strip() in content]
        if kept:
            c["key_excerpts"] = kept
    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# Prompt assembly
# ══════════════════════════════════════════════════════════════════════════════

def build_user_prompt(query: str, chunks: list[dict], fallback_note: str = "") -> str:
    context_parts = []
    for i, c in enumerate(chunks):
        excerpts = c.get("key_excerpts")
        if excerpts:
            # highlight 附加：重點摘錄擺全文前面放大訊號；原文全保留（citation/期間提示不受影響）。
            excerpt_block = (
                "KEY EXCERPTS (verbatim, most relevant to the question):\n"
                + "\n".join(f"- {s}" for s in excerpts)
                + "\nFULL CONTEXT:\n"
            )
        else:
            excerpt_block = ""
        context_parts.append(
            f"[Reference {i+1}: {c['source']}, chunk #{c['chunk_index']}]\n"
            f"{excerpt_block}"
            f"{c['content'].strip()}"
        )
    context = "\n\n".join(context_parts)

    note_section = f"{fallback_note}\n\n" if fallback_note else ""

    # 期間代碼消歧義提示（見 CHANGELOG 2026-07-10）：使用者若在問題裡打了 YYYYMM 期間碼
    # （如「202512」），但 AAPL/MSFT/NVDA 等非日曆財年公司的來源文件本身用 fiscal quarter
    # 措辭（如「first quarter of fiscal 2026」），生成端若照抄來源用語、不回填使用者的
    # 期間碼，會被 judge（甚至真實使用者）誤讀成「引用錯季度」。這裡重用既有的
    # deterministic _PERIOD_CODE_RE（本來就用在 parse_query_filters 的結構化抽取），
    # 不需要額外 LLM 呼叫，直接把消歧義指示注入 prompt。
    period_note = ""
    m = _PERIOD_CODE_RE.search(query)
    if m:
        period_note = (
            f"\nNote: the user specified period code {m.group(1)} (YYYYMM format). Some "
            f"companies (e.g. AAPL, MSFT, NVDA) use a non-calendar fiscal year, so source "
            f"filings may label this same period using fiscal-quarter terms (e.g. \"first "
            f"quarter of fiscal 2026\") instead of the calendar code. When discussing this "
            f"period, explicitly restate the {m.group(1)} code (or its calendar month/quarter) "
            f"alongside any fiscal-quarter label the source uses, so the period is unambiguous.\n"
        )

    return (
        f"{note_section}"
        f"=== Reference Materials ===\n"
        f"{context}\n\n"
        f"=== Question ===\n"
        f"{query}\n"
        f"{period_note}\n"
        f"Please answer based solely on the reference materials above. "
        f"Cite every factual claim using [filename, chunk #N] format."
    )


def format_sources(chunks: list[dict]) -> str:
    lines = ["\n📚 Referenced Sources (Qdrant Hybrid: Dense + Sparse → RRF → Rerank):"]
    for i, c in enumerate(chunks):
        lines.append(
            f"  [{i+1}] {c['source']}  |  chunk #{c['chunk_index']}  "
            f"|  rrf={c['rrf_score']:.6f}  |  rerank={c['rerank_score']:.4f}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# LLM call
# ══════════════════════════════════════════════════════════════════════════════

def call_llm(messages: list[dict], model_name: str, temperature: float = 0.0) -> str:
    """依 model 名稱路由：'gemini-' 開頭走 Google GenAI（GEMINI_API_KEY），
    其餘走 Groq OpenAI-compatible（GROQ_API_KEY）。messages 為 OpenAI 格式
    （[{role, content}]），Gemini 路徑會就地轉成 contents + system_instruction。

    temperature 預設 0.0：filter / rewrite / judge 等「檢索與評分」呼叫要確定性、可重現。
    生成答案的呼叫端應顯式傳 GEN_TEMPERATURE(=0.3)——temp=0 的 greedy 生成會重複並漏 rubric 點。"""
    if model_name.startswith("gemini-"):
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        system_prompt = None
        conversation  = []
        for msg in messages:
            role    = msg["role"]
            content = msg["content"]
            if role == "system":
                system_prompt = content
            elif role == "user":
                conversation.append({"role": "user",  "parts": [{"text": content}]})
            elif role == "assistant":
                conversation.append({"role": "model", "parts": [{"text": content}]})

        # temperature=0：讓 filter / rewrite / 生成盡量可重現（factual RAG 不需要多樣性；
        # 也讓 eval A/B 時「改動」是唯一變數）。注意 temp=0 仍非完全 deterministic。
        config = types.GenerateContentConfig(
            temperature=temperature,
            **({"system_instruction": system_prompt} if system_prompt else {}),
        )

        response = client.models.generate_content(
            model=model_name,
            contents=conversation,
            config=config,
        )
        return response.text

    # 其餘一律走 Groq（OpenAI-compatible）；messages 已是 OpenAI 格式可直接送。
    import openai
    for _attempt in range(5):
        try:
            client = openai.OpenAI(api_key=_get_groq_key(), base_url=GROQ_BASE_URL)
            resp = client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)
            return resp.choices[0].message.content
        except Exception as _e:
            if _rotate_groq_key(str(_e)):
                continue
            raise


# ══════════════════════════════════════════════════════════════════════════════
# Query modes
# ══════════════════════════════════════════════════════════════════════════════

def run_single_query(query, retrieval_model, gen_model, top_k, bge_m3, rerank_model, client,
                     enable_rewrite=DEFAULT_ENABLE_REWRITE, translate_query_en=DEFAULT_TRANSLATE_QUERY_EN,
                     enable_compress=DEFAULT_ENABLE_COMPRESS):
    print(f"\n🔍 Query: {query}")
    print("⏳ Hybrid retrieval (Qdrant dense + sparse → RRF → rerank)...")

    chunks, fallback_note = retrieve(query, bge_m3, rerank_model, client, top_k,
                                     model_name=retrieval_model, enable_rewrite=enable_rewrite,
                                     translate_query_en=translate_query_en)
    if not chunks:
        print("❌ No chunks found. Run: python data_update.py --rebuild")
        return

    if fallback_note:
        print(f"⚠️  {fallback_note}")

    if enable_compress:
        print("✂️  Contextual compression (verbatim excerpt extraction)...")
        chunks = compress_chunks(query, chunks, retrieval_model)

    user_prompt = build_user_prompt(query, chunks, fallback_note)
    messages    = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    print(f"🤖 Calling {gen_model}...")
    answer = call_llm(messages, gen_model, temperature=GEN_TEMPERATURE)

    print(f"\n{'═'*60}")
    print("💡 Answer:")
    print(answer)
    print(format_sources(chunks))
    print("═" * 60)


def run_interactive(retrieval_model, gen_model, top_k, bge_m3, rerank_model, client,
                    enable_rewrite=DEFAULT_ENABLE_REWRITE, translate_query_en=DEFAULT_TRANSLATE_QUERY_EN,
                    enable_compress=DEFAULT_ENABLE_COMPRESS):
    print("\n🚀 US Stock Intelligence RAG — Qdrant Hybrid (Interactive)")
    print(f"   Retrieval model: {retrieval_model}  |  Gen model: {gen_model}  |  "
          f"Top-k: {top_k}  |  Rewrite: {'on' if enable_rewrite else 'off'}  |  "
          f"Translate-EN rerank: {'on' if translate_query_en else 'off'}  |  "
          f"Compress: {'on' if enable_compress else 'off'}")
    print("   Type your question, or 'quit' / 'exit' to exit.")
    print("═" * 60)

    history: list[dict] = []
    while True:
        try:
            query = input("\n❓ Question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye! 👋")
            break

        if not query:
            continue
        if query.lower() in {"quit", "exit", "q", "bye"}:
            print("Goodbye! 👋")
            break

        chunks, fallback_note = retrieve(query, bge_m3, rerank_model, client, top_k,
                                         model_name=retrieval_model, enable_rewrite=enable_rewrite,
                                         translate_query_en=translate_query_en)
        if not chunks:
            print("❌ No chunks found. Run: python data_update.py --rebuild")
            continue

        if fallback_note:
            print(f"⚠️  {fallback_note}")

        if enable_compress:
            print("✂️  Contextual compression (verbatim excerpt extraction)...")
            chunks = compress_chunks(query, chunks, retrieval_model)

        user_prompt = build_user_prompt(query, chunks, fallback_note)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history[-(MAX_HISTORY * 2):])
        messages.append({"role": "user", "content": user_prompt})

        print(f"🤖 Generating answer ({gen_model})...")
        answer = call_llm(messages, gen_model, temperature=GEN_TEMPERATURE)

        print(f"\n{'─'*60}")
        print("💡 Answer:")
        print(answer)
        print(format_sources(chunks))
        print("─" * 60)

        history.append({"role": "user",      "content": query})
        history.append({"role": "assistant", "content": answer})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    import sys
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="US Stock Intelligence — RAG Query (Qdrant Hybrid Retrieval)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python rag_query.py
  python rag_query.py --query "NVDA revenue growth?"
  python rag_query.py --query "..." --top-k 3
  python rag_query.py --query "..." --model gemini-2.5-flash
        """,
    )
    parser.add_argument("--query", "-q", type=str, default=None)
    parser.add_argument("--top-k", "-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--model", "-m", type=str, default=DEFAULT_MODEL,
                        help="Retrieval 模型（filter/rewrite/translate 用）。生成模型獨立由 --gen-model 控制。")
    parser.add_argument("--gen-model", type=str, default=DEFAULT_GEN_MODEL,
                        help="生成答案用的模型；預設 openai/gpt-oss-120b（見 CHANGELOG 2026-07-09 "
                             "乾淨隔離 A/B：retrieval 固定 70b、k=3 全量 11 題 semantic 零回歸、"
                             "mean 0.627→0.870）。retrieval 側維持 --model 不變——兩者是獨立維度。")
    parser.add_argument("--rewrite", action=argparse.BooleanOptionalAction,
                        default=DEFAULT_ENABLE_REWRITE,
                        help="query rewrite（口語→標準金融術語，multi-query 擴召回）；生產預設開啟，"
                             "用 --no-rewrite 關閉")
    parser.add_argument("--translate-query-en", action=argparse.BooleanOptionalAction,
                        default=DEFAULT_TRANSLATE_QUERY_EN,
                        help="rerank 用「原句 + 英文翻譯」逐候選取最高分（解 cross-lingual rerank 失真，"
                             "見 CHANGELOG 2026-07-12/13）；生產預設開啟，用 --no-translate-query-en 關閉")
    parser.add_argument("--compress", action=argparse.BooleanOptionalAction,
                        default=DEFAULT_ENABLE_COMPRESS,
                        help="檢索後句級抽取：生成前把每個 chunk 的相關句逐字抽出擺前面（見 CHANGELOG "
                             "2026-07-14）。實驗性，**預設關**（未做全量回歸），用 --compress 開啟")
    args = parser.parse_args()
    gen_model = args.gen_model

    from FlagEmbedding import BGEM3FlagModel
    from sentence_transformers import CrossEncoder

    print(f"[INFO] Loading BGE-M3 (dense+sparse): {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)

    print(f"[INFO] Loading reranker: {RERANK_MODEL}")
    rerank_model = CrossEncoder(RERANK_MODEL, max_length=RERANK_MAX_LENGTH)

    client = make_qdrant_client()

    if args.query:
        run_single_query(args.query, args.model, gen_model, args.top_k,
                         bge_m3, rerank_model, client, enable_rewrite=args.rewrite,
                         translate_query_en=args.translate_query_en, enable_compress=args.compress)
    else:
        run_interactive(args.model, gen_model, args.top_k,
                        bge_m3, rerank_model, client, enable_rewrite=args.rewrite,
                        translate_query_en=args.translate_query_en, enable_compress=args.compress)


if __name__ == "__main__":
    main()
