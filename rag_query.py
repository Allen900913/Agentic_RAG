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
import threading

import llm_replay as _replay
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
QDRANT_PATH      = os.getenv("QDRANT_PATH", "./qdrant_db")
QDRANT_URL       = os.getenv("QDRANT_URL", "")   # 若設定則走 server mode（Docker）
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL     = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# 生產 collection。用 env 覆蓋才能在不改碼的情況下跑 collection A/B（gold 生成、eval、
# agentic 全都是 import rag_query 取這個常數，改碼跑完忘了改回來是實際發生過的風險）。
# 2026-08-27 升生產：`us_stock_rag_edgar_mdna`（21 份 filing、單年）→ `..._multiyear`
# （77 份、每家 3×10-K ＋ 8×10-Q 橫跨 ~2.5 年）。收益／損害兩半的證據見 docs/EVAL.md
# 〈多年語料買到了什麼〉與〈階段 4〉。⚠ 這一次同時翻了 `RQ_PERIOD_INTENT_LLM` 的預設
# （見下方 retrieve() 內），**兩者必須一起**：多年語料上線而 A 沒開，當期題會退步。
COLLECTION_NAME  = os.getenv("RAG_COLLECTION", "us_stock_rag_edgar_multiyear")
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
DEFAULT_MODEL   = "openai/gpt-oss-120b"       # retrieval 側預設（filter/rewrite/translate）；2026-07-21 統一改走 NVIDIA NIM
                                               # （單一 OpenAI-compatible endpoint、單把 NVIDIA_API_KEY，取代 Groq 4-key TPD
                                               # 輪換）。見 agentic_rag_nv.py 已驗證的模型選型：gpt-oss-120b 在 NVIDIA 上快
                                               # 且合法；meta/llama-3.3-70b-instruct 反而會 timeout，不可用。
DEFAULT_GEN_MODEL = "openai/gpt-oss-120b"     # 生成答案側預設（見 CHANGELOG 2026-07-09 乾淨隔離 A/B：
                                               # retrieval 固定 70b、只換 gen model，120b 對 k=3 全量 11 題 semantic
                                               # 零回歸、mean 0.627→0.870 且更穩定，故轉正式預設）。此 model id 在 NVIDIA NIM
                                               # 目錄下同名，換 backend 不必換 id。
GEN_TEMPERATURE = 0.3    # 生成答案用；檢索側（filter/rewrite）與 judge 一律 temp=0（call_llm 預設）。
                         # 實測：生成用 temp=0（greedy）會重複/mode collapse，反而漏 rubric 點（見 CHANGELOG 2026-07-06）。
# 生產預設：只用「1 query + translate_query_en 雙 query 取最高分 rerank」，不開 rewrite 擴召回。
# retrieve() 本身的參數預設維持 False（保持 eval 腳本向後相容），只有生產入口（CLI / api_server）
# 預設打開 translate。
#
# 2026-07-20 變更：rewrite 從 True 改 False。動機——rewrite × translate 的 2×2 全量拆解
# （r3v3 設定，retrieval=gpt-oss-20b、NVIDIA、--repeat 3 --judge-votes 3）顯示兩者是**負交互**：
#   baseline 0.806 / +rewrite 0.820 / +translate 0.824 / +rewrite+translate 0.795（四格最差、低於 baseline）。
# 各自單開都是小增益，疊起來被 rewrite 灌進池的雜訊候選抵銷（colloquial 受創最深：0.721→0.646）。
# 單一乾淨 query（靠 translate 補英文語感）勝過「1 query + 一堆變體」。
# ⚠ regime caveat：此結論確立於 gpt-oss-20b retrieval regime（Groq TPD 燒乾後的實際 regime）。
# rewrite 在名義生產模型 llama-3.3-70b（DEFAULT_MODEL）上 07-13 曾驗證有效；若改回 llama-70b
# retrieval，需重跑本 2×2 才能確認 rewrite 是否仍該關。rewrite 程式碼保留為 opt-in（eval/agentic 仍用）。
DEFAULT_ENABLE_REWRITE     = False
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
GROQ_BASE_URL   = "https://api.groq.com/openai/v1"    # 保留常數供其他檔案沿用（如 agentic_rag_nv.py
                                                        # 的 AGENTIC_BRAIN_PROVIDER=groq 選項）；生產 call_llm 已不走這條路徑。
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_HISTORY     = 3


def make_qdrant_client():
    """依 QDRANT_URL 決定走 Docker server 或 local path 模式。"""
    from qdrant_client import QdrantClient
    if QDRANT_URL:
        print(f"[INFO] Opening Qdrant server: {QDRANT_URL}")
        return QdrantClient(url=QDRANT_URL)
    print(f"[INFO] Opening Qdrant local: {QDRANT_PATH}")
    return QdrantClient(path=QDRANT_PATH)


# ── 答案尾端的「證據尾巴」──────────────────────────────────────────────────────
# 生成器會在答案後面接一段 metadata（`---\n📚 引用來源…` 或 `---\n⚠ 口徑說明…`）。那是**呈現層**
# 不是答案內容，任何「對答案本身做判斷」的地方都該先把它切掉。
# ⚠ 這個概念在 repo 裡**已經各自長出四份定義**（`agentic_rag_v2` 的 producer、
# `eval/check_number_defects.FOOTER`、`eval/check_rounding_fidelity._TAIL_RE`、
# `eval/eval_ragas_vs_rubric` 的 footer strip）。這裡是**唯一的正式定義**，新的消費端一律用它；
# 既有那幾份的收攏見 BACKLOG（動 RAGAS 那份會移動分數，不可順手改）。
# 2026-08-27 加：起因是 `looks_like_refusal` 的長度閘把這條尾巴一起數進去，
# 於是 agentic 的拒答少算 8/43（19%）。
EVIDENCE_TAIL_RE = re.compile(r"\n-{3,}\n(?=\s*(?:📚|⚠))")


def strip_evidence_tail(text: str) -> str:
    """切掉答案尾端的引用／口徑 metadata，只留答案本體。"""
    return EVIDENCE_TAIL_RE.split(text or "", 1)[0]


# ── 「這份答案整篇都不作答」的唯一判準 ─────────────────────────────────────────
# ⚠ 這三個常數與 `looks_like_refusal` 原本住在 `eval/eval_generation_llm_judge.py`（量尺端）。
# 2026-08-27 搬到這裡，因為**生產端也要用它**：agentic 會在答案尾端機械式附上
# 「📚 引用來源（Generator 實際依據的 chunk）」，而拒答時那句話是**假的**——生成器
# 剛剛才說自己沒有依據。舊守門寫成 `answer.startswith("I don't have enough")`，
# 只擋得住 graph 崩潰時那句英文預設值，擋不住模型自己用中文寫的拒答
# （實測既有結果檔 4175 份答案／59 份拒答，**21 份帶著引用尾巴出貨**）。
# ⚠ 判準刻意是「**整份**都不作答」，兩個方向的邊界都是設計不是漏洞：
#   ①「其中一項未揭露」不算拒答——那是誠實標註，答案本體有作答。
#   ②「只有 2023–2025 的資料、未包含 2022」這種**寫得長的誠實答案**也不算——它真的讀了
#      來源、真的有依據，引用清單對它成立。要判那個概念看 `eval/check_historical_generation.py`。
REFUSAL_MARKERS = [
    "don't have enough information",
    "do not have enough information",
    "知識庫", "無法回答", "沒有足夠",
    # 2026-08-11 加：實測 mix-07 的拒答就是這個措辭（「根據提供的參考資料，沒有任何文件提及…」），
    # 舊清單一個都沒命中。⚠ 加標記前逐一量過誤報：`未提及`(18/22 誤報)、`資料中未`(20/23)、
    # `參考資料中未`(5/8)、`沒有提及`(1/1)、`無相關資料`(1/1) **全部退回不加**——它們絕大多數是
    # 「答案有實質作答，只是其中一項未揭露」。只有這一條在 2209 份存檔答案裡 1 命中、0 誤報。
    "沒有任何文件提及",
]

_CITE_MARK = re.compile(r"[【\[][^】\]]{0,80}[】\]]")

# 拒答的長度上界。⚠ 2026-08-11 加：只比對標記會把「答案主體有作答、只是其中一項未揭露」
# 也判成拒答。實測 2209 份存檔答案裡命中標記的 42 份，剝掉引用標記後的長度分佈是
#   ≤100 字元 34 份（真拒答）／100~200 1 份（mh-06，其實答了目標價 $330→$365）／
#   200~300 **0 份**／≥300 字元 8 份（news-02、mi-01、mi-09、mi-12、mi-14 全是有實質
#   作答的長答案）——中間有一段空白，門檻落在 150 兩邊都不擦邊。
REFUSAL_MAX_CHARS = 150


def looks_like_refusal(answer: str) -> bool:
    """整份答案都不作答才算拒答；「其中一項未揭露」不算（那是誠實標注，不是拒答）。

    ⚠ 2026-08-27 修：長度閘要量的是**答案本體**，但 agentic 會在答案尾端接一整塊
    `---\\n📚 引用來源…` 的 metadata，舊寫法把那塊也數進去 → 拒答的答案幾乎永遠超過
    `REFUSAL_MAX_CHARS`。實測 66 份 agentic 結果檔／2975 份答案：**少算 8 筆（35 → 43）**。
    """
    a = (answer or "")
    if not any(m.lower() in a.lower() for m in REFUSAL_MARKERS):
        return False
    body = strip_evidence_tail(a)
    return len(_CITE_MARK.sub("", body).strip()) < REFUSAL_MAX_CHARS


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
   (a) Lead with a PLAIN-LANGUAGE direct answer to the question in the FIRST sentence — \
phrase the single most important conclusion the way you would explain it to a smart \
non-specialist, and include the key headline number if the question asks for one. Avoid \
opening with dense jargon; the first sentence should be immediately understandable.
   (b) Then provide the supporting details and secondary figures, using precise financial \
