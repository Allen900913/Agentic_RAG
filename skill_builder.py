"""
skill_builder.py — US Stock RAG Skill Document Generator

向 RAG 系統提出 4 個全域問題，整合回答，自動生成 skill.md。

Usage:
  python skill_builder.py                         # 輸出 skill.md
  python skill_builder.py --output my_skill.md    # 自訂輸出路徑
  python skill_builder.py --model gpt-oss:20b     # 使用其他模型
  python skill_builder.py --help
"""

import argparse
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
QDRANT_PATH        = os.getenv("QDRANT_PATH", "./qdrant_db")
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
COLLECTION_NAME    = "us_stock_rag_unstructured"
DENSE_VECTOR_NAME  = "dense"
SPARSE_VECTOR_NAME = "sparse"
DEFAULT_MODEL      = "gpt-oss:20b"
DEFAULT_OUTPUT     = "skill.md"
TOP_K              = 3      # 最終餵給 LLM 的 chunk 數
FETCH_N            = 20     # 每路 prefetch 的候選數
RRF_TOP_N          = 6      # RRF 融合後保留多少筆給 LLM（取 TOP_K 用）

SYSTEM_PROMPT_ANALYST = """\
You are a senior equity research analyst at a top-tier investment bank, \
specializing in publicly listed companies and corporate intelligence.
Answer ONLY based on the provided reference materials.
Be specific, cite key data points, and maintain analyst-grade precision.
Cite sources using [filename] notation.
"""

SYSTEM_PROMPT_SYNTHESIZER = """\
You are a senior financial analyst tasked with creating a structured knowledge \
document from research findings.
Synthesize information accurately, prioritize quantitative data, \
and structure output for use by AI agents.
"""


# ══════════════════════════════════════════════════════════════════════════════
# RAG query helper
# ══════════════════════════════════════════════════════════════════════════════

def rag_query(
    question: str,
    model_name: str,
    bge_m3,
    client,
) -> tuple[str, list[str]]:
    """Run one hybrid RAG query (Qdrant dense+sparse + RRF); return (answer, [sources])."""
    import openai
    from qdrant_client import models

    try:
        total = client.count(collection_name=COLLECTION_NAME, exact=True).count
    except Exception:
        return "No data found. Please run data_update.py first.", []
    if total == 0:
        return "No data found. Please run data_update.py first.", []

    # Encode query → dense + sparse in one shot
    encoded = bge_m3.encode(
        [question], return_dense=True, return_sparse=True, return_colbert_vecs=False,
    )
    q_dense  = encoded["dense_vecs"][0].tolist()
    q_sparse = encoded["lexical_weights"][0]
    s_indices = [int(tok) for tok in q_sparse.keys()]
    s_values  = [float(w)  for w   in q_sparse.values()]

    # Hybrid retrieval via Qdrant server-side RRF
    fused = client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            models.Prefetch(query=q_dense, using=DENSE_VECTOR_NAME, limit=FETCH_N),
            models.Prefetch(
                query=models.SparseVector(indices=s_indices, values=s_values),
                using=SPARSE_VECTOR_NAME, limit=FETCH_N,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=max(TOP_K, RRF_TOP_N),
        with_payload=True,
    ).points

    context = ""
    sources: list[str] = []
    for p in fused[:TOP_K]:
        payload = p.payload or {}
        src = payload.get("source", "unknown")
        idx = payload.get("chunk_index", 0)
        doc = payload.get("document", "")
        context += f"\n[{src}, chunk #{idx}]\n{doc.strip()}\n"
        if src not in sources:
            sources.append(src)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_ANALYST},
        {
            "role": "user",
            "content": (
                f"=== Reference Materials ===\n{context}\n\n"
                f"=== Question ===\n{question}\n\n"
                f"Answer based solely on the reference materials. "
                f"Keep your answers concise and precise to avoid timeout. "
                f"Cite sources using [filename] notation."
            ),
        },
    ]

    llm_client = openai.OpenAI(
        api_key=os.getenv("LITELLM_API_KEY"),
        base_url=os.getenv("LITELLM_BASE_URL", "https://litellm.netdb.csie.ncku.edu.tw"),
    )
    response = llm_client.chat.completions.create(
        model=model_name,
        messages=messages,
    )
    return response.choices[0].message.content, sources


