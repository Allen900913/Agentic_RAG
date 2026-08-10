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

## 反覆出現的方法論教訓
繞過 brain 測 ≠ 真實 graph／先排除工具環境毛病再懷疑系統／單次 LLM-judge 不能論因果、要確定性消融／
別過度外推（file-level 無效 ≠ chunk-level 無效、edgar_exp4 無效 ≠ 全面無用）／prompt 是機率不是保證，
grounding/citation 要靠 Python 強制不靠拜託模型。

---

# 附錄：知識庫（2026-08-07 由 `~/.claude` 跨 session memory 併入）

> 原為隨環境搬遷、不隨 repo 走的工作記憶；環境遷移前落檔保存。只保留「**結論＋機制＋關鍵數字**」，過程細節在對話與 git 歷史。與上方 ①–⑩ 重疊處只補充、不複述。**判系統好壞前先讀 A1**——很多「低分」是量尺 bug，不是系統病。

## A0 工作慣例
- **回覆語言＝繁體中文**（使用者 2026-07-11「以後也是」）：對話全繁中；程式碼 docstring/comment 繁中、identifier/log 英文。⚠ 此為行為偏好，**真正該放的是 [`CLAUDE.md`](CLAUDE.md)**（會被自動載入，changelog 不會）——記在此僅為遷移保存，若要它持續生效請補進 CLAUDE.md。