terms and exact numbers (with their time period) — do NOT sacrifice numerical precision or \
correct terminology for simplicity; the plain-language framing applies to how you LEAD and \
explain, not to dropping or rounding away the specific figures the sources give.
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
contain. Every number you state must be traceable to a cited chunk. NUMERIC FIDELITY — copy \
every digit EXACTLY as the source writes it. If the chunk says "18.30%", write "18.30%"; never \
"18%", "~18%", "about 18%", "約 18%", "接近兩成", or any other rounded or approximated restatement. \
Rounding to fewer digits breaks traceability exactly as fabrication does, and it silently \
merges figures that are genuinely different — a TTM growth of 18.30% and a fiscal-year growth \
of 18% are NOT the same number, and a reader who sees only "about 18%" cannot tell which one \
you used. This applies to percentages, margins, growth rates, dollar amounts, share counts and \
ratios alike, in EVERY sentence including bullets and parenthetical asides. You MAY append an \
approximation AFTER the exact figure ("18.30%, i.e. roughly 18%") — never in place of it. The \
only figure you may restate in different digits is the unit composition described in Rule 11 (a \
raw statement line carrying a stated scale).
9. When the question concerns MORE THAN ONE company (e.g. "these companies", "compare \
A and B", or the materials clearly span multiple companies), you MUST attribute each \
point to a NAMED company in the prose itself — write "META faces X [cite]; Microsoft \
faces Y [cite]", NOT a generic "these companies face X and Y". Name each company \
explicitly and bind its specific risks/figures/strategy to it. A citation tag alone is \
NOT sufficient attribution — the company name must appear in the sentence. Cover at \
least the distinct companies the materials support.
10. PERIOD BASIS — the SAME metric appears on different time bases across the sources, so \
every financial figure you state MUST carry its basis label. Use "FY2025" for a full \
fiscal year, "TTM as of YYYY-MM" for a trailing-twelve-month figure, and "Q2 FY2026" (or \
"three months ended <date>") for a single quarter. As a rule of thumb: *_Fundamentals_* \
files are TTM; 10-K / 10-Q / income-statement figures are fiscal-year or quarterly. Never \
give a bare revenue / margin / growth number without its basis. When the question uses a \
relative term ("latest", "most recent year", "past year", "近一年") that could map to more \
than one basis AND the sources carry DIFFERENT values on different bases, do NOT silently \
pick one — state BOTH and label them (e.g. "on a full-year basis FY2025 revenue grew ~22%; \
on a trailing-twelve-month basis it grew ~33%"). For "most recent fiscal year", use the \
latest COMPLETED fiscal year present in the sources (e.g. FY2025, not the prior FY2024).
11. UNIT & MAGNITUDE — the safest rule: KEEP the source's own unit token VERBATIM. If the \
source writes "$84.75 billion", write "$84.75 billion" (or "$185 billion", "$34.75 billion") \
in your answer — do NOT convert it into 億 at all. "billion" is NOT "億": mechanically \
rendering "X billion" as "X 億" is a 10x error (the actual value is 10X 億, e.g. $84.75 \
billion = 847.5 億). Because that transliteration is the single most common mistake, avoid \
the conversion entirely — preserve "billion" / "million" exactly as the source states them. \
Only when a source gives a raw number with a stated scale (e.g. a statement line "in \
millions" showing "131,819" = $131,819 million = $131.8 billion) do you compose the unit \
yourself, and then state it as "$131.8 billion". If you ever do write 億, anchor it: 1 \
billion = 10 億, 1 million = 0.01 億 — and sanity-check that a mega-cap's quarterly segment \
revenue lands in the tens of billions (數百億), not the 兆 range. Apply this to EVERY figure \
in the answer, including every bullet and secondary number.
12. DIRECTION & SIGN — never infer the direction of a change yourself; quote the source's \
own direction word (rose / fell / increased / decreased / up / down / 上升 / 下降). Preserve \
every sign: a value shown as "(2)%", "decreased 2%", or "-2%" is NEGATIVE — do NOT report it \
as +2%. When two period values are given (e.g. two quarters or two years), decide which is \
earlier vs later STRICTLY from the explicit dates / period labels in the source — do NOT \
assume the larger number is the later one or that the trend is "improving". If the source \
shows margin went 18.7% → 16.8% over time, that is a DECLINE; do not flip it into a rise.
13. PER-INTENT EVIDENCE CHECK (extends Rule 3) — for a multi-part question, evaluate EACH \
sub-question separately against ALL references. If ANY reference carries information bearing \
on a sub-question — even a single news item, one figure, or a passage whose main topic is \
something else — you MUST use it and answer that part. Do NOT write "no information" / "the \
materials do not mention this" / "not disclosed" for a sub-question when a relevant reference \
is actually present in the materials. Saying a fact is missing is permitted ONLY after you \
have scanned every reference and none touches it. Answering one intent well does not excuse \
dropping the other with a false "no info".
"""

# ══════════════════════════════════════════════════════════════════════════════
# Evidence-first generation（實驗性，見 CHANGELOG 2026-07-20 方案 A）
#
# 動機：sem-08 家族的決定性診斷（07-14 句級抽取實驗）證明目標句已經在 chunk 最前面、
# 模型看得到，3 次生成仍 2 次不引用——病灶是模型「一次性完成相關性判定+寫作」時
# 內部略過了它認為與問法無關的內容，不是訊號埋沒。之前三種修法（chunk 切分、
# Rule 10 prompt、句級抽取）全部是「把訊號推到模型眼前」的同一族，對這個病灶
# 無效（死路表已載）。
#
# 這裡改變的是「相關性判定發生的位置」：強制模型先輸出一個 Evidence Log，對
# 每個 reference 逐一顯式表態相關/不相關，再根據標記為相關的部分作答。格式要求
# 的服從性遠高於「你應該認為這段相關」這種內容說服（模型對輸出格式指令的服從性
# 普遍比對內容判斷的說服力更可靠），且逐 chunk 盤點把篩選過程變成可稽核的顯式
# 步驟，不再是模型腦內看不見的一次性判斷。
#
# opt-in：預設不使用，需呼叫端主動選用 SYSTEM_PROMPT_EVIDENCE_FIRST 並在生成後
# 呼叫 extract_final_answer() 剝除 Evidence Log、只把 Answer 區塊留給使用者/judge。
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_EVIDENCE_FIRST = SYSTEM_PROMPT + """
14. Before writing your answer, you MUST first produce an Evidence Log: go through
EVERY numbered reference in order and write ONE line per reference stating whether it
is relevant to the question. Do this even for references that seem tangential — a
reference can be partially relevant (e.g. it mentions the topic inside a passage that
mostly discusses something else) or fully relevant; only mark "not relevant" if it
truly contains nothing useful for this specific question. If a reference contains ANY
information bearing on the question (numbers, named products, stated positions), you
MUST mark it relevant and quote the key phrase, even if the reference's main topic is
something else.
Then write your final Answer, following rules 1-13 above, drawing on every reference you
marked relevant in the Evidence Log — do not silently drop a reference you just marked
relevant.

Output format (use exactly these two headers, nothing before "## Evidence Log"):
## Evidence Log
[Reference N]: relevant — <key phrase quoted verbatim> | OR | [Reference N]: not relevant — <one-clause reason>
(one line per reference, in order)