# ══════════════════════════════════════════════════════════════════════════════
# Skill.md builder
# ══════════════════════════════════════════════════════════════════════════════

def synthesize_skill_md(
    qa_results: list[tuple[str, str, list[str]]],   # (key, answer, sources)
    model_name: str,
    all_source_files: list[str],
) -> str:
    """Call LLM to synthesize a complete skill.md from 4 Q&A answers."""
    import openai

    answers_text = "\n\n".join(
        f"[Topic: {key.upper()}]\n{answer}"
        for key, answer, _ in qa_results
    )

    synthesis_prompt = f"""\
Based on the following research findings about the covered companies, generate a structured skill document.

RESEARCH FINDINGS:
{answers_text}

Generate content for EACH of these sections. Be specific and data-rich:
1. CORE CONCEPTS: 8-12 key concepts/terms, each with 1-2 sentence explanations.
2. KEY TRENDS: 5-8 current market/technology trends (bullet points).
3. KEY ENTITIES: Categorised list of companies, products, people, tools, frameworks.
4. METHODOLOGY: How to analyse and compare these stocks (valuation, growth, moats).
5. KNOWLEDGE GAPS: Honest limitations (data cutoff, missing segments, etc.).
6. EXAMPLE QA: 5 representative Q&A pairs demonstrating this skill's capabilities.

Format each section clearly with its section name as a header (e.g., ## Core Concepts). Do not include an Overview section, as that is handled by the template.
"""

    llm_client = openai.OpenAI(
        api_key=os.getenv("LITELLM_API_KEY"),
        base_url=os.getenv("LITELLM_BASE_URL", "https://litellm.netdb.csie.ncku.edu.tw"),
    )
    response = llm_client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_SYNTHESIZER},
            {"role": "user",   "content": synthesis_prompt},
        ],
    )
    return response.choices[0].message.content


