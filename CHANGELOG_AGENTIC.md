# CHANGELOG_AGENTIC

紀錄 `agentic_rag*.py`（實驗性入口，複用 `rag_query.py` 檢索/生成，不動生產）的演進。2026-07-17 從
[`CHANGELOG.md`](CHANGELOG.md) 拆出：agentic 與生產是兩條平行線，各自動機/踩坑/定案不互相依賴。
本檔 2026-07-26 由 649 行詳版壓縮成摘要；逐條細節（P0 驗證、trace 全文、消融數字）保存在對話記錄與
git 歷史。貫穿哲學：**只有需要決策的角色才給 agency，不需要決策的角色給 agency 就是給它犯錯空間**。

---

## 架構演進主線（v1 deepagents → 最終 LangGraph）

### ① 2026-07-17 初版：deepagents 雙層 agent（`agentic_rag.py`）
- 主 agent 拆子問題 → `task` 委派 `rag-researcher` subagent → 合成；subagent：檢索 → 看 rerank gate →
  低分才升級（rewrite/english）→ `generate_answer`。
- 取捨：**累積池**（去重聯集重排，重搜只加不洗）取代信任 context；gate 複用既有 rerank 分數，不另建評分模型。
- 模型坑（已試無效）：llama-3.3-70b 對大 schema 吐格式錯 tool-call、gpt-oss-20b parse 失敗 →
  定案 brain=gpt-oss-120b。Groq free tier TPM=8000 / TPD=200k 是真瓶頸。

### ② 續二～三：混合版重構——「檢索用 agency、生成由 Python 強制接管」
- 發現弱腦會**跳過 `generate_answer`、直接用背景知識作答**（prompt 是建議非約束）。
- 定案：**Scout 只挑 chunk（`select_chunks`）、Writer 由 Python 主導單次生成**——把「生成」抽出 agent
  迴圈，結構性消除「跳過生成 / 幻覺 / 弄丟 citation」三類病灶。
- 三層保證：確定性 citation validator（regex + allowlist 比對，抓沒引用/捏造 ID）、coverage validator
  （生成後 LLM 稽核漏點、重生成上限 1）、機械式引用附錄。
- 坑：citation regex 只認 ASCII `[]`，中文輸出全形 `【】` 全 false-negative（已修容錯解析）。

### ③ 續四～五：崩潰降級 + summary 模式 + pool key 根因 bug
- **graph 崩潰降級保證（恆開）**：agent 迴圈崩了就沿用已檢索池走 Writer 收尾，最壞退化成生產單發
  baseline，不再零產出。兩種新崩潰：scout 400 tool_use_failed、gpt-oss-120b 413 單請求 > TPM。
- **`AGENTIC_MODE=summary`**（實驗開關）：Scout 產子摘要、主 agent 看內容判斷夠不夠——修復混合版
  「主 agent 只看 chunk id 無法判斷檢索是否切題」的監督缺陷。前提是強腦；free tier 上安全網會拉回
  Writer、行為趨同 hybrid。日常預設維持 hybrid。
- **pool key 碎裂 bug（根因級）**：`_pool_key` 用完整 `checkpoint_ns`（含 per-step task id）→ 同
  subagent 每次工具呼叫各開新池，「累積池」在真實 graph 從未生效。**改判**續三「弱腦不挑 chunk」是
  冤案——真兇是水管；修 key（只取 ns 第一段）後 scout 其實挑得動。教訓：繞過 brain 直接測 ≠ 真實 graph。

## Provider 演進（Groq → NVIDIA）

### ④ 續六～八：取證 + Groq 惡化
- 主/子腦可分離 + `[TRACE]` instrumentation：用真實執行痕跡回答「agent 真在決策還是走過場」。
- **argv cp950 損毀教訓**：中文 query 經 Bash/PowerShell 損毀 → 壓低語意匹配，我基於損毀 trace 下的三個
  結論全被乾淨 query 推翻。定案：非 ASCII 一律 PowerShell + `$env:PYTHONUTF8=1`（Git Bash 不論旗標都不行）。