## A1 Eval 量尺與方法論（最大宗；不懂這層會把量尺 bug 當系統缺陷追）
- **`answer_correctness` 是長度假象、對 retrieval 免疫**：factual-F1(0.75)＋語意(0.25)，主要由「系統答案 vs 固定 reference 的詳略對齊」決定。lex-07 答「$7.46」完全正確只拿 0.40（reference 塞 6 事實，F1 recall=1/6）。**別當 chunking/系統成敗閘門**，改用 recall/precision/faithfulness。修正：類別級「長度單調」在逐題級站不住（Pearson≈−0.10）——低分尾巴真因是①檢索失敗②false-negative gold。語言錯配假說也已 A/B 推翻（EN 0.475 vs ZH 0.494，沒升反微跌）。
- **false-negative gold（五批，全 eval 側非系統）**：reference 誤寫「來源未揭露」但語料白紙黑字有，系統答對甚至比 gold 準，被打 recall=0。兩機制：(a) `eval_set.relevant`/gold_files 範圍太窄漏標 10-Q/10-K；(b) `gen_reference_answers.py` 檔內選錯 chunk 行/錯期別。修法全在 eval 側（補 relevant、`pin_chunks`、gen_reference 改英文譯句 dense＋`GOLD_TOP_K` 5→8）。特例：mi-04 是 FY vs TTM 真口徑歧義（非乾淨 false-negative），硬加 10-K 反雙降→已 revert。**鐵律：判系統幻覺前先 grep 語料驗事實在不在；「系統 vs gold 差 10x」先讀原文 billion/億 對一次（gold 也會犯單位錯，如 mi-11 誤寫 $380B→$38B）；反向也要驗（grep 抓不到 ≠ 語料沒有，數字常在 MD&A 敘述段）。**
- **gold 全量稽核（2026-08-07，13 題 23 處錯，全部已修）**：上一條的單位鐵律先前只修過 mi-11 單題，**從未全掃**；這次把 100 題 reference 逐一對回它自己的 gold chunk，結果：
  - **機械層 100% 乾淨**：relevant glob 0 個落空、`pin_chunks` 50 個在 exp4 全解析、reference↔eval_set 的 qid/query/gold_files 逐字對齊、無 stale cache、無空答案。→ **量尺壞掉的地方全在語意層，機械檢查給不出警訊**。
  - **10 倍單位錯 6 題 14 處**（mi-02/07/09/11/13、mh-07）：病灶單一——來源 `Fundamentals`/`IncomeStatement`/新聞寫 `$253.49B`，reference 直接抄成「253.49 億美元」（正解 2,534.9 億）。**對照組證明這不是隨機**：來源是 10-K/10-Q `(In millions)` 表格時換算全對（mix-10/11/14、col-12 共 15 處 0 錯）——**錯的只有「B 結尾」那一種字面**。
  - **句內自我矛盾 2 題**：mix-06 開頭「11.1 億」vs 內文「111.44 億（$11,144 百萬）」；mix-14 開頭「42.19 億」vs 內文「4.219 億」。→ 同一句話裡對的錯的並存，抽樣看必漏。
  - **期間錯置 2 題**：mix-03 把 10-Q 的**九個月累計** `$20.4B/22%` 當成單季（單季實為 `$6.4B/20%`，同章節分屬 `Three Months Ended`／`Nine Months Ended` 兩段）；mix-06/mix-14 把 6/30 那季標成「第三季」（曆年制＝Q2）。
  - **抄錯數字 1 題**：mix-11 `$75,246M` 寫成 751.46 億（應 752.46）。
  - **false-negative gold 1 題**：sem-05 寫「文件中未提及與 Anthropic 的具體合作細節」，但 `AMZN_10K_2025.html` 有 6 個 chunk 詳載（$5.3B 可轉債、轉換 $2.3B 重分類利得、$7.2B 上調、2025 年底特別股 $14.8B／可轉債公允價值 $45.8B、2026 年將再認列 $12B）。答對的系統會被判錯。
  - **衝擊面**：**multi_intent 15 題裡 5 題**（mi-02/07/09/11/13）帶 10x 錯，全部集中在「Fundamentals 數值」子意圖——這正是 A3「multi_intent recall 殘因」一路追的題類。**先前 multi_intent 的 answer_correctness 有一部分是被錯 gold 壓的，不是系統答錯**；那批分數需重跑才有效。
  - **根因＝防護「有印警告但不修」**：生成端其實早有兩道（prompt CRITICAL 規則禁寫「億」＋ `gen_reference_clean` 重試 2 次），2026-08-03 `3417ad0` 就在，gold 是 08-06 生成的——**防護當時在，照樣漏**。因為重試耗盡後只 `print("⚠ 重試後仍含手轉「億」，需人工檢查")` 就放行，100 題輸出裡那行捲過去，沒有人檢查。`rq.convert_usd_units_to_yi` 也救不了：它只認「數字+英文單位詞」，LLM 一旦先斬後奏寫成「253.49 億美元」，該函式明文「不碰已經是億的值」→ 結構性失明（覆蓋率：agentic 答案 186 雙寫:21 solo＝90%；gold 29:50＝**37%**，同一支轉換器，差在 Writer 交出的原料）。
  - **修法＝勸不動就用程式修**（`repair_yi_against_source`，[`eval/gen_reference_answers.py`](eval/gen_reference_answers.py)）：拿該題 context 逐筆判定，三條件同時成立才動手——①來源有 `$N billion` ②來源沒有 `$N/10 billion` ③來源沒有 `$N*100 million`（②③排掉「答案其實對、來源另有數字長得像」）。**回測**（套在修正前的 `.bak`）：14/14 全抓、0 危險誤報；套在修正後檔案殘留 0。判不出的一律不改，但改印出清單留痕。
  - **寫 guard 時自己踩的兩個坑**（都被回測抓出來）：①幣別標記寫成**可選**→「10 億**使用者**」「25 億部裝置」被當金額，來源剛好有 `$10 billion` 就會改成「100 億美元（$10 billion）使用者」。**漏修無害、改壞有害，一律從嚴要求 `美元/歐元`**。②「後接括號就當已雙寫而跳過」太粗→ mi-09「84.75 億美元（其中…」是敘述性括號，被誤跳過漏修；改成只認「括號內以數字/`$` 開頭」。
  - **`mix-06` 順帶校正到來源揭露值**：10-Q MD&A 明文 `Google Cloud revenues increased $11.1 billion ... or 82%`，故 gold 用 **111 億美元（$11.1 billion）**，而非從分部表自行相減的 111.44 億（24,768−13,624）。**系統當時答的就是 111 億＝對的，被舊 gold 的 11.1 億扣分。**
  - **同一種病在生成腳本裡共四處**（全是「偵測到問題→印一行警告→照樣放行」）：①`pin_chunks` 解析失敗靜默退回 dense-only ②手轉「億」重試耗盡只印「需人工檢查」③**快取只比對 `query`、不比對 `gold_files`**——語料換版後 glob 展開到不同檔案（SEC 新申報讓 `MSFT_10K_*.html` 從 2025 版變 2026 版），題目一字未改就照樣 `cached, skip`，正是 `eval-gold-version-drift-bug` 的形狀，2026-07-21 的修法只堵了 query 漂移這半邊 ④**`lang` 預設 English**——忘帶 `--match-lang-from` 就無聲產出英文 gold（2026-08-07 實測踩到：重生成 news-01/mi-13 兩題都變英文），而 register 落差正是壓垮 answer_correctness 的已知主因。③④ 已修：快取改雙條件並印新舊 gold diff；語言改「`--match-lang-from` > 既有 `reference_lang` > English」。實測偽造語料換版後正確報 STALE 並保持繁中。
  - **`eval/reference_answers.json` 先前被 `*.json` 一併 gitignore**（白名單只列了 `eval_set*.json`）——它是 RAGAS 的 ground truth，跟 eval_set 同級的「標準答案」，且不是腳本能免費重現的東西（要一次 LLM 全量生成，且含 24 處人工校正）。本專案已發生過未追蹤 eval 檔被刪、git 完全救不回的事故。已加 `!reference_answers.json`。
  - **eval/ 目錄清理**：6 個過期產物（`generation_judge*.json` 三支、`generation_judge_multiintent_new.json`、`ragas_vs_rubric.json`、以及**沒有任何程式讀取**的 `answer_shape_labels.json`）移入 `eval/_archive_stale/`（沿用 `data/raw/_archive_stale/` 慣例，未硬刪——它們未納版控，刪掉即永久遺失）。清理後 `eval/` 只剩 `eval_set.json`／`eval_set_agentic.json`／`reference_answers.json` 三個版控中的輸入／標準答案。⚠ 兩支腳本的 `--output` 預設仍寫回 `eval/`（`eval/generation_judge.json`、`eval/ragas_vs_rubric.json`），下次跑會再把輸出混進來。
  - **`eval_ragas_vs_rubric.py` 加 `--ids`**：RAGAS 每題獨立評分，只改少數題 reference 時可只重跑那幾題再拼接回原結果，避免整份重跑多吃一輪 judge 噪音。
  - **方法論**：判準要**自足**。「reference 的 N 億 vs 來源的 $N billion」單看會大量誤報（來源同時有 `$2.5B` 與 `$250 million` 就會撞），有效判準是 ①雙寫括號內外自洽（`N 億美元（$M billion）`→ N==M×10，全庫 0 錯）②沒雙寫的才逐筆查來源字面單位。稽核腳本自身的 regex 也會騙人：`(?![\w])` 抓不到 `$60.46B` 的尾隨 `B`，害 lex-03/lex-14/mh-01/mh-10 全被誤標「數字無出處」，逐筆看才發現都是對的。
