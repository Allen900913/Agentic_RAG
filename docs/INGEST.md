# 語料處理（ingest）知識庫

> **這裡放什麼**：語料怎麼進到 collection、切塊每一層在做什麼、重建要注意什麼，以及每層當初的診斷結論。
>
> **這裡不放什麼**：日期式變更記錄（→ [`../CHANGELOG.md`](../CHANGELOG.md)）、還沒做的事（→ [`../BACKLOG.md`](../BACKLOG.md)）、量測方法（→ [`EVAL.md`](EVAL.md)）。
>
> ⚠ **這條路已經到頂，不要再改**：六個 RAGAS 指標五個已達或超過 gold 上限，檢索全修到完美只值 +0.024（低於噪音底線）。詳見 [`EVAL.md`](EVAL.md)〈量尺飽和〉。本檔的用途是**重建時不要弄壞既有的東西**，不是找下一個改進點。

---

## 目錄

- [語料目錄與取件](#語料目錄與取件)
- [重建 collection](#重建-collection)
- [切塊六層](#切塊六層)
  - [① Item 邊界](#-item-邊界)
  - [② 期間章節邊界](#-期間章節邊界)
  - [③ 通用小標邊界](#-通用小標邊界)
  - [④ 幅度接地](#-幅度接地)
  - [⑤⑥⑦ SemanticChunker / RCTS 上界 / min-size 下界](#-semanticchunker--rcts-上界--min-size-下界)
- [三個貫穿全篇的判準](#三個貫穿全篇的判準)

---

## 語料目錄與取件

`data/raw/` 分成四類子目錄，**根目錄不再有任何檔案**：

| 目錄 | 內容 | ingest 行為 |
|---|---|---|
| `Filings/` | 10-K/10-Q `.html`（primary document） | 供人眼查閱；**ingest 不讀這裡**（餵不進 `filing.obj()`） |
| `sec_local/` | `filings/{YYYYMMDD}/{accession}.nc` 完整申報檔 | **ingest 的 10-K/10-Q 唯一來源** |
| `Fundamentals/` | Fundamentals + IncomeStatement `.txt` | 讀取，套 keep-latest |
| `News/` | News `.txt` | **整個目錄排除**（`RAW_EXCLUDE_DIRS`），理由見 [`../CLAUDE.md`](../CLAUDE.md)〈資料範圍〉 |
| `_archive_stale/` | 過期快照 | **整個目錄排除** |

⚠ 掃描必須**遞迴**（`RAW_DIR.rglob`）：改回非遞迴的 `iterdir()` 會掃到 0 個檔案，而且**不報錯**，只是安靜地少 ingest 一批 `.txt`。傾印輸出 `data/edgar_processed/` 鏡像同樣的分類。

### 抓取／處理分離

**在此之前兩支程式各自去 SEC 抓同一批 filing**，後果是每次重新切塊都可能悄悄換到不同版本的文件——**改了切塊規則跑出來的差異，分不清是規則造成還是換版造成**。

現在：**抓取層決定「抓哪幾份」並落地，處理層只照 manifest 取件、零網路。** 控制點是 `data/raw/sec_manifest.json`（ticker/form/cik/accession/filing_date）。

```bash
python fetch_data.py --annuals 3 --quarters 8 --skip-fundamentals     # 抓（會連網）
python data_update_edgar.py --rebuild --rcts-fallback --collection X  # 處理（零網路）
```

**三個踩過的坑，都會靜默出錯**：
1. **`Filing.sgml()` 找不到本機檔會無聲 fallback 去下載**。所以 `_filings_from_manifest` 自己檢查 `.nc` 存在與否，缺檔就**中止**；`--allow-fetch` 是明知後果時的逃生口。
2. **`Filing.html()` 對 inline-XBRL 一律重新下載**：`html.startswith("<?xml")` 分支會丟掉已從本機讀到的 HTML。而 SEC 財報全都是 `<?xml` 開頭 → 本機儲存對 10-K/10-Q 幾乎沒生效。`_install_offline_html_patch()` 繞過它；**繞過前已驗證內容 MD5 完全相同**。
3. `local_filing_path()` 回**絕對路徑**、`SEC_LOCAL_DIR` 是相對路徑，`relative_to` 會拋 ValueError 並被外層 `except` 吞掉——第一次跑就是這樣：`.nc` 全寫成功，manifest 卻是空的。

⚠ **驗證方法別只看有沒有報錯**：攔掉 `sec.gov` 的 DNS 解析再跑完整處理流程。實測全程 **0 次連線嘗試**。

---

## 重建 collection

```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection <名稱>
```

### `--rcts-fallback` 不可省，但預設是關的

漏掉會得到一個實質劣化的索引：實測**少 12% chunks**，且 **7.4%** 的 text chunk 超過 RCTS 的 1200-token 補切門檻（開啟時僅 0.1%），超出部分在 rerank 階段會被 `RERANK_MAX_LENGTH=2048` 截斷、**內容等於看不到**。

> ⚠ **別用字元數推 token 數**：曾有 6898 字元的 chunk，直覺換算成 ~1700 token 而誤判成「沒開 RCTS」，實測只有 1208 token（BGE-M3 多語 tokenizer 對英文約 **5.7 字元/token**）。**2026-08-07 就是這樣推錯一次、用錯旗標跑掉一次完整重建。** 要判斷就載 `BAAI/bge-reranker-v2-m3` 的 tokenizer 實際數。

### 重建不會逐字重現舊 collection——兩個正當差異

1. **SEC 新申報**：一有新申報就換版。
2. **表格抽取有跑次間變異（約 2~3 個 chunk/filing）**，兩個成因：
   ① 無 caption 的大表格由 `_llm_summarize_table` 生成摘要（走 Groq ＋ `temperature=0`，但**變異會收斂不會消失**——實測三張表各跑三次仍有一張在兩種正確措辭間跳動）；
   ② **連表格本體都會變**（行政表格的表頭列有無、長度差 5~17 字元），推測來自 unstructured 對原始 HTML 的表格偵測。**text chunk 完全不受影響。**

> **這是變異不是 bug 的證據**：三方交叉比對同一份 filing 的 127 個 table chunk，exp4↔HEAD＝2、HEAD↔新碼＝2、exp4↔新碼＝3——**兩個都不含新改動的版本彼此就差 2**，沒有任何一方是離群值。

→ **判斷重建是否正常，看的是「扣掉換版文件後 text chunk 是否逐字相同」，不是總 chunk 數，也不要拿 table 當判準。** 實測基準：`.txt` 三類逐字全等；排除換版文件後 filing chunk 數相同，其中 2355 個 text chunk 逐字全等。

### 重建後必跑三道閘門

| 閘門 | 抓什麼 |
|---|---|
| [`eval/verify_table_captions.py`](../eval/verify_table_captions.py) | `missing_on_big` 是 **Groq 429 靜默降級的唯一出口**；`numeric/stub` 是 caption 選錯來源。⚠ `no_caption` 本身不是缺陷 |
| [`eval/verify_segment_split.py`](../eval/verify_segment_split.py) | 三層硬邊界（**section 層**）。零網路、零 embedding、不碰 Qdrant → 可在別的實驗跑的時候執行 |
| [`eval/verify_chunk_grounding.py`](../eval/verify_chunk_grounding.py) | 幅度接地的 **chunk 層**。⚠ **與上一支缺一不可**：同一次跑，section 層報「19 → 0 全部接地」，chunk 層卻抓到一筆漏抓 |

### 冪等性

- **`.txt`（Fundamentals／IncomeStatement）**：MD5 存進 `hashes_edgar.json`，**以 collection 為第一層 key**（避免「A collection 已 ingest 過」誤讓 B collection 跳過）。`--force-txt` 可繞過。
- **SEC filing**：**不做 hash 快取**。filing 一經申報，同一 accession number 內容就不再變動；且內容要呼叫 API 抓回來才存在，加快取省不到 API 呼叫、只省 embedding。改以「依 `source` 刪除舊 points 再 upsert」保證重跑正確。
- **keep-latest**（Fundamentals／IncomeStatement）：同一公司只留最新快照，且**主動刪除過期快照殘留的 chunk**。10-K/10-Q 則允許多期並存。
- Point ID 用 deterministic UUID（`uuid5(NAMESPACE_DNS, chunk_id_str)`），同一 chunk 重跑 ID 不變。

---

## 切塊六層

```
Item → 期間章節 → 通用小標 → 幅度接地 → SemanticChunker → RCTS 上界 → min-size 下界
└──────────── 規則：決定「哪裡不准切」 ────────────┘  └── 切塊器：決定「裡面哪裡切」 ──┘
```

**前者取代不了後者**，這是量清楚的（見〈三個貫穿全篇的判準〉①）。

### ① Item 邊界

用 edgartools 取得官方 Item 結構（Item 1A／Item 7／Part I, Item 2…），語意切割只在同一個 Item 內進行，避免不同章節因語意相近被併成同一個 chunk。另外：財報三表各自整份保留為一個 chunk（不切不摘要）；三大表以外的表格另從原始 HTML 抽出，用「大數字集合重疊度」與三大表去重。

### ② 期間章節邊界

**病灶**：10-Q 的 MD&A 把「單季比較」與「累計比較」寫成兩個相鄰章節，期間**只寫在章節標題**、內文每句都不重述。SemanticChunker 把標題切到別的 chunk，內文就讀不出自己屬於哪一期。**生成器不是讀錯，是資訊根本不在 context 裡。**

| 實測（剝掉自注入的 metadata 前綴後） | 數字 |
|---|---|
| 含變動陳述的 filing text chunk | 109 |
| 其中讀不出期間 | **15（14%）** |
| 集中在 MSFT 10-Q | **9／24＝38%** |

集中的原因：`X Compared with Y` 這個章節標題結構**只有 MSFT 在用**（12 份 10-Q 全掃：MSFT 24 處、其餘 6 家 0 處），別家把期間寫在句子裡，切塊切不掉。已知受害案例是 `col-11`（單季 19/33/12/22% 與九個月 18/29/20% 被當成互斥數值並陳）。

**修法**：`_split_by_period_section` 把標題當**硬邊界**（先切段、各段再各自送 SemanticChunker），再把期間標籤前綴進被 embed 的文字。
- **必須切段、不能只貼標**：實測某 chunk 開頭是九個月數字、標題卻出現在它中段——一個 chunk 橫跨兩期，只貼標會標錯。
- `item_chunk_index` 跨期間段**連續編號**（Neighbor Expansion 靠 ±1 查鄰居，各段從 0 起算會重號）。
- 對沒有這種標題的 filing 回傳單一段，行為與加這層之前完全相同。

⚠ **`_PERIOD_SECTION_RE` 必須大小寫敏感**：MSFT 10-K 裡有一句句中小寫的 `… fiscal year 2026 compared with fiscal year 2025 included:`，加 `re.I` 會從句子中間切開。

### ③ 通用小標邊界

**病灶與 ② 同形、低一層，但後果更嚴重**：讀不出期間是「資訊缺失」，冒充總計是「**產生一個看起來有憑有據的錯數字**」，而 faithfulness 結構上抓不到（數字確實在來源裡）。

**確診案例 `mix-03`**：問「MSFT 最新一季**整體**營業利益成長多少」，系統答 +$2.7B / 24%。那句原文**開頭就是** `Operating income increased $2.7 billion or 24%.`，帶了期間標籤卻沒有分部標籤。算一次減法就定案：

| | 單季 |
|---|---|
| 合併 operating income 2026 / 2025 | 38,398 / 32,000 → **+6,398（+20%）** |
| Intelligent Cloud 分部 | 13,753 / 11,095 → **+2,658（+24%）** |

**gold 是對的**，正解那句也**在語料裡**，只是沒被檢索到——冒充總計那句開頭就是那個句型，字面與語意都比「數字埋在段中」的正解更像答案。所以貼標同時修兩邊：生成端不再讀成總計，檢索端對「全公司」問題的相似度下降。

⚠ **兩個歸因錯誤都出在只看表面樣態**：① 當初把 `mix-03` 歸成期間問題（只看到「兩個 operating income 並存」）② 後來又先判 gold 錯（只看到「九個月是 22%、gold 寫 20%」的印象）。→ **有確定性答案可算的時候，先算。**

**偵測器刻意不用硬編碼指標／分部名單**。條件：① 獨立成行的短標題（4~60 字元、無數字、不以標點結尾、字數 ≤6、字首大寫、不在導航殘渣清單）② **下一個非空行像散文**（≥8 詞或以句號結尾）。

- **② 是關鍵**：少了它會把壓平表格的列標籤（`Total` 31 次／`Revenue` 11／`Amount`／`Numerator`／`Assets`）全當標題。這些是**開放集合、列不完**，只能用結構判。
- **必須按行號切、不能按字串切**：標題名同時是 SEGMENT RESULTS 表的列標籤，字串比對會把表格切成兩半。
- **表格為主體的 Item 整層不套用**（`_TABLE_DOMINATED_ITEMS`）：實測 TSLA 10-K Item 15（展覽表）只用條件① 會抓 27 個候選、**區間中位數 10 字元**；Item 8 抓 150 個混雜 `Page`／`/s/ PricewaterhouseCoopers LLP`。這是格式定義的封閉集合，列清單正當。

> ⚠ **「重複次數 ≥3 判為頁面殘渣」是錯的，實測推翻**：`Table of Contents` 在 GOOGL 20 次、TSLA 只有 1 次（連自己都不穩定），而真標題 `Intelligent Cloud` 7 次、`More Personal Computing` 8 次——**完全重疊**。照那個門檻做會把 `mix-03` 的修復砸掉。所以拆成兩個機制：導航殘渣＝**格式定義的封閉集合**（列清單正當）；表格列標籤＝**開放集合**（用條件② 擋）。
> ⚠ **別被「其餘 6 家零誤報」誤導**：原版探針**跳過了沒有期間章節的檔案**，等於根本沒測到那些情境。當日差點據此把範圍開太大。
> ⚠ **想刪掉 bare-digit 獨立行（頁碼）也是錯的**：MSFT 10-K 有 **502** 個，遠多於頁數，多數是壓平表格的儲存格，刪掉會毀損表格數字。改由合併吸收。
> ⚠ **殘留已知限制**：簽名頁的人名（`TIMOTHY D. COOK`）仍可能被當標題。判定為外觀噪音不追——那些 chunk 不帶任何財務主張。

**效果**（`period` 基準 → 加這層）：帶標籤的散文字元 **2.8% → 53.0%**，七家全覆蓋；退化 chunk（剝前綴後 <200 字元）**11.1% → 2.6%**；`mix-03` 斷言從三個 run 全 FAIL 變成 PASS。

### ④ 幅度接地

> 這層修的是**上一層自己製造的反向缺陷**——所以它不是獨立的新功能，而是通用小標層的必要配套。

**病灶**：AAPL 的 MD&A 把每個地區/產品線寫成「粗體小標 ＋ 一句話」，幅度**全部只在上方那張表裡**。小標層在 `Greater China` 切開之後，那段只剩 451 字元、**一個數字都沒有**。

| （MD&A item、含「營收/利潤 + increased/decreased」的散文 chunk） | 加小標層前 | 加小標層後 |
|---|---|---|
| 含漲跌陳述的散文 chunk | 76 | 124 |
| 中位長度 | 2000 字元 | **616 字元** |
| **整段無任何幅度數字** | **0（0.0%）** | **28（22.6%）** |

28 筆**全是 AAPL**（該公司 31 個裡 90.3%），其餘六家 0。

**判準＝自足性，不是長度**：`_is_orphan_explainer` 三條件同時成立才併——① 短（`_ORPHAN_MAX_CHARS=1200`）② 有漲跌陳述 ③ 段內無任何幅度數字。併回**前一段**（帶著那張表的母節），標籤取前一段的。

- **MSFT 一段都不會被動到**，所以 `mix-03` 的修復不受影響：`Operating income increased $2.7 billion or 24%` 段內就有數字＝自足。**這不是巧合**——判準問的正是「MSFT 把幅度寫在句子裡、AAPL 只寫在表裡」這個差異。
- **前一段本身沒有數字時不併**：那代表兩段沒有「表格→解釋」的關係。
- **只套 MD&A item**（`_MDNA_ITEMS`）：實測 28 筆全落在 MD&A，別的 item（如 Risk Factors）「沒有數字的散文」是常態。

> ⚠ **① 那個長度上界不能省**，少了它會誤抓一整類長篇質性敘述：MSFT `Economic Conditions, Challenges, and Risks`（3212 字元）、GOOGL `Understanding Alphabet's Financial Results`（6362）、AMZN `Overview`（8229）、TSLA `Automotive and AI Enabled Products—Production`（7809）。它們的 "increased" 是泛論而非「某科目成長多少」，併進表格段只會製造稀釋型大 chunk。
> ⚠ **閘門要問「還能接地卻沒接」，不是「宇宙中沒有孤兒句」**：第一版寫成後者，閘門直接 exit 1，而殘留的全是上面那類長敘述和無處可接的首段。**量尺過鈍會把「規則正確地不作用」讀成失敗。**

### ⑤⑥⑦ SemanticChunker / RCTS 上界 / min-size 下界

- **SemanticChunker**（BGE-M3 dense）：決定「同一個硬邊界段落裡面哪裡斷」。
- **RCTS 上界**：超過 1200 reranker token 就用 `RecursiveCharacterTextSplitter`（800/80，length function 用 reranker 自己的 tokenizer）補切。見上方 `--rcts-fallback`。
- **min-size 下界（`_merge_small_chunks`）**：③ 這層自己會製造碎片——實測切出 1210 段中 161 段（13.3%）不到 200 字元。拆開看是 80 個「開頭無標籤」（就是 Item 標題行，已改為併進下一段）＋ 81 個「有標籤」（**刻意保留**：短但自帶 scope，往前併會被貼上前一段的標籤＝**貼錯標，比短更糟**）＋ 0 個中段無標籤。

⚠ `_merge_small_chunks` 是**逐硬邊界呼叫**的，而 `verify_chunk_grounding` 只看 `chunk_index - 1`（會跨邊界）→ **兩者對「相鄰」的定義不同**。這是現存那筆 0.1% 漏抓還沒定案的原因，見 [`../BACKLOG.md`](../BACKLOG.md)。

---

## 三個貫穿全篇的判準

**① 「有規則就不需要語意切割」不成立。**
規則邊界的覆蓋率只有 **2.8%**（`X Compared with Y` 期間標題只有 MSFT 在寫），而 276 個散文 Item 平均被切成 **10.7 塊**，最大的 `META_10Q Part_II_Item_1A` 是 **105 塊**。三件事是**不同的工作**：

| 工作 | 有無唯一正確答案 | 該用什麼 |
|---|---|---|
| 這一行是不是章節邊界（**不可跨越**） | 有 | 規則／Python |
| 這 40k 字的敘述**該在哪裡斷** | **沒有** | SemanticChunker |
| 標籤要不要傳進 chunk 文字 | 有 | 規則（前綴注入） |

**② 「語意切割會少掉資訊」也不成立。**
`col-11`／`mix-03` **不是語意切割造成的**：RCTS 在 1200 token 一樣會把獨立成行的小標與 5000 字的段落本體分開。管用的修法是**正交的一層**（硬邊界＋標籤前綴進 embed 文字），與裡面用哪個切塊器無關。

⚠ 而「語意切割會切出不好的 chunk」是真的，但**量級遠小於第一眼**：第一次量到的「10.1% 低於 200 字元」是**讀錯的**（沒剝掉自注入前綴、也把合法的短 Item 算進去）。分類後真正退化的約 1~2%，更決定性的是**掃 1000 個 retrieved context 只有 3 個（0.30%）**，且是同一個 chunk。

**③ 這一層刻意不用 RAGAS 驗收。**
改動會動到幾乎每個散文 chunk 的邊界，但能否提升分數遠在 MDE（4~7 題）之下，跑分只會給噪音。**驗收一律用零 LLM 的確定性閘門**（`verify_segment_split.py`／`verify_chunk_grounding.py`／`verify_table_captions.py`）＋ 逐條斷言（`check_number_defects.py`）。