- rewrite 升級對「已是英文的 query」是結構性 no-op → 升級槓桿改「針對缺口換一句明顯不同的 query」。
- llama-4-scout 被 Groq 下架 → brain 改回 gpt-oss-120b。

### ⑤ 續九：改走 NVIDIA build.nvidia.com（新分支 `agentic_rag_nv.py`）
- `rq.call_llm` 整個 monkeypatch 成 NVIDIA 路由（`gemini-` 除外），內部裸名呼叫透過共用 `__dict__` 一併生效。
- 選型：BRAIN/RETRIEVAL=`gpt-oss-120b`（最均衡）、GEN=`z-ai/glm-5.2`（中文品質佳）。NVIDIA tool-call
  可靠度/延遲全面優於 Groq、無 TPM 天花板結構瓶頸；gpt-oss-120b 有 harmony token 洩漏殘留（不影響最終答案）。

## ⑥ 2026-07-24~26：LangGraph 全面重寫 + P0 五修 + translate 定案

- **重寫成 6 節點顯式 state machine**（`plan →(retrieve→check→[rewrite|advance])*→ generate→reflect`），
  只有 Sufficiency Checker 做判斷，其餘純函式單次呼叫——消除 deepagents 動態迴圈的所有「稅」。
- **P0 五修**（code review 全屬實）：P0-1 跨子問題 rerank 不可比 →`_fair_select` round-robin；
  P0-2 `bool("false")==True` →`_coerce_bool`；P0-3 CJK snippet 失效 → 2-gram；P0-4 不足判定被丟棄 →
  `unmet` 欄位 + 收尾揭露；P0-5 補救輪二次 rewrite 稀釋 →`enable_rewrite=False`。
- **translate_query_en 三次反覆**：誤植 escalation-only → 常開（colloquial 退步）→ **確定性消融定案關閉**。
  教訓：不能拿「生成→RAGAS LLM judge」論斷檢索層因果；確定性消融（比對 gold、同 judge 背靠背）才承重。
  ⚠ 結論限 `us_stock_rag_edgar_exp4`，換底座需重跑 `eval/ablate_translate_rerank.py`。
- **90 題 RAGAS 三版**：OVERALL 大致持平（4/6 微正、2/6 微負）；multi_intent recall/precision 如期回升
  （驗證 P0-1/4）；`answer_correctness` 微降主因是長度假象非事實（見記憶 `ragas-correctness-length-artifact`）。

## ⑦ 2026-07-26~27：planner prompt 通用化 + 檢索中間層全英文（已驗證）

- **planner 三原則**（不寫死清單）：意圖保全（口語/隱含意圖不可丟）、集合詞 fallback（>上限或不確定成員
  → 保留群體詞當單一寬鬆 query 不猜殘缺清單）、時間消歧。`MAX_SUBQUERIES` 5→7。
  逐題診斷實測：news-13 真兇是 **cap 截斷**（非 prompt 措辭，A/B 證明舊 prompt 配新 cap 就對）、col-15
  非 planner 病（Checker 過度改寫 × merge 全域 top-5 × rerank 對中文平坦，潛在 bug 未結構性修）。
- **`full_translate_en`（新 flag，架構定案）**：中文問中文答，但 dense/sparse 召回 + rerank 一律用英文
  譯句，消除 cross-lingual rerank 對「中文 query × 英文 chunk」評分平坦（診斷：news-08 正解 chunk 因中文
  rerank≈0 被壓到 rank 6，英文後 →rank 2 跨過 commit 門檻）。推翻先前「dense 召回英文傷 sem-11」的保留
  結論（舊消融只量 file-level 已飽和、沒量 chunk 內排序）。
