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

## A5 未決 / 下一步
- **router（複雜度分派，pending，measure-gated）**：簡單題→單次 RAG、複雜→agentic；保守偏 agentic（誤判複雜為簡單代價高）。ratio 路由屬**正交檢索提示、非第三分支**，應放共用檢索層讓兩 pipeline 都吃到。
- 生產 `rag_query.py` 是否跟進 `full_translate_en=True` 未決（需先確認同樣 chunk-level rerank 平坦問題存在）。
- FY 雙向硬 filter、「最新年度」as-of 錨定未動（要先跑全量 eval 防回歸）。
- 工作樹尚有 19 行既存未提交改動（env top-k＋Planner 反注入 prompt），非本 session 作者，待處理。