- **footer 毒害 faithfulness**：agentic 答案尾端「📚 引用來源…rerank=0.730」footer 是 metadata、context 裡不存在→RAGAS faithfulness 逐 claim 判 unsupported，短答案佔比高被砸最重（解釋 lexical 悖論：檢索近乎完美但 faith 全類最低）。已於 [`eval/eval_ragas_vs_rubric.py`](eval/eval_ragas_vs_rubric.py) `load_answers` 加 `strip_citation_footer()`。全 100 重跑 faith 0.763→**0.815**、lexical 0.672→**0.889**。
- **RAGAS `context_precision` 對多 chunk 比較題判定器不穩**：同一份 rec=1.0/faith=1.0/corr=0.89 正確答案，precision 在 0/0.333/1.0 亂跳（re-roll 就變）。**比較題（multi_hop）別信 precision 欄**，非 gold/系統可修。
- **gold 版本漂移**：`eval_set.json` 精確檔名鎖 10-Q 版本、新聞用 glob；語料刷新加了 202606 新季報但 gold 沒同步→撈到最新的系統被判 0、撈到舊的反得高分。修：6 題 gold 202603→202606；`_meta` 應宣告 collection＋snapshot cutoff。**改 `eval_set.json` 用 `open('wb')` 保 LF，別讓 CRLF 假爆 diff。**
- **eval 改造三定案**：①答案風格＝白話結論先行＋精確數字，SYSTEM_PROMPT 與 reference register 必須一致（否則 correctness 因 register 落差崩）；②RAGAS 跑獨立 `.venv-ragas`（ragas 0.2.x 綁 langchain 0.3，會扯壞生產 `.venv` 的 langchain 1.x／SemanticChunker）；③multi_intent GT **不降級**（不改 news-only 粉飾、不加「含財報字就不觸發 news filter」guard）。
- **eval 改版拆分（2026-07-29，90→100 題）**：multi_intent 收斂為嚴格「1 財務指標＋1 新聞事實」平行雙意圖（15 題）；新增 multi_hop 10 題（hop-2 key 待 hop-1 才成形）＝5 題比較→屬性(A)＋5 題新聞→實體→財務(B)。戰略：**multi_intent 本不該 agentic 贏（平行、單管線拆解即可），multi_hop 才是 plan→execute→replan 正當戰場**。

## A2 檢索層決定（多在 `rag_query.py`，agentic 複用）
- **`full_translate_en`（架構定案，2026-07-27）**：中文問中文答，但 dense/sparse 召回＋rerank 一律用英文譯句，消除 cross-lingual reranker 對「中文 query × 英文 chunk」評分平坦（news-08 正解 chunk 中文 rerank≈0 壓到 rank6，英文後 rank2 跨過門檻）。全 90 題 RAGAS 六項全升，precision/nv_relevance 漲最多(+0.059/+0.062)。agentic `_retrieve_chunks` 固定開；**生產 `rag_query.py` 預設仍 False，未套用（跟進與否未決）**。
- **`translate_query_en`（舊 flag，只影響 rerank）**：`max(原句分,英譯分)` 重評、不動召回池，生產單次查詢常態預設 True（曾兩次誤植成 escalation-only，勿再犯）；agentic 主路徑已被 full_translate_en 取代。
- **`period_basis` TTM 口徑消歧義（2026-07-30）**：Fundamentals.txt 已含預算好的 TTM 比率（毛利率/淨利率/成長率/P/E）＝**路由問題非算術**，不該讓 LLM 現算。每 chunk 加 `period_basis` payload（fundamentals→TTM、10-K/Q→fiscal_year）；`rag_query._detect_period_basis` 命中 TTM 詞→`period_basis=TTM` **單向**硬 filter（fiscal_year 有標籤但不硬路由，避免回歸；開雙向前先跑全量 eval）。[`migrate_add_period_basis.py`](migrate_add_period_basis.py)。SYSTEM_PROMPT Rule 10：財務數字必帶口徑標籤、相對詞多口徑→兩個都給並標。同日去重 0508/0519 舊快照。
- **gap2 `mentioned_tickers`（2026-08-02）**：市場級新聞物理歸檔某一家但內文提及全七家，owner-ticker 硬 filter 把 gold 全排除（即使 rerank 全場最高）。news chunk 加 `mentioned_tickers` 陣列 payload、retrieve 改「陣列包含 X」；**只動 news，財報維持 owner ticker 單值**（財報提到對手 ≠ 報表是對方的）。multi_intent recall 0.593→0.638、faith +0.073、corr +0.061、**precision −0.135**（七公司 chunk 內生代價＋單跑噪音）。淨賺收下。
- **col-07 否定框架檢索限制（已知限制，決定不修）**：query「非市佔龍頭」框架 vs 語料「minority market share」框架，BGE dense+sparse+reranker 跨不過否定/框架鴻溝，連正確英譯也跨不過（只有已知答案字面＝HyDE 才撈到 rank1）。不為單題上 HyDE（全管線改、有反傷）。gold 亦同源標錯已修（24/103/110）。
- **EDGAR chunking Exp0→4**：管線越換越乾淨（edgartools Item 邊界＋RCTS 消 oversized/截斷），Faithfulness 單調升到 Exp4 新高；Answer Correctness 單調降是長度假象非真退步。生產＝Exp4（`us_stock_rag_edgar_exp4`）。

## A3 Agentic 架構診斷主線（v2 價值＝確定性 planner 拆解；最 agentic 的自由迴圈是負資產）
- **multi_intent 退步三階診斷（逐階自我推翻）**：① 檔案層級 probe：v2 檢索**不弱反大勝**（final8 覆蓋 28/38 gold 檔 vs 單管線 12/38，嚴格超集）→推翻「多源檢索弱」。② claim/chunk 層級 probe：真實 v2 只 16/75 gold chunk、**乾淨 decomposition（無 Agent A）拿 35/75**、單管線 25/75→**煙槍：漏水點在 Agent A(ReAct) 執行層亂改 query＋亂搜，非 planner 拆解、非 doc-type filter**。③ 修法＝v2 minus ReAct（executor 改 deterministic：plan→每子問題直接 retrieve→commit）＝**v2 現狀**。
- **multi_intent recall 殘因（2026-08-06）**：真因兩機制——(A) **Grader 對雙來源擲硬幣**：毛利率/淨利率/成長率等可從 10-K/10-Q 現算的指標，池中 Fundamentals(TTM 寫死) 與 10-Q(可現算) 近似平手，Grader `relevant_ids` 一次只圈 1 個→擲硬幣，eval 那次圈中 10-Q（季度口徑 vs gold TTM）。**關鍵洞見：只有雙來源可計算指標會低 recall＝指標型別非題型**（單一來源 P/E/ROE/市值沒得挑錯故 recall 高）。(B) Plan 期間解讀分歧＋開放式子意圖答太薄。「gold 灌水」假設查證後**撤銷**（系統答太窄非 gold 太寬，動 gold＝gaming eval，違反不降級 GT，不做）。
- **multihop 依賴解析（2026-08-03，新能力）**：Type B「識別再查」第二跳帶未解代名詞→`_is_dependent_hop`/`_resolve_hop_entity`/`_fill_dependent_hop`，`_node_execute` 分波延後＋實體回填、`_node_replan` 兩道 guard，**全程無 LLM 改寫**（確定性回填不幻覺）。10 題零拒答、實體解析 5/5 命中；100 題 multi_hop faith **0.904**/recall **0.892**/corr **0.710** 全類最紮實。Type A 比較題 6 子問題（3家×2指標）是必要非過度拆。不走 Gemini 的 Plan-and-Execute 重寫（YAGNI，eval 全 2-hop）。
- **corrected verdict（2026-07-28，此語料下）**：結構化 txt＋長文 filing 混合＋中型模型下，動態編排自由度是負資產；生產維持單管線，agentic 真正舞台是多跳/開放 web/真路由工具。

