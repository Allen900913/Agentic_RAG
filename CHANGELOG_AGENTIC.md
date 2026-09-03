# CHANGELOG_AGENTIC — agentic 管線的早期演進（已封存）

> ⚠ **這份是歷史記錄。** 它描述的模組（`agentic_rag.py` deepagents 版、`agentic_rag_nv.py`、`agentic_rag_mix`）**都已不在 repo 裡**，現役入口只有 [`agentic_rag_v2.py`](agentic_rag_v2.py)。
>
> **要看現況**：節點設計與 validator → [`docs/AGENTIC.md`](docs/AGENTIC.md)；2026-08-08 之後的變更 → [`CHANGELOG.md`](CHANGELOG.md)；量測 → [`docs/EVAL.md`](docs/EVAL.md)。
>
> **保留本檔的理由**：它記載了**四次架構轉向各自被什麼證據推翻**。那條線比任何單一結論都有用——每一次「更 agentic」的嘗試都是負資產，而推翻它的都是自己寫的確定性測試。

**貫穿哲學**：**只有需要決策的角色才給 agency；不需要決策的角色給 agency 就是給它犯錯空間。**

---

## 架構演進：v1 deepagents → LangGraph

### ① 初版：deepagents 雙層 agent

主 agent 拆子問題 → 委派 `rag-researcher` subagent → 合成；subagent 自己看 rerank gate 決定要不要升級檢索。

**取捨**：**累積池**（去重聯集重排，重搜只加不洗）取代信任 context；gate 複用既有 rerank 分數，不另建評分模型。
**模型坑（已試無效）**：llama-3.3-70b 對大 schema 吐格式錯 tool-call、gpt-oss-20b parse 失敗 → 定案 brain=`gpt-oss-120b`。

### ② 混合版：檢索用 agency、生成由 Python 強制接管

**發現弱腦會跳過 `generate_answer`、直接用背景知識作答**——prompt 是建議不是約束。

**定案**：Scout 只挑 chunk、Writer 由 Python 主導單次生成——**把「生成」抽出 agent 迴圈，結構性消除「跳過生成／幻覺／弄丟 citation」三類病灶**。三層保證：確定性 citation validator（regex ＋ allowlist）、coverage validator、機械式引用附錄。

> ⚠ **踩坑**：citation regex 只認 ASCII `[]`，中文輸出的全形 `【】` 全 false-negative。**這個坑後來又踩過一次**（web 引用的 `【web:】`）。

### ③ 崩潰降級 ＋ 一個根因級的水管 bug

- **graph 崩潰降級保證（恆開）**：agent 迴圈崩了就沿用已檢索池走 Writer 收尾，最壞退化成單發 baseline，**不再零產出**。
- **`_pool_key` 碎裂 bug（根因級）**：key 用了完整 `checkpoint_ns`（含 per-step task id）→ 同 subagent 每次工具呼叫各開新池，「**累積池」在真實 graph 從未生效**。
  → **改判**：先前「弱腦不挑 chunk」是冤案——**真兇是水管**；修 key 後 scout 其實挑得動。
  > **教訓：繞過 brain 直接測 ≠ 真實 graph。**

### ④ Provider：Groq → NVIDIA

- **argv cp950 損毀教訓**：中文 query 經 Bash/PowerShell 損毀 → 壓低語意匹配，**我基於損毀 trace 下的三個結論全被乾淨 query 推翻**。定案：非 ASCII 一律 PowerShell ＋ `$env:PYTHONUTF8=1`。
- rewrite 升級對「已是英文的 query」是結構性 no-op → 槓桿改成「針對缺口換一句明顯不同的 query」。
- NVIDIA 的 tool-call 可靠度／延遲全面優於 Groq，且無 TPM 天花板結構瓶頸。

### ⑤ LangGraph 全面重寫（現行架構的起點）

**重寫成顯式 state machine**（`plan →(retrieve→check→[rewrite|advance])* → generate→reflect`），**只有 Sufficiency Checker 做判斷**，其餘純函式單次呼叫——消除 deepagents 動態迴圈的所有「稅」。

**P0 五修**（code review 全屬實）：跨子問題 rerank 不可比 → round-robin 公平選；`bool("false")==True`；CJK snippet 失效 → 2-gram；不足判定被丟棄 → `unmet` 欄位 ＋ 收尾揭露；補救輪二次 rewrite 稀釋 → `enable_rewrite=False`。

**`translate_query_en` 三次反覆**（誤植 escalation-only → 常開 → 確定性消融定案）：
> **教訓：不能拿「生成→RAGAS LLM judge」論斷檢索層因果**；確定性消融（比對 gold、同 judge 背靠背）才承重。

### ⑥ planner prompt 通用化 ＋ 檢索中間層全英文

**planner 三原則（不寫死清單）**：意圖保全（口語／隱含意圖不可丟）／**集合詞 fallback**（>上限或不確定成員 → 保留群體詞當單一寬鬆 query，**不猜殘缺清單**）／時間消歧。

⚠ **逐題診斷推翻了兩個歸因**：某題的真兇是 **`MAX_SUBQUERIES` cap 截斷**（不是 prompt 措辭——A/B 證明舊 prompt 配新 cap 就對）；另一題根本不是 planner 的病。

**`full_translate_en` 架構定案**：中文問中文答，但 dense/sparse 召回 ＋ rerank 一律用英文譯句，消除 cross-lingual rerank 對「中文 query × 英文 chunk」評分平坦。
⚠ **它推翻了先前「dense 召回用英文會傷 sem-11」的保留結論**——舊消融**只量已飽和的 file-level、沒量 chunk 內排序**。六指標全面上升，漲幅最大的兩項正是診斷對應的檢索層指標（precision +0.059、nv_relevance +0.062）。
⚠ 但**逐題核對才算數**：四個診斷題裡一題大幅救回、一題明顯改善、一題**混合訊號**（correctness 升但 recall 降）、一題**持平但病因已換層**（planner 這輪正確生成全部子問題，撈到的 chunk 談的是別的角度）。

