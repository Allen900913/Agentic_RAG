# 語料處理（ingest）知識庫

> **這裡放什麼**：語料怎麼進到 collection、切塊每一層在做什麼、重建要注意什麼、以及每層改動當初的診斷過程。

**這裡不放什麼**：日期式變更記錄（→ [`CHANGELOG.md`](../CHANGELOG.md)）、還沒做的事（→ [`BACKLOG.md`](../BACKLOG.md)）、量測方法（→ [`EVAL.md`](EVAL.md)）。

## 目錄

  - [語料目錄結構（2026-08-07 重整）](#語料目錄結構2026-08-07-重整)
  - [抓取／處理分離（2026-08-08）](#抓取-處理分離2026-08-08)
  - [重建生產 collection 的正確指令（2026-08-07 補記）](#重建生產-collection-的正確指令2026-08-07-補記)
  - [重建不會逐字重現舊 collection——兩個正當差異](#重建不會逐字重現舊-collection-兩個正當差異)
  - [期間章節邊界（2026-08-08 加）](#期間章節邊界2026-08-08-加)
  - [通用小標邊界（2026-08-09 加，當日由「分部小標」通用化）](#通用小標邊界2026-08-09-加-當日由-分部小標-通用化)
  - [幅度接地（2026-08-11 加，`_merge_unquantified_sections`）](#幅度接地2026-08-11-加-_merge_unquantified_sections)
  - [A5.6 mix-03 端到端定案：不是 gold 錯，也不是期間，是**分部小標脫落**（2026-08-09）](#a5-6-mix-03-端到端定案-不是-gold-錯-也不是期間-是分部小標脫落2026-08-09)
  - [A5.7 分部小標 → **通用小標**，並補上切塊的下界護欄（2026-08-09 同日下午）](#a5-7-分部小標-通用小標-並補上切塊的下界護欄2026-08-09-同日下午)
  - [A5.8 端到端驗收：`us_stock_rag_edgar_head` 重建 + mix-03 修好（2026-08-10）](#a5-8-端到端驗收-us_stock_rag_edgar_head-重建-mix-03-修好2026-08-10)

---

### 語料目錄結構（2026-08-07 重整）
`data/raw/` 已分成三類子目錄，**根目錄不再有任何檔案**：

| 目錄 | 內容 | ingest 行為 |
|---|---|---|
| `Filings/` | 10-K/10-Q `.html`（primary document） | 由 `fetch_data.py` 產生供人眼查閱；**ingest 不讀這裡**（餵不進 `filing.obj()`，見「抓取／處理分離」） |
| `sec_local/` | `filings/{YYYYMMDD}/{accession}.nc` 完整申報檔 | **ingest 的 10-K/10-Q 唯一來源**（2026-08-08 加，279 MB） |
| `Fundamentals/` | Fundamentals + IncomeStatement `.txt` | 讀取，套 keep-latest |
| `News/` | News `.txt` | 讀取，不去重（時間序列） |
| `_archive_stale/` | 過期快照（0508/0519） | **整個目錄排除**（`RAW_EXCLUDE_DIRS`） |

掃描必須**遞迴**（`RAW_DIR.rglob`）：改回非遞迴的 `iterdir()` 會掃到 0 個檔案，而且**不報錯**，只是安靜地少 ingest 38 個 `.txt`（新聞＋基本面全滅）。傾印輸出 `data/edgar_processed/` 鏡像同樣三分類（`_category_for`）。

### 抓取／處理分離（2026-08-08）
**在此之前兩支程式各自去 SEC 抓同一批 filing**：`fetch_data.py::fetch_sec_filings` 抓一次存成 `.html`，`data_update_edgar.py` 又自己 `company.get_filings()` 抓一次、且完全不理會前者存下的檔。後果是**每次重新切塊都可能悄悄換到不同版本的文件**——改了切塊規則跑出來的差異，分不清是規則造成還是換版造成。

現在：**抓取層決定「抓哪幾份」並落地，處理層只照 manifest 取件、零網路。**

| 產物 | 用途 |
|---|---|
| `data/raw/Filings/*.html` | primary document，人眼查閱（處理層**不讀**） |
| `data/raw/sec_local/filings/{YYYYMMDD}/{accession}.nc` | **完整申報檔，處理層唯一讀得懂的格式**（279 MB / 21 份） |
| `data/raw/sec_manifest.json` | 取件清單（ticker/form/cik/accession/filing_date）＝**控制點** |

```bash
python fetch_data.py --tickers NVDA MSFT AAPL GOOGL AMZN META TSLA --skip-news --skip-fundamentals   # 抓
python data_update_edgar.py --tickers MSFT --skip-txt --rcts-fallback --collection <名稱>            # 處理（離線）
```

**三個踩過的坑，都會靜默出錯**：
1. **`Filing.sgml()` 找不到本機檔會無聲 fallback 去下載**（讀原始碼確認）。所以 `_filings_from_manifest` 自己檢查 `.nc` 存在與否，缺檔就**中止**；`--allow-fetch` 是明知後果時的逃生口。
2. **`Filing.html()` 對 inline-XBRL 一律重新下載**：`html.startswith("<?xml")` 分支會丟掉已從本機讀到的 HTML、改打 `homepage.primary_html_document.download()`。而 SEC 財報全都是 `<?xml` 開頭 → 本機儲存對 10-K/10-Q 幾乎沒生效。`_install_offline_html_patch()` 繞過它；**繞過前已驗證內容 MD5 完全相同**（MSFT 10-Q、NVDA 10-K 各驗一份，只差一個結尾換行）。
3. `local_filing_path()` 回**絕對路徑**，`SEC_LOCAL_DIR` 是相對路徑，`relative_to` 會拋 ValueError 並被外層 `except` 吞掉——第一次跑就是這樣：21 份 `.nc` 全寫成功，manifest 卻是空的，而畫面上顯示 `x ... failed`。

**驗證方法（別只看有沒有報錯）**：攔掉 `sec.gov` 的 DNS 解析再跑完整處理流程。實測 MSFT 10-Q 全程 **0 次連線嘗試**，產出 141 records、40 個帶 `period_context` 的 chunk。

### 重建生產 collection 的正確指令（2026-08-07 補記）
```bash
.venv/Scripts/python.exe data_update_edgar.py --rebuild --rcts-fallback --collection us_stock_rag_edgar_exp4
```
**`--rcts-fallback` 不可省，但預設是關的**——這是最容易踩、且先前沒有任何文件記載的坑。漏掉它會得到一個實質劣化的索引：實測 3272 vs 3673 chunks（少 12%），且 **7.4%** 的 text chunk 超過 RCTS 的 1200-token 補切門檻（開啟時僅 0.1%），超出部分在 rerank 階段會被 `RERANK_MAX_LENGTH=2048` 截斷、內容等於看不到。

判定 exp4 當初有開的依據是 **token 分佈**：exp4 的 text chunk 最大 1208 token、>1200 者只有 4 個，精準卡在門檻上，是 RCTS 開啟的明確特徵。
> ⚠ **別用字元數推 token 數**：exp4 有 6898 字元的 chunk，直覺會換算成 ~1700 token 而誤判成「沒開 RCTS」，實測卻只有 1208 token（BGE-M3 多語 tokenizer 對英文約 **5.7 字元/token**）。2026-08-07 就是這樣推錯一次、用錯旗標跑掉一次完整重建。要判斷就載 `BAAI/bge-reranker-v2-m3` 的 tokenizer 實際數。

### 重建不會逐字重現舊 collection——兩個正當差異
1. **SEC 新申報**：edgar 每次抓「最新 10-K ＋ 最近 2 份 10-Q」，SEC 一有新申報就換版（2026-08-07 實測換掉 4 份：AAPL/AMZN/META 的 10-Q、MSFT 的 10-K）。
2. **表格抽取有跑次間變異（約 2~3 個 chunk/filing）**：兩個成因——① 無 caption 的大表格由 `_llm_summarize_table` 生成摘要（**2026-08-11 起走 Groq `llama-3.3-70b-versatile`＋`temperature=0`，此前是 Gemini 且未固定 temperature、`max_output_tokens=60` 會截斷**；變異會收斂但不會消失，實測三張表各跑三次仍有一張在兩種正確措辭間跳動，細節見該函式 docstring） ② **連表格本體都會變**（實測 GOOGL Exhibit Index 等行政表格，表頭列有無、長度差 5~17 字元），推測來自 unstructured 對原始 HTML 的表格偵測與 `_find_caption` 回溯。**text chunk 完全不受影響**。
   > 這是變異不是 bug 的證據：2026-08-07 三方交叉比對同一份 `GOOGL_10K_2025`（127 個 table chunk），exp4↔HEAD＝2、HEAD↔新碼＝2、exp4↔新碼＝3——**兩個都不含新改動的版本彼此就差 2**，沒有任何一方是離群值。要判斷某次改動有沒有動到切塊，比 text chunk（確定性）而不是 table。

扣除這兩項後，2026-08-07 驗證結果：`.txt` 三類（news 98／fundamentals 22／income_statement 59）**逐字全等**；排除換版文件後 filing chunk 數 2832 vs 2832 相同，其中 2355 個 text chunk **逐字全等**。→ 判斷重建是否正常，看的是「扣掉換版文件後 **text chunk** 是否逐字相同」，不是總 chunk 數，也不要拿 table 當判準。

### 期間章節邊界（2026-08-08 加）
> ⚠ **生產 collection `us_stock_rag_edgar_exp4` 尚未套用**——這是 ingest 期的改動，只有重建後才生效。要判斷有沒有生效：payload 有沒有 `period_context` 欄位。

**病灶**：10-Q 的 MD&A 把「單季比較」與「累計比較」寫成兩個相鄰章節，期間**只寫在章節標題**、內文每句都不重述。SemanticChunker 依語意切塊時把標題切到別的 chunk，內文 chunk 就讀不出自己屬於哪一期。生成器不是讀錯，是**資訊根本不在 context 裡**。

| 實測（exp4 全庫，剝掉自注入的 metadata 前綴後） | 數字 |
|---|---|
| 含變動陳述的 filing text chunk | 109 |
| 其中讀不出期間 | **15（14%）** |
| 集中在 MSFT 10-Q | **9／24＝38%** |

集中的原因：`X Compared with Y` 這個章節標題結構**只有 MSFT 在用**（12 份 10-Q 全掃：MSFT 24 處、其餘 6 家 0 處），別家把期間寫在句子裡，切塊切不掉。已知受害案例：**col-11**（單季 19/33/12/22% 與九個月 18/29/20% 被當成互斥數值並陳）。
> ⚠ **mix-03 曾被歸在這裡，2026-08-09 查清後移除**：它的病不是期間而是**分部**（把 Intelligent Cloud 的 +$2.7B/24% 當成全公司總計），修在下一小節。當初的歸因只看了「兩個 operating income 數字並存」就下結論，沒有回原文驗算是哪一種混用。

**2026-08-09 補**：`_PERIOD_SECTION_RE` 加入 10-K 的財年寫法 `Fiscal Year 2026 Compared with Fiscal Year 2025`。動機不是期間標籤本身（10-K 整份就是一個財年，`report_period_code` 已足夠），而是當時**它是分部小標那層的開關**——分部切段只在有期間標籤的章節內生效。⚠ **這個開關作用在同日下午的通用化後已不存在**（通用小標層改由 `_TABLE_DOMINATED_ITEMS` 決定適用範圍，不再看有無期間標籤）；這條 pattern 現在只影響 10-K 的期間標籤本身。⚠ 這個 pattern **必須大小寫敏感**：MSFT 10-K 裡還有一句句中小寫的 `… fiscal year 2026 compared with fiscal year 2025 included:`，加 `re.I` 會從句子中間切開。

**修法**：`_split_by_period_section` 把標題當**硬邊界**（先切段、各段再各自送 SemanticChunker），再把期間標籤前綴進被 embed 的文字。
- **必須切段、不能只貼標**：實測 MSFT `#104` 開頭是九個月數字、標題卻出現在它中段——一個 chunk 橫跨兩期，只貼標會標錯。
- `item_chunk_index` 跨期間段**連續編號**（Neighbor Expansion 靠 ±1 查鄰居，各段從 0 起算會重號）。
- 對沒有這種標題的 filing 回傳單一段，行為與加這層之前完全相同。
- 驗證方式：`_split_by_period_section` 對 7 家最新 10-Q 的實際 SEC 原文跑過——MSFT `Part I, Item 2` 切出 13 段、95 條變動陳述有 85 條（89%）拿到期間標籤；其餘 6 家 0 段。

### 通用小標邊界（2026-08-09 加，當日由「分部小標」通用化）
> ⚠ 與期間那層同樣是 ingest 期改動，**需重建後才生效**；判斷有沒有生效看 payload 有沒有 `heading_context`。
> **2026-08-10 已重建成 `us_stock_rag_edgar_head`**（4221 points，`--rcts-fallback` ON）。實測結果：

| | `us_stock_rag_edgar_period`（基準） | `us_stock_rag_edgar_head` |
|---|---|---|
| filing 散文 chunk | 2955 | 3477（+18%） |
| 帶 `heading_context` | 0 | **2154（61.9%）**，七家全覆蓋 |
| 前綴進 embed 文字 | — | 2154/2154 |
| **退化 chunk（剝前綴後 <200 字元）** | **328（11.1%）** | **92（2.6%）** |
| mix-08 孤兒句所在 chunk | 65 字元 `The increases were almost entirely driven by…`（指涉全被切掉） | **255 字元**，開頭 `Family of Apps FoA revenue in the three and six months ended June 30, 2026 incre…` |
| **mix-03 斷言**（`number_claims.json`） | 三個 run 全 **FAIL**（讀到 24%） | **PASS**（答「增加 64 億美元，增幅約 20%」） |

剩下的 92 個短 chunk 都是**本來就該短**的 Item（`Item 1B. Unresolved Staff Comments None.`／`ITEM 6.[Reserved]`），沒有鄰居可併也不該丟。

**完整 100 題驗收（2026-08-10，`gj_head_full100_20260810`，共用 replay fixture）**：

| | period（兩個 run） | head |
|---|---|---|
| 接地主張斷言 | 2 PASS / 1 FAIL、1 PASS / 1 FAIL / 1 N/A | **3 PASS / 0 FAIL** |
| 頭條百分比候選（篩選用，非閘門） | 6／4 | **1** |

⚠ **是否升為生產 collection 未定案**——`us_stock_rag_edgar_exp4` 仍是表頭寫的生產值。**還缺的是 RAGAS 沒退步的確認**（+18% 散文 chunk 會改變候選池組成），而 exp4 vs head 的對照要用同樣剝除規則的結果檔。

**病灶與期間那層同形、低一層，但後果更嚴重**：讀不出期間是「資訊缺失」，冒充總計是「產生一個看起來有憑有據的錯數字」。

**確診案例 mix-03**（端到端查清）：問「MSFT 最新一季**整體**營業利益成長多少」，系統答 +$2.7B / 24%。那句原文在 `MSFT_10Q_202603 #111`，**開頭就是** `Operating income increased $2.7 billion or 24%.`，帶了期間標籤卻沒有分部標籤。用合併損益表驗算：

| | 單季 | 九個月 |
|---|---|---|
| 合併 operating income 2026 / 2025 | 38,398 / 32,000 → **+6,398（+20%）** | 114,634 / 94,205 → +20,429（+22%） |
| Intelligent Cloud 分部 | 13,753 / 11,095 → **+2,658（+24%）** | — |

→ #111 是 Intelligent Cloud。**gold（$6.4B / 20%）是對的**，正解那句也**在語料裡**（`#104`），只是沒被檢索到——#111 開頭就是那個句型，字面與語意都比「數字埋在段中」的 #104 更像答案。所以貼標同時修兩邊：生成端不再讀成總計，檢索端 #111 對「全公司」問題的相似度下降。

**曝險**（`us_stock_rag_edgar_period` 全庫）：32 個含 `X increased $Y or Z%` 的 filing text chunk 裡 **5 個（16%）不含任何分部名稱**＝讀起來像合併總計，**全在 MSFT**（10-Q 4、10-K 1）。

**通用化（同日下午）**：原版只認分部、只在有期間標籤的章節內生效，覆蓋率**全庫 2.8%（84/2955 個散文 chunk，全部是 MSFT）**——因為 `X Compared with Y` 期間標題只有 MSFT 在寫。通用化後**帶標籤的散文字元 53.0%**，7 家全覆蓋。

**「有規則就不需要語意切割」不成立**，這是通用化時量清楚的：276 個散文 Item 平均被切成 10.7 塊，最大的 `META_10Q Part_II_Item_1A` 是 105 塊。三件事是**不同的工作**：

| 工作 | 有無唯一正確答案 | 該用什麼 |
|---|---|---|
| 這一行是不是章節邊界（**不可跨越**） | 有 | 規則／Python |
| 這 40k 字的敘述**該在哪裡斷** | **沒有** | SemanticChunker |
| 標籤要不要傳進 chunk 文字 | 有 | 規則（前綴注入） |

規則決定「哪裡**不准**切」，切塊器決定「裡面**哪裡**切」，**前者取代不了後者**。切塊層級：Item → 期間章節 → 通用小標 → **幅度接地**（把不自足的解釋段併回它的表格）→ SemanticChunker → RCTS 上界 → min-size 下界。

**偵測器刻意不用硬編碼指標／分部名單**（見 memory `llm-vs-python-task-split`）。條件：① 獨立成行的短標題（4~60 字元、無數字、不以標點結尾、字數 ≤6、字首大寫、不在導航殘渣清單）② **下一個非空行像散文**（≥8 詞或以句號結尾）。
- **② 是關鍵**：少了它會把壓平表格的列標籤（`Total` 31 次／`Revenue` 11／`Amount`／`Numerator`／`Denominator`／`Assets`）全當標題。這些是**開放集合、列不完**，只能用結構判。
- **必須按行號切、不能按字串切**：標題名同時是 SEGMENT RESULTS 表的列標籤，字串比對會把表格切成兩半。
- **通用化時刪掉舊條件 ③「同檔以獨立行出現 ≥2 次」**：它是為「專門找分部」而設，對通用標題過度收斂。
- **表格為主體的 Item 整層不套用**（`_TABLE_DOMINATED_ITEMS = {Item 6, Item 8, Item 15, Part I Item 1}`）：實測 TSLA 10-K Item 15（展覽表）只用條件① 會抓 27 個候選、**區間中位數 10 字元**；Item 8 抓 150 個混雜 `Page`／`/s/ PricewaterhouseCoopers LLP`。這是格式定義的封閉集合，列清單正當（同 `VALID_*_ITEMS`）。

> ⚠ **「重複次數 ≥3 判為頁面殘渣」是錯的，實測推翻**：`Table of Contents` 在 GOOGL 20 次、META 5 次、TSLA 只有 1 次（連自己都不穩定），而真標題 `Intelligent Cloud` 7 次、`More Personal Computing` 8 次、`Energy Generation and Storage` 8 次——**完全重疊**。照那個門檻做會把 mix-03 的修復砸掉。所以拆成兩個機制：導航殘渣（`Table of Contents`／`Page`／`SIGNATURES`）＝格式定義的**封閉集合**，列清單正當；表格列標籤＝**開放集合**，用條件② 擋。
> ⚠ **別被「其餘 6 家零誤報」誤導**：原版探針**跳過了沒有期間章節的檔案**，等於根本沒測到那些情境。當日差點據此把範圍開太大。原先記錄的四類「誤報」中有三類（TSLA `Cash Flows from Operating Activities`、GOOGL `Other Bets`）**其實是真標題**，只是對「分部」偵測器而言算誤報——通用化後它們是正確標籤。
> ⚠ **這層自己會製造碎片，需要 min-size 配套**：實測切出 1210 段中 161 段（13.3%）不到 200 字元。拆開看是 80 個「開頭無標籤」（就是 Item 標題行 `ITEM 1. BUSINESS`，已改為併進下一段）＋ 81 個「有標籤」（**刻意保留**：短但自帶 scope，往前併會被貼上前一段的標籤＝貼錯標，比短更糟）＋ 0 個中段無標籤。
> ⚠ **殘留已知限制**：簽名頁的人名（`TIMOTHY D. COOK`）仍可能被當標題。判定為外觀噪音不追——那些 chunk 不帶任何財務主張，加人名偵測器的成本高於收益。

**驗收用 [`eval/verify_segment_split.py`](../eval/verify_segment_split.py)**（零網路、零 embedding、可在別的實驗跑的時候執行）。2026-08-09 通用化後基準：21 份 filing 全部有切段（2~13 段/份）、抓到 **405 種**標籤（`Income Taxes` 19／`Google Cloud` 10／`More Personal Computing` 9／`Other Bets` 7…）、帶標籤散文字元 **53.0%**、表格型 Item 被切段 **0** 次、表格對帳 **0** 筆不符、目標句落在 `Intelligent Cloud`。

### 幅度接地（2026-08-11 加，`_merge_unquantified_sections`）
> ⚠ 同樣是 ingest 期改動，**需重建才生效**。這層修的是**上一層自己製造的反向缺陷**——所以它不是獨立的新功能，而是通用小標層的必要配套。

**病灶**：AAPL 的 MD&A 把每個地區/產品線寫成「粗體小標 ＋ 一句話」，幅度**全部只在上方那張表裡**。小標層在 `Greater China` 切開之後，那段只剩 451 字元、**一個數字都沒有**。

| （MD&A item、含「營收/利潤 + increased/decreased」的散文 chunk） | period | head |
|---|---|---|
| 含漲跌陳述的散文 chunk | 76 | 124 |
| 中位長度 | 2000 字元 | **616 字元** |
| **整段無任何幅度數字** | **0（0.0%）** | **28（22.6%）** |

28 筆**全是 AAPL**（該公司 31 個裡 90.3%），其餘六家 0。**已知受害案例 mix-09**：head 讀不到 22%，退回引用新聞的 28%（那是三月季，期間也錯）。

**判準＝自足性，不是長度**：`_is_orphan_explainer` 三條件同時成立才併——① 短（`_ORPHAN_MAX_CHARS=1200`）② 有漲跌陳述 ③ 段內無任何幅度數字。併回**前一段**（帶著那張表的母節），標籤取前一段的。
- **MSFT 一段都不會被動到**，所以 mix-03 的修復不受影響：`Operating income increased $2.7 billion or 24%` 段內就有數字＝自足。這不是巧合——判準問的正是「MSFT 把幅度寫在句子裡、AAPL 只寫在表裡」這個差異。
- **前一段本身沒有數字時不併**：那代表兩段沒有「表格→解釋」的關係。實測 MSFT `Interest and dividends income increased primarily due to…` 是章節首段，無處可接，**保留原狀才對**。
- **只套 MD&A item**（`_MDNA_ITEMS`）：實測 28 筆全落在 MD&A，別的 item（如 Risk Factors）「沒有數字的散文」是常態。同 `_TABLE_DOMINATED_ITEMS` 的理由。
> ⚠ **① 那個長度上界不能省，少了它會誤抓一整類長篇質性敘述**：不設限時殘留 14 段全是這種——MSFT `Economic Conditions, Challenges, and Risks`（3212 字元）、GOOGL `Understanding Alphabet's Financial Results`（6362）、AMZN `Overview`（8229）、TSLA `Automotive and AI Enabled Products—Production`（7809）。它們的 "increased" 是泛論而非「某科目成長多少」，併進表格段只會製造稀釋型大 chunk（＝ sem-08 的病）。
> ⚠ **閘門要問「還能接地卻沒接」，不是「宇宙中沒有孤兒句」**：第一版寫成後者，`verify_segment_split.py` 直接 exit 1，而殘留的全是上面那類長敘述和無處可接的首段。量尺過鈍會把「規則正確地不作用」讀成失敗。

**驗收**：[`eval/verify_segment_split.py`](../eval/verify_segment_split.py) 判準⑤（零網路、零 embedding）。2026-08-11 基準：孤兒解釋段 **46 → 可接地卻沒接 0**（AAPL 31→0、GOOGL 7→0、MSFT 5→0、META 3→0），且 mix-09 的 `18,816`／`15,369`／`22%` 與 Greater China 那句落在同一段（`Segment Operating Performance`，2251 字元）、mix-03 目標句仍在 `Intelligent Cloud`。

### A5.6 mix-03 端到端定案：不是 gold 錯，也不是期間，是**分部小標脫落**（2026-08-09）

追「頭條百分比與 gold 衝突」的 8 題時，mix-03（「MSFT 最新一季**整體**營業利益成長多少」→ 系統答 +$2.7B / 24%，gold $6.4B / 20%）被我先判成「gold 錯」。**算一次減法就推翻了**：

| | 單季 | 九個月 |
|---|---|---|
| 合併 operating income 2026 / 2025 | 38,398 / 32,000 → **+6,398（+20%）** | 114,634 / 94,205 → +20,429（+22%） |
| Intelligent Cloud 分部 | 13,753 / 11,095 → **+2,658（+24%）** | — |

gold 對，系統錯。系統那句在 `MSFT_10Q_202603 #111`，**開頭就是** `Operating income increased $2.7 billion or 24%.`，帶了期間標籤卻沒有分部標籤 → 讀起來就是全公司總計。正解那句**在語料裡**（`#104`）只是沒被撈到：#111 開頭就是那個句型，字面與語意都比「數字埋在段中」的 #104 更像答案。

**這是 A5 期間問題的第三層**，後果更嚴重：讀不出期間是資訊缺失，冒充總計是**產生一個看起來有憑有據的錯數字**，而 faithfulness 結構上抓不到（數字確實在來源裡）。

**兩個歸因錯誤都出在只看表面樣態**：① 當初把 mix-03 歸成期間問題（只看到「兩個 operating income 並存」）② 我這次先判 gold 錯（只看到「九個月是 22%、gold 寫 20%」的印象）。→ 併入 A5.5 的紀律：**有確定性答案可算的時候，先算。**

**修法**（`_split_by_segment_section`，ingest 第三層硬邊界）：偵測器刻意不用硬編碼分部名單，用三個條件——獨立成行的短標題 ＋ **下一個非空行是變動陳述** ＋ 該字串在同一份 filing 出現 ≥2 次。第二個條件是關鍵：少了它會把壓平表格的列標籤（`Revenue`／`Total`／`Percentage`）全當標題，把散文切碎。並且**必須按行號切、不能按字串切**（分部名同時是 SEGMENT RESULTS 表的列標籤）。

**適用範圍收斂到「有期間標籤的章節」**，這是實測逼出來的：套到沒有期間章節的 Item 上會抓到真分部（GOOGL/META/TSLA 的），但同時誤報 META `NM — not meaningful`、META `Other Actions`（法律訴訟，吃 16~58k 字元）、TSLA `Cash Flows from Operating Activities`、GOOGL `Other Bets` 吞掉 20k 字元 Item 尾巴。⚠ 第一版探針顯示「其餘 6 家零誤報」是假象——它**跳過了沒有期間章節的檔案**。

順帶把 `_PERIOD_SECTION_RE` 加上 10-K 的 `Fiscal Year 2026 Compared with Fiscal Year 2025`（動機是它是分部層的開關，不是期間標籤本身）；**大小寫敏感是必要的**，MSFT 10-K 有一句句中小寫的同構文字。

**驗收**：`eval/verify_segment_split.py`（零網路、零 embedding、可與其他實驗併行）。受影響 = MSFT 10-Q ×2（各 2 章節）＋ MSFT 10-K Item 7 ×1；其餘 18 份 filing **0 段**；表格對帳 0 筆不符；目標句落進 Intelligent Cloud。曝險 5/5 全涵蓋。

**未決**：R2 本來該抓到這個答案（它自己說總計 +$2.7B 卻列出單一部門 +$3.6B，算術上不可能），沒抓到是因為 `len(seg_changes) >= 2` 這道門——那輪只列了一個部門。放寬到 ≥1 之前要先量誤報率：「總計 < 某部門」在**另一部門衰退**時是合法的。

### A5.7 分部小標 → **通用小標**，並補上切塊的下界護欄（2026-08-09 同日下午）

起因是使用者的一個設計質疑：「既然知道有規則可以切分，那就不需要語意切割了不是嗎？語意切割還會切出不好的 chunk。」量完後**對了一半，而錯的那一半正是 A5.6 修的東西**。

**① 「規則可以取代語意切割」不成立。** 規則邊界覆蓋率＝ 84/2955 個 filing 散文 chunk ＝ **2.8%，全部是 MSFT**（`X Compared with Y` 期間標題只有 MSFT 在寫）。而 276 個散文 Item 平均被切成 10.7 塊，最大的 `META_10Q Part_II_Item_1A` 105 塊。規則決定「哪裡不准切」，切塊器決定「裡面哪裡切」，前者取代不了後者。

**② 「語意切割會切出不好的 chunk」是真的，但量級遠小於第一眼。** 我第一次量得到「10.1% 低於 200 字元」是**讀錯的**（沒剝掉自注入前綴、也把合法的短 Item 算進去）。分類後真正退化的約 1~2%；更決定性的是**掃 1000 個 retrieved context 只有 3 個（0.30%）**，且是同一個 chunk——`mix-08` 撈到 `The increases were almost entirely driven by advertising revenue.`（65 字元，「The increases」指什麼全被切掉）。真實但稀有，不是指標壓在 0.65 的原因。

**③ 「語意切割會少掉資訊」——這點要更正。** col-11／mix-03 **不是語意切割造成的**：RCTS 在 1200 token 一樣會把獨立成行的小標與 5000 字的段落本體分開。管用的修法是正交的一層（硬邊界＋標籤前綴進 embed 文字），與裡面用哪個切塊器無關。

**改動**：`_split_by_segment_section` → `_split_by_subheading`（通用化，刪掉舊條件 ③ 與「只在有期間章節內生效」的 gate），payload `segment_context` → `heading_context`，前綴 `[segment: X]` → `[section: X]`（`Cash Flows from Operating Activities` 不是分部，叫它 segment 就是貼錯標）；新增 `_merge_small_chunks` 補上此前缺的**下界**護欄（上界 `RCTS_THRESHOLD` 一直都有）。

**量測救掉的兩個錯誤設計**：
1. 想用「同檔重複次數 ≥3 判為頁面殘渣」取代黑名單——實測 `Table of Contents` 在 GOOGL 20 次／TSLA 1 次（連自己都不穩），而真標題 `Intelligent Cloud` 7 次、`More Personal Computing` 8 次，**完全重疊**。照那樣做會把 A5.6 的修復砸掉。
2. 想刪掉 bare-digit 獨立行（頁碼）——MSFT 10-K 有 **502** 個，遠多於頁數，多數是壓平表格的儲存格，刪掉會毀損表格數字。改由合併吸收。

**這層自己會製造碎片**：1210 段裡 161 段（13.3%）不到 200 字元。拆開＝ 80 個「開頭無標籤」（Item 標題行，已併進下一段）＋ 81 個「有標籤」（**刻意保留**，往前併會被貼上前一段的標籤＝貼錯標）＋ 0 個中段無標籤。

**驗收**（`eval/verify_segment_split.py`，零網路零 embedding）：21 份 filing 全部有切段（2~13 段/份）、405 種標籤、帶標籤散文字元 **2.8% → 53.0%**、表格型 Item 被切段 0 次、表格對帳 0 筆不符、目標句仍落在 `Intelligent Cloud`。
**刻意不用 RAGAS 驗收**：改動會動到幾乎每個散文 chunk 的邊界，但能否提升分數遠在 MDE（4~7 題）之下，跑分只會給噪音。

### A5.8 端到端驗收：`us_stock_rag_edgar_head` 重建 + mix-03 修好（2026-08-10）

重建 4221 points（`--rcts-fallback` ON，零網路）。**三個確定性指標，全部可重跑**：

| | period（基準） | head |
|---|---|---|
| 帶 `heading_context` 的散文 chunk | 0 | **2154 / 3477＝61.9%**，七家全覆蓋 |
| 退化 chunk（剝前綴後 <200 字元） | **328（11.1%）** | **92（2.6%）** |
| mix-03 斷言 | 三個 run 全 **FAIL**（讀到 24%） | **PASS**（「增加 64 億美元，增幅約 20%」＝gold） |

**mix-08 的孤兒句也直接修掉**：65 字元的 `The increases were almost entirely driven by advertising revenue.`（「The increases」指什麼全被切掉）→ 併進 255 字元的 chunk，開頭是 `Family of Apps FoA revenue in the three and six months ended June 30, 2026 incre…`，主體與期間都補回來了。

**檢索端也可見**：mix-03 的 context `#0` 帶期間標籤且**無** `[section:]`＝合併層，`#1` 才是 `[section: Productivity and Business Processes]`。原本病灶就是續段 chunk 沒有分部標籤而冒充總計。

**同時修好量尺**（見 `eval/check_number_defects.py` 開頭）：舊的「比第一個百分比」對 col-11／mi-04／mi-08 誤報，而「比任一個百分比」會**漏抓** mix-03（它的 21% 落在 gold 20% 的 ±1pt 內）。現行做法是 `eval/number_claims.json` 逐條斷言＋`anchored_pct`，三態 PASS／FAIL／N/A。**判別力已驗證**：同一份斷言在舊 collection 三次全 FAIL、新 collection PASS。
> ⚠ N/A 這一態是必要的：mix-03 修好後答案不再用「整體/合併」字樣，anchor 未命中 → 若把 N/A 併進 PASS 就會靜默通過。改用**金額**判準（`expect_text` `6.4 billion|64 億美元` / `forbid_text` `2.7 billion|27 億美元`）接手——金額在單題內幾乎不撞，百分比會。

**未升生產**：`us_stock_rag_edgar_exp4` 仍是生產值。要升之前該先跑 100 題確認沒有整體退步（+18% 散文 chunk 會改變候選池組成）。