## A4 本 session 三個 commit（2026-08-06~07；正式變更記錄，補齊 changelog）
- **`5fbad55` footer eval 修正**：見 A1 footer 條。faith 0.763→0.815。
- **`784e4a2` ratio 財務錨源保底**：消 Grader 擲硬幣（A3-A）。`_is_ratio_intent`＋`_ensure_ratio_source_coverage`（doc_type 版 `rq._ensure_ticker_coverage`，只加不減、純確定性零 LLM）：ratio 意圖 commit 後缺該公司 Fundamentals 就從池補回。**用程式碼保底非改 Grader prompt/換大模型**（規則可 metadata 確定檢查，不交機率元件每次重推）。multi_intent recall 0.524→0.621(+0.098)、precision +0.024。已知限制：mi-14 分部題略離題（後由 Plan A 救回）。
- **`5300b4c` Planner 比率期間中性**：修 Plan 把「目前毛利率」釘成「FY2026 Q2 毛利率」→帶季度 query 把 TTM/Fundamentals 整個濾出池、答錯口徑。【時間消歧】加「比率指標期間中性」例外（毛利率/淨利率/成長率等相對詞問且未點名某季→保持期間中性、不釘季度）。multi_intent precision 0.714→**0.934(+0.22)**、recall 持平、mi-13 口徑修回 TTM 19.1%（=gold）。
- **probe（2026-08-07，唯讀，未改碼）：不擴白名單到估值比率（P/E/ROE）**：機制 B 在單一來源估值題**不端到端復現**——檢索半確有 ticker-dependent 傷害（Apple「FY2026 Q2 本益比」→Fundamentals 完全离池；NVDA 同 query 不离池，rank0），但規劃半 **Planner 對 P/E 自然保持期間中性、3/3 不釘季度**，觸發不成立。∴ margin/成長率 scoping 收窄在「有實測失效」邊界是對的，別為不存在的 bug 加規則；backstop regex 同樣不擴。

## A5 2026-08-07 雙臂 100 題對照 + 一致性 validator（新增確定性層）

**實驗設定**：同 collection（`us_stock_rag_edgar_exp4`）、同 `full_translate_en=ON`（只控制編排這一個變因）、同 gold（當日修完的 `reference_answers.json`，MD5 `ae6b165a03ba`）。結果檔 `experiments/agentic/gj_{single_ft,v2}_full100_20260807.json` → `ragas_{single_ft,v2_ft}_full100_20260807.json`。判讀一律套 **0.067 噪音底線**。