def parse_and_format_skill_md(
    synthesis: str,
    all_source_files: list[str],
    covered_companies: set[str],
    today: str,
) -> str:
    """Wrap LLM synthesis into the required skill.md Markdown structure."""

    # Build source reference table
    source_rows = ""
    doc_types = set()
    for src in sorted(all_source_files):
        name = src
        src_lower = src.lower()
        if "news" in src_lower:
            ftype, desc = "Finance News",   "Financial news article"
        elif "10k" in src_lower or "10-k" in src_lower:
            ftype, desc = "SEC 10-K",       "Annual report"
        elif "10q" in src_lower or "10-q" in src_lower:
            ftype, desc = "SEC 10-Q",       "Quarterly report"
        elif "fundamental" in src_lower:
            ftype, desc = "Fundamentals",   "Key financial ratios & metrics"
        elif "incomestatement" in src_lower:
            ftype, desc = "Income Stmt",    "Annual income statement"
        else:
            ftype, desc = "Text document",  "General reference"
        source_rows += f"| {name} | {ftype} | {desc} |\n"
        doc_types.add(ftype)

    companies_str = ", ".join(sorted(covered_companies)) if covered_companies else "Unnamed entities"
    doc_types_str = ", ".join(sorted(doc_types)) if doc_types else "Unknown"

    skill_md = f"""\
---
name: Public Company Intelligence Analyst
description: >
  A universally applicable RAG-powered skill for in-depth analysis of publicly listed companies. 
  Capabilities include extracting insights from public filings, fundamentals, financial 
  performance, competitive landscape, and market trends based on the mapped knowledge base.
---

# Skill: Public Company Intelligence Analyst

## Metadata
- **知識領域**：公開發行公司商業智能與市場分析
- **資料來源數量**：{len(all_source_files)} 份文件
- **最後更新時間**：{today}
- **適用 Agent 類型**：股票研究助手 / 財務分析顧問 / 投資情報機器人

## Overview
本 Skill 提供深度的企業商業智能與財務情報分析能力，能夠在限定的知識庫範圍內，
精準萃取公司的技術優勢、財務表現趨勢、市場競爭風險及未來戰略。
本技能的範圍與知識邊界將隨著底層資料集的更新而動態調整，不預先綁定特定公司，
能夠通用於各類股票或產業板塊分析。

## Coverage
- **Covered companies (自動偵測)**：{companies_str}
- **Covered document types**：{doc_types_str}
- **Not covered**：任何未見於知識庫內的公司、內部機密文件或付費牆外分析
- **Data cutoff**：依知識庫最新更新日為準

{synthesis}

## Source References

| 來源文件 | 類型 | 說明 |
|---|---|---|
{source_rows}
"""
    return skill_md


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="US Stock RAG — Skill Document Builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python skill_builder.py                     # 生成預設 skill.md
  python skill_builder.py --output my.md      # 自訂輸出路徑
  python skill_builder.py --model gpt-oss:20b # 使用其他模型
        """,
    )
    parser.add_argument(
        "--output", "-o", type=str, default=DEFAULT_OUTPUT,
        help=f"Output file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--model", "-m", type=str, default=DEFAULT_MODEL,
        help=f"LLM model name (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    # ── Load deps ───────────────────────────────────────────────────────────────
    from qdrant_client import QdrantClient
    from FlagEmbedding import BGEM3FlagModel

    print(f"[INFO] Loading BGE-M3 (dense+sparse): {EMBEDDING_MODEL}")
    bge_m3 = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
    print(f"[INFO] Opening Qdrant: {QDRANT_PATH}")
    client = QdrantClient(path=QDRANT_PATH)

    print(f"[INFO] LLM model               : {args.model}")
    print(f"[INFO] Output file             : {args.output}")

    # ── Discover Covered Companies ──────────────────────────────────────────────
    all_sources: set[str] = set()
    try:
        # Scroll through every point's payload to harvest source filenames
        next_offset = None
        while True:
            points, next_offset = client.scroll(
                collection_name=COLLECTION_NAME,
                limit=256,
                offset=next_offset,
                with_payload=["source"],
                with_vectors=False,
            )
            for p in points:
                src = (p.payload or {}).get("source", "")
                if src:
                    all_sources.add(src)
            if next_offset is None:
                break
    except Exception:
        all_sources = set()

    covered_companies = set()
    for src in all_sources:
        parts = src.split('_')
        if len(parts) > 1 and parts[0].isalpha() and parts[0].isupper():
            covered_companies.add(parts[0])
            
    companies_str = ", ".join(sorted(covered_companies)) if covered_companies else "the covered companies"
    print(f"[INFO] Detected companies      : {companies_str}")

    global_questions = [
        (
            "core_tech",
            f"What are the core technologies, main products, and competitive "
            f"moats of {companies_str} based on the knowledge base?",
        ),
        (
            "financials",
            f"What are the key revenue figures, profit margins, growth rates, "
            f"and balance sheet highlights of {companies_str} from the latest reports?",
        ),
        (
            "risks",
            f"What are the major market risks, competitive threats, regulatory "
            f"challenges, and macroeconomic headwinds facing {companies_str}?",
        ),
        (
            "strategy",
            f"What are the strategic initiatives, R&D priorities, investments, "
            f"and future growth opportunities for {companies_str}?",
        ),
    ]

    print(f"[INFO] Running {len(global_questions)} global RAG queries...\n")

    # ── Run global questions ────────────────────────────────────────────────────
    qa_results: list[tuple[str, str, list[str]]] = []

    for i, (key, question) in enumerate(global_questions, 1):
        print(f"[{i}/{len(global_questions)}] {question[:80]}...")
        answer, sources = rag_query(question, args.model, bge_m3, client)
        qa_results.append((key, answer, sources))
        all_sources.update(sources)
        print(f"  ✓ sources: {', '.join(sources[:4])}")

    # ── Synthesise ──────────────────────────────────────────────────────────────
    print(f"\n[INFO] Synthesising final skill document...")
    synthesis = synthesize_skill_md(qa_results, args.model, sorted(all_sources))

    today      = date.today().strftime("%Y-%m-%d")
    skill_md   = parse_and_format_skill_md(synthesis, sorted(all_sources), covered_companies, today)

    # ── Write output ────────────────────────────────────────────────────────────
    output_path = Path(args.output)
    output_path.write_text(skill_md, encoding="utf-8")

    print(f"\n✅  skill.md saved → {output_path}  ({len(skill_md):,} chars)")
    print(f"    Sections: Overview / Core Concepts / Key Trends / "
          f"Key Entities / Methodology / Knowledge Gaps / Example Q&A / Source References")


if __name__ == "__main__":
    main()
