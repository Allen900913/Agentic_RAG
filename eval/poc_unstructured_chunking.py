"""
poc_unstructured_chunking.py — PoC: 用 unstructured 重新解析 + 切割 AAPL 10-K

對照組：data/processed/AAPL_10K_2026.txt（BeautifulSoup get_text + clean_text + SemanticChunker）
實驗組：unstructured.partition_html + chunk_by_title

只做觀察用，不寫入 Qdrant。
"""

from collections import Counter
from pathlib import Path

from unstructured.partition.html import partition_html
from unstructured.chunking.title import chunk_by_title
from unstructured.cleaners.core import clean_extra_whitespace

RAW_HTML = Path("data/raw/AAPL_10K_2026.html")
OLD_PROCESSED = Path("data/processed/AAPL_10K_2026.txt")


def section(title: str) -> None:
    print(f"\n{'═'*70}\n {title}\n{'═'*70}")


# ── 1. partition_html：解析成帶語意角色的 elements ──────────────────────────
section("1. unstructured partition_html → elements（前 40 筆，看角色分類）")

elements = partition_html(filename=str(RAW_HTML))
print(f"總 elements 數: {len(elements)}")

type_counts = Counter(type(el).__name__ for el in elements)
print(f"Element 類型分布: {dict(type_counts)}")

for i, el in enumerate(elements[:40]):
    text = clean_extra_whitespace(el.text)[:90]
    print(f"  [{i:>3}] {type(el).__name__:<15} | {text}")

# ── 2. 與舊版 processed txt 開頭比較 ──────────────────────────────────────────
section("2. 與舊版 processed/AAPL_10K_2026.txt 開頭比較")

old_text = OLD_PROCESSED.read_text(encoding="utf-8")
print("── 舊版（BeautifulSoup get_text，純文字流，無角色資訊）──")
print(old_text[:600])

print("\n── 新版（unstructured，每個 element 帶 Title/NarrativeText/Table/ListItem 角色）──")
for el in elements[:15]:
    print(f"  <{type(el).__name__}> {clean_extra_whitespace(el.text)[:80]}")

# ── 3. 找出被辨識成 Title 的元素：對照舊版「攤平」後完全看不出章節 ─────────────
section("3. unstructured 辨識出的章節 Title（舊版攤平文字中找不到這層資訊）")

titles = [el for el in elements if type(el).__name__ == "Title"]
print(f"共偵測到 {len(titles)} 個 Title 元素，前 25 個：")
for el in titles[:25]:
    print(f"  • {clean_extra_whitespace(el.text)[:100]}")

# ── 4. Table 元素：舊版被攤平成散裂數字行，新版獨立保留 ──────────────────────
section("4. Table 元素範例（舊版會被打散成一行行數字，新版保留表格邊界）")

tables = [el for el in elements if type(el).__name__ == "Table"]
print(f"共偵測到 {len(tables)} 個 Table 元素")
if tables:
    t = tables[0]
    print("第一個 Table 的純文字內容（前 400 字）：")
    print(clean_extra_whitespace(t.text)[:400])
    if hasattr(t.metadata, "text_as_html") and t.metadata.text_as_html:
        print("\n同一個 Table 的 text_as_html（保留列/欄結構，可給 LLM 更好理解）：")
        print(t.metadata.text_as_html[:500])

# ── 5. chunk_by_title：用偵測到的 Title 當邊界切 chunk ───────────────────────
section("5. chunk_by_title 切出的 chunks（前 8 個，含長度與內容預覽）")

chunks = chunk_by_title(
    elements,
    combine_text_under_n_chars=200,   # 太短的 element（如孤立小標題）併入下一塊
    max_characters=1500,              # chunk 上限字數（對齊一般 embedding 視窗）
    new_after_n_chars=1200,           # 超過此長度即使同一節也會斷開
    overlap=100,                      # chunk 間重疊字數，避免邊界資訊遺失
)

print(f"共切出 {len(chunks)} 個 chunk（相較之下舊版 SemanticChunker 切法是逐句語意比較）\n")

for i, ch in enumerate(chunks[:8]):
    text = ch.text
    head = clean_extra_whitespace(text)[:120]
    tail = clean_extra_whitespace(text)[-120:]
    print(f"  ── chunk #{i} | {len(text)} chars ──")
    print(f"    開頭: {head}")
    print(f"    結尾: {tail}")
    # chunk_by_title 的 metadata.orig_elements 可回看這個 chunk 是由哪些原始
    # element（含其角色：Title / NarrativeText / Table...）組合而成
    orig = getattr(ch.metadata, "orig_elements", None) or []
    roles = [type(o).__name__ for o in orig]
    print(f"    組成元素角色: {roles}")
    print()

# ── 6. 驗證「上下文是否保留」：抽一個 chunk，看它是否帶有所屬章節標題 ─────────
section("6. 驗證上下文：chunk 是否記得自己屬於哪個章節")

for i, ch in enumerate(chunks):
    orig = getattr(ch.metadata, "orig_elements", None) or []
    if orig and type(orig[0]).__name__ == "Title" and i > 3:
        print(f"chunk #{i} 的第一個原始元素是 Title:")
        print(f"  章節標題 → {clean_extra_whitespace(orig[0].text)[:100]}")
        print(f"  本 chunk 完整內容（前 300 字）→")
        print(f"  {clean_extra_whitespace(ch.text)[:300]}")
        break

print(f"\n[總結] 舊版 processed txt 行數: {len(old_text.splitlines())}，"
      f"純文字、無結構標記")
print(f"[總結] 新版 elements 數: {len(elements)}（含角色分類），"
      f"chunk_by_title 切出 {len(chunks)} 個 chunk（含章節歸屬 metadata）")