## Answer
<your final answer, following rules 1-13>
"""

_ANSWER_SECTION_RE = re.compile(r"##\s*Answer\b", re.IGNORECASE)


def extract_final_answer(raw: str) -> str:
    """evidence-first 模式專用：把生成輸出的 Evidence Log 前綴剝除，只留 '## Answer' 之後
    的內容給使用者/judge 看。找不到標記時保守回傳整段原文（向後相容、不遺失內容，
    例如模型沒有遵守格式指令的情況）。"""
    m = _ANSWER_SECTION_RE.search(raw)
    if not m:
        return raw
    return raw[m.end():].strip()


# ── 確定性單位換算後處理（billion / million / trillion → 億）─────────────────────
# 動機：LLM 把英文 "$84.75 billion" 音譯成 "84.75 億"（正確是 847.5 億）是翻譯層 token
# 習慣，prompt（Rule 11）只能隨機壓住、reflect 也共用同盲點。正解＝Rule 11 要 Writer
# 「原樣保留 billion」，再由**這支純程式**把 billion→億 的乘 10 算對（程式算不會錯、零誤報，
# 因為 "X billion" render 成 "X 億" 100% 是錯）。1 billion=10 億、1 trillion=10,000 億、
# 1 million=0.01 億。只轉「數字+英文單位詞」，不碰已經是 億/% 的值。
_USD_UNIT_RE = re.compile(
    r'(?:((?:US)?\$)\s?)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*'
    r'(billion|trillion|million|bn|mn|十億|百萬|兆)(?![A-Za-z])'
    r'(?:\s*(?:美元|美金|dollars?|USD))?',
    re.IGNORECASE)
# 含中文單位詞安全網：LLM 若沒照 Rule 11、寫成「$13.63 十億 / 890 百萬」也一律歸成億。
_UNIT_TO_YI = {"billion": 10.0, "bn": 10.0, "trillion": 10000.0, "million": 0.01, "mn": 0.01,
               "十億": 10.0, "兆": 10000.0, "百萬": 0.01}


def _fmt_yi(yi: float) -> str:
    """把億值格式化：整數不留小數、否則去尾零，千分位加逗號。"""
    if abs(yi - round(yi)) < 1e-9:
        return f"{int(round(yi)):,}"
    return f"{yi:,.4f}".rstrip("0").rstrip(".")


# 前置正規化：財報源常用「$37.01B / $2899.62B / $510M」縮寫（B/M/T 附在金額後）。要求 $ 前綴
# 且大寫 B/M/T（不吃小寫、不吃無 $ 的裸字母，避免誤傷），先展開成 billion/million/trillion。
_DOLLAR_ABBR_RE = re.compile(r'((?:US)?\$\s?[0-9][0-9,]*(?:\.[0-9]+)?)\s*([BMT])(?![A-Za-z])')
_ABBR_WORD = {"B": " billion", "M": " million", "T": " trillion"}


def convert_usd_units_to_yi(text: str) -> str:
    """把答案裡的 "$X billion / X million 美元 / ..." 換算成正確的「X 億(美元)（$X billion）」雙寫。
    純確定性、無 LLM。保留幣別語意：原文帶 $ 或「美元/USD」→ 輸出「… 億美元」，否則「… 億」。
    **雙寫**：億值在前（給中文使用者、且已由程式乘對），來源原始英文數字 verbatim 留在括號內——
    一來可人工稽核，二來讓 RAGAS faithfulness 判官在答案裡找得到與 context 對得上的原始
    "$X billion" 字串（只留「億」時判官不會算單位換算 → 把答對的當幻覺打 faith=0，見 CHANGELOG_AGENTIC ⑨）。
    只呼叫一次（synthesize 尾端），故不處理重入時二次匹配。"""
    def _repl(m: "re.Match") -> str:
        num_s, unit = m.group(2), m.group(3).lower().rstrip()
        mult = _UNIT_TO_YI.get(unit)
        if mult is None:
            return m.group(0)
        try:
            yi = float(num_s.replace(",", "")) * mult
        except ValueError:
            return m.group(0)
        whole = m.group(0)
        has_ccy = bool(m.group(1)) or any(k in whole for k in ("美元", "美金", "dollar", "USD", "usd"))
        src = f"{m.group(1) or ('$' if has_ccy else '')}{num_s} {m.group(3).rstrip()}"
        return f"{_fmt_yi(yi)} 億{'美元' if has_ccy else ''}（{src}）"
    text = _DOLLAR_ABBR_RE.sub(lambda m: m.group(1) + _ABBR_WORD[m.group(2)], text or "")
    return _USD_UNIT_RE.sub(_repl, text)


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


# ⚠ 2026-08-19 起 **這組詞表不參與任何生產判斷**。KB 已不收新聞語料，原本靠它注入的
# `doc_type=news` 硬 filter 已移除（見 retrieve() 內的說明）。保留函式的唯一理由是
# `eval/probe_multi_company_period.py` 等診斷探針仍拿它當分類標籤——那是**觀測**不是決策。
# ⚠ 不要再把它接回任何路由：它是硬編碼詞表做感知，換個措辭就漏，而漏掉的代價是整題失敗
# （見 CLAUDE.md〈LLM 與 Python 的分工〉）。
_NEWS_KEYWORDS = (
    "news", "headline", "headlines", "favorable", "unfavorable",
    "新聞", "消息", "利多", "利空", "有利", "不利", "媒體", "頭條", "外電",
)
_NEWS_GUARDED_RE = re.compile(r"(?<![財季年月週日快半])報導")


def looks_like_news_query(query: str) -> bool:
    """**僅供診斷探針分類使用**，不接在任何生產路徑上（理由見上方註解）。"""
    query_lower = query.lower()
    if any(keyword in query_lower for keyword in _NEWS_KEYWORDS):
        return True
    return bool(_NEWS_GUARDED_RE.search(query or ""))


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

FILTERABLE_FIELDS = ("filing_type", "fiscal_year", "fiscal_period", "report_period_code", "ticker", "period_basis")

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


def _find_all_ticker_aliases(q_lower: str, query: str) -> list[str]:
    """deterministic（無 LLM）：偵測『所有』命中的公司別名（英文或中文），回傳去重後的
    ticker 清單（保留首次出現順序）。與 _find_ticker_alias（只回第一個）相對——多公司比較題
    需要圈出全部提及的公司，否則 ticker hard filter 只鎖第一個會漏掉其餘公司（見對話 2026-07-30
    mh-01 trace：'Amazon、Microsoft、Alphabet、Meta' 只鎖到 MSFT）。"""
    found: list[str] = []
    for name, symbol in _COMPANY_TICKER.items():
        hit = (name in query) if _CJK_RE.search(name) \
            else bool(re.search(r"\b" + re.escape(name) + r"\b", q_lower))
        if hit and symbol not in found:
            found.append(symbol)
    return found


# 「最近十二個月 (TTM / trailing twelve months / 滾動)」口徑訊號。命中 → 硬 filter 導向
# Fundamentals（period_basis=TTM），避免抓到年度(fiscal_year)結算數字造成口徑歧義
# （見對話 2026-07-30：mh-01~05 這類 TTM 比較題原本會撈到 10-K/IncomeStatement 的年度數）。
_TTM_RE = re.compile(
    r"\bttm\b|\bltm\b"
    r"|(?:最近|近|過去|滾動|過往)\s*(?:十二|12)\s*個?\s*月"
    r"|trailing\s*(?:twelve|12)\s*months?|last\s*twelve\s*months?",
    re.IGNORECASE,
)


def _detect_period_basis(query: str) -> str | None:
    """deterministic（無 LLM）：偵測問題是否明確要「最近十二個月 (TTM)」口徑。
    命中回傳 'TTM'，否則 None（不注入 basis filter，維持原行為）。
    目前只單向偵測 TTM——fiscal_year 口徑刻意不硬性路由（避免回歸既有『年度』題），
    但語料端每個 filing/statement/fundamentals chunk 都已標好 period_basis 供未來擴充。"""
    return "TTM" if _TTM_RE.search(query or "") else None


def _extract_structural_filters(query: str) -> list[dict]:
    """deterministic（無 LLM）：所有「乾淨字面訊號」的統一入口——從問題抽出 yyyymm 期間碼、
    ticker（含中文別名）、以及 period_basis 口徑（TTM）。這三者都不需要 LLM，天生適合 regex，
    集中在此一處吐出，與 LLM 只管的「模糊散文約束」（filing_type/fiscal_year/fiscal_period）分工。"""
    filters: list[dict] = []
    m = _PERIOD_CODE_RE.search(query)
    if m:
        filters.append({"field": "report_period_code", "value": m.group(1), "polarity": "include"})
    tickers = _find_all_ticker_aliases(query.lower(), query)
    if tickers:
        # 單一 → 字串（MatchValue，行為不變）；多個 → list（build_qdrant_filter 轉 MatchAny/OR），
        # 讓多公司比較題一次圈出所有提及的公司，不再只鎖第一個。
        filters.append({"field": "ticker",
                        "value": tickers[0] if len(tickers) == 1 else tickers,
                        "polarity": "include"})
    basis = _detect_period_basis(query)
    if basis:
        filters.append({"field": "period_basis", "value": basis, "polarity": "include"})
    return filters


# ══════════════════════════════════════════════════════════════════════════════
# 「最新一季」deterministic 路由（2026-08-02，Gap 1 修）
#
# 病灶（mix-06 chunk-level probe 實證）：問「最新一季」的財報指標時，cross-encoder 無法
# 區分同公司「Google Cloud 營收」講的是哪一季——舊季(202603)/年報(2025) 的同主題 chunk
# 分數與最新季(202606) 幾乎並列(0.70~0.73)，把最新季的 gold 表格 chunk 壓到 rank 7/13/17、
# commit 門檻(top-5)之外 → context_recall=0。治本：偵測到「最新一季 + 財報指標 + 單一公司」
# 時，deterministic 解出該公司 KB 中最新的 10-Q 期碼，注入 report_period_code 硬 filter，
# 把舊季/年報 chunk 在排序前就排除，讓最新季 gold 浮上 top。與 TTM 單向硬 filter 同一哲學。
#
# 觸發需「同時」滿足（刻意收窄，避免誤觸跨期/質化/TTM/新聞/Fundamentals 題）：
#   ① 相對時間詞（最近/最新/近期/這一季/current…）
#   ② 季度/財報指標訊號（季/營收/成長/利益/銷售/賣/margin/revenue…）——把「最近做了什麼/
#      搭上線」這類跨期質化題（col-03）與「目前市值/FCF」這類 Fundamentals 題擋在門外
#   ③ 恰好一家公司（多/零公司 → 不明確，不路由）
#   ④ 非 TTM（_detect_period_basis）
#   ⑤ 問題未自帶明確 yyyymm 期碼（尊重使用者指定的期間）
#   ⚠ 原本還有一條「非新聞題」（`looks_like_news_query`），2026-08-19 隨 KB 拔除新聞一併移除：
#     那道守衛會讓任何含「新聞／消息／報導」的複合題拿不到最新一季路由。
# 任一不滿足 → 回 None（原行為）。即使誤觸，report_period_code 硬 filter 仍有 Tier2 fallback 兜底。
# ══════════════════════════════════════════════════════════════════════════════

_LATEST_QUARTER_TIME_RE = re.compile(
    r"最近|最新|近期|這一季|當季|本季|目前|現在"
    r"|\b(?:latest|recent|current|most\s+recent|this\s+quarter)\b",
    re.IGNORECASE,
)
# 季度 / 財報指標訊號：命中其一即算「問的是某季的財務數字」。刻意不含「市值/FCF/員工數」等
# Fundamentals 專屬指標（那些走 .txt、無 report_period_code，硬 filter 到 10-Q 期碼只會空轉）。
_QUARTER_METRIC_RE = re.compile(
    r"季|營收|營業|收入|成長|增長|利益|利潤|毛利|淨利|銷售|賣"
    r"|\b(?:eps|margin|revenue|growth|sales|profit|operating\s+income|net\s+income)\b",
    re.IGNORECASE,
)
# 年度（10-K）意圖訊號：命中即「問的是年報/全年」，不該被季路由搶走。動機（2026-08-02）：
# 「NVIDIA 最新年度營收成長多少？」同時帶相對時間(最新)＋財報指標(營收/成長)，若無此 guard
# 會誤觸季路由、被導向最新 10-Q（該走 10-K）。年度題本就走 LLM 抽 filing_type=10-K + Tier fallback，
# 不需硬路由——命中此 RE → 讓季路由讓路（return None）。
_ANNUAL_INTENT_RE = re.compile(
    r"年度|年報|全年|財年|會計年度"
    r"|\b(?:annual|full[\s-]*year|fiscal\s+year|\bfy\b)\b",
    re.IGNORECASE,
)

# 每個 ticker 最新 10-Q 期碼的 cache（以 collection 為界；eval 切 collection 時自動重算）。
_latest_10q_cache: dict | None = None
_latest_10q_cache_collection: str | None = None
_latest_10q_lock = threading.Lock()


def _get_latest_10q_periods(client) -> dict:
    """掃目前 COLLECTION_NAME，回傳 {ticker: 最新 10-Q report_period_code(yyyymm 字串)}。
    只讀少量 payload（不取 vector/document）；cache 以 collection 為界。掃描失敗回空 dict
    （→ 上層 resolve 回 None → 退回原行為，不讓 coverage 故障拖垮檢索）。"""
    global _latest_10q_cache, _latest_10q_cache_collection
    if _latest_10q_cache is not None and _latest_10q_cache_collection == COLLECTION_NAME:
        return _latest_10q_cache
    with _latest_10q_lock:
        if _latest_10q_cache is not None and _latest_10q_cache_collection == COLLECTION_NAME:
            return _latest_10q_cache
        latest: dict[str, str] = {}
        offset = None
        try:
            while True:
                points, offset = client.scroll(
                    collection_name=COLLECTION_NAME, limit=256, offset=offset,
                    with_payload=["ticker", "filing_type", "doc_type", "report_period_code"],
                    with_vectors=False,
                )
                for p in points:
                    pl = p.payload or {}
                    ft = str(pl.get("filing_type") or "").upper()
                    dt = str(pl.get("doc_type") or "").lower()
                    if ft != "10-Q" and dt != "10-q":
                        continue
                    tk = str(pl.get("ticker") or "").upper().strip()
                    code = re.sub(r"\D", "", str(pl.get("report_period_code") or ""))
                    if not tk or len(code) < 6:
                        continue
                    if tk not in latest or code > latest[tk]:
                        latest[tk] = code
                if offset is None:
                    break
        except Exception as e:
            print(f"WARN  - latest-10Q coverage scan failed ({e!r}); latest-quarter routing disabled")
        _latest_10q_cache = latest
        _latest_10q_cache_collection = COLLECTION_NAME
        return _latest_10q_cache


# ══════════════════════════════════════════════════════════════════════════════
# 期別 ladder（2026-08-19）：每家公司的 filing 由新到舊排好
#
# **為什麼不能沿用 `_get_latest_10q_periods` 的字串比大小**：那個函式靠 `code > latest[tk]`
# 取 max，並用 `len(code) < 6: continue` 把 10-K 擋在門外。期碼是「顯示用編碼」不是
# 「可比大小的序」——10-K 是 4 位年份、10-Q 是 6 位 yyyymm，混在一起比字串會**時對時錯**：
#   · `'2026' > '202510'` 為 True（比到第 4 位 6>5）→ 10-K 較新，**這個剛好對**
#   · `'2025' < '202506'` 為 True（'2025' 是前綴、較短即較小）→ 判 10-Q 較新，**這個是錯的**
#     （AAPL FY2025 年報當然比自己的 FY2025 Q3 新）
# **2026-08-19 在 77 份 filing 上實測：385 個配對有 31 對（8.1%）被字串比大小判反。**
# 正確的序是 payload 的 `(fiscal_year, fiscal_period)`。
#
# ⚠ **財年不等於曆年**：`NVDA_10K_2026` 是 FY2026 FY，而 `NVDA_10Q_202604` 是 **FY2027 Q1**
# ——後者才新。同一個坑 agentic 端已經踩過並用 `_fiscal_rank` 修好（見
# `agentic_rag_v2._fiscal_rank` 的 NVDA 反轉註解）；這裡是**同一套序**放在共用層，
# 讓兩條管線不要各寫一份。
# ══════════════════════════════════════════════════════════════════════════════

_FISCAL_PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}

_period_ladder_cache: dict | None = None
_period_ladder_cache_collection: str | None = None
_period_ladder_lock = threading.Lock()


def _fiscal_sort_key(fiscal_year, fiscal_period) -> tuple[int, int] | None:
    """(年, 期別序)——可直接比大小。任一欄缺漏回 None（＝不參與比較，寧可漏判也不誤報）。"""
    order = _FISCAL_PERIOD_ORDER.get(str(fiscal_period or "").upper().strip())
    year = re.sub(r"\D", "", str(fiscal_year or ""))
    return (int(year), order) if order is not None and len(year) == 4 else None


def _get_period_ladder(client) -> dict:
    """掃目前 COLLECTION_NAME，回傳 {ticker: [entry, ...]}，每個 ticker 內**由新到舊**。

    entry = {source, kind(10-K/10-Q), period_code, fiscal_year, fiscal_period, rank}
    cache 以 collection 為界（eval 切 collection 時自動重算）。掃描失敗回空 dict
    → 上層退回原行為，不讓 coverage 故障拖垮檢索。"""
    global _period_ladder_cache, _period_ladder_cache_collection
    if _period_ladder_cache is not None and _period_ladder_cache_collection == COLLECTION_NAME:
        return _period_ladder_cache
    with _period_ladder_lock:
        if _period_ladder_cache is not None and _period_ladder_cache_collection == COLLECTION_NAME:
            return _period_ladder_cache
        by_source: dict[str, dict] = {}
        offset = None
        try:
            while True:
                points, offset = client.scroll(
                    collection_name=COLLECTION_NAME, limit=512, offset=offset,
                    with_payload=["source", "ticker", "filing_type",
                                  "fiscal_year", "fiscal_period", "report_period_code"],
                    with_vectors=False)
                for p in points:
                    pl = p.payload or {}
                    src = pl.get("source") or ""
                    if not src or src in by_source:
                        continue
                    kind = str(pl.get("filing_type") or "").upper()
                    if kind not in ("10-K", "10-Q"):
                        continue
                    rank = _fiscal_sort_key(pl.get("fiscal_year"), pl.get("fiscal_period"))
                    if rank is None:
                        continue
                    by_source[src] = {
                        "source": src,
                        "ticker": str(pl.get("ticker") or "").upper().strip(),
                        "kind": kind,
                        "period_code": str(pl.get("report_period_code") or ""),
                        "fiscal_year": rank[0],
                        "fiscal_period": str(pl.get("fiscal_period") or "").upper().strip(),
                        "rank": rank,
                    }
                if offset is None:
                    break
        except Exception as e:
            print(f"WARN  - period ladder scan failed ({e!r}); period routing disabled")
            by_source = {}
        ladder: dict[str, list] = {}
        for rec in by_source.values():
            ladder.setdefault(rec["ticker"], []).append(rec)
        for tk in ladder:
            ladder[tk].sort(key=lambda r: r["rank"], reverse=True)
        _period_ladder_cache = ladder
        _period_ladder_cache_collection = COLLECTION_NAME
        return _period_ladder_cache


def ladder_pick(ladder: dict, ticker: str, *, kind: str | None = None,
                year: int | None = None, nth: int = 0) -> dict | None:
    """從 ladder 挑第 `nth` 新的 filing（0 ＝ 最新）。

    `kind`：限定 10-K／10-Q；`year`：限定**財年**（`fiscal_year`，不是檔名年份）。
    挑不到回 None ＝ 呼叫端不注入任何期別 filter（維持原行為），**不要退而求其次挑別的**
    ——挑錯期別比不挑更糟（答案會帶著引用一起錯）。"""
    rows = ladder.get((ticker or "").upper().strip()) or []
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if year is not None:
        rows = [r for r in rows if r["fiscal_year"] == year]
    return rows[nth] if 0 <= nth < len(rows) else None


# ══════════════════════════════════════════════════════════════════════════════
# 期間意圖解析（2026-08-19）：把「這題問的是哪個期間」交給 LLM，「那是哪個期碼」留給 Python
#
# **為什麼要另開一個函式，而不是往 `parse_query_filters` 加欄位**（實測逼出來的）：
# `parse_query_filters` 前面有一道 `_FILING_HINT_RE` 詞表閘門，沒命中就**整個跳過 LLM**。
# 2026-08-19 實測：eval_set 的 filing 類題目裡，**`relative`（相對期間指稱）21 題有 20 題被
# 那道閘門擋下**——「Microsoft Azure 最新一季的營收成長率是多少？」不含年報／季報／FY／Qx／
# 四位數年份任何一個。所以把 `period_ref` 加進那個 prompt 對最需要它的一類**完全無效**。
# 那道閘門的正確性論證是「這類 query 的 LLM 本來就只會回空」，而那句話**只對『抽取明確
# 約束』成立**；`period_ref` 問的正是隱含意圖，論證不成立。
#
# 本函式因此：①獨立呼叫、不吃那道閘門 ②自己的 `llm_replay` kind → **不動任何既有 prompt、
# 不讓任何既有 fixture 失效** ③代價明確＝每次 retrieve 多一次 LLM（實測中位 1.54s）。
#
# **分工照 CLAUDE.md**：「這題要哪個期間」沒有唯一機械答案 → LLM；「那是哪個期碼」是
# 確定性的 → Python 從 `_get_period_ladder()` 算。
# ══════════════════════════════════════════════════════════════════════════════

PERIOD_INTENT_SYSTEM_PROMPT = """\
You are a query-understanding assistant for a financial RAG system over SEC filings \
(10-K annual reports, 10-Q quarterly reports).