- **全 90 題 RAGAS 驗證（`experiments/agentic/ragas_scores_fulltrans.json`）**：六項全面上升，非噪音——
  漲幅最大的兩項正是診斷對應的檢索層指標：

  | 指標 | 舊(translate 全關) | 新(full_translate_en) | Δ |
  |---|---|---|---|
  | context_recall | 0.630 | 0.652 | +0.022 |
  | context_precision | 0.639 | 0.698 | **+0.059** |
  | nv_context_relevance | 0.819 | 0.881 | **+0.062** |
  | faithfulness | 0.908 | 0.914 | +0.006 |
  | answer_relevancy | 0.799 | 0.824 | +0.025 |
  | answer_correctness | 0.440 | 0.458 | +0.018 |

  逐題核對原始四個診斷題（不只看 overall，實際比對數字才算數）：news-08 correctness 0.17→0.58／
  recall 0.4→1.0（大幅救回，符合診斷）；col-15 correctness 0.165→0.273／recall 0.0→0.6（明顯改善）；
  sem-15 correctness 0.175→0.283 但 recall 0.875→0.75（**下降**，混合訊號，未完全解釋）；**news-13
  correctness/recall 皆持平（recall 仍 0.0）**——但病因已換層：確認 planner 這輪正確生成全部 7 家子問題、
  `NVDA_News` chunk 也確實進了 sources，可是撈到的具體 chunk（Q1 財報預告、SpaceX 分割）談的是別的
  角度，跟參考答案要的特定數字（JPMorgan 6/12 報告「Nvidia 當日 +2.22%」）沒對上——是 chunk 級/rerank
  對「特定數字事實」的命中問題，不是這輪修的 planner 截斷問題，殘留待查。

## ⑧ 2026-08-02：生成後處理（單位換算）+ 忠實度稽核升級 + 逐題驗證

承生產 `rag_query.py` 同日新增的 Rule 10-13 生成契約與 `convert_usd_units_to_yi()`（見
[`CHANGELOG.md`](CHANGELOG.md) 2026-08-02），本節記 agentic 端的接入與驗證。**未動檢索/filter/模型/
agentic 節點結構**——純生成層讀法紀律 + 後處理。核心洞察：`answer_correctness` 長期偏低是**兩件事疊加**
——(1) RAGAS claim-F1 措辭天花板（改系統無用）、(2) 少數「生成讀壞」真 bug（被 run 噪音藏住）；本輪處理 (2)。

**接入 synthesize**
- synthesize 節點（reflect 之後、freshness 之前）接入 `rq.convert_usd_units_to_yi()`：Writer 依 Rule 11
  原樣保留 `$X billion`，再由純程式乘算成正確億。動機——billion→億 是 LLM 翻譯層 token 習慣，reflect
  （同 gpt-oss-120b）**共用同盲點**靠不住，故用零誤報的程式層根治；prompt「叫它×10」實測 news-09 兩跑
  一對一全錯，改「保留 verbatim + 程式換算」後 news-09 三跑全對。
- **忠實度稽核 `_REFLECT_PROMPT` 升級**：模糊「找幻覺」→ 結構化**逐 claim 蘊含 + 雙向**——【捏造/矛盾】
  數字/單位/方向/主體/期間對不上或牴觸來源；【假性查無（反向）】答案說「未提及/未揭露」但來源其實有。
  護欄：判事實不判措辭、billion↔億 換算 / FY↔TTM 口徑差不算錯（避免誤殺）。同模型 gpt-oss-120b、溫度 0
  ——**不換模型**（gpt-oss-20b 比 generator 弱不適合；Gemini 是唯一合格異模型，留必要時才上）。

**逐題 smoke 驗證（snapshot mode，對 gold）**

