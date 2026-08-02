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

## 反覆出現的方法論教訓
繞過 brain 測 ≠ 真實 graph／先排除工具環境毛病再懷疑系統／單次 LLM-judge 不能論因果、要確定性消融／
別過度外推（file-level 無效 ≠ chunk-level 無效、edgar_exp4 無效 ≠ 全面無用）／prompt 是機率不是保證，
grounding/citation 要靠 Python 強制不靠拜託模型。