Decide WHICH REPORTING PERIOD the question is asking about. Output ONLY this JSON object:
{"period_ref": "latest" | "absolute" | "range" | "none",
 "fiscal_year": "<4-digit year>" | null,
 "granularity": "quarter" | "annual" | null}

period_ref:
- "latest"   — asks about the MOST RECENT period. Includes relative wording such as \
"latest", "most recent", "current", "so far this year", "year-to-date", "right now", \
"最新", "最近", "近期", "目前", "現在", "本季", "當季", "這一季", "今年至今".
- "absolute" — names ONE specific period ("fiscal 2024", "FY2025", "Q2 2026", "2025 年第二季").
- "range"    — needs MORE THAN ONE FILING to answer: multi-period trends such as \
"past three years", "逐季", "逐年", "趨勢", "這幾年", "變化過程", or an explicit comparison \
of two or more named periods.
  IMPORTANT — a question about ONE period that also asks how it compares with the SAME \
period a year earlier (e.g. "this quarter vs the year-ago quarter", "跟去年同期相比") is \
"latest", NOT "range": every 10-Q and 10-K already reports the prior-period comparison \
inside the same filing, so one filing answers it.
- "none"     — no period constraint at all. Use this for questions about a specific event, \
product, policy or fact where the asker did not indicate any period.

fiscal_year: the 4-digit year IF the question names one (works with "latest" too — \
"2026 年至今" is {"period_ref":"latest","fiscal_year":"2026","granularity":"quarter"}). \
Otherwise null.

granularity: "quarter" if the question is about a quarter / quarterly results \
("一季", "本季", "quarterly", "Q1".."Q4", "year-to-date" within a year); "annual" if it is \
about a full fiscal year / annual report ("年度", "全年", "財年", "annual", "full-year", "FY"); \
null if unclear.

Rules:
- Judge the ASKER'S INTENT, not the vocabulary. A question with no time words at all is "none".
- "range" beats "latest": if the question needs several periods to answer, it is "range" \
even when it also says "最近".
- Output ONLY the JSON object — no markdown fences, no explanation.
"""

_VALID_PERIOD_REFS = ("latest", "absolute", "range", "none")
_PERIOD_INTENT_NONE = {"period_ref": "none", "fiscal_year": None, "granularity": None}


def resolve_period_intent(query: str, model_name: str = DEFAULT_MODEL) -> dict:
    """LLM 判「這題問的是哪個期間」→ {period_ref, fiscal_year, granularity}。

    失敗一律回 `none`（＝不注入任何期別 filter、也不擋 collapse），**不讓期間理解成為單點
    故障**，行為退回本函式存在之前。"""
    import json
    import re as _re

    q = (query or "").strip()
    if not q:
        return dict(_PERIOD_INTENT_NONE)
    _hit = _replay.get("period_intent", q)
    if _hit is not _replay.MISS:
        return _hit
    try:
        raw = call_llm([{"role": "system", "content": PERIOD_INTENT_SYSTEM_PROMPT},
                        {"role": "user", "content": q}], model_name)
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        parsed = json.loads(raw)
        ref = str(parsed.get("period_ref") or "none").lower().strip()
        if ref not in _VALID_PERIOD_REFS:
            ref = "none"
        year = _re.sub(r"\D", "", str(parsed.get("fiscal_year") or ""))
        gran = str(parsed.get("granularity") or "").lower().strip()
        out = {"period_ref": ref,
               "fiscal_year": int(year) if len(year) == 4 else None,
               "granularity": gran if gran in ("quarter", "annual") else None}
    except Exception as e:
        print(f"WARN  - period intent parsing failed ({e!r}); treating as 'none'")
        return dict(_PERIOD_INTENT_NONE)
    _replay.put("period_intent", q, out)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 實體解析（2026-08-28）：產品／子公司／品牌名 → 母公司 ticker
#
# **病灶**（多年語料收益探針 `bh-14` 抓到）：「AWS 在 2022 年的淨銷售額」抽不到 ticker →
# 沒有 ticker hard filter → Tier 3 退回無 filter → top-5 是 META／MSFT／TSLA 的
# IncomeStatement 跨公司污染。
#
# **為什麼不是往 `_COMPANY_TICKER` 加一筆 `"aws": "AMZN"`**：那是 O(n) 的開始。實測 20 個
# 一般人會用的產品／子公司問法，**18 個抽不到**（Azure／iPhone／YouTube／Instagram／
# Reality Labs／CUDA／Model Y／Xbox／LinkedIn／GeForce／Prime／WhatsApp／Bing／Waymo／
# App Store／Kindle／Superchargers…），而這種名字每季都在長。硬編碼詞表在做感知＝換個
# 措辭就漏，見 CLAUDE.md〈LLM 與 Python 的分工〉。
#
# ⚠ **這件事我在 BACKLOG 裡寫錯過**：原本寫「ticker 抽取本來就在 LLM 那一側，該補的是
# prompt 裡的規則」。**不是**——`QUERY_FILTER_SYSTEM_PROMPT` 只抽 filing_type／fiscal_year／
# fiscal_period，**ticker 從頭到尾只有 `_COMPANY_TICKER` 這張表在做**。所以這裡是「新增一個
# LLM 判斷」，不是「補一條既有 prompt 的規則」，代價與風險都不一樣。
#
# **分工照 CLAUDE.md**：「這個產品是誰家的」沒有唯一機械答案、而且是**開放集合**（新產品
# 每季都在出）→ LLM；「這個 ticker 在不在 KB 裡」是**格式定義的封閉集合** → Python 驗
# （`_KB_TICKERS` 由 `_COMPANY_TICKER` 導出，不另抄一份）。
#
# **只在 regex 抽不到時才叫**，理由不是省錢是精度：regex 命中的是字面公司名，那是高精度的，
# LLM 沒有理由推翻它。⚠ 這跟 ratio 意圖那次（詞表必須真的退位）**不是同一個形狀**：那裡
# 詞表會在 LLM 表過態之後還蓋回去；這裡詞表沉默時才問 LLM，詞表不可能覆寫 LLM 的答案。
#
# ⚠ **這個修法在既有跑分上量不到任何差異**：eval_set 65 題**全部**由 regex 解出 ticker
# （實測），這條路一次都不會觸發。那是預期不是缺陷——要量它得靠 `eval/probe_ticker_resolution.py`。
#
# ⚠ **不對稱的錯誤**：判不出來 ＝ 退回今天的行為（無 filter、跨公司污染）＝ 只是沒改善；
#   判成**錯的公司** ＝ hard filter 鎖到別家的財報 ＝ **比今天更糟**。所以 prompt 明講
#   「不確定就回空」、輸出過封閉集合，probe 的判讀也照這個不對稱來看。
# ══════════════════════════════════════════════════════════════════════════════

# 封閉集合：KB 裡真的有 filing 的 ticker。由 `_COMPANY_TICKER` 導出而非另列一份，
# 免得哪天加減公司時兩邊漂移。
_KB_TICKERS: tuple[str, ...] = tuple(sorted(set(_COMPANY_TICKER.values())))

TICKER_RESOLUTION_SYSTEM_PROMPT = """\
You are an entity-resolution assistant for a financial RAG system. Its knowledge base \
contains SEC filings for exactly these seven companies:

  AAPL (Apple), AMZN (Amazon), GOOGL (Alphabet/Google), META (Meta/Facebook), \