| 測點 | 病 | 結果 |
|---|---|---|
| news-09 | 單位 | ✅ end-to-end 六數字全正確億（847.5/347.5/400/100/1,850/915 億美元） |
| mi-14 | 方向 | ✅ 舊「improved」→ 新「下降 17.2→16.8」，反轉消失 |
| mix-02 | 符號 | ✅ 舊「OEM 上升 2%」→ 新「fell -2%」 |
| mh-10 | 漏答 | ✅ 舊「net income not disclosed」→ 新 `$60.46B`（=gold） |
| mi-12 | 漏答 | ✅ Rule 13 **正確不捏造**（該 chunk 真的沒 SpaceX 拋售；病灶是檢索，見下） |
| col-14 | 幻覺 | ✅ reflect 移除捏造的「GPU vesting 條件」 |
| mi-10 | 幻覺 | ✅ reflect 移除捏造的「15 個產品全用 Gemini」 |
| col-03 | 非幻覺 | ✅ reflect **正確不 flag**——原文真有 `$15B OpenAI Series C`，系統對且比 gold 完整 |
| mi-02 | 非誤 | ✅ reflect **正確不 flag**——74.9%(Q1) vs 71.1%(TTM) 口徑差，系統已標 |

**語意稽核淨評**：0 誤殺、移除 2 個真 embellishment、正確保留 2 個 grounded 事實 → 有判別力、非橡皮圖章。

**順帶揭穿的高估**：col-03/mi-02/mi-10 為 false-negative gold 或口徑差，先前 bincorr「~26 真 error」
清單被高估——真實正確率高於 0.645。再次坐實鐵律：**判系統幻覺前先讀原文**（見記憶 `eval-false-negative-gold`）。

**尚未處理（本輪範圍外）**
1. **檢索側缺口**（非生成規則能修）：mi-12 跨公司市場新聞（hedge-fund/SpaceX 只在 TSLA/NVDA/AAPL/MSFT 的
   news 檔、`META_News` 沒有）→ Meta-scoped 檢索撈不到；「最新一季」未 deterministic 路由（mix-06/col-13
   撈到舊季度）。
2. 單管線 SSE 串流（`api_server.py`）未接單位換算（token 串流 post-hoc 較麻煩；Rule 11 verbatim billion
   本身可讀且正確）。
3. **全量 eval（agentic 100 + RAGAS）尚未重跑**——拿真實 before/after；須連 gold artifact 一起看才準。
4. col-03 gold 補完（加 `AMZN_10Q_202603.html` 到 relevant 重生成）— 選配 eval hygiene。

## ⑨ 2026-08-03：P2 過度拆解修正 + multi_hop 依賴解析（新能力）+ 首份 100 題全量基準 + 比較題 gold 修正

本輪動兩處 `agentic_rag_v2.py`（planner prompt、execute/replan 節點）＋ eval 側 gold。**未動檢索模型/filter/collection**。

**(1) P2 過度拆解 — `_PLANNER_PROMPT`**
- 新增【檢索標的檢驗】：子問題必須指向「向量庫撈得到的離散事實」；只要求「解讀/影響/意義/背景/綜合看法/透露訊息/反映策略」者**沒有自己的檢索標的**，併進對應事實子問題不得獨立成刀；泛化收束句（「市場綜合看法」）原問題沒點名 → 當杜撰刪掉;同一新聞事件多面向（同投行「調升目標價」＋「調升評等」）合成一問。
- 收緊【精簡優先】：典型「財報數字＋新聞事件」複合題就是 **2 個**子問題。
- 驗證：重跑 15 題 multi_intent 拆解，6 題病灶收斂（mi-03 4→2、mi-06/11/12/13 3→2、mi-14 轉合法多期比較），無合法雙意圖被壓成 1。RAGAS payoff：precision +0.038、answer_relevancy +0.043 如假設；mi-12 faithfulness 0.31→0.556 回復。總分淨效果落單跑噪音帶內，需多跑取平均才能定量。