**結論（OVERALL 單管線/agentic）**：ctx_prec .790/**.864**、nv_ctx .873/**.950** 是真勝；ctx_rec .740/.748、**answer_correctness .628/.637 不可分辨**。分類看：**multi_intent 全面翻盤**（recall +.217、nv_ctx +.400、ans_rel **+.397**、corr +.095）、**multi_hop 檢索三項全過門檻**（prec +.250 最大）；代價是 **mixed/semantic 的 recall 退步**（−.123／−.094）——agentic 平均只收 3.31 chunk / 6.6k 字（單管線 4.94 / 10.7k），38 題 ≤2 chunk。∴ **ctx_prec 的漲幅有一部分是機械性的**（收得少必然精準），別當純品質提升讀。

**faithfulness −0.068 查證為量尺偏誤，非幻覺**（詳見 memory `faithfulness-penalizes-completeness`）：
- 4 個 agentic faith=0.000 逐題查證全是判官誤判或拒答退化（mix-01 的「40%」在 context 逐字、lex-08 的 `$37.01B` 逐字、col-07 是拒答）。剔除後 .801 vs .839＝**−0.038 < 噪音底線**。
- **拒答紅利**：mh-07 單管線寫「reference materials do not provide…」只剩 1 個聲明→**1.000**；agentic 兩跳都答對（`$716,924` 確實在 context 的 10-K 損益表）→**0.333**。
- **雙寫稀釋**：agentic 寫 472 個數字 vs 單管線 302；逐字命中 56% vs 79%、「換算後對得上」39% vs 17%。判官逐聲明查**字面**支持，算術正確的換算形字面不在來源 → 判 unsupported。
- 長度中介假說**被推翻**（單管線短答 .910 > 長答 .811）；footer 剝除機制沒壞（100/100 命中）。

**真錯誤只有 2 個，且都不是算術**（推翻「LLM 減法弱、該給 calculator」的直覺）：
- **mix-06**：把 `Six Months Ended 2025` 欄（25,884）當 Q2 2026 → 25,884−13,624=12,260、÷13,624=90.0%，**減法除法全對**，錯在運算元的期間欄。正解 24,768−13,624=11,144 → $11.1B／82%。
- **mix-03**：把 Intelligent Cloud **單一部門**的 +$2.7B/24% 當全公司。它自己列的三部門增幅 3,594+2,658+146=**6,398≈$6.4B**、÷32,000=**20.0%**＝正解——**運算元全在它自己的答案裡、全正確，只是選錯要報哪個**。
- ∴ calculator/code-interpreter 對兩題都零效果：mix-06 會忠實回傳同樣的錯誤答案，mix-03 根本沒觸發運算（它以為在引述）。**病灶在算術之前的「範圍/期間」選擇。**

**新增：確定性一致性 validator**（`find_numeric_conflicts` / `_consistency_check_and_fix`，接在 citation validator 之後、reflect 之前）
- 抓「同一主體同一指標、並陳兩組互斥數值卻不調和」。純 regex 零 LLM，只有觸發才花一次重生成。
- **刻意少報**：必須有模型自己端出第二組數字的措辭（`_ALSO_MARK`：另一段落／文件亦指出…）才判衝突。無此條件時「整體 vs 部門/產品線」的合法並列會大量誤殺——離線實測 4 個觸發只有 1 個是真的（mi-01 總營收 vs 資料中心、mix-02 部門 vs Windows&Devices、news-03 公司 vs iPhone 週期皆誤報）。**靜默選錯運算元（答案裡只有一組數字）確定性抓不到**，是已知盲區。
- **離線量測（先量再接，未動管線就先跑過 200 份存檔答案）**：agentic 2/100（mix-03、mix-06）、單管線 1/100（mix-02，搜尋廣告 +10%/$1.0億 vs +9%/$304M 未調和）——**3/3 全真陽性、零誤報**。單管線也中，證明此病灶非 agentic 專屬。
- **修復路徑端到端驗證**：把兩份存檔壞答案灌回修復路徑 → mix-06 改出 **$11.1B／82%**、mix-03 改出 **$6.4B／20%**，皆與 gold 一致，修復後再偵測 0 組衝突。
  > 註：live 重跑 mix-06 當場產出的是**正確**答案（LLM 隨機性，該輪無衝突、validator 未觸發）。∴ 修復路徑是用存檔壞答案定向驗證的，不是靠 live 重現。
- **「怎麼知道兩個數字在講同一件事」——它不知道，只是無法證明不同**（2026-08-08 追問後補測）：③④ 原本都是免責式比較（`a and b and a != b`，**兩邊都有標記才排除**），所以從未證明共指。真正扛住正確率的是 ②：`_ALSO_MARK` 抓的不是我對數字的語意判斷，而是**生成器自己宣告**「這是同一個指標的另一種說法」——把不可解的共指問題換成可讀的表面標記。①「指標詞相同」很粗糙（「資料中心營收」裡也有「營收」），分不出總體與分部；mi-01／mix-02／news-03 三個「整體 vs 部分」誤報全靠 ② 才沒被殺。
  - **crafted 探測找到兩個真實破口**：③ 依賴寫死的 `_CONSIST_ENTITIES`，分部名不在清單裡（如「伺服器產品」）→ `ent` 抽成空 → 放行 → 誤報；④ 期間只有單邊標記時同樣失效。
  - **修法（實測驗證後才改）**：③ 改成**對稱**比較（`a["ent"] != b["ent"]` 即排除，含兩邊皆空）——擋掉「伺服器產品」誤報，且 **3/3 真陽性全保留**（mix-06 兩句 ent 相同、mix-03 命中那組兩句 ent 皆空）。④ **維持非對稱**：mix-06 是 `period=q vs None`，改對稱就漏抓。
  - **殘餘風險（未測）**：誤報時模型被告知「有衝突」，是否可能反而刪掉本來正確的數字？重寫 prompt 已明寫「若兩組都正確必須各自標出期間或範圍」允許保留兩者，但此路徑未做定向測試。
- 附帶發現：生成端偶爾吐 **U+FFFD 壞字**（mix-06 的「營收」變「�收」），第一版偵測器因此漏抓 → `_consist_metric_in` 已容忍指標詞任一字被替換。

### A5.1 2026-08-08 改架構：LLM 抽取 + Python 判斷（取代純 regex 判定）

**動機（使用者提的，正確）**：「為什麼不先讓 verifier 抽出所有數字以及代表什麼指標，再用程式碼去判斷？」——原本的 regex 版把分界線畫錯了。「確定性 > 勸 LLM」講的是**不要把判斷交給 LLM**，不是不要用 LLM 做抽取；本管線 Plan=LLM／Execute=確定性就是同一個分工。**抽取是感知任務（LLM 強），比對是邏輯任務（Python 可靠）。**

**新結構**：`_extract_claims`（CHECKER_MODEL，temperature=0）把答案每個數值宣稱抽成 `value/unit/metric/entity/scope/period/kind/basis/quote` → `find_claim_conflicts` 跑零 LLM 規則。（初版保留 regex 為降級路徑，**2026-08-08 已移除**，見 A5.3。）

**換來 regex 結構上做不到的 R2（分部加總 vs 合併總計）**：mix-03 的輸出從「有兩個數字對不上」升級成指出正解——「回報的全公司增幅 2700 比它自己列的最大單一部門增幅 3594 還小，且與各分部加總 **6398** 對不上」。R2 判準刻意收窄成「回報的合併值比它自己列的某個分部還小」，才不會誤殺「只列兩三個部門當佐證」的合法寫法（對照組實測：正解 6400 不觸發、分部只列 2 個不觸發）。

**第一版規則太天真，量測直接打臉**：agentic 觸發 8 題只有 ~2 個站得住（regex 版是 2/2）。誤報全是抽取器的**系統性樣態**，修在規則層而不是回去勸抽取器——因為抽取本身沒錯（把「$330 提升至 $365」看成兩個 level 是正確的，錯的是我拿 level 去比）：
- **ⓐ `level` 不參與比對**（只比 change／growth_pct）：「從 X 增至 Y」抽出的兩個 level 幾乎都被填同一個 period（mi-08 637,959→716,924、mix-12 391億→752億）。
- **ⓑ `basis` 進分組 key**：YoY 92% vs QoQ 21%（mix-11）、reported 33% vs 固定匯率 29%（mix-08）是合法並列。
- **ⓒ 共用同一段 quote 者不比**：區間「EPS 增加 1 至 3 美元」(news-10) 被拆成兩筆。
- **ⓓ 百分比加總 ≈100 → 佔比不是矛盾**：sem-09 的 Reality Labs 支出 70%/30%。
- **ⓔ 同格出現 3 個以上相異值 → 列舉不是矛盾**：mix-08 一句 `Regional data ... (+29%, +39%, +40%)` 是四個地區，entity 全被填成 Meta。真正的「同一個量兩種互斥讀法」是二選一。

**最終量測（200 份存檔答案，claims 抽一次存檔後離線重評）**：

| | agentic | single | 精準度 |
|---|---|---|---|
| regex 版 | mix-03, mix-06 | mix-02 | 3/3 |
| **LLM 版（新規則）** | mix-03, mix-06 | mix-02, **col-11** | **4/4** |

**col-11 是 LLM 版獨有的真陽性、regex 版漏抓**：單管線答案自己寫「Microsoft 365 商業雲端收入…增長 **18%**【chunk #109】；**另一份報告則顯示**同一期間增長 **19%**【chunk #105】」，四個指標各有兩個互斥值全未調和。regex 版漏抓的原因就是 `_ALSO_MARK` 清單裡沒有「另一份報告則顯示」——**這正是硬編碼措辭清單的必然破口**。

**兩個量測方法教訓**：
1. 第一輪腳本用 `if claims` 判斷成敗，**空 list `[]` 也是 falsy** → 27/29 題合法的空抽取（敘述型答案本來就沒有可比較數值）被誤報成「解析失敗」。實際解析失敗率是 **0**。
2. 第一輪只存觸發數量沒存 claims，導致每改一次規則就要重跑 200 次 LLM（約 20 分鐘）。第二輪改成**抽取一次存檔、規則離線重評**，後續三輪規則迭代成本歸零。

**成本**：每份答案固定多一次 CHECKER_MODEL 呼叫（中位 5.1s，平均抽出 4.0 筆宣稱／題）；原 regex 版是零成本、只有觸發才付費。

### A5.2 2026-08-08 In-Place Healing 提案的分析（結論：定位可行、裁決無解，病灶在更上游）

**提案**（使用者提出）：不要把整段丟給 LLM 重寫，改用程式碼/輕量 NER 精準定位出錯的那一句，只抽換、抹除或標記那一個數值，周圍不動。動機是 A5.1 末尾發現的 col-11 反例——重寫把「內部矛盾」換成了「有自信的錯標」。

**拆成兩個能力來看：定位、裁決。**

**① 定位：可行，而且不需要 NER**——`_extract_claims` 已經回傳 `quote`。拿 591 條存檔 claim 量：

| | agentic | single |
|---|---|---|
| quote 逐字命中答案 | 34.3% | 52.3% |
| **正規化空白/全形後命中** | **85.1%** | **88.1%** |
| 抽取器改寫原文（定位不可能） | 14.9% | 11.9% |

逐字只有 34% 是假象：51 個百分點純粹是 ` `（窄不斷行空格）與 markdown `**` 的差異。剩下 12~15% 是抽取器真的改寫（`'Nvidia (NVDA)…上漲約 +2.22%'` 的 `…` 是它自己縮的），但比對不上就 fallback，可偵測。**再上一個 NER 去重推已經有的東西，只是多一層失敗面。**

**② 裁決：提案沒有處理，而 col-11 的病灶正好落在這裡。** 把 col-11 的衝突印出來是四組，不是一組：

```
Microsoft 365 商業雲端  18% / 19%      LinkedIn      11% / 12%
Microsoft 365 消費者雲端 29% / 33%      Dynamics 365  20% / 22%
```

回 Qdrant 對原文：`#98`／`#105` 是 19/33/12/22（**無期間標頭**），`#109` 是 18/29/20（`Nine Months Ended`）。**兩組數字都是對的**——19% 是單季、18% 是前九個月。

這直接打穿提案的操作集 `{抽換, 抹除, 標記}`：**抽換**沒有東西可以換進去、**抹除**會刪掉正確資訊、**標記**是唯一活下來的但那等於承認不確定而非修復。真正缺的是**期間限定詞**，那是要**插入**的文字，而且要知道哪個數字配哪個限定詞——必須回去讀原文。In-Place Healing 只是把「重寫整段」換成「重寫一句」，裁決能力一點都沒增加。

**③ 真正的發現在 ingest 層**：`#105`/`#98` 沒有期間標頭，是切塊時把 `Three Months Ended … Compared with …` 這行標頭切到別的 chunk 去了。**生成器不是讀錯，是資訊不在 context 裡。** → 修法與量測見 CLAUDE.md「期間章節邊界」小節（已實作於 `data_update_edgar.py`，**需重建 collection 才生效**）。與 mix-06 的「四欄表分不清哪欄」同一族病：**期間標籤在切塊時脫落**。

**「太多 Python/regex 是否正確」**（使用者同時問的）：判準不是用得多不多，是**該子任務有沒有唯一正確答案**。抽取＝開放感知→LLM；比對＝封閉邏輯→Python；定位＝給定 quote 的封閉問題→Python；**裁決＝要回讀原文→LLM，而這層現在不存在**。按這個表，Python 沒有太多，是少了一層。真正用錯地方的是 regex 降級路徑（見 A5.3）。

### A5.3 2026-08-08 移除 regex 降級路徑

刪除 `find_numeric_conflicts` 與 `_CONSIST_*` 詞表（含 `_consist_metric_in`／`_consist_money`／`_consist_period`／`_consist_close`）、env `AGENTIC_CONSISTENCY_LLM`。`_consistency_check_and_fix` 現在抽取失敗就跳過這層。

**兩個理由**：
1. **它在用字串比對做感知**——指標靠寫死詞表（「資料中心營收」裡也有「營收」）、主體靠寫死的公司/分部清單（不在清單就抽成空）、並陳靠寫死的措辭清單。每加一家公司、每換一種說法就要改表。而且已經被實測抓包：A5.1 的 col-11 漏抓，原因就是「另一份報告則顯示」不在措辭清單裡。
2. **它從來沒跑過**——觸發條件是「LLM 抽取失敗」，200 份答案的離線量測失敗率是 **0**。等於一段沒被測過、卻要永久維護的死碼。跳過檢查不會比降級偵測差（本來就沒檢查）。

**回歸驗證**：拿同一份 `claims_all.json` 離線重評，`find_claim_conflicts` 觸發結果與刪除前逐題相同（agentic: mix-03×3, mix-06×1；single: mix-02×3, col-11×4）。無衝突答案原樣回傳的路徑亦 smoke 過。

### A5.4 2026-08-08 期間接地：讓一致性 validator 看得到原文（三個改動，踩過兩個錯的設計）

**起點是使用者的質疑**：col-11 的病灶既然是「chunk 讀不出期間」，那**抽取層的欄位是不是也該更精準**？驗證後成立一半——`_extract_claims` 的 user content 只有 `f"答案:\n{answer}"`，**它看不到任何 chunk，所以無法糾正答案的錯，只能忠實複製**。而 col-11 的答案自己把單季 19% 寫成「九個月期間」，抽取層照抄 → 兩筆同期 → 必然誤報。

**改動**：① `_extract_claims` 加 `chunks` 參數並餵原文 ② 期間判準從自由文字 `period` 換成 `period_key = f"{period_months}M@{period_end}"` ③ 新增零 LLM 的 `_ground_period_from_source`（拿數字回 chunk 定位期間段並覆寫）。

**受控實驗**（答案文字固定＝重現 col-11 錯誤樣態，只換餵進去的原文）：

| 組別 | 誤報組數（3 輪） |
|---|---|
| ① 不餵原文 | 1 / 0 / 1 |
| ② 餵舊 collection 原文（無期間標籤） | 2 / 1 / 2 ← **比不餵更糟** |
| ③ 餵新 collection 原文（有標籤） | 0 / 0 / 0 |
| ④ ③的同一批 chunks，程式剝掉注入標籤 | 2 / 1 / 0 |

③ vs ④ 是乾淨對照——**同一批 chunks，唯一差別是那行 `[Three Months Ended…]` 在不在**，剝掉就退回 ② 的水準。故效果來自標籤，不是新 collection 檢索變好。② 更糟的原因：原文殘留的章節標題讓抽取器把四筆全套上同一期。**兩層缺一不可，且 ingest 必須先做**。

**中途走錯一次，記下來**：加了「舉證稽核」——要求 LLM 填 `period_evidence`，填不出原文出處就把期間降級成 `unknown`。誤報測從 3/6 降到 **0/8**，看起來成功。但補跑真衝突偵測是 **0/5**：捏造的數字必然在原文查不到 → 必然降級 → R1 永遠抓不到幻覺。**誤報歸零是因為整層被關掉了**。正確順序是「先偵測、再用原文駁回」，查不到出處要**保留答案自述的期間**。

**收斂版**（誤報 5/5 全對、真衝突 5/5 全中，且十輪零變異——判斷在 Python，無抽樣變異）：期間定位改用零 LLM 字串比對。實測讓 LLM 自己查證，8 輪只有 4 輪真的去查、其餘直接填 `answer` 照抄答案。

**回歸**：`claims_all.json` 200 份離線重放觸發數仍為 4/200（agentic 2、single 2），與改動前相同——`_period_key` 在兩欄任一缺時退回舊 `period` 字串，舊 schema 行為逐字保留，故既有的四道排除規則校準不受影響。端到端 smoke 兩題正常。

**副產物**：新 collection 下生成層**自己就把兩期分開陳述**了（「以九個月為基礎…18%」「以三個月為基礎…19%」），col-11 的矛盾根本沒產生，validator 不需介入。

**限制**：`_ground_period_from_source` 只處理百分比（金額寫法太多，誤配風險高過收益）；且依賴 chunk 帶標籤，對 `us_stock_rag_edgar_exp4` 這層等於不存在。擋不掉「查了但查錯」（evidence 抄了原文某個真實標題，但不是該數字所在段落）。

### A5.5 2026-08-09 量測基礎建設：先讓實驗可被相信，再談優化

這一節不是功能改動，是**把「為什麼指標優化不動」查到底**的結果。結論是：**量測的解析度比改動的效果粗一個數量級，而且量測路徑自己也在抖。**

#### (1) 三層噪音，全部大於訊號

| 層 | 實測 | 量法 |
|---|---|---|
| RAGAS judge | `context_precision` 均值移動 **0.046**（兩次獨立樣本 0.0459 / 0.0452 複現）；`context_recall` 0.002 / 0.013 | 同一份結果檔重評兩次 |
| Plan 節點 | **12/25 題（48%）子問題不同** | 同輸入、`temperature=0` 連呼叫兩次 |
| query 英譯 → 檢索 | **47/100 題 top-8 不同**（重複對 149 vs 151） | 同組態完整重跑兩次 |

`temperature=0` 不等於確定性——`gpt-oss-120b` 是 MoE，溫度只固定取樣、不固定專家路由。

**MDE（95%, n=100）**：recall 0.029 / precision 0.036 / faithfulness 0.028 / correctness 0.019 → 換算成「要幾題從 0 修到 0.5」是 **4~7 題**。而 ingest 層的改動（期間邊界 11 個 chunk、影響 2 題）**在原理上就量不出來**。這不是運氣，是設計問題。

#### (2) `llm_replay.py`：檢索前 LLM 中間產物的重放快取

接三個點：`_plan_subqueries`、`translate_query_to_english`、`_check_sufficiency`。未設 `RAG_REPLAY_CACHE` 時**完全 no-op，生產路徑不受影響**。

**key 的兩個相反設計，各有理由**：
- plan / translate 的 key **不含 system prompt** —— planner 的 prompt 內嵌隨 collection 變動的 KB Coverage Snapshot，納入 key 會讓跨 collection A/B 全部 miss，正好毀掉唯一用途。副作用：改 planner prompt 時快取不會自動失效，要手動刪檔。
- checker 的 key **含這次看到的候選 id** —— 候選變了是**合法 miss**，那正是被測改動造成的差異，用舊決策蓋掉會把訊號洗掉。

**它買到什麼、買不到什麼**：買到 A/B 兩臂共用同一份 plan；**買不到生產端穩定**（MoE 路由不確定不打算解）。代價是固定下來的是「某一次抽樣」不是「正確答案」——跟 `reference_answers.json` 同一種東西，**定版後別再重生成**。

fixture：`eval/replay_cache.json`（plan 100 / translate_en 181 / check 206）。

#### (3) 判讀護欄：讓分數無法被誤讀

`eval_ragas_vs_rubric.py` 每次跑完在 OVERALL 底下印出每個指標的**噪音門檻**與 **gold 上限**。gold 上限來自把 `reference_answers.json` 原文當成系統答案餵回去評分（n=100）：

| metric | 系統 | gold 當答案 | 判讀 |
|---|---|---|---|
| answer_correctness | 0.651 | **0.989**（85/100 滿分） | 唯一有空間的指標，距上限 0.338 |
| faithfulness | 0.820 | **0.656**（61/100 輸給系統） | **負空間**，停止追 |
| answer_relevancy | 0.823 | 0.842 | 幾乎無空間 |
| context_precision | 0.867 | 0.822 | 噪音 0.046，量不出來 |
| nv_context_relevance | 0.968 | 0.965 | 已飽和 |

faithfulness 是負空間的原因：gold 依 `gold_files` 生成，與 agentic 實際撈到的 contexts 不同源。**繼續往上推等於要求系統答得比標準答案還保守。**

**推翻的舊結論**：`recall==1.0` 的 44 題，系統 correctness 0.724、gold 0.983 —— 證據全到位仍差 0.26，**瓶頸在生成層不在檢索層**。（先前我用 0.724 論證「correctness 有天花板」是錯的，gold 拿得到 0.989。）

#### (4) 剝 inline 引用標記：留下，但理由不是分數

`strip_citation_footer` 現在同時剝句末 `【檔名, chunk #N】`（佔答案本文 **25.0% 字元**）。**實測分數反而變差**：judge 重評 28 題 correctness **-0.041**；而零 LLM 的確定性測試顯示相似度分量只變 **+0.0011**（cosine 0.9105→0.9148），**「檔名雜訊稀釋 embedding」的假說證偽**。殘差來自 0.75 權重的 statement F1，未查清。

保留是方法論選擇（引用是 metadata、reference 一個都沒有，剝掉才 like-for-like）；照「分數變低就退回」做就是在 gaming 量尺。⚠ **這使 2026-08-09 之後的數字與之前的結果檔不可比。**

#### (5) 兩個 ingest/檢索層改動改成預設關閉

| 改動 | 量測 | 處置 |
|---|---|---|
| `_strip_tabular_blocks`（壓平表格去重） | 六指標無明確勝方；核心指標 precision 的差落在噪音內 | `--strip-dup-tables`，**預設關閉** |
| `_suppress_near_duplicates`（檢索層近重複抑制） | top-8 重複對 149→139（噪音區間 149~151，訊號約噪音 5 倍，**有效但很小**）；而最終 contexts 的重複本來就只有 18 對 / 11 題，**Grader 已清掉 88%** | `RAG_SUPPRESS_NEAR_DUP=1`，**預設關閉** |

#### (6) 尚未動的最大槓桿：Grader 保留率

`_check_sufficiency` 的 `relevant_ids` 只留下 **49.3%** 的候選（58 題單一子問題、池固定 5 → 143/290），**18/58 題最後只剩 1 個 chunk**，全 100 題最終 chunk 數中位數 = **3**。而 `context_recall ↔ correctness` 相關 **0.53**（六指標最強），`precision ↔ correctness` 只有 0.14。

**這條管線每一題都在用一次 LLM 判斷丟掉一半證據，換來一個跟答對與否幾乎無關的 precision。** 這是唯一每題都作用、因此唯一有機會超過 MDE 的系統性改動。正確的消融是**把確定性去重放在 Grader 之前**（讓 5 個席位裝 5 份不同證據），而不是單純放寬保留率。

#### 方法論教訓（當天犯了三次同一個錯）

| 我的機制假設 | 推翻它的確定性證據 |
|---|---|
| 「移除冗餘剝奪 chunk 自足性」（precision -0.0205） | judge 噪音 0.046，兩次複現 |
| 「correctness 有天花板」（perfect recall 只有 0.724） | gold 當答案拿 0.989 |
| 「inline 標記稀釋 embedding」 | 確定性 cosine 差 +0.0043 → 影響 +0.0011 |

共同模式：**先看到數字，再編一個合理的機制，然後沒去驗那個機制。** 定為紀律——**任何機制宣稱都要先有一個能證偽它的確定性測試**（零 LLM、可重跑、無抽樣變異）。另外：「確定性指標」不會自動變確定，只要量測路徑上還有一次 LLM 呼叫，它就跟 RAGAS 一樣髒。

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

## A6 未決 / 下一步
- **router（複雜度分派，pending，measure-gated）**：簡單題→單次 RAG、複雜→agentic；保守偏 agentic（誤判複雜為簡單代價高）。ratio 路由屬**正交檢索提示、非第三分支**，應放共用檢索層讓兩 pipeline 都吃到。
- 生產 `rag_query.py` 是否跟進 `full_translate_en=True` 未決（需先確認同樣 chunk-level rerank 平坦問題存在）。
- FY 雙向硬 filter、「最新年度」as-of 錨定未動（要先跑全量 eval 防回歸）。
- 工作樹尚有 19 行既存未提交改動（env top-k＋Planner 反注入 prompt），非本 session 作者，待處理。