MSFT (Microsoft), NVDA (NVIDIA), TSLA (Tesla)

The question you are given does NOT name any of the seven literally. Decide whether it \
is nonetheless ABOUT one or more of them — because it names a product, brand, service, \
subsidiary, business segment, platform, chip, executive or other entity that BELONGS TO \
one of them.

Output ONLY this JSON object:
{"tickers": ["<TICKER>", ...]}

Rules:
- Map the named entity to the ticker of the company that OWNS it TODAY.
- List every one of the seven the question is about; order does not matter.
- Return {"tickers": []} when the question is not about any of them. That includes: \
it is about some OTHER company (a competitor, supplier, customer or private company), \
it is a general industry / market / macro question, or you are simply not sure. \
An empty list is the correct and safe answer — GUESSING IS NOT. A wrong company here \
makes the system retrieve another company's financial statements.
- Only ever output tickers from the seven listed above. Never invent one.
- Output ONLY the JSON object — no markdown fences, no explanation.
"""


def resolve_tickers_llm(query: str, model_name: str = DEFAULT_MODEL) -> list[str]:
    """LLM 實體解析：問題提到的產品／子公司屬於哪幾家 KB 公司。判不出來一律回 `[]`。

    失敗（LLM 掛掉／解析不出／吐了 KB 以外的 ticker）一律回 `[]` ＝ 退回本函式存在之前的
    行為（沒有 ticker filter），**不讓實體解析成為單點故障**。"""
    import json
    import re as _re

    q = (query or "").strip()
    if not q:
        return []
    _hit = _replay.get("ticker", q)
    if _hit is not _replay.MISS:
        return list(_hit)
    try:
        raw = call_llm([{"role": "system", "content": TICKER_RESOLUTION_SYSTEM_PROMPT},
                        {"role": "user", "content": q}], model_name)
        raw = _re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=_re.MULTILINE).strip()
        parsed = json.loads(raw)
        got = parsed.get("tickers") if isinstance(parsed, dict) else None
        if not isinstance(got, list):
            return []
        # 封閉集合過濾 ＋ 去重保序。⚠ 不做任何「看起來像」的模糊比對：LLM 吐了集合外的
        # 東西就是丟掉，不要試著猜它想講哪一家。
        out: list[str] = []
        for t in got:
            t = str(t).strip().upper()
            if t in _KB_TICKERS and t not in out:
                out.append(t)
    except Exception as e:
        print(f"WARN  - ticker resolution failed ({e!r}); no ticker filter")
        return []
    _replay.put("ticker", q, out)
    return out


def _append_llm_ticker(filters: list[dict], query: str, model_name: str) -> list[dict]:
    """regex 抽不到 ticker 時補一次 LLM 實體解析；抽得到就**完全不呼叫 LLM**。

    ⚠ 只補、不覆寫（理由見本節上方註解）。⚠ env `RQ_TICKER_LLM=0` 可整個關掉做 A/B。"""
    if any(f.get("field") == "ticker" for f in filters):
        return filters
    if os.getenv("RQ_TICKER_LLM", "1").strip().lower() in ("0", "off", "false", "no"):
        return filters
    tickers = resolve_tickers_llm(query, model_name)
    if not tickers:
        return filters
    print(f"DEBUG - ticker resolution (LLM): {tickers}")
    # 單一 → 字串、多個 → list，與 `_extract_structural_filters` 同一套形狀
    # （`build_qdrant_filter` 靠這個區分 MatchValue / MatchAny）。
    return filters + [{"field": "ticker",
                       "value": tickers[0] if len(tickers) == 1 else tickers,
                       "polarity": "include"}]


def _resolve_period_filter_llm(query: str, client, intent: dict) -> dict | None:
    """`period_ref` → 具體的 `report_period_code` 硬 filter（確定性，零 LLM）。

    只有 `latest` 會產生 filter：
      · `absolute` 由既有的 `parse_query_filters`（fiscal_year／fiscal_period）處理，這裡不搶
      · `range` 刻意**不注入**——多期題硬鎖單一期碼會直接答不出來
      · `none` 沒有期間可鎖
    多／零公司回 None：`build_qdrant_filter` 吃 flat AND list，**結構上寫不出 per-ticker 的
    OR-of-ANDs**（MatchAny 是全域 OR，會讓 A 公司的舊季通過 B 公司的期碼）。這是既有限制，
    見 BACKLOG〈「最新期間」路由的覆蓋面〉。"""
    if intent.get("period_ref") != "latest":
        return None
    tickers = _find_all_ticker_aliases(query.lower(), query)
    if len(tickers) != 1:
        return None
    # ⚠ `granularity` 缺漏時預設 **10-Q**，不是「所有類型裡最新的」。
    #   實測（col-11「微軟的雲端服務最近成長得快不快？」）：不設限會挑到 `MSFT_10K_2026`
    #   （年報財年序最新），把 gold 的 `MSFT_10Q_202603` 整個擠掉，gold_rank 1 → None。
    #   舊的 `_resolve_latest_quarter_filter` 本質上是**季路由**、只在偵測到年度意圖時讓路
    #   （`_ANNUAL_INTENT_RE`），這裡沿用同一個預設方向：**沒說就是問季**。
    kind = "10-K" if (intent.get("granularity") == "annual") else "10-Q"
    pick = ladder_pick(_get_period_ladder(client), tickers[0],
                       kind=kind, year=intent.get("fiscal_year"))
    if pick is None and intent.get("fiscal_year") is not None:
        # 指定年份挑不到（例：問 2026 但該公司當年還沒有那種 filing）→ 放掉年份限制再試一次。
        # ⚠ 只在「有 kind」時才退：完全不設限地挑「最新」會把年報與季報混在一起比，
        #   而那正是使用者可能沒指定的維度。
        pick = ladder_pick(_get_period_ladder(client), tickers[0], kind=kind) if kind else None
    if pick is None:
        return None
    return {"field": "report_period_code", "value": pick["period_code"],
            "polarity": "include", "routed_latest": True}


def _resolve_latest_quarter_filter(query: str, client) -> dict | None:
    """Gap 1（2026-08-02）：偵測「最新一季財報指標 + 單一公司」→ 回傳該公司最新 10-Q 的
    report_period_code include filter；任一觸發條件不滿足回 None。判定準則見上方註解。"""
    q = query or ""
    if not _LATEST_QUARTER_TIME_RE.search(q):
        return None
    if not _QUARTER_METRIC_RE.search(q):
        return None
    if _detect_period_basis(q):           # TTM 走既有口徑硬 filter，不搶
        return None
    if _ANNUAL_INTENT_RE.search(q):       # 年度/年報意圖 → 走 10-K + Tier fallback，不搶
        return None
    if _PERIOD_CODE_RE.search(q):         # 使用者已指定明確期碼，尊重原期間
        return None
    tickers = _find_all_ticker_aliases(q.lower(), q)
    if len(tickers) != 1:                 # 多/零公司 → 不明確，不路由
        return None
    latest = _get_latest_10q_periods(client).get(tickers[0])
    if not latest:
        return None
    # routed_latest 標記：Tier1 對此期碼放寬成「== 期碼 OR report_period_code 為空」，
    # 只排除「衝突期別的 filing」而不動 News/Fundamentals（其 report_period_code=None）。
    # 動機（col-15，2026-08-02）：新聞+財報混合題（如「馬斯克另一家公司上市搶風頭 + 這季賺多少」）
    # 會同時觸發本路由，若硬 filter「只收該期」會把新聞 gold 整批排除、Tier2 又因財報 chunk 充足
    # 不觸發。使用者/LLM 明確指定的期碼不帶此標記，維持原嚴格語意。
    return {"field": "report_period_code", "value": latest,
            "polarity": "include", "routed_latest": True}

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


# LLM query-filter 只可能抽出 filing_type / fiscal_year / fiscal_period；若問題裡「完全沒有」
# 這些字樣，呼叫 LLM 必然回空、純屬浪費（且是每次 retrieve、每個子問題、每個補救輪都打的高頻呼叫）。
# 這個 gate：偵測不到任何 filing/期別 hint → 直接跳過 LLM，只回 deterministic 抽取（ticker + yyyymm
# 期碼，本來就不需 LLM）。偏保守（寧可誤觸發去呼叫 LLM＝維持原行為，也不漏掉該抽的條件）。
# 涵蓋 LLM 能抽的全部目標：10-K/10-Q、年報/季報、fiscal/FY/Qx、獨立四位數年份（yyyymm 期碼不算）。
_FILING_HINT_RE = re.compile(
    r"10\s*-?\s*[kq]"
    r"|annual\s+report|quarterly\s+report|\bannual\b|\bquarterly\b"
    r"|\bfiscal\b|\bfy\b|\bq[1-4]\b"
    r"|年報|季報|年度|季度|財年|會計年度|全年|第\s*[一二三四1-4]\s*季"
    r"|\b20\d{2}\b",
    re.IGNORECASE,
)


def parse_query_filters(query: str, model_name: str = DEFAULT_MODEL) -> list[dict]:
    """用 LLM 把問題中『明確』提到的 filing 篩選條件抽取成結構化清單：
        [{"field": "fiscal_year", "value": "2025", "polarity": "include"}, ...]
    polarity 區分「要」(include -> must) 與「不要」(exclude -> must_not)。
    解析失敗或沒有明確條件時回傳 []（= 不套用篩選，行為等同原本）。

    效率 gate（見 _FILING_HINT_RE）：問題裡沒有任何 filing/期別字樣時，跳過 LLM 呼叫，
    只回 deterministic 抽取（ticker + yyyymm 期碼）——省掉最高頻的 retrieval-side LLM 呼叫，
    語意等價（LLM 對這類 query 本來就只會回空）。"""
    import json
    import re as _re

    if not _FILING_HINT_RE.search(query or ""):
        return _append_llm_ticker(_extract_structural_filters(query), query, model_name)

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
        return _append_llm_ticker(_extract_structural_filters(query), query, model_name)

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
    # ⚠ 三個 return 都要接上實體解析，漏一個就是「有些路徑解得出 AWS、有些解不出」。
    #   （這正是 web_search 那次的教訓：只改了觸發、漏改時效警語那一道。）
    return _append_llm_ticker(cleaned, query, model_name)


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
    # 重放快取（見 llm_replay）：未設 RAG_REPLAY_CACHE 時完全 no-op。
    _hit = _replay.get("translate_en", query)
    if _hit is not _replay.MISS:
        return _hit
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
        en = en or query
        _replay.put("translate_en", query, en)
        return en
    except Exception as e:
        print(f"WARN  - rerank query translation failed ({e!r}); using original query")
        return query


def build_qdrant_filter(filters: list[dict], strict: bool = False,
                        relax_empty_fields: set | None = None):
    """把 parse_query_filters() 的結果轉成 qdrant_client.models.Filter。

    strict=True（Tier 1）：include 條件完全不加 IsEmptyCondition，確保只撈到
      明確命中所有指定欄位的 chunk——用於 filing 識別碼明確的查找型 query。
      fiscal_year 仍然同時比對 report_label_year（雙座標系 OR）。

    strict=False（Tier 3，預設）：include 條件包成 should=[match OR IsEmpty(field)]，
      讓沒有 filing metadata 的 News/Fundamentals/IncomeStatement chunk 也能進候選。

    relax_empty_fields（strict 模式專用）：即使在 Tier1 strict 下，這些欄位也改成
      should=[match OR IsEmpty(field)]。用於「最新一季」路由（Gap 1，col-15）——只想排除
      衝突期別的 filing，不想連沒有期碼的 News/Fundamentals 一起排除。

    exclude 條件兩種模式都維持單純 must_not。
    """
    from qdrant_client import models

    relax_empty_fields = relax_empty_fields or set()
    must, must_not = [], []
    for f in filters:
        field, value = f["field"], f["value"]
        # value 為 list（如多個 ticker）→ MatchAny（OR/IN 查詢）；單值 → MatchValue。
        _match = models.MatchAny(any=value) if isinstance(value, list) else models.MatchValue(value=value)
        condition = models.FieldCondition(key=field, match=_match)

        if f["polarity"] == "exclude":
            must_not.append(condition)
            if field == "fiscal_year":
                must_not.append(models.FieldCondition(
                    key="report_label_year", match=models.MatchValue(value=value),
                ))
            continue

        if strict and field in relax_empty_fields:
            # Tier 1 但放寬此欄：值相符 OR 欄位為空（放行無期碼的 News/Fundamentals）
            must.append(models.Filter(should=[
                condition,
                models.IsEmptyCondition(is_empty=models.PayloadField(key=field)),
            ]))
        elif strict:
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


def tier1_hit_is_qualified(filters: list[dict], fused_points: list) -> bool:
    """Tier 1 撈到東西了，但**「命中」不等於「答得了」**——這個函式判它算不算真的命中。

    背景：`fiscal_year` 的 include 條件在 strict 模式下是與 `report_label_year` 的**雙座標系
    OR**（見 `build_qdrant_filter`）。實測 `multiyear` 的 77 份 filing（`mdna` 那 21 份的超集）：
    **10-K 的 `fiscal_year` 與 `report_label_year` 永遠相等（21/21），會分歧的只有 10-Q**
    （15/56，其中 3 份在生產的 `mdna` 裡）。所以那半個 OR 的**全部效果**就是「額外放行曆年
    標籤是 V、但財年不是 V 的 10-Q」。

    當它是 Tier 1 **唯一**的命中理由時，結果一定是拿一份別的財年的季報去回答一個財年問題，
    而且因為 Tier 1 算命中，**Tier 2 的降級與 `_build_fallback_note` 的揭露語都不會發生**。
    2026-08-27 在生產 collection 上實測：問「Microsoft 在 2025 財年的營收是多少？」→ top-5
    五席全是 `MSFT_10Q_202512`（FY2026 Q2、`report_label_year=2025`）、`note` 是空字串，
    而含 FY2025 三年表的 `MSFT_10K_2026` 被 `fiscal_year=2025` 擋在外面。

    → 規則：**label-year 那半個 OR 只能「放寬」命中，不能自己「構成」命中。** 回 False 時
    呼叫端把 Tier 1 當落空、照常降級 Tier 2——那條路撈得到年報，而且會附上揭露語。

    ⚠ **`fiscal_year` 為空的點算合格**（News/Fundamentals，由 `relax_empty_fields` 刻意放行）：
    它們不是被 label-year 放進來的，一起判不合格會誤殺「最新一季」那條路（col-15）。
    ⚠ 這個修法的作用對象在生產語料上**只有兩組** `(ticker, year)`（`MSFT 2025`／`NVDA 2025`），
    是刻意的：blast radius 可枚舉，不是全域行為改變。"""
    want = next((f["value"] for f in filters
                 if f["field"] == "fiscal_year" and f["polarity"] == "include"), None)
    if want is None:
        return True                                    # 沒有年份約束 → 不干預
    wanted = set(want) if isinstance(want, list) else {want}
    for p in fused_points:
        fy = (p.payload or {}).get("fiscal_year")
        if not fy or fy in wanted:
            return True
    return False


def _build_tier2_filter(filters: list[dict]):
    """Tier 2：只保留 ticker + filing_type + doc_type（放掉年份/期碼），strict 模式。
    讓「問 2026 但庫裡只有 2025」退回最新一份 filing，而不是拒答。
    doc_type 一併保留：LLM 仍可能抽出 doc_type（如「年報」→ 10-K），Tier1 落空時 Tier2 要守住
    那個意圖。⚠ 2026-08-19 前這裡守的是「只回新聞」，KB 拔除新聞後不再有 doc_type=news 這條路。"""
    tier2 = [f for f in filters
             if f["field"] in ("ticker", "mentioned_tickers", "filing_type", "doc_type")
             and f["polarity"] == "include"]
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

    actual_str = ", ".join(sorted(actual, reverse=True)) if actual else "未知"
    scope_parts = [", ".join(f["value"]) if isinstance(f["value"], list) else f["value"]
                   for f in [ticker_f, type_f] if f]
    scope = " ".join(scope_parts) or "所詢問的文件"

    # 繁中揭露句：此字串同時是（a）api_server SSE 直接顯示給使用者的提示、（b）注入
    # generator prompt 的事實。改中文的動機（2026-08-02）：中文系統對中文使用者顯示英文
    # note 不一致；且注入英文句要 generator 二次翻譯、忠實度不穩。值仍全動態（scope/
    # requested/actual 皆由 filter 與實際 payload 算出，無任何年份寫死）。呈現層的「請揭露」
    # 指示在 build_user_prompt 以模板包裹，與此使用者可見句分離。
    # 「所詢問的是哪個座標系」要講清楚，否則這句話會自相矛盾：MSFT_10Q_202512 的
    # fiscal_year 是 2026、report_label_year 是 2025，於是問「FY2025」降級之後，
    # `actual` 裡會同時出現 2025 → 「沒有 2025 的資料，改用最接近的（…2025…）」。
    # 2026-08-27 `tier1_hit_is_qualified` 上線後，走到這條路的曆年查詢變多，這句話就
    # 開始被看見了。⚠ 這是**使用者可見句 ＋ 注入 generator 的事實**，措辭要精確。
    coord = "財年" if year_f else "期間"
    return (
        f"知識庫沒有「{scope}」在所詢問{coord}（{requested}）的資料，"
        f"以下回答改用最接近的可得期間（{actual_str}）。"
    )


_NEAR_DUP_NUM = re.compile(r"\b\d{1,3},\d{3}\b")


def _suppress_near_duplicates(ranked: list[dict], min_shared: int = 5,
                              min_cover: float = 0.9) -> tuple[list[dict], int]:
    """在 rerank 降序池上做確定性近重複抑制：**同一來源檔**、且低分那個的千分位數字集合被
    高分那個近乎完整包含（子集）時，丟掉低分那個。回傳 (過濾後的池, 丟棄數)。零 LLM。

    ⛔ **預設關閉**（env `RAG_SUPPRESS_NEAR_DUP=1` 才啟用）。原因見下面的量測——它解決的
    問題在下游其實已經被處理掉大半，收益落在量不出來的區間，但誤刪風險是真的。

    量測（`us_stock_rag_edgar_period`，100 題）：
      · top-8 檢索池內「同來源且共享 >=3 個千分位數字」的重複對＝**149 對 / 32 題**
        （組合分佈：text+text 81、table+text 61、table+table 7）
      · 但這些**不是最後餵給生成器的東西**。agentic 的 Grader（`_check_sufficiency` 圈
        `relevant_ids`）只留下約一半候選 → **最終 contexts 內只剩 18 對 / 11 題**，
        LLM 相關性判斷已經順手清掉 88% 的重複。
      · 殘留最嚴重的是 lex-14（Meta FY2025 Diluted EPS）：最後 4 個 chunk 全部來自
        `META_10K_2025.html` 且兩兩重複（C(4,2)=6 對全中）。真正的病是**來源單一化**
        （top-8 全部來自同一份文件），不只是「同一張表出現兩次」。

    規則刻意保守,三個條件同時成立才丟：
      1. 同一個 `source`（跨檔絕不比——不同公司/不同期的相同數字是巧合,不是重複）
      2. 共享 >= min_shared 個千分位數字（<5 個容易是年份/股數等偶然相同）
      3. 低分那個自己的數字有 >= min_cover 落在高分那個裡（**它是子集**）
    **只丟子集、只丟低分的那個**：反過來丟超集會損失資訊,而高分那個是 cross-encoder
    認為更相關的。數字少於 min_shared 個的 chunk（純敘述段落）永遠不會被丟。
    """
    kept: list[dict] = []
    kept_nums: list[tuple[str, set]] = []
    dropped = 0
    for c in ranked:
        nums = set(_NEAR_DUP_NUM.findall(c.get("content", "")))
        src = c.get("source", "")
        redundant = False
        if len(nums) >= min_shared:
            for ksrc, knums in kept_nums:
                if ksrc != src:
                    continue
                shared = nums & knums
                if len(shared) >= min_shared and len(shared) / len(nums) >= min_cover:
                    redundant = True
                    break
        if redundant:
            dropped += 1
            continue
        kept.append(c)
        kept_nums.append((src, nums))
    return kept, dropped


def _section_group(c: dict):
    """`(ticker, filing_type, item_id)` ——「同一家公司的同一節」。只有 filing 有 `item_id`；
    News／Fundamentals 回 None（它們沒有跨期競爭的概念，硬塞進來只會製造假的排擠訊號）。"""
    if c.get("filing_type") not in ("10-K", "10-Q") or not c.get("item_id"):
        return None
    return (c.get("ticker", ""), c["filing_type"], c["item_id"])


def _collapse_cross_period_sections(ranked: list[dict], keep_per_group: int = 1,
                                    prefer: str = "rank") -> tuple[list[dict], int]:
    """跨期 field collapsing：同一節（`_section_group`）最多只讓 `keep_per_group` **份 filing**
    佔位，其餘丟掉。回傳 (過濾後的池, 丟棄數)。零 LLM、零參數（`keep_per_group` 除外）。

    **這是搜尋引擎的標準原語**（Solr `CollapsingQParser`／Elasticsearch `collapse`／Google
    「同一站點只顯示一兩筆」），不是自創。⚠ **不是 MMR**：MMR 靠 embedding 相似度、帶 λ 超參數
    ＝連續權衡；這裡是 metadata 分組的**硬保證**。本專案的量尺已飽和（六個 RAGAS 指標五個達
    gold 上限），**沒有能調 λ 的尺**，所以只有零參數的版本可被零噪音斷言驗證。

    **為什麼需要它**（2026-08-19 多年語料壓測，詳見 docs/EVAL.md〈多年語料的期別干擾〉）：
    KB 從 1 年長到 ~2.5 年後，「問題沒提期間」那類題的 top-5 有 **48%** 席位被**同一節的別年份**
    佔走（單年時是 8%）。10-K 的 Item 1／1A 年年 80~90% 逐字相同，embedding 與 cross-encoder
    **兩層都分不出來**。最極端的實例：`col-02`「微軟怎麼把 AI 塞進 Office」top-5 有 4 席是
    MSFT 三個年度 10-K 的同一節 `Item_1`。

    ⚠ **同一份 filing 的同一節有多個 chunk 時全部保留**（`item_chunk_index` 0/1/2 是互補內容，
    不是重複）。只丟「額外的 filing」。這條界線就是本函式與 `_suppress_near_duplicates` 的分野：
    後者比的是**同檔內的數字集合包含關係**、規則一明寫「跨檔絕不比」；本函式要的是**章節同一性、
    跨檔才比**。概念不同，所以是兩個函式而不是改那一個。

    ⚠ **收益指標與本規則是同一個定義** → 開了之後「排擠席位」必然歸零，那是套套邏輯不是證據。
    驗收要看 (a) 讓出來的席位換到了什麼（gold@k）(b) 有沒有弄壞本來就需要多期的題
    （`eval/period_probe_trend_queries.json` 的陰性對照）。

    **`prefer` ＝ 組內留誰**，這一格是實測逼出來的、不是設計時想到的：
      · `"rank"`   留 rerank 最高的那份 filing（最直覺）。**但 cross-encoder 對年份無感**
        ——實測 `sem-03`／`sem-04` 的 gold `MSFT_10K_2026` 在 `MSFT Item_1` 那組裡不是最高分，
        於是 collapse 留下 `MSFT_10K_2024`、**把 gold 整個刪掉**（`gold_rank` 從 3 變成 None）。
        降低了排擠卻換來 F2，正是本來要防的東西換個方向發作。
      · `"newest"` 留**財年序最新**的那份（`fiscal_rank`）。只決定「同一節顯示哪一年」，
        不決定「這一節要不要出現」，所以比全域時間衰減安全得多。⚠ 但它仍然是一個時間先驗，
        對「問 FY2024 那一節」的題是錯的 → 期別 filter 生效時本函式其實是 no-op（同組只剩
        一份 filing），所以真正吃到這個先驗的只有**期間無法解析**的題。

    **預設開啟**（`RAG_CROSS_PERIOD_COLLAPSE=0` 可關）。參數與四臂實測見呼叫端註解，
    以及 docs/EVAL.md〈跨期 field collapsing〉。
    """
    if keep_per_group < 1:
        return ranked, 0

    if prefer == "newest":
        # 先決定每組的贏家（財年序由新到舊取前 N），再照原順序過濾——這樣輸出仍是
        # rerank 降序，只是組內換了代表。`fiscal_rank` 為 None 的排最後（不參與時間比較，
        # 但仍可能因為 rerank 名次而入選）。
        best: dict[tuple, list[str]] = {}
        seen_src: dict[tuple, dict] = {}
        for c in ranked:
            g = _section_group(c)
            if g is None:
                continue
            seen_src.setdefault(g, {}).setdefault(c["source"], c.get("fiscal_rank"))
        for g, srcs in seen_src.items():
            best[g] = [s for s, _ in sorted(
                srcs.items(), key=lambda kv: (kv[1] is not None, kv[1] or (0, 0)),
                reverse=True)][:keep_per_group]
        kept, dropped = [], 0
        for c in ranked:
            g = _section_group(c)
            if g is None or c["source"] in best.get(g, ()):
                kept.append(c)
            else:
                dropped += 1
        return kept, dropped

    seen: dict[tuple, list[str]] = {}
    kept: list[dict] = []
    dropped = 0
    for c in ranked:
        g = _section_group(c)
        if g is None:
            kept.append(c)
            continue
        srcs = seen.setdefault(g, [])
        if c["source"] in srcs:        # 同一份 filing 的同節多 chunk：互補，全留
            kept.append(c)
            continue
        if len(srcs) >= keep_per_group:
            dropped += 1
            continue
        srcs.append(c["source"])
        kept.append(c)
    return kept, dropped


def _ensure_ticker_coverage(ranked: list[dict], selected: list[dict], want_tickers) -> list[dict]:
    """每家 ticker 保底覆蓋（Dynamic Top-K Expansion，Phase 1）：保證 want_tickers 每一家在
    selected（已截斷的回傳/commit 集合）裡至少有一個 chunk；缺的就從 ranked（完整 rerank 降序池）
    補上該家分數最高的一個，必要時擴充集合大小。回傳仍按 rerank 降序。

    動機：cross-encoder 分數跨公司不可比，top-k 截斷會讓高分公司佔滿名額、把其他被比較的公司
    整個擠掉（mh-01：四公司題只有一家的 chunk 存活）。ranked 需已按 raw_rerank_score 降序。
    單一或零 ticker（want<=1）時原樣回傳（一般單公司題不需保底）。"""
    want = [want_tickers] if isinstance(want_tickers, str) else list(want_tickers or [])
    if len(want) <= 1:
        return selected
    want_set = set(want)
    present = {c.get("ticker", "") for c in selected}
    missing = want_set - present
    if not missing:
        return selected
    out = list(selected)
    seen = {(c["source"], c["chunk_index"]) for c in selected}
    for c in ranked:                       # ranked 已降序：每家第一個命中就是該家最高分 chunk
        if not missing:
            break
        t = c.get("ticker", "")
        key = (c["source"], c["chunk_index"])
        if t in missing and key not in seen:
            out.append(c)
            seen.add(key)
            missing.discard(t)
    out.sort(key=lambda x: x["raw_rerank_score"], reverse=True)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Hybrid Retrieval via Qdrant (server-side RRF) + client-side rerank
# ══════════════════════════════════════════════════════════════════════════════

def _payload_to_chunk(payload: dict, rrf_score: float, raw_rerank: float) -> dict:
    """Qdrant payload → 下游共用的 chunk dict。**這是唯一的建構點**（retrieve 與任何補撈路徑都走它）。

    ⚠ 為什麼抽成具名函式：2026-08-21 實測，`period_basis` 在 payload 裡、也建了索引，
      但**沒有被帶進 chunk dict**，於是 `agentic_rag_v2._basis_disclosure_notice`
      在生產上結構性永遠不觸發。而它的閘門之所以全綠，是因為測試自己造 dict、
      親手寫上了 `period_basis`——量尺與被測物耦合，這是同類第五次。
      抽成函式之後，閘門可以拿**真實 payload 餵這個生產建構子**，
      「欄位有沒有傳下來」就變成可證偽的。加欄位請一律加在這裡。"""
    source = payload.get("source", "unknown")
    return {
        "content":          payload.get("document", ""),
        "source":           source,
        "source_type":      infer_source_type(source),
        "ticker":           payload.get("ticker", ""),   # 供每家保底覆蓋（_ensure_ticker_coverage）
        "chunk_index":      payload.get("chunk_index", 0),
        "chunk_type":       payload.get("chunk_type", "n/a"),
        # ↓ 期別三欄（2026-08-19 補）。加進來之前，任何需要「這個 chunk 是哪一節、哪一期」
        #   的下游都得自己再掃一次 Qdrant——實測被迫這麼做的有三處：
        #   agentic_rag_v2 的期別 validator、`_scan_kb_coverage`、
        #   eval/probe_temporal_interference。payload 本來就有，只是沒帶出來。
        "item_id":          payload.get("item_id", ""),
        "filing_type":      payload.get("filing_type", ""),
        "period_code":      str(payload.get("report_period_code") or ""),
        # 數字口徑（TTM／fiscal_year）。Fundamentals 是 TTM，filing 是 fiscal_year；
        # 供 agentic 的口徑揭露 validator 判斷「答案引到的是哪一種口徑」。
        "period_basis":     payload.get("period_basis", ""),
        # ⚠ 比新舊一律用這個 `(fiscal_year, 期別序)`，**不要拿 `period_code` 比字串**
        #   ——10-K 是 4 位、10-Q 是 6 位，混比會時對時錯（實測 385 對裡 31 對判反，
        #   見 `_get_period_ladder` 上方註解）。缺欄位時是 None ＝ 不參與比較。
        "fiscal_rank":      _fiscal_sort_key(payload.get("fiscal_year"),
                                             payload.get("fiscal_period")),
        "rrf_score":        rrf_score,
        "raw_rerank_score": raw_rerank,
        "rerank_score":     float(1 / (1 + math.exp(-raw_rerank))),
    }


def retrieve(query: str, bge_m3, rerank_model, client, top_k: int = DEFAULT_TOP_K,
             model_name: str = DEFAULT_MODEL, enable_rewrite: bool = False,
             disable_filter: bool = False, rewrite_merge_top_n: int | None = None,
             rewrite_fusion: bool = False, return_pool: bool = False,
             rerank_multi_query: bool = False, translate_query_en: bool = False,
             sparse_translate_en: bool = False, full_translate_en: bool = False):
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
    # ⚠ 2026-08-19 **KB 不再收新聞**（見 CLAUDE.md〈資料範圍〉）。原本這裡有兩段：
    #   ① `looks_like_news_query` → 注入 `doc_type=news` 硬 filter
    #   ② 新聞題把 `ticker` 硬篩改寫成 `mentioned_tickers`
    # 兩段都已移除。① 是硬編碼詞表做的**硬排除**，代價是「NVIDIA 最新財報…加上新聞中…」
    # 這種複合題只因為出現「新聞」二字，整題就被鎖死在 news chunk 裡、filing gold 連候選池
    # 都進不去（實測 mi-01／mi-14／mh-07 三題，是 KB 拔新聞前最大的殘留失敗）。
    # 「市場現在怎麼看」改由 agentic 的 live web 路徑供應，那條路有發布日、來源白名單與
    # 過時過濾，是這件事的正解。復活條件：KB 重新收新聞語料時（屆時 ① 要改成加權不是硬篩）。
    # 註：period_basis（TTM 口徑）已由 _extract_structural_filters 統一吐出（走 parse_query_filters
    # 的 deterministic 抽取，含 gate-skip 路徑），不再在此另外注入。硬 filter 效果不變——Tier1
    # strict 命中 Fundamentals，某 ticker 無 TTM chunk 則自動退回 Tier2/3（不會因 basis 過濾而拒答）。
    #
    # 「最新一季」deterministic 路由（Gap 1，2026-08-02）：問最新單季財報指標、單一公司、且未
    # 自帶期碼時，解出該公司 KB 中最新 10-Q 期碼注入 report_period_code 硬 filter，排除舊季/年報
    # chunk 對排序的污染（見 _resolve_latest_quarter_filter 註解）。已有 report_period_code（使用者指定或
    # LLM 抽出）時不覆蓋。coverage 掃描需 client，故在此（而非 parse_query_filters）注入。
    # env RQ_LATEST_QUARTER_ROUTING=0/off/false 可關閉（供 eval on/off A/B；預設開）
    _lq_routing_on = os.getenv("RQ_LATEST_QUARTER_ROUTING", "1").strip().lower() \
        not in ("0", "off", "false", "no")
    # 期間意圖交 LLM 判（`resolve_period_intent`），取代 `_resolve_latest_quarter_filter`
    # 的兩道硬編碼詞表。動機：那兩道詞表擋掉 eval_set 裡 20/21 題的相對期間指稱（見
    # resolve_period_intent 上方註解），而多年語料下漏一題的代價從「差一季」變成「差兩年」。
    # **2026-08-27 預設翻成開**（env `RQ_PERIOD_INTENT_LLM=0` 可關掉做 A/B）。
    # ⚠ 這一翻**必須與 COLLECTION_NAME 換成 multiyear 同一次做**：實測多年語料上 A 關掉時
    # 當期題會退步（收益探針的 control 臂 gold@5 4/4 → 3/4、同節別期席位 0 → 7），而
    # `range` 意圖跳過 cross-period collapse 那條趨勢題保護也要靠 A。
    _period_intent = None
    if _lq_routing_on and not disable_filter \
            and not any(f["field"] == "report_period_code" for f in detected_filters):
        if os.getenv("RQ_PERIOD_INTENT_LLM", "1").strip().lower() \
                not in ("0", "off", "false", "no"):
            _period_intent = resolve_period_intent(query, model_name)
            _lq_filter = _resolve_period_filter_llm(query, client, _period_intent)
            print(f"DEBUG - period intent (LLM): {_period_intent}")
        else:
            _lq_filter = _resolve_latest_quarter_filter(query, client)
        if _lq_filter is not None:
            detected_filters.append(_lq_filter)
            print(f"DEBUG - latest-quarter routing → report_period_code={_lq_filter['value']}")
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
    _want_en = translate_query_en or full_translate_en
    en_query = translate_query_to_english(query, model_name) if _want_en else query
    if _want_en and en_query != query:
        # ascii-safe：Windows cp950 終端無法印某些字元，debug 訊息不值得為此崩潰
        _safe = en_query.encode("ascii", "replace").decode()
        _scope = "recall+rerank" if full_translate_en else "rerank only"
        print(f"DEBUG - Query translated to EN ({_scope}): {_safe!r}")

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

    # 召回 query：預設用原始 query（見上方 0b 說明：翻譯對 dense/sparse 中性偏負）；
    # full_translate_en=True 時整個檢索階段（dense+sparse 召回 + rerank）一律改用英文譯句
    # ——2026-07-26 定案的架構決定：中文問、中文答，但檢索中間層全英文以消除 cross-lingual
    # 失真（推翻先前「dense 召回用英文會傷 sem-11」的保留結論，改為端到端 eval 背書）。
    recall_query = en_query if full_translate_en else query
    q_vecs = _encode(recall_query)
    if sparse_translate_en and not full_translate_en:
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
    if full_translate_en and en_query != query:
        rerank_queries = [en_query]           # 一律英文：召回與 rerank 都用單一英文譯句
    elif translate_query_en and en_query != query:
        rerank_queries = [query, en_query]    # 舊模式：原句 + 英譯句，逐候選取 max
    else:
        rerank_queries = [rerank_query]  # rerank_multi_query=True 時，額外併入 rewrite 變體

    if detected_filters:
        has_period_code = any(f["field"] == "report_period_code" and f["polarity"] == "include"
                              for f in detected_filters)
        if has_period_code:
            # 只保留 ticker + report_period_code + doc_type（唯一定位，排除衝突的年份欄位；
            # doc_type 保留是為了「NVDA 202605 的新聞」這種罕見的期別+新聞混合意圖——news 無
            # report_period_code 故 Tier1 strict 會落空，隨即由 Tier2（含 doc_type）接住只回新聞）
            tier1_key_filters = [f for f in detected_filters
                                 if f["field"] in ("ticker", "report_period_code", "doc_type")
                                 and f["polarity"] == "include"]
        else:
            tier1_key_filters = detected_filters

        # 「最新一季」路由（routed_latest）產生的 report_period_code 在 Tier1 放寬成
        # 「== 期碼 OR 期碼為空」——排除衝突期別 filing，但保留 News/Fundamentals（col-15）。
        _relax = {f["field"] for f in tier1_key_filters if f.get("routed_latest")}
        tier1_filter = build_qdrant_filter(tier1_key_filters, strict=True, relax_empty_fields=_relax)
        fused = _query_points(q_vecs, tier1_filter)
        if fused and not tier1_hit_is_qualified(tier1_key_filters, fused):
            # 只靠 report_label_year（曆年標籤）命中 → 當作落空，讓 Tier 2 接手。
            # 理由與實測見 tier1_hit_is_qualified 的 docstring。
            print("DEBUG - Tier 1 命中但只靠 report_label_year（曆年標籤，財年不符）"
                  "→ 視為落空，降級 Tier 2")
            fused = []
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

    enriched = [_payload_to_chunk(p.payload or {}, float(p.score), float(raw))
                for p, raw in zip(fused, raw_scores)]

    enriched.sort(key=lambda x: x["raw_rerank_score"], reverse=True)

    print("DEBUG - Reranked top candidates:")
    for i, c in enumerate(enriched[:top_k], start=1):
        print(
            f"  [{i}] {c['source']} | chunk #{c['chunk_index']} | "
            f"type={c['source_type']} | rrf={c['rrf_score']:.6f} | "
            f"raw={c['raw_rerank_score']:.4f} | sigmoid={c['rerank_score']:.4f}"
        )

    # 每家保底覆蓋：query 明確點名多家 ticker 時，保證每一家在回傳 top_k 裡至少有一個 chunk
    # （缺的從完整排序池補上，必要時擴充；見 _ensure_ticker_coverage）。
    # 近重複抑制（預設關閉，見 _suppress_near_duplicates）：在截斷成 top_k 之前做，
    # 被丟掉的名額自動由池裡下一個候選遞補。
    if os.getenv("RAG_SUPPRESS_NEAR_DUP", "") == "1":
        enriched, _n_dup = _suppress_near_duplicates(enriched)
        if _n_dup:
            print(f"DEBUG - Near-dup suppression: dropped {_n_dup} redundant candidates")

    # 跨期 field collapsing（預設關閉，見 _collapse_cross_period_sections）：同一節最多留
    # N 份 filing。跟近重複抑制一樣掛在截斷之前，讓出的名額由池裡下一個候選遞補。
    # 預設 keep=2 / prefer=newest 是量出來的，不是選的——四個臂的完整對照見
    # docs/EVAL.md〈跨期 field collapsing〉。摘要：keep=1 收益較高（gold@5 0.878 vs 0.857）
    # 但代價是**新增 3 個沉默失效**（事件錨定題）＋ 趨勢題期別數 −38%；keep=2/newest 在每個
    # 安全性軸上都不退步。`prefer="rank"` 更是嚴重退步（gold@5 0.653），別改回去。
    # ⚠ `period_ref == "range"` 時**整個跳過 collapse**：趨勢／逐年比較題本來就需要多個
    #   期別，collapse 會把它們砍掉（實測趨勢題期別數 keep=1 −38%、keep=2 −6%）。這是 A
    #   反過來保護 B 的一格——沒有 LLM 的期間意圖就做不出這個條件式。
    _skip_collapse = bool(_period_intent and _period_intent.get("period_ref") == "range")
    if _skip_collapse:
        print("DEBUG - Cross-period collapse skipped (period_ref=range，多期題需要跨期證據)")
    # **預設開啟**（`RAG_CROSS_PERIOD_COLLAPSE=0` 可關）。參數 keep=2／prefer=newest 是量出來
    # 的、不是選的——四臂完整對照見 docs/EVAL.md〈跨期 field collapsing〉：
    #   · `prefer="rank"`（留 rerank 最高）**嚴重退步**：gold@5 0.837 → 0.653、錯期率 ×3.5。
    #     cross-encoder 對年份無感，「分數最高」與「對的年份」無關。別改回去。
    #   · `keep=1` 聚合較好（gold@5 0.878 vs 0.857）但代價是**新增 3 個沉默失效**（事件錨定題
    #     ——Wiz 收購金額只揭露在 `202603`，更新的季報不重複）、趨勢題期別數 −38%，
    #     **而且會讓現在的單年生產 collection 退步**（gold@5 0.918 → 0.878）。
    #   · `keep=2` 在單年 collection 上是**逐題完全的 no-op**（0/49 題 top-5 有任何變化），
    #     在多年上改 23/49 題、gold@5 +0.020、排擠率 −30%、零新增失效。
    # 敢開預設就是因為最後那一條：**資料還不需要時它不作用，需要時自己生效**。
    if os.getenv("RAG_CROSS_PERIOD_COLLAPSE", "1") == "1" and not _skip_collapse:
        _keep = max(1, int(os.getenv("RAG_CROSS_PERIOD_KEEP", "2")))
        _prefer = os.getenv("RAG_CROSS_PERIOD_PREFER", "newest")
        enriched, _n_col = _collapse_cross_period_sections(
            enriched, keep_per_group=_keep, prefer=_prefer)
        if _n_col:
            print(f"DEBUG - Cross-period collapse (keep={_keep}, prefer={_prefer}): "
                  f"dropped {_n_col} same-section candidates")

    result = enriched[:top_k]
    _ticker_f = next((f for f in detected_filters
                      if f["field"] == "ticker" and f["polarity"] == "include"), None)
    if _ticker_f:
        result = _ensure_ticker_coverage(enriched, result, _ticker_f["value"])

    if return_pool:
        return result, fallback_note, pre_rerank_pool
    return result, fallback_note


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

    # fallback_note 是「只有 retrieval 層知道」的事實（使用者問的期間 KB 沒有、已降級到最近可得）。
    # 用中文指示模板包裹，要 generator 在回答開頭主動揭露（而非靜默拿替代期間當原期間答）。
    note_section = (
        "=== 資料期間提示（系統偵測，請在回答開頭以繁體中文主動告知使用者）===\n"
        f"{fallback_note}\n\n"
    ) if fallback_note else ""

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
    其餘走 NVIDIA NIM OpenAI-compatible endpoint（NVIDIA_API_KEY）——2026-07-21 統一由 Groq
    換過來（見 agentic_rag_nv.py 的驗證：單一 key、無 Groq 免費層那種 TPD/TPM 硬牆）。
    messages 為 OpenAI 格式（[{role, content}]），Gemini 路徑會就地轉成 contents + system_instruction。

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

    # 其餘一律走 NVIDIA NIM（OpenAI-compatible）；messages 已是 OpenAI 格式可直接送。
    import openai

    nv_key = os.getenv("NVIDIA_API_KEY")
    if not nv_key:
        raise RuntimeError("NVIDIA_API_KEY 未設定，call_llm 無法路由到 NVIDIA NIM。")
    client = openai.OpenAI(api_key=nv_key, base_url=NVIDIA_BASE_URL)
    resp = client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)
    return resp.choices[0].message.content


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