**(2) multi_hop 依賴解析（新能力，Type B「識別再查」）**
- 病：mh-06~10 第二跳帶未解代名詞（「該公司」），planner 無法在規劃時填實體。
- 解（**確定性、無 LLM 改寫**）：新 helper `_is_dependent_hop`（`_BACKREF_RE` 偵測代名詞且無具體 ticker）／`_resolve_hop_entity`（從 hop-1 已 done 結果的 ticker aliases ＋ committed-chunk owner 解出公司）／`_fill_dependent_hop`（回填代名詞）。`_node_execute` **分波**：一波先跑無依賴 pending（含 hop-1），依賴型留到下一波再回填實體檢索。`_node_replan` 兩道 guard：未跑的依賴型第二跳不准被 drop、不准因 hop-1 done 就判 sufficient。
- 為何不採 Gemini 建議的 Plan-and-Execute/subgraph 重寫：codebase 已是 LangGraph，確定性分波＝輕量 Structured State Routing 已夠；N-hop 泛化 YAGNI（eval 全 2-hop）；LLM 改寫第二跳會引入幻覺，確定性回填不會。
- 驗證（10 題）：零拒答；Type B 實體解析 **5/5 命中 gold**（mh-06→AAPL、07→AMZN、08→GOOGL、09→MSFT、10→META）；smoke trace 確認 wave-1 只跑 hop-1、replan 不誤收斂、wave-2 回填後檢索。Type A（mh-01~05 比較→查詢）本就正確且不受影響——那是「哪家 X 最高？該公司 Y 多少」的**兩指標**題，6 子問題（3 家×2 指標）是必要非過度拆（易被子問題數量誤判，須讀答案）。

**(3) 首份 100 題全量 eval + RAGAS（現行題庫，P2＋multi_hop 齊備）**
- `experiments/agentic/gj_v2_full100_20260803.json`（結果）、`ragas_full100_20260803.json`（六指標）。零拒答。
- multi_hop 類（gold 修正後）：**faithfulness 0.904、recall 0.892、correctness 0.710**，全 eval 最紮實一類——驗證確定性分波/回填無幻覺的設計。
- 其他類別「指標偏低」診斷：低分幾乎全在 **answer_correctness**，而 answer_relevancy 0.80~0.93／nv_context 0.87~1.0／faithfulness 0.79~0.84 皆高；corr↔ansrel 落差 lexical 0.36／mixed 0.34／semantic 0.32＝長度假象量化證據（系統精簡正解 vs reference 冗長多報）。實查 lex-09/col-10 數字與 ref 完全相同且正確，連 faith=0.000 都是「370.1億 vs $37.01 billion」單位格式的假幻覺。真系統缺陷極少：**news-08 確認真漏召**（語意 query「AI 資安合作」沒撈出字面遠的 Mythos chunk，mh-09 因帶「Mythos」字面就撈到）——屬檢索召回精修，待另立題目。

**(4) 比較題 gold 修正（eval 側，承 ⑧ 第②批同型）**
- mh-01/02/03 原 `relevant` 只給贏家單檔 → `gen_reference_answers.py` 生的 reference 殘廢寫「未提供其他家數據無法比較」，RAGAS 拿壞 gt 評系統的完整比較答案 → correctness/recall 被冤。
- 修：`eval/eval_set.json` 補齊全 N 家 Fundamentals（mh-01 加 MSFT/GOOGL/META、mh-02 加 NVDA/MSFT、mh-03 加 META/GOOGL）→ 重生 `eval/reference_answers.json`（現為完整比較）→ 重跑 mh-01~05 splice 回 `ragas_full100_20260803.json`。
- payoff：**corr mh-01 0.62→0.93、mh-02 0.60→0.89；recall mh-02/03 0.67→1.0**；multi_hop 類 recall 0.825→0.892、corr 0.664→0.710、faith 0.887→0.904。
- ⚠ **`context_precision` 對多 chunk 比較答案不穩**：同一份 rec=1.0/faith=1.0/corr=0.89 的正確答案 precision 在 0/0.333/1.0 間亂跳（mh-02 修後反 1.0→0.0、mh-04 re-roll 0.0→1.0），是 RAGAS LLMContextPrecisionWithReference 判定器 bug，**非 gold/系統可修，比較題別信 precision 欄**。

