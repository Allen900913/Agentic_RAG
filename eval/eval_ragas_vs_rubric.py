"""RAGAS 六指標評分（本專案 2026-07-21 起改為純 RAGAS 架構，不再用自製 rubric 判定）。

目的：用業界標準 RAGAS 量六個指標——
    Context Recall / Context Precision / Context Relevance /
    Faithfulness / Answer Relevance / Answer Correctness
六個指標全部只需要 (query, answer, contexts, reference)，**都不需要 rubric**
（must_include/weight/is_critical 是本專案舊自製判定機制的產物，RAGAS 從未用到它）。
eval_set.json 因此不再需要為每題撰寫 rubric checklist，只需 query + relevant（黃金來源
檔，供 gen_reference_answers.py 撈 chunk）+ answerable；reference 由該腳本自動生成。

若某結果檔仍是舊版（含 rubric-based `correctness`），本腳本會把它當「自製對照分數」
一併印出（欄名沿用 `rubric`，純historical，非必要）——純新架構下這欄會是 n/a，不影響
六指標本身的計算。

⚠️ 環境陷阱（重要）：
ragas 0.2.x 需要 langchain 0.3.x；專案 `.venv` 用 langchain 1.x（生產 ingest 的
SemanticChunker 依賴它）。把 ragas 裝進 .venv 會降 langchain 版本弄壞生產 ingest。
因此本腳本**刻意 standalone**（不 import rag_query），跑在獨立的 `.venv-ragas`，只讀
eval_set.json、reference_answers.json 與**已存的結果檔（含 answer + contexts）**，
不重跑 retrieve/generate。

⚠️ 環境陷阱（重要）：
ragas 0.2.x 需要 langchain 0.3.x；專案 `.venv` 用 langchain 1.x（生產 ingest 的
SemanticChunker 依賴它）。把 ragas 裝進 .venv 會降 langchain 版本弄壞生產 ingest。
因此本腳本**刻意 standalone**（不 import rag_query），跑在獨立的 `.venv-ragas`，只讀
eval_set.json、reference_answers.json 與**已存的結果檔（含 answer + contexts）**，
不重跑 retrieve/generate。

⚠️ 資料前置需求：
本腳本的 4 個 context 指標需要「檢索到的 chunk 文字」。結果檔的 record 必須含
`contexts` 欄位（eval_generation_llm_judge.py 於 2026-07-21 起落盤）。舊結果檔沒有
contexts 的話，context 系指標會是 NaN（answer_relevancy/answer_correctness 仍可算）。

執行（用裝了 ragas 的 .venv-ragas，非 .venv）：
    .venv-ragas/Scripts/python.exe eval/eval_ragas_vs_rubric.py \
        --from-results eval/generation_judge_run1.json ... \
        --reference-file eval/reference_answers.json \
        --output eval/ragas_vs_rubric.json

RAGAS 判定 LLM = 下方 `DEFAULT_RAGAS_MODEL`（走 NVIDIA NIM）。**刻意不在這裡複述模型名**：
2026-09-03/04 十二小時內換了兩次，而這一行留在 120b 沒跟上。
RAGAS 語意相似 embedding = BAAI/bge-m3（對齊生產 dense embedding）。

2026-07-23：拿掉 Groq 支援（GROQ_BASE_URL、EVAL_LLM_PROVIDER 切換、4-key 輪換），全部改走
NVIDIA NIM——全專案已於 2026-07-21/22 統一遷移，繼續保留 Groq 分支只會讓「忘記覆寫預設值」
這類設定漂移悄悄發生（同一類 bug 已在 eval_generation_llm_judge.py 炸過一次：--gen-model
沿用舊的 Groq 模型名稱，打去 NVIDIA endpoint 直接 404）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# RAGAS judge。**2026-09-04 從 `openai/gpt-oss-120b` 換過來**——那支於 2026-09-03T08:00Z
# 被 NVIDIA 退役（`410 Gone`），留著死常數第一次跑就炸，而且它**保不住任何可比性**：
# 舊 judge 產不出新數字，可比性在退役那一刻就沒了（2026-09-04 一度把「留著是為了可比性」
# 寫進 BACKLOG，那是錯的，已更正）。
# **同日再從 `gpt-oss-20b` 換成 `google/gemma-4-31b-it`**（20b 只當了幾小時的預設，
# 沒有跑過任何一輪全量，所以這一次換不損失任何可比性）。理由是**跑太慢**：一輪 65 題
# RAGAS 六指標是幾百次呼叫，judge 的中位延遲直接決定能不能跑。
# **選型證據**（`scratchpad/judge_bakeoff.py`，5 模型 × 3 個 judge 角色 × 3 輪，
# 角色是 NLI 裁決／rubric 評分／長 context faithfulness，判準沿用本 repo 的
# **輸出穩定性 ＋ 延遲**）：
#   | 模型                              | parse | 判定對 | 中位   | max    | 事故     |
#   |-----------------------------------|-------|-------|--------|--------|----------|
#   | openai/gpt-oss-20b（前任）         | 9/9   | 9/9   | 8.8s   | 18.6s  | —        |
#   | minimaxai/minimax-m3              | 7/9   | 7/9   | 10.5s  | 16.2s  | **429×2**|
#   | **google/gemma-4-31b-it**         | 9/9   | 9/9   | **3.0s**| 24.0s | —        |
#   | nemotron-3-nano-omni-…-reasoning  | 8/9   | 8/9   | 9.0s   | 16.0s  | 503×1    |
#   | nemotron-3.5-lightning-30b-a3b    | 9/9   | 9/9   | 33.1s  | 38.3s  | —        |
# ⚠ **這場 bake-off 對「判定品質」零判別力**——五個全 9/9，那三個角色太容易。
#   它量到的**只有延遲與可用性**，不要拿它說 gemma 判得準；判別力要靠下面兩條重量。
# ⚠ **TTFT 榜與真實 judge 負載是反的**：`minimax-m3`（TTFT 131ms，榜首）第 8 次呼叫就 429，
#   而 RAGAS 一輪要打幾百次；`nemotron-3.5-lightning`（TTFT 2.1s）把思考過程寫進 content，
#   中位變 **33 秒**＝比前任慢 4 倍。**單 token 延遲量不到 judge 會失敗的方式。**
# **為什麼不是現在的 GEN_MODEL `nemotron-3-super`**：`eval_generation_llm_judge.py` 記著
#   `judge_regression.py` 11 案例實測 `gpt-oss-20b` 10/11 vs `gpt-oss-120b` 9/11，而後者被
#   判掉的理由之一就是**與 gen_model 同一支（自評偏誤）**——judge 與 generator 同源是本 repo
#   量過並否決的形狀，換成 nemotron 會原封不動地把它搬回來。gemma 與 nemotron 不同源。
# **為什麼刻意選非推理模型**：本 repo 栽過兩次「reasoning token 吃光正文額度 → **靜默吐空
#   字串**」（`unstructured_components.py` 的 `TABLE_SUMMARY_MAX_TOKENS`，gpt-oss-120b @200
#   是 10/10 全空）。20b 自己在使用者的 TTFT 榜上就回「無內容」。gemma-4-31b-it 是 instruct
#   模型，沒有這條路。
# ⚠ **換 judge ＝ 所有既有 RAGAS 聚合值失去可比性**。下面 `print_guardrails` 印的
#   gold 上限與噪音門檻是綁在**更早的 gpt-oss-120b** 上量的，**在新 judge 上重量之前不可引用**。
DEFAULT_RAGAS_MODEL = "google/gemma-4-31b-it"   # NVIDIA NIM，單一 key、無 Groq 免費層 TPD 硬牆
EVAL_SET_PATH = Path("eval/eval_set.json")

# RAGAS 六指標的欄位名（df 欄位 = metric.name）。context_relevance 的實際名稱依 ragas
# 版本而異，於 build_metrics() 執行期解析後補進來。
RAGAS_METRICS = [
    "context_recall",
    "context_precision",
    "context_relevance",   # 佔位，實際欄名執行期決定
    "faithfulness",
    "answer_relevancy",
    "answer_correctness",
]

# RAGAS ↔ 自製指標 的對照（用於並排表；None 表示無自製對應）。"rubric" 欄純為
# 相容舊結果檔（若某結果檔仍是 rubric-based 產生，correctness.score 會被讀出來對照
# 顯示），純新架構下所有題目都無 rubric，此欄一律 n/a。
RAGAS_TO_SELFMADE = {
    "faithfulness":       "selfmade_faithfulness",   # = 1 - hallucination_rate
    "answer_relevancy":   "selfmade_relevance",
    "context_recall":     "selfmade_ctx_recall",
    "answer_correctness": "rubric",
    "context_precision":  None,
    "context_relevance":  None,
}


# ── 讀 eval_set：id → {query, category, ground_truth}────────────────────────
# 純 RAGAS 架構：不再讀/篩 rubric。ground_truth 只能來自 gen_reference_answers.py 產生
# 的完整參考答案；沒有 reference 的題目直接跳過（六指標沒有「rubric checklist 拼接」
# 這種 fallback 可用——那是舊機制的產物,新架構下 reference 缺失就代表這題還沒能評分,
# 不是可以將就的狀態）。
def load_eval_set(references: dict | None = None) -> dict:
    references = references or {}
    data = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    out = {}
    skipped_no_ref = []
    for q in data["queries"]:
        if not q.get("answerable", True) or not q.get("relevant"):
            continue  # 純檢索/不可答題不做 correctness 對照
        ref = references.get(q["id"], {}).get("reference")
        if not ref:
            skipped_no_ref.append(q["id"])
            continue
        out[q["id"]] = {
            "query": q["query"],
            "category": q["category"],
            "ground_truth": ref,
        }
    if skipped_no_ref:
        print(f"  [WARN] {len(skipped_no_ref)} 題缺 reference（尚未跑 gen_reference_answers.py），"
              f"已跳過: {skipped_no_ref[:10]}{' ...' if len(skipped_no_ref) > 10 else ''}",
              file=sys.stderr)
    return out


def load_references(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f"  [WARN] reference file missing: {path} — 所有題目都會因缺 reference 被跳過，"
              f"請先跑 gen_reference_answers.py", file=sys.stderr)
        return {}
    return {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))}


# 生成器在答案尾端附的「📚 引用來源」footer 是 metadata（chunk 檔名 + rerank 分數），
# 不是答案內容，且其 rerank=X.XXX 之類字串在檢索 context 裡不存在 → faithfulness 判官會把
# 整段 footer 判為 unsupported，短答案被砸最重（footer 佔比高）。餵 RAGAS 前一律剝掉。
_CITATION_FOOTER_MARK = "\n\n---\n📚 引用來源"

# 2026-08-09：footer 只是引用 metadata 的一半。生成契約要求**每句話後面**都掛
# 【檔名, chunk #N】的 inline 標記，實測它佔答案本文的 **25.0% 字元**（中位 28.3%、
# 最大 51.1%、96/100 題有），而 reference_answers 一個都沒有。
#
# ⚠ **剝除的理由是「像比像」，不是「分數會變好」——實測分數反而變差。** 別把下面兩個
# 數字讀反了（我當天先假設「檔名雜訊稀釋 embedding」，兩個實驗都不支持）：
#   · 確定性量測（零 LLM，BGE-M3 直接算 cosine，n=100）：含標記 0.9105 → 剝掉 0.9148，
#     **差 +0.0043**，乘上 answer_correctness 的 0.25 相似度權重＝**+0.0011**。
#     四分之一的字元是檔名，對 BGE-M3 的句向量幾乎沒有影響 → **稀釋假說證偽**。
#   · judge 重評（28 題配對）：answer_correctness **-0.0408**。方向與假說相反，且該
#     樣本數的偵測門檻是 0.035（1.96*0.094/sqrt(28)），只勉強超過 → 不足以反推機制。
#     殘差只可能來自 0.75 權重的 statement F1 分解，尚未查清。
# 保留剝除是**方法論選擇**：引用標記是 metadata 不是答案內容，reference 一個都沒有，
# 剝掉才是 like-for-like。照「分數變低就退回」做,就是在 gaming 量尺。
# ⚠ 這個改動使**所有後續數字與 2026-08-09 之前的結果檔不可比**，比較前先確認同一側。
_INLINE_CITATION = re.compile(r"\s*【[^】]*?\.(?:html|txt)[^】]*?】")


# ── 判讀護欄（2026-08-09）──────────────────────────────────────────────────
# 兩組實測常數，用來擋掉「拿落在噪音裡的數字反推機制」這個已經犯過兩次的錯。
#
# NOISE：拿**同一份結果檔**評兩次的整體均值移動（檢索與生成完全固定，只有 judge 在變）。
#   量了兩次獨立樣本（2026-08-09）：context_precision 0.0459 / 0.0452 幾乎複現——那是穩定
#   的噪音底線，不是運氣。它不隨題數收斂，因為 precision 對每個 context 做二元判定再依排名
#   加權，**最高排名那個判定翻面整題就 1.0→0.0**（實測 mh-05、mi-05 在相同輸入下正是如此）。
#   context_recall 兩個樣本 0.0021 / 0.0128，取大的當門檻。
#
# GOLD_BASELINE：把 `reference_answers.json` 原文當成系統答案餵進來評分的結果（n=100）。
#   它回答「這個指標的分數上限在哪」以及「追它有沒有意義」：
#     · answer_correctness 0.989（85/100 拿滿分）→ 指標拿得到滿分,系統與 gold 的差距是真的。
#     · faithfulness 0.656 → **gold 自己比系統的 0.82 還低,61/100 題輸給系統**。原因是 gold
#       依 gold_files 生成、與 agentic 實際撈到的 contexts 不同源。繼續往上推等於要求系統
#       答得比標準答案還保守 → 這個指標**沒有可追空間**。
#     · answer_relevancy 0.842 vs 系統 0.823 → 幾乎沒有空間。
# 2026-08-20 在 **65 題**上重量一對（同一份結果檔 gj_mdna_65q_r1 餵給 judge 兩次，
# experiments/ragas_system_65q_j{1,2}.json）。實測 |j1-j2|：
#   recall .0170 / precision .0050 / nv .0040 / faith .0100 / relevancy .0040 / correctness .0070
#
# ⚠ **取新舊兩者的較大值當門檻，不是直接換成新值。** 兩個理由：
#   ① **一對樣本只是噪音的一次抽樣，不是噪音的上界**。舊值 context_precision 0.046 的成因寫在
#      上面那段註解裡——precision 對每個 context 做二元判定再依排名加權，**最高排名那個判定
#      翻面整題就 1.0→0.0**。這次那一對剛好沒翻（.005），**不代表它不會翻**。
#      把門檻從 .046 降到 .005 會讓一堆抽樣變異被誤讀成「顯著改善」。
#   ② 噪音門檻取大只會讓我**要求更多證據**，永遠不會讓我少要求——錯的方向是安全的那一邊。
#   → 想真的收斂 precision 的門檻，要多跑幾對，不是拿一對就改。
#
# 2026-08-21 **第二對**（同樣是 gj_mdna_65q_r1 餵給 judge 兩次，相隔一天；
# experiments/agentic/ragas_65q_r1.json vs 前一輪的聚合值）。實測 |passA-passB|：
#   recall .006 / precision .013 / nv .000 / faith .003 / relevancy .003 / **correctness .012**
# → `answer_correctness` 的 **.007 被這一對推翻，改成 .012**（取兩對的較大值）。
# ⚠ **這條常數差點害我誤判一次修法**：拿 after 臂去比**前一天那一輪**的 correctness 得到
#   −.009，看起來「略高於 .007 門檻的下降」→ 我已經開始找機制了。把基準換成同期重跑的
#   passB 之後，真實差值是 **+.003（噪音內）**。
#   兩個教訓：① **跨輪比較 RAGAS 分數等於把 judge 噪音算進系統差異**——A/B 兩臂要在
#   同一批判分裡比，或至少各自附一個同期基準；② 上面①「一對不是上界」那句話寫得很對，
#   但寫下它的同一支腳本仍然用一對定了 correctness 的門檻。**寫下警告不等於服從警告。**
# 2026-09-06 **換 judge（google/gemma-4-31b-it）之後重量一對**：同一份結果檔
# experiments/agentic/gj_multiyear_r2_unitstats.json 餵給同一個 judge 兩次
#（experiments/ragas_65q_gemma_sysA2.json vs ragas_65q_gemma_sysB.json，n=65）。實測 |A2−B|：
#   recall .006 / precision .006 / **nv .012** / **faith .021** / relevancy .002 / correctness .008
# → 照上面那條規則取新舊較大值，**兩個被推高**：nv .008 → .012、faithfulness .010 → .021。
#   faithfulness 那 .021 不是均值的抖動而已——逐題最大 |Δ| 是 **mix-15 的 1.000 → 0.500**（整題砍半），
#   mix-07 .867 → .500、lex-06 1.000 → .667 同一形狀 → **這個 judge 在 faithfulness 上比舊的抖**。
# ⚠ 新舊兩組不同源（舊：gpt-oss-120b／100 題；新：gemma／65 題），所以「取較大值」在這裡不是
#   「兩份證據取聯集」而是**保守**：門檻取大只會要求更多證據，錯的方向是安全的那一邊。
# ⚠ 一樣只有**一對**。上面 ① 那句「一對是抽樣不是上界」對這一對逐字成立，**不要拿它往下調門檻**。
# ⚠ A2 臂有 1 題（`mix-13`）的 answer_correctness 仍是 NaN（`--nvidia-passes 2 --timeout 900`
#   之後剩的唯一一筆；同一支腳本預設值那輪是 11 筆）→ correctness 的 |Δ| 算在 n=64 的交集上。
NOISE = {"context_recall": 0.017, "context_precision": 0.046, "nv_context_relevance": 0.012,
         "context_relevance": 0.012, "faithfulness": 0.021, "answer_relevancy": 0.004,
         "answer_correctness": 0.012}
# 2026-09-06 **換 judge 之後在 gemma 上重量**（experiments/ragas_65q_gemma_goldceiling.json，
# 輸入 experiments/ragas_gold_as_answer_65q_gemma_input.json）。作法與 2026-08-20 那次**逐字相同**：
# 把 reference_answers.json 原文當成系統答案、**contexts 沿用同一份 agentic 結果**
#（experiments/agentic/gj_multiyear_r2_unitstats.json）→ 只換 answer 這一個變因。六個指標 **0 筆 NaN**。
# ⚠ 這裡**不取新舊較大值**（那是噪音門檻的規則，理由不同）：gold 上限是拿來跟**同一輪**系統分數比的，
#   拿 gemma 判的系統分數去比 gpt-oss-120b 判的上限，正是本檔明令禁止的跨 judge 比較 → **整組換掉**。
#   舊值（gpt-oss-120b／65 題）留著供對照，**不要引用**：
#     correctness .972 / faith .659 / relevancy .871 / recall .788 / precision .843 / nv .969
#   更舊的 n=100：correctness .989 / faith .656 / relevancy .842 / recall .766 / precision .822 / nv .965
# 判讀（系統臂＝A2/B 兩輪均值）：
#   · answer_correctness .987 vs 系統 .654 → **唯一還有空間的指標，落差 .333**（舊 judge 上是 .33，
#     換 judge 沒有改變這個結論——那 0.33 對 retrieval 免疫這件事也跟著不變）
#   · faithfulness .726 vs 系統 .876 → **gold 自己比系統低 .149**，成因與 2026-08 同一個
#    （gold 依 gold_files 生成、與 agentic 實際撈到的 contexts 不同源）→ 沒有可追空間
#   · recall .826／precision .780／nv .923 → 系統與上限的差全部落在各自噪音門檻內 ＝ 已到頂
#   · answer_relevancy .888 vs 系統 .859 → +.029，高於 .004 門檻 ＝ **唯一的第二條細縫**（很窄）
GOLD_BASELINE = {"answer_correctness": 0.987, "faithfulness": 0.726, "answer_relevancy": 0.888,
                 "context_recall": 0.826, "context_precision": 0.780,
                 "nv_context_relevance": 0.923, "context_relevance": 0.923}


def _print_interpretation_guide(overall: dict, present: list[str]) -> None:
    """在 OVERALL 底下印出每個指標的噪音門檻與 gold 上限，讓分數無法被誤讀。"""
    print("\n" + "=" * 92)
    print("判讀護欄（實測常數，見本檔 NOISE / GOLD_BASELINE 註解）")
    print(f"  {'metric':<24}{'本次':>8}{'噪音門檻':>10}{'gold當答案':>11}   判讀")
    for m in present:
        v = overall.get(m)
        if not _is_num(v):
            continue
        noise = NOISE.get(m)
        gold = GOLD_BASELINE.get(m)
        notes = []
        if noise is not None:
            notes.append(f"跑分差異 <{noise:.3f} 不可解讀")
        if gold is not None:
            # 「差多少才算真的有空間」的容差＝該指標自己的噪音門檻（下限 .005）。
            # ⚠ 寫死 .005 會讓 context_precision（噪音 .046）印出「距上限 +0.008」,
            #   而那個 .008 依定義就讀不出來——護欄自己製造了它要擋的那種誤讀。
            tol = max(0.005, noise or 0.0)
            if v >= gold - tol:
                notes.append("已達/超過 gold 水準 → 無可追空間")
            else:
                notes.append(f"距 gold 上限 {gold - v:+.3f}")
        print(f"  {m:<24}{v:>8.3f}{(f'{noise:.3f}' if noise else 'n/a'):>10}"
              f"{(f'{gold:.3f}' if gold else 'n/a'):>11}   {'；'.join(notes)}")
    print("  ⚠ 這些常數綁定「judge 模型 ＋ 題庫 ＋ 當時的 reference」三者。換掉任何一個都必須重量,別沿用。")
    print(f"  ✔ **2026-09-06 已在 {DEFAULT_RAGAS_MODEL} ＋ 65 題上重量**（judge 於 2026-09-04 換掉,")
    print("     因為 gpt-oss-120b 被 NVIDIA 退役、410）：gold 上限**整組換新**,六個指標 0 筆 NaN；")
    print("     噪音門檻照既有規則取新舊較大值 → nv .008→.012、faithfulness .010→.021 被推高,其餘不動。")
    print("     ⚠ 只量了**一對**。「一對是抽樣不是噪音的上界」對這一對逐字成立,別拿它往下調門檻。")
    print("  ⛔ **不要拿本次分數去比別輪跑出來的聚合值**：實測同一份結果檔判兩次,")
    print("     faithfulness 就差 .021（mix-15 整題 1.000→0.500）。跨輪比＝把 judge 噪音算進系統差異")
    print("     （2026-08-21 誤判過一次）。A/B 兩臂要在同一批判分裡比,或各自附一個同期基準。")
    print("     尤其 context_precision：最高排名判定翻面整題就 1.0→0.0。")
    print("  ⛔ 跨 2026-08-19（題庫 100→65 題）與跨 2026-09-04（換 judge）的分數,一律不可直接比。")


def strip_citation_footer(text: str) -> str:
    """剝掉引用 metadata（尾端 footer ＋ 句末 inline 標記），只留答案本文。"""
    if not text:
        return text
    idx = text.find(_CITATION_FOOTER_MARK)
    body = text[:idx].rstrip() if idx != -1 else text
    return _INLINE_CITATION.sub("", body)


# ── 讀結果檔：id → list of {answer, contexts, rubric_score(optional), self-made 指標} ──
def load_answers(result_paths: list[str]) -> dict:
    by_id: dict[str, list[dict]] = defaultdict(list)
    for path in result_paths:
        p = Path(path)
        if not p.exists():
            print(f"  [WARN] result file missing: {path}", file=sys.stderr)
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for rec in data.get("records", []):
            ans = rec.get("answer")
            if not ans:
                continue  # 沒答案（生成失敗/跳過）才略過；rubric_score 不再是必要條件
            corr = rec.get("correctness")  # 純新架構下恆為 None（無 rubric 可算）；舊檔可能有值
            # RAGAS context 指標需要的檢索文字；舊結果檔可能沒有 → 空 list → context 指標 NaN
            contexts = rec.get("contexts") or []
            hall = rec.get("hallucination_rate")
            ctx_llm = rec.get("context_recall_llm") or {}
            by_id[rec["id"]].append({
                "answer": strip_citation_footer(ans),
                "contexts": [c for c in contexts if c and c.strip()],
                "rubric_score": corr["score"] if corr is not None else None,
                # 自製指標（供並排比對，選配）——缺值以 None 表示
                "selfmade_faithfulness": (1.0 - hall) if hall is not None else None,
                "selfmade_relevance": rec.get("relevance_score"),
                "selfmade_ctx_recall": ctx_llm.get("context_recall_score"),
                "source": p.name,
            })
    return by_id


# ── BGE-M3 embeddings（langchain Embeddings 介面，供 RAGAS 語意相似用）──────
def make_embeddings():
    from langchain_core.embeddings import Embeddings
    from sentence_transformers import SentenceTransformer

    class BGEM3Embeddings(Embeddings):
        """對齊生產 dense embedding（BAAI/bge-m3）。reuse HF 快取，不重新下載。"""

        def __init__(self):
            self.model = SentenceTransformer("BAAI/bge-m3")

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return self.model.encode(texts, normalize_embeddings=True).tolist()

        def embed_query(self, text: str) -> list[float]:
            return self.model.encode([text], normalize_embeddings=True)[0].tolist()

    return BGEM3Embeddings()


# ── 組出 RAGAS 六指標物件（context_relevance 名稱依版本容錯）─────────────────
def build_metrics():
    """回傳 (metrics, names)。names 與 df 欄位對齊，供聚合逐欄讀取。"""
    from ragas.metrics import (
        answer_correctness, answer_relevancy, context_precision,
        context_recall, faithfulness,
    )
    metrics = [context_recall, context_precision, faithfulness,
               answer_relevancy, answer_correctness]
    names = ["context_recall", "context_precision", "faithfulness",
             "answer_relevancy", "answer_correctness"]

    # Context Relevance：ragas 0.2.x 的類別名與匯出名跨版本不一，盡量掛上；掛不上就略過
    cr_obj = None
    for import_attempt in (
        lambda: __import__("ragas.metrics", fromlist=["ContextRelevance"]).ContextRelevance(),
        lambda: __import__("ragas.metrics._context_relevance", fromlist=["ContextRelevance"]).ContextRelevance(),
    ):
        try:
            cr_obj = import_attempt()
            break
        except Exception:
            continue
    if cr_obj is None:
        try:
            from ragas.metrics import context_relevancy as _legacy_cr
            cr_obj = _legacy_cr
        except Exception:
            cr_obj = None
    if cr_obj is not None:
        metrics.insert(2, cr_obj)
        names.insert(2, getattr(cr_obj, "name", "context_relevance"))
    else:
        print("  [WARN] Context Relevance 指標在此 ragas 版本無法載入，將略過該指標",
              file=sys.stderr)
    return metrics, names


# ── RAGAS 評分（單一 key 一次 pass，回傳 name → list[float]）─────────────────
def _ragas_one_pass(rows: list[dict], ragas_model: str, api_key: str, max_workers: int,
                    embeddings, metrics, names, timeout: int = 120, max_retries: int = 1
                    ) -> dict[str, list[float]]:
    from datasets import Dataset
    from langchain_openai import ChatOpenAI
    from ragas import evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig

    llm = LangchainLLMWrapper(ChatOpenAI(
        model=ragas_model, base_url=NVIDIA_BASE_URL, api_key=api_key, temperature=0.0,
    ))
    # contexts 為空的列，用單一空白佔位避免 schema 崩（該列的 context 指標自然為 NaN/低分）
    ds = Dataset.from_dict({
        "question": [r["query"] for r in rows],
        "answer": [r["answer"] for r in rows],
        "contexts": [r["contexts"] if r["contexts"] else [" "] for r in rows],
        "ground_truth": [r["ground_truth"] for r in rows],
    })
    result = evaluate(
        ds, metrics=metrics, llm=llm, embeddings=embeddings,
        run_config=RunConfig(max_workers=max_workers, timeout=timeout, max_retries=max_retries),
        show_progress=True,
    )
    df = result.to_pandas()
    out: dict[str, list[float]] = {}
    for name in names:
        if name in df.columns:
            out[name] = [float(x) for x in df[name].tolist()]
        else:
            out[name] = [float("nan")] * len(rows)
    return out


def run_ragas(rows: list[dict], ragas_model: str, max_workers: int,
              timeout: int = 120, max_retries: int = 1, nvidia_passes: int = 1
              ) -> dict[str, list[float]]:
    """對 rows 跑六指標。NVIDIA 只有單一 key，沒有「換 key」這件事可做，改用 nvidia_passes
    把同一把 key 重複排進 pass 列表——每輪只把「任一指標仍 NaN」的列送進下一輪重跑，讓逾時的
    列有機會重新嘗試（2026-07-21 首次 NVIDIA 跑遇到不少 TimeoutError，原本單 pass 沒有重試機會）。"""
    from ragas.embeddings import LangchainEmbeddingsWrapper
    embeddings = LangchainEmbeddingsWrapper(make_embeddings())
    metrics, names = build_metrics()

    nv_key = os.getenv("NVIDIA_API_KEY")
    if not nv_key:
        sys.exit("找不到 NVIDIA_API_KEY（.env）")
    keys = [nv_key] * max(1, nvidia_passes)

    scores: dict[str, list[float]] = {name: [float("nan")] * len(rows) for name in names}
    pending = list(range(len(rows)))
    for ki, key in enumerate(keys, 1):
        if not pending:
            break
        key_label = f"NVIDIA_API_KEY (pass {ki})"
        print(f"\n[RAGAS pass {ki}/{len(keys)}] scoring {len(pending)} rows with {key_label} ...")
        sub = [rows[i] for i in pending]
        sub_scores = _ragas_one_pass(sub, ragas_model, key, max_workers, embeddings, metrics, names,
                                     timeout=timeout, max_retries=max_retries)
        still = []
        for pos, idx in enumerate(pending):
            any_nan = False
            for name in names:
                v = sub_scores[name][pos]
                if not math.isnan(v):
                    scores[name][idx] = v
                else:
                    any_nan = True
            if any_nan:
                still.append(idx)
        got = len(pending) - len(still)
        print(f"[RAGAS pass {ki}] {got} rows fully scored, {len(still)} still have NaN (→ next key)")
        pending = still
    return scores


# ── 聚合與輸出 ──────────────────────────────────────────────────────────────
def pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2:
        return None
    va, vb = np.array(a), np.array(b)
    if va.std() == 0 or vb.std() == 0:
        return None
    return float(np.corrcoef(va, vb)[0, 1])


def _nanmean(xs: list[float]) -> float:
    vals = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return round(statistics.mean(vals), 3) if vals else float("nan")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="RAGAS 6-metric vs self-made 5-metric 對照")
    ap.add_argument("--from-results", nargs="+", required=True,
                    help="一或多個結果檔（含 records[].answer/contexts/correctness）。"
                         "⚠ 多個檔是**拼接同一臂**（同 id 的多筆會被平均，用於局部重跑後併回、"
                         "或同一臂重跑多次消變異）——**不是 A/B 兩臂對照**。"
                         "要比兩臂請各跑一次、各給一個 --output；"
                         "把兩臂丟進同一次會得到兩臂的平均值，哪一邊都回答不了"
                         "（2026-08-14 這樣燒掉 2h49m）。")
    ap.add_argument("--ragas-model", default=DEFAULT_RAGAS_MODEL)
    ap.add_argument("--categories", nargs="*", default=None,
                    help="只跑指定類別（semantic/lexical/mixed/colloquial/news/multi_intent）")
    ap.add_argument("--limit", type=int, default=None, help="每類最多幾個 id（smoke test 用）")
    ap.add_argument("--ids", nargs="*", default=None,
                    help="只評指定 query id。RAGAS 每題獨立評分，改了少數題的 reference 時"
                         "可只重跑那幾題再拼接回原結果，避免整份重跑多吃 judge 噪音。")
    ap.add_argument("--max-workers", type=int, default=2, help="RAGAS 併發（NVIDIA 限速，別開太高）")
    # ⚠ **預設 2026-09-04 從 120 拉到 420，跟著換 judge 一起做的**（量在 `gpt-oss-20b` 上，
    #   同日 judge 再換成 gemma 之後**沒有重量**——420 只是上界，留著不會有害，
    #   要往下調得先量）：`gpt-oss-20b` 上
    #   `answer_correctness`（六個指標裡最貴——要把答案拆成 claim 逐條比對）在 120 秒
    #   **會逾時**，實測 `Exception raised in Job[3]: TimeoutError()` → 該題該指標變 NaN。
    #   **危險的是它靜默**：NaN 在輸出裡印成 `n/a`，外觀與「這個指標算不出來」相同，
    #   而 `answer_correctness` 正是 CLAUDE.md 說唯一還有空間的那一個 → 一整輪跑分會
    #   在最重要的那一格上無聲歸零。420 秒下同樣兩題 0.615／0.513，全部算得出來。
    ap.add_argument("--timeout", type=int, default=420, help="RAGAS 每次 judge 呼叫的逾時秒數")
    ap.add_argument("--max-retries", type=int, default=1, help="RAGAS 單次呼叫內部重試次數")
    ap.add_argument("--nvidia-passes", type=int, default=1,
                    help="把逾時/失敗的列重跑幾輪（同一把 NVIDIA key，沒有多 key 輪換可用）")
    ap.add_argument("--reference-file", default="eval/reference_answers.json",
                    help="完整黃金參考答案（gen_reference_answers.py 產）；缺該題 reference 時該題會被跳過")
    ap.add_argument("--output", default="eval/ragas_vs_rubric.json")
    args = ap.parse_args()

    load_dotenv()
    references = load_references(args.reference_file)
    eval_set = load_eval_set(references)
    answers = load_answers(args.from_results)

    # 可續跑：load 既有輸出，跳過已完整評分的 id（answer_correctness 非 NaN 視為該 id 已跑）
    prior_per_id: dict[str, dict] = {}
    out_path = Path(args.output)
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            for qid, d in prev.get("per_id", {}).items():
                ac = d.get("answer_correctness")
                if ac is not None and not (isinstance(ac, float) and math.isnan(ac)):
                    prior_per_id[qid] = d
            if prior_per_id:
                print(f"resume: {len(prior_per_id)} ids already scored in {args.output}, skipping them")
        except Exception:
            pass

    print(f"ground_truth: {len(eval_set)} 題有 reference（answer_correctness 等指標可算的題數）")

    # 組出要評的 rows（每列一個答案實例；k-run 檔會有多列同 id）
    rows = []
    seen_cats = defaultdict(int)
    for qid, meta in eval_set.items():
        if args.ids and qid not in args.ids:
            continue
        if args.categories and meta["category"] not in args.categories:
            continue
        if qid not in answers or qid in prior_per_id:
            continue
        if args.limit and seen_cats[meta["category"]] >= args.limit:
            continue
        seen_cats[meta["category"]] += 1
        for inst in answers[qid]:
            rows.append({
                "id": qid,
                "category": meta["category"],
                "query": meta["query"],
                "ground_truth": meta["ground_truth"],
                **inst,
            })

    if not rows and not prior_per_id:
        sys.exit("no rows to evaluate (check --from-results / --categories)")

    per_id: dict[str, dict] = {qid: dict(d) for qid, d in prior_per_id.items()}

    if rows:
        n_with_ctx = sum(1 for r in rows if r["contexts"])
        print(f"Evaluating {len(rows)} answer instances across "
              f"{len({r['id'] for r in rows})} queries with RAGAS 6 metrics "
              f"(model={args.ragas_model}); {n_with_ctx}/{len(rows)} rows have contexts.")
        if n_with_ctx == 0:
            print("  [WARN] 沒有任何 row 帶 contexts → 4 個 context 指標將全 NaN。"
                  "請確認結果檔是 2026-07-21 後（含 contexts）產生的。", file=sys.stderr)

        ragas_scores = run_ragas(rows, args.ragas_model, args.max_workers,
                                 timeout=args.timeout, max_retries=args.max_retries,
                                 nvidia_passes=args.nvidia_passes)
        actual_names = list(ragas_scores.keys())

        # 逐 id 聚合（k-run 取平均）：RAGAS 六指標 + rubric + 自製對應
        new_per_id: dict[str, dict] = {}
        for i, r in enumerate(rows):
            d = new_per_id.setdefault(r["id"], {"category": r["category"],
                                                "_ragas": defaultdict(list),
                                                "_rubric": [], "_self": defaultdict(list)})
            for name in actual_names:
                d["_ragas"][name].append(ragas_scores[name][i])
            d["_rubric"].append(r["rubric_score"])
            for sm in ("selfmade_faithfulness", "selfmade_relevance", "selfmade_ctx_recall"):
                d["_self"][sm].append(r.get(sm))
        for qid, d in new_per_id.items():
            entry = {"category": d["category"], "rubric": _nanmean(d["_rubric"])}
            for name in actual_names:
                entry[name] = _nanmean(d["_ragas"][name])
            for sm in ("selfmade_faithfulness", "selfmade_relevance", "selfmade_ctx_recall"):
                entry[sm] = _nanmean(d["_self"][sm])
            per_id[qid] = entry
    else:
        print("all queries already scored (resume); re-aggregating from prior output")

    # ── 逐題表（六指標）──────────────────────────────────────────────────
    # 從 per_id 實際存在的欄位收集 RAGAS 指標名（排除 meta / 自製欄）；Context Relevance 在
    # ragas 0.2.15 的實名是 nv_context_relevance，故不能寫死 RAGAS_TO_SELFMADE 的 key。
    _NON_METRIC = {"category", "rubric", "n", "_corr_vs_selfmade",
                   "selfmade_faithfulness", "selfmade_relevance", "selfmade_ctx_recall"}
    _PREF = ["context_recall", "context_precision", "nv_context_relevance", "context_relevance",
             "faithfulness", "answer_relevancy", "answer_correctness"]
    metric_keys: list[str] = []
    for q in per_id.values():
        for k in q:
            if k not in _NON_METRIC and k not in metric_keys:
                metric_keys.append(k)
    present = sorted(metric_keys, key=lambda m: _PREF.index(m) if m in _PREF else 99)
    print("\n" + "=" * 100)
    hdr = f"{'id':<9}{'cat':<12}{'rubric':>8}" + "".join(f"{m[:11]:>13}" for m in present)
    print(hdr)
    print("-" * 100)
    for qid in sorted(per_id, key=lambda k: (per_id[k]["category"], k)):
        d = per_id[qid]
        line = f"{qid:<9}{d['category']:<12}{d.get('rubric', float('nan')):>8.3f}"
        for m in present:
            v = d.get(m, float("nan"))
            line += f"{v:>13.3f}" if isinstance(v, (int, float)) and not math.isnan(v) else f"{'n/a':>13}"
        print(line)

    # ── 類別 + 整體聚合 ──────────────────────────────────────────────────
    def agg(ids: list[str]) -> dict:
        a = {"n": len(ids), "rubric": _nanmean([per_id[i].get("rubric") for i in ids])}
        for m in present:
            a[m] = _nanmean([per_id[i].get(m) for i in ids])
        # RAGAS ↔ 自製 對照的相關性（有自製對應的指標才算）
        a["_corr_vs_selfmade"] = {}
        for m in present:
            sm = RAGAS_TO_SELFMADE.get(m)
            if not sm:
                continue
            sm_key = "rubric" if sm == "rubric" else sm
            paired = [(per_id[i].get(m), per_id[i].get(sm_key)) for i in ids
                      if _is_num(per_id[i].get(m)) and _is_num(per_id[i].get(sm_key))]
            if len(paired) >= 2:
                a["_corr_vs_selfmade"][m] = round(
                    pearson([x for x, _ in paired], [y for _, y in paired]) or float("nan"), 3)
        return a

    by_cat = defaultdict(list)
    for qid, d in per_id.items():
        by_cat[d["category"]].append(qid)

    print("\n" + "=" * 100)
    print(f"{'category':<13}{'n':>4}{'rubric':>8}" + "".join(f"{m[:11]:>13}" for m in present))
    print("-" * 100)
    summary = {}
    for cat in sorted(by_cat):
        a = agg(by_cat[cat])
        summary[cat] = a
        line = f"{cat:<13}{a['n']:>4}{_fmt(a['rubric']):>8}"
        for m in present:
            line += f"{_fmt(a[m]):>13}"
        print(line)
    overall = agg(list(per_id))
    summary["_overall"] = overall
    line = f"{'OVERALL':<13}{overall['n']:>4}{_fmt(overall['rubric']):>8}"
    for m in present:
        line += f"{_fmt(overall[m]):>13}"
    print("-" * 100)
    print(line)
    _print_interpretation_guide(overall, present)

    # ── RAGAS ↔ 自製 並排（整體）──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("RAGAS ↔ 自製 5 維 並排（OVERALL，Pearson r 越高代表兩把尺越一致）")
    print(f"  {'RAGAS metric':<20}{'RAGAS':>8}{'self-made':>11}{'pearson_r':>11}")
    pairs = [("faithfulness", "selfmade_faithfulness", "1-hallu"),
             ("answer_relevancy", "selfmade_relevance", "relevance"),
             ("context_recall", "selfmade_ctx_recall", "ctx_recall_llm"),
             ("answer_correctness", "rubric", "rubric_corr")]
    for rag_m, sm_key, label in pairs:
        if rag_m not in present:
            continue
        rv = overall.get(rag_m, float("nan"))
        sv = overall.get(sm_key if sm_key != "rubric" else "rubric", float("nan"))
        r = overall["_corr_vs_selfmade"].get(rag_m)
        print(f"  {rag_m:<20}{_fmt(rv):>8}{_fmt(sv):>11}{(_fmt(r) if r is not None else 'n/a'):>11}"
              f"   ({label})")

    out = {
        "meta": {
            "ragas_model": args.ragas_model,
            "ragas_metrics": present,
            "ground_truth": "reference_answers.json（缺則 rubric.must_include 合成）",
            "from_results": args.from_results,
        },
        "summary": summary,
        "per_id": per_id,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_json_default),
                                 encoding="utf-8")
    print(f"\nSaved → {args.output}")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.3f}"


def _json_default(o):
    if isinstance(o, float) and math.isnan(o):
        return None
    raise TypeError


if __name__ == "__main__":
    main()