### ⑦ 生成後處理：單位換算與忠實度稽核

**核心洞察**：`answer_correctness` 長期偏低是**兩件事疊加**——(1) RAGAS claim-F1 的措辭天花板（改系統無用）(2) 少數「生成讀壞」的真 bug（被 run 噪音藏住）。

- **billion→億 用程式換算，不用 prompt**：那是 LLM 翻譯層的 token 習慣，reflect（同模型）**共用同盲點靠不住**。實測 prompt「叫它 ×10」兩跑一對一全錯，改成「保留 verbatim ＋ 程式換算」後三跑全對。
- **後來又改成雙寫**「370.1 億美元（$37.01 billion）」：因為 RAGAS faithfulness 判官**不會算單位換算**，在 context 找不到「370.1」就把答對的打 faith=0（實測 0.000 → **1.000**）。
  > 原則：**不為討好壞掉的判官弄爛產品**——改考卷，或讓答案同時服務兩邊，不把好答案改爛。
- **忠實度稽核 `_REFLECT_PROMPT` 升級**：模糊「找幻覺」→ 結構化**逐 claim 蘊含 ＋ 雙向**（【捏造/矛盾】＋【假性查無】：答案說「未提及」但來源其實有）。護欄：**判事實不判措辭**，billion↔億 換算、FY↔TTM 口徑差不算錯。
- **驗證結果**：0 誤殺、移除 2 個真 embellishment、正確保留 2 個 grounded 事實 → **有判別力、非橡皮圖章**。
  ⚠ **順帶揭穿一個高估**：先前「~26 個真 error」的清單裡有多筆是 false-negative gold 或口徑差。**再次坐實：判系統幻覺前先讀原文。**

### ⑧ multi_hop 依賴解析（新能力，全程無 LLM 改寫）

**病**：第二跳帶未解代名詞（「該公司」），planner 無法在規劃時填實體。
**解（確定性）**：`_is_dependent_hop`／`_resolve_hop_entity`（從 hop-1 已 done 結果的 ticker aliases ＋ committed-chunk owner 解出公司）／`_fill_dependent_hop`。execute **分波**：一波先跑無依賴的，依賴型留到下一波再回填。replan 兩道 guard：未跑的依賴型第二跳不准被 drop、不准因 hop-1 done 就判 sufficient。

**為何不採 Plan-and-Execute／subgraph 重寫**：確定性分波已夠；N-hop 泛化 YAGNI（eval 全 2-hop）；**LLM 改寫第二跳會引入幻覺，確定性回填不會**。

**驗證**：零拒答、實體解析 **5/5 命中**；multi_hop 是全 eval 最紮實的一類。
⚠ **Type A 比較題的 6 子問題（3 家×2 指標）是必要非過度拆**——**子問題數量會誤導，必須讀答案**。

### ⑨ 過度拆解修正

新增【檢索標的檢驗】：子問題必須指向「向量庫撈得到的離散事實」。只要求「解讀／影響／意義／綜合看法」者**沒有自己的檢索標的**，併進對應的事實子問題；泛化收束句（原問題沒點名的「市場綜合看法」）當杜撰刪掉。
驗證：6 題病灶收斂，**無合法雙意圖被壓成 1**。

### ⑩ 全繁中對齊（治本，非只動 gold）

發現**系統本身語言不一致**：100 題裡 41 題整段英文（財務數據傾向英文答）。使用者要 gold 全中文 → **真正的落差是系統該全中文**。
修法是 agentic-scoped 的 `_ZH_ANSWER_DIRECTIVE`（**不動共用的 `rq.SYSTEM_PROMPT`／單管線**）。重跑 → 100/100 中文、0 拒答。

---

## eval 側的同期修正（都在 gold 不在系統）

- **比較題 gold 殘廢**：`relevant` 只給贏家單檔 → 生成的 reference 寫「未提供其他家數據無法比較」，**RAGAS 拿壞 gt 評系統的完整比較答案**。補齊全 N 家之後 correctness 0.62→0.93。
- **季度題選錯 chunk（20 題，最實質）**：10-Q 的損益／分部表數字密集，dense 分數輸給訴訟／敘述段，生成腳本漏選含答案的表格。修法是 `eval_set.json` 加 `pin_chunks`（釘選必含、dense 補足）。**reference 數值本就對，只是 gold 沒收錄。**
- **版本漂移 11 題**：gold 指向已被去重刪掉的舊快照；數值穩定，重生自癒。
- **`context_precision` 對多 chunk 比較題判定器不穩**：同一份正確答案 precision 在 0/0.333/1.0 間亂跳。**非 gold／系統可修，比較題別信 precision 欄。**

---

## 知識庫已搬出本檔

| 內容 | 現在在哪 |
|---|---|
| 工作慣例、全域規則、檔案地圖 | [`CLAUDE.md`](CLAUDE.md) |
| Eval 量尺、噪音、量測基礎建設、方法論教訓 | [`docs/EVAL.md`](docs/EVAL.md) |
| 檢索層決定、架構診斷、validator、期間接地 | [`docs/AGENTIC.md`](docs/AGENTIC.md) |
| 切塊層診斷 | [`docs/INGEST.md`](docs/INGEST.md) |
| 未決／下一步 | [`BACKLOG.md`](BACKLOG.md) |