**100 題 OVERALL（本輪定案基準）**：context_recall 0.668／context_precision 0.820／nv_context_relevance 0.940／faithfulness 0.789／answer_relevancy 0.823／answer_correctness 0.585。

## ⑩ 2026-08-03：生成雙寫單位 + Q1 reference 範圍對齊 + eval 完整性稽核 + 全繁中對齊

承 ⑨ 同日，處理兩個 RAGAS 量尺假象（Q1 長度、Q2 單位盲），並做全 100 題 eval gold 稽核，最後把系統與 gold 統一成全繁中。原則貫穿：**不為討好壞掉的判官弄爛產品——改考卷或讓答案同時服務兩邊，不把好答案改爛。**

**(Q2) 單位盲假幻覺 → `rq.convert_usd_units_to_yi` 改雙寫**
- 病：系統把 `$37.01 billion` 換成「370.1 億」後，RAGAS faithfulness 判官（不會算單位換算）在 context 找不到「370.1」→ 把答對的打 faith=0（lex-09/col-10）。
- 修：改「**370.1 億美元（$37.01 billion）**」雙寫——億給中文使用者、verbatim `$X billion` 留括號供判官比對＋人工稽核。驗證：lex-09 faith 0.000→**1.000**。
- 單位機制補到滴水不漏：英文 billion/million/trillion/bn/mn + **中文 十億/百萬/兆** + **`$`前綴 B/M/T 縮寫**（源用 `$37.01B` 格式）全部歸「X 億美元（原文）」；`\b`→`(?![A-Za-z])`（CJK 相容）；無 `$` 裸字母不誤傷。

**(Q1) 長度覆蓋假象 → reference 範圍對齊**
- 病：`answer_correctness` 各類 0.48~0.65 但 answer_relevancy 0.80~0.93，落差 0.32~0.36＝長度假象（系統精簡正解 vs reference 冗長多報毛利/流動比）。
- 修：`gen_reference_answers.py` 的 `REFERENCE_SYSTEM` 從「涵蓋 chunk 每個事實」改「**對齊題目範圍**——問什麼答什麼、不多報題目沒問的」。不動系統（灌水答案會傷 relevancy/faithfulness）。

**eval 完整性稽核（100 題逐題進 chunk 對證，SUSPECT 32→5→9 全非真錯）**
- **版本漂移 11 題**：gold 指標指向 period-basis 去重刪掉的 `_0508/_0519` 舊快照；數值穩（0612 存活檔一致），重生自癒。
- **季度題選錯 chunk（20 題，最實質）**：10-Q 損益/分部表數字密集、dense 分數輸給訴訟/敘述段，`gen_reference` 漏選含答案的表格（mix-08 選到 Meta 訴訟段、營收其實在 #164）。逐檔定位正確分部表 chunk（NVDA Data Center $75,246 在 #0、AMZN International $39,789 在 #12…），`eval_set.json` 加 `pin_chunks`、`gen_reference` 加 pin 機制（釘選必含、dense 補足）。reference 數值本就對、只是 gold 沒收錄。
- **reference billion→億 換算錯**：中文 reference 的 `$84.75 billion` 被 LLM 音譯成「84.75 億」（少 10x）。修：prompt 要 LLM 保留 `$X billion` verbatim + 用 source 精確數字（非 query 的「850多億」措辭）+ `gen_reference` 生成後套 `convert_usd_units_to_yi`。mh-08「$850 billion」10x 錯：全繁中重生自然消除（中文「850多億」=$85B 正確）。
- 殘留 SUSPECT 全非錯：計算 delta（mix-08 營收增額 $13,997M＝56,311−42,314）、「860 thousand」文字型、已知 col-03 false-negative、query 回音「850多億」。

**全繁中對齊（治本，非只動 gold）**
- 發現系統本身語言不一致：59 題中文、**41 題整段英文**（financial 數據傾向英文答，CJK 占比 0~0.2%）。使用者要 gold 全中文 → 真正落差是系統該全中文。
- 修：`agentic_rag_v2._write_final_answer` 加 `_ZH_ANSWER_DIRECTIVE`（agentic-scoped，不動共用 `rq.SYSTEM_PROMPT`／單管線）強制繁中作答、保留 Rule 11 單位。重跑 100 題→**100/100 中文、0 拒答、0 殘留未轉單位**。gold 對齊此結果重生→**100/100 繁中**。
- **全繁中最終基準**（系統與 gold 同語言，`ragas_zh_full100_20260803.json`）：recall 0.699／precision 0.798／nv_context 0.922／faithfulness 0.814／answer_relevancy 0.822／answer_correctness 0.591。mixed correctness 0.478→**0.589**（季度 pin 修的可歸因改善）。multi_hop precision 0.434 是 RAGAS 比較題判定器不穩（非系統/gold 錯）。
- reference 版本備份鏈：`reference_answers.backup_20260803`（原始）→`pre_v3_backup`→`v3_backup`→現行 v4（全繁中）。

## 知識庫已搬出本檔（2026-08-11 重整）

本檔**只放日期式變更記錄**。原「附錄：知識庫」A0~A6 已依主題搬到：

| 內容 | 現在在哪 |
|---|---|
| 工作慣例（全域規則） | [`CLAUDE.md`](CLAUDE.md) |
| Eval 量尺、噪音、量測基礎建設、方法論教訓 | [`docs/EVAL.md`](docs/EVAL.md) |
| 檢索層決定、架構診斷、一致性 validator、期間接地 | [`docs/AGENTIC.md`](docs/AGENTIC.md) |
| 切塊層診斷（mix-03 定案、通用小標、head 重建驗收） | [`docs/INGEST.md`](docs/INGEST.md) |
| 未決 / 下一步 | [`BACKLOG.md`](BACKLOG.md) |

## A4 本 session 三個 commit（2026-08-06~07；正式變更記錄，補齊 changelog）
- **`5fbad55` footer eval 修正**：見 A1 footer 條。faith 0.763→0.815。
- **`784e4a2` ratio 財務錨源保底**：消 Grader 擲硬幣（A3-A）。`_is_ratio_intent`＋`_ensure_ratio_source_coverage`（doc_type 版 `rq._ensure_ticker_coverage`，只加不減、純確定性零 LLM）：ratio 意圖 commit 後缺該公司 Fundamentals 就從池補回。**用程式碼保底非改 Grader prompt/換大模型**（規則可 metadata 確定檢查，不交機率元件每次重推）。multi_intent recall 0.524→0.621(+0.098)、precision +0.024。已知限制：mi-14 分部題略離題（後由 Plan A 救回）。
- **`5300b4c` Planner 比率期間中性**：修 Plan 把「目前毛利率」釘成「FY2026 Q2 毛利率」→帶季度 query 把 TTM/Fundamentals 整個濾出池、答錯口徑。【時間消歧】加「比率指標期間中性」例外（毛利率/淨利率/成長率等相對詞問且未點名某季→保持期間中性、不釘季度）。multi_intent precision 0.714→**0.934(+0.22)**、recall 持平、mi-13 口徑修回 TTM 19.1%（=gold）。
- **probe（2026-08-07，唯讀，未改碼）：不擴白名單到估值比率（P/E/ROE）**：機制 B 在單一來源估值題**不端到端復現**——檢索半確有 ticker-dependent 傷害（Apple「FY2026 Q2 本益比」→Fundamentals 完全离池；NVDA 同 query 不离池，rank0），但規劃半 **Planner 對 P/E 自然保持期間中性、3/3 不釘季度**，觸發不成立。∴ margin/成長率 scoping 收窄在「有實測失效」邊界是對的，別為不存在的 bug 加規則；backstop regex 同樣不擴。

