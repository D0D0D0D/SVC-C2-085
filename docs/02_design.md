# Template Design Specification — SVC-C2-085 放送考査・スポンサー開示レビュードラフト エージェント

> 本書は、放送基準・スポンサー開示レビューの公開設計契約を定義する。
> 以下の主要設計判断は、規則設定、裁定記録、法令上の論点提示、データ分離、出力統制を対象とする。

## Position in AgentCore Architecture

- **Agent Class**: `BroadcastStandardsReviewAgent`
- **L1 Base**: **AgentBaseGraph**（L1 直継承）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不使用 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` でノード登録）

**Cat 判定**: Cat 2 — 放送考査という特定ユースケースを達成する複数ステップのドメイン特化パイプライン。
汎用技術能力単体（Cat 1）ではなく、エージェントが次アクションを自己決定する自律ループ（Cat 3）でもない。

---

## 適用共通部品一覧（parts library）

本テンプレは `wamuroCommonparts` の ready 部品を**全面適用**する。各部品は既存テンプレで発生した
実バグ・レビュー指摘を織り込み済みであり、写経により同種の欠陥の再発を防ぐ。

| # | 部品 | ver | 本テンプレでの役割 | 主な適応点 |
|---|---|---|---|---|
| 1 | `cat2-graph-skeleton` | 0.1.4 | 外側 `main` スロットの `GraphNode`。内側 domain_workflow_graph の呼び出し・merge | `mode_specs`（ingest/invoke/feedback）・`propagate_hitl=**True**`（本テンプレは HITL 有効）・内側 checkpointer 分離 |
| 2 | `output-envelope` | 0.1.2 | post_process の `formatted_output` envelope 生成 | `gate_kind="draft"` 宣言・モードスコープ返却・degradation folding |
| 3 | `ingest-row-pipeline` | 0.3.1 | `/ingest` の行データ処理 | 認証後 request-scoped 構築・品質ゲート → サニタイズ → 正規化 → 書込 → `ingest_summary` |
| 4 | `ingest-file-sanitizer` | 0.1.1 | `/ingest` のファイル入力検査 | マジックバイト判定・未知形式 reject。**対応形式は csv / xlsx / docx のみ**（部品は `%PDF-` を明示的に reject する）。**PDF 台本は対象外**とし、必要なら顧客側でテキスト化してから投入する |
| 5 | `scoped-retrieval` | 0.3.0 | Stage2 の過去裁定検索 | 宣言式 scope キー・保存レコード由来 scope の hit 検証・safe projection allowlist |
| 6 | `evidence-gate` | 0.2.0 | S-3(b) 引用 ID 実在検証 | malformed evidence の fail-closed・`criteria_id` / `ruling_id` の実在照合 |
| 7 | `llm-draft-stage` | 0.2.0 | Stage3 の当てはめ判定ドラフト生成 | `config["llm"]` 注入・provider 障害の明示 error・deterministic fallback |
| 8 | `payload-store` | 0.4.0 | 台本原文の out-of-state 保管 | 宣言式 scope dict binding・digit-free ref・TTL・single-use replay 防止 |
| 9 | `entry-adapter` | 0.4.0 | FastAPI entry の operation-aware AUTH + SCOPE core | 実 operation policy・verified trust gate・**クライアントヘッダ由来 scope 禁止** |

> **部品の "Do Not Remove" 事項は本テンプレでも維持する。** 特に `scoped-retrieval` の
> scope 欠落 fail-closed、`payload-store` の unscoped `get()` 不採用、`entry-adapter` の
> クライアントヘッダ scope 禁止は、いずれも過去テンプレで Critical 指摘となった防御である。

### 🔴 既定値のままでは出力が落ちる箇所（実装で必ず上書きする）

部品の既定 mapping は本テンプレの出力フィールド名と一致しない。**既定のまま実装すると
考査論点が出力から静かに消える**ため、以下を必須の適応項目とする。

**(1) `cat2-graph-skeleton` の `mode_specs["invoke"]`**

既定は `{"result": "output", "evidence_map": ..., "cited_source_ids": ..., "retrieved_records": ...,
"missing_data_flags": ..., "requires_human_review": ..., "error_log": ...}` であり、
**`review_issues` / `disclosure_map` / `escalation_flags` / `criteria_records` を返さない**。
→ invoke の `fields` に**これら 4 つを明示追加**する（`gate_kind="draft"` は維持）。

**(2) `output-envelope` の `format_invoke()`**

既定実装は `state.get("result")` を読む。本テンプレは `result` を持たないため
**`formatted_output` が実質空になる**。
→ `format_invoke()` を override し、`review_issues` / `disclosure_map` / `escalation_flags` /
`evidence_map` / `cited_source_ids` / `retrieved_records` / `degradation_reason` を組み立てる。

**(3) `output-envelope` の `required_fields_by_mode["invoke"]`**

既定は `("mode", "gate_kind")` のみ。
→ **`("mode", "gate_kind", "review_issues", "evidence_map")` に強化**し、
論点が欠落した envelope が客先へ出ることを防ぐ。

**(4) `scoped-retrieval` の safe projection allowlist**

既定 allowlist は `ruling_id` / `criteria_id` / `criteria_version` を落とす（provenance に必須）。
→ `TOP_LEVEL_SAFE_KEYS` / `METADATA_SAFE_KEYS` に追加する。

**(5) S-3 フックの適用順序**

`post_process` では **① 可否フィールド排除 assert → ② 断定 lexicon → ③ claim→source 照合
（`evidence-gate`）→ ④ PII/機密 再スキャン** の順で実行する。
**name resolution は `post_process` に含めない** — グラフ外の応答シリアライザで行う
（`formatted_output` は checkpoint に載る State フィールドのため。§name resolution 契約を参照）。
`output-envelope` の `_extra_security_gate_output()` と併存させるため、
**envelope 整形後の `formatted_output` に対して ①〜④ を掛ける**（整形前に掛けると
envelope が後から差し込むフィールドを検査できない）。

---

## Architecture Overview

### Node Configuration（外側 5 ノード backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | framework 既定 | — | — | `InitializeNode`（既定・自動注入） |
| pre_process | 入力 envelope の検証・mode 解決・**scope 解決 fail-closed**・**初回のみ** `payload_ref` から原文解決（`validated_input` が既にあれば取得をスキップ＝retry/resume 対応）・S-2 入力ゲート（企画関係者 PII の属性表記化・`confidential_business_info` 分類・ref 化） | `user_input`（envelope: `payload_ref` / `mode` / 検証済み scope） | `validated_input`（サニタイズ済み）, `mode`, `broadcaster_id`, `review_desk_id` | `FunctionNode` |
| main | `GraphNode` — 内側 domain_workflow_graph を呼び 3 モードを分岐実行 | `validated_input`, `mode`, `broadcaster_id`, `review_desk_id` | **invoke**: `review_issues`, `criteria_records`, `retrieved_records`, `disclosure_map`, `escalation_flags`, `evidence_map`, `cited_source_ids`, `requires_human_review`, `degradation_reason`<br>**ingest**: `ingest_summary`<br>**feedback**: `feedback_result` | `DomainWorkflowGraphNode`（`cat2-graph-skeleton`） |
| post_process | S-3 出力ゲート（**可否フィールド排除の schema assert**・断定 lexicon・claim→source 照合・PII 再スキャン）＋ envelope 整形 | main の出力 | `formatted_output` | `FunctionNode` + `output-envelope` |
| finalize | **カスタム安全 finalizer** — エラー経路の唯一の出口。部分生成物を破棄し定型エラー応答のみを返す | 任意（エラー時は `status`, `error_log`） | 正常時: `formatted_output` を透過<br>エラー時: `{status, error_code, correlation_id}` のみ | `FinalizeNode` を継承し `on_finalize()` を override |

> `add_edges()` / `route()` は **override しない**（L1 既定の固定 backbone に従う）。
> 全 `FunctionNode` サブクラスは `required_trust_level` をクラス本体で**明示宣言**する
> （既定 `ANONYMOUS` の暗黙継承は不可）。本テンプレの最小要件は `VERIFIED_EXTERNAL`。

### 内側 domain_workflow_graph（`main` の GraphNode が呼ぶ）

```
                       ┌── mode: ingest ──→ [IngestNode] ─────────────────┐
START → [ModeRouter] ──┼── mode: feedback ─→ [FeedbackNode] ──────────────┼──→ END
                       └── mode: invoke ───→ [Stage1 決定論照会]           │
                                              → [Stage2 スコープ検索]      │
                                              → [Stage3 LLM 当てはめ判定]  │
                                              → [Stage4 開示/エスカレ判定] │
                                              → [HitlRefineNode] ─────────┘
```

| Stage | 種別 | 処理 | 出力 |
|---|---|---|---|
| Stage1 | **決定論** | `review_criteria` から条項テキスト照会・過去裁定 `ruling_id` / `record_id` 実在照会・広告主リスト照合 | `criteria_records`（条項の実レコード集合） |
| Stage2 | **検索** | scope 内の類似案件の過去裁定を検索（`scoped-retrieval`） | `retrieved_records`（`ruling_id` 付き） |
| Stage3 | **LLM** | 放送基準152条・薬機法・ステマ告示の**各規則への当てはめ判定＋根拠の言語化**（`llm-draft-stage`） | `review_issues`（候補論点＋根拠） |
| Stage4 | **決定論のみ** | 開示/エスカレーション判定（`review_issues` の `axis` と `requires_human_review` から規則ベースで導出）・過去裁定の紐づけ・レビューパケット生成 | `disclosure_map`, `escalation_flags`, `evidence_map` |

> **Stage4 は決定論に固定する**（LLM を呼ばない）。Stage3 で生成済みの論点に対し、
> 「どの論点をエスカレーションに上げるか」「どの過去裁定を紐づけるか」は
> `axis` / `requires_human_review` / scope 一致という**規則で決まる**ため、
> ここで 2 回目の自由生成を挟むと**根拠のない文言が provenance 検証を経ずに出力へ入り込む**。
> LLM 必須性は Stage3 に一本化する。

> **ID の呼称を統一する**: 案件は `record_id`、裁定は `ruling_id`、規則条項は `criteria_id` とし、
> `case_id` という別名は使わない（検索・引用・feedback 書込のすべてで同一名を使う）。

> **LLM 必須性は Stage3 に集約する**。Stage1/4 の決定論部分は
> レコード照会・照合であり LLM を使わない。

### Data Flow（外側）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                         pre_process
```

### State Definition（flat TypedDict・`AgentState` 継承）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` | ✅ |
| `broadcaster_id` | `str` | scope 第1階層 = **放送局・系列局**（`broadcaster_id` の実体） | ✅（欠落時 fail-closed） |
| `review_desk_id` | `str` | scope 第2階層 = **考査区分**（番組考査 / CM 考査） | ✅（欠落時 fail-closed） |
| `validated_input` | `str` | S-2 通過後の**サニタイズ済み**考査対象テキスト（PII は属性表記化・未公開企画名は置換済み。**生原文ではない**） | ✅ |
| `payload_ref` | `str \| None` | 台本原文の out-of-state 参照 ID（**原文は State に載せない**） | — |
| `criteria_records` | `list[dict]` | Stage1 の条項実レコード | — |
| `retrieved_records` | `list[dict]` | Stage2 の過去裁定。**部品契約上 `id` / `source_id` が必須**なので、`id` = `ruling_id` の値、`source_id` = 由来レコード（`record_id`）を入れる。加えて `ruling_id` / `criteria_id` / `criteria_version` / `axis` を safe projection で保持する | — |
| `review_issues` | `list[dict]` | 規則別の**確認対象論点**（可否ではない） | — |
| `disclosure_map` | `dict` | 開示要否のマッピング | — |
| `escalation_flags` | `list[dict]` | 考査責任者へのエスカレーション論点 | — |
| `evidence_map` | **`list[dict]`** | claim → source 対応表。**`evidence-gate` は `list` を要求する**（`dict` を渡すと gate が reject する）。各要素は `{claim, source_ids[], ...}` | — |
| `cited_source_ids` | `list[str]` | 引用した実レコード ID 集合 | — |
| `requires_human_review` | `bool` | **集約値** = `any(issue["requires_human_review"] for issue in review_issues)`。`cat2-graph-skeleton` の invoke `mode_specs` 既定に含まれる top-level フィールドで、クライアントが「人手確認が要るか」を 1 値で判定するために使う。**個票は `review_issues[].requires_human_review` が正**（本フィールドはその OR 集約であり、単独では論点を特定できない） | — |
| `missing_data_flags` | `list[str]` | 部品既定の invoke フィールド。KB 不足等で判定材料が欠けた項目 | — |
| `ingest_summary` | `dict` | `/ingest` の `written_ids` / `rejected[]` | — |
| `feedback_result` | `dict` | `/feedback` の確定記録結果 | — |
| `degradation_reason` | `str \| None` | 空 KB 等の縮退理由（システムエラーと区別） | — |
| `hitl_draft` | `str \| dict \| None` | HITL 再開時の LLM 再実行防止 | — |
| `hitl_feedback` | framework 型 | 考査担当者のフィードバック | — |
| `hitl_status` | `HitlStatus \| None` | approved / corrected / rejected | — |
| `formatted_output` | `dict` | post_process の envelope | — |

**State Constraints（必須）:**
- flat TypedDict のみ（primitives + JSON シリアライズ可能型）
- **JWT / API キー / 認証情報を State に置かない**（checkpoint DB への漏洩）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に置かない）。
  ノード内では `InvocationContext.from_state(state)` を使い、**直接構築しない**
- Pydantic / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- **台本原文・企画関係者の生 PII を State に置かない** — `payload_ref` による out-of-state 参照とする（PB-2 / PB-5 で assert）
- ノードのインスタンス変数に可変状態を持たない（`self._result` 等は並行実行で競合）

---

## 主要設計判断

### #1 判定規則スキーマと局固有基準 config の確定 ✅

**規則軸は 3 本に固定する**。健康増進法は薬機法系ヘルス表現の
**補助参照**に留め、独立の判定軸にしない。

**配置先**: `review_criteria` は**運用中に改定される業務ポリシー**（基準改正・局内規の追加）であり、
AgentRegistry が読む静的マニフェスト（`agent.yaml`）に置くべきものではない。
**`config/review_criteria.yaml` として独立ファイルに置く**。
`config/config.yaml` からパスを参照し、`schema_owner`（顧客考査部門）が版管理する。
`agent.yaml` には**置かない**。

`review_criteria` エントリ構造:

| フィールド | 必須 | 内容 |
|---|---|---|
| `criteria_id` | ✅ | 規則条項の一意 ID（例: `JBA-BC-STD-ART-XXX`） |
| `axis` | ✅ | `broadcast_standards` / `pmd_act` / `stealth_marketing` の 3 値のみ |
| `source_url` | ✅ | 一次ソース URL |
| `criteria_version` | ✅ | 基準バージョン |
| `effective_date` | ✅ | 施行日（**施行日処理に使用**） |
| `scope_note` | ✅ | 適用範囲 |
| `approved_by` / `approved_at` | ✅ | 承認記録 |
| `tenant_policy_id` | — | 局内規で上書きした場合の内規 ID |

- **`schema_owner` = 顧客の考査部門**。基準の追加・変更は考査部門レビューを要する運用とする
- **施行日処理**: `effective_date` が考査対象の放送予定日より後の規則は適用しない。
  改正前後で条番号が変わる場合は `criteria_version` で区別し、過去裁定の想起時は
  **その裁定が下された時点の `criteria_version`** を併記する（改正で判断が変わった論点を誤って再利用しない）
- **変更検知**: `review_criteria` の `criteria_version` / `effective_date` の変化を起動時に検出し、
  変更があった規則に紐づく過去裁定へ `criteria_stale` フラグを立てる（自動再判定はしない — 再判定は考査部門の判断事項）

### #2 スコープの事前考査（テキスト）限定 ✅

**本 AG は企画書・構成台本・スポンサー統合案という「テキスト段階の事前考査」に限定する。**

| | 対象 | 理由 |
|---|---|---|
| ✅ 含む | 番組アウトライン・構成台本・スポンサー統合案・広告読み原稿（**テキスト**） | テキストのみで当てはめ判定が完結する |
| ❌ 含まない | **完パケ試写（完成映像の考査）** | 映像解析（マルチモーダル）を要するため、**構造的に対象外とする設計判断** |
| ❌ 含まない | 音声実体の解析・配信後監査 | テキストによる事前レビューに限定する |
| ❌ 含まない | **最終考査判断・放送許可の発出** | 考査責任者の専権（S-3 第1層で構造的に排除） |

> **入力ガード**: pre_process で非テキスト（動画・音声バイナリ）を検出した場合は
> `ingest-file-sanitizer` のマジックバイト判定で reject し、`status=error` とする。
> 「テキストのみ」というスコープを**実行時にも強制**する。

### #3 ステマ告示・薬機法の放送適用ロジックの確定 ✅

**課題**: 消費者庁のステマ運用基準は SNS/Web 中心で、
放送番組タイアップ特化の記述が薄い。

**設計方針 — 断定せず「確認対象論点」として提示する:**

| 判定軸 | 放送適用の考え方 | 出力の扱い |
|---|---|---|
| ステマ告示 | 「広告主から便宜供与を受けた紹介か」「広告である旨が一般消費者に明瞭か」の 2 要件を**論点として提示**。放送特化の運用基準が薄いことを出力に明記し、**自局基準の確認を促す** | `review_issue` + `requires_human_review` |
| 薬機法 | 医薬品等適正広告基準の「医薬品的な効能効果の標榜」該当性を論点提示。健康食品表現は**補助参照として健康増進法**も併記 | `review_issue` + `requires_human_review` |
| 放送基準152条 | 条項単位で該当しうる論点を提示（例: 広告と番組の識別） | `review_issue` |

**精度検証方針:**
- 考査担当者の判定を正解ラベルとし、**論点検出の再現率（見落とし率）を主指標**とする。
  誤検出（過剰フラグ）より**見落としを重く扱う** — 本 AG は最終判断をせず、過剰フラグは人が捨てられるが、見落としは事故に直結するため
- ステマ告示は放送適用基準が薄いため、**当面 `requires_human_review` を既定で付与**し、精度が確認されるまで自動的な論点の確定を行わない
- PoC で自局基準との突き合わせを行い、局内規（`tenant_policy_id`）で判定閾値を調整可能にする

### #4 `broadcast_review_records` / `ruling_records` スキーマの確定 ✅

**2 系列スキーマ**（`/ingest` で構築・`/feedback` で成長）:

**`broadcast_review_records`（考査案件）**

| フィールド | 型 | 内容 |
|---|---|---|
| `record_id` | str | 一意 ID |
| `broadcaster_id` / `review_desk_id` | str | **scope キー（必須・行キー）** |
| `program_type` | str | 番組種別（統制語彙） |
| `sponsor_ref` | str | スポンサー参照 ID（**実名は `confidential_business_info` として別扱い**） |
| `content_summary` | str | 企画内容の要約（**台本原文は持たない**） |
| `reviewed_at` | date | 考査日 |
| `criteria_version` | str | 適用した基準バージョン |

**`ruling_records`（裁定実績＝moat の核）**

| フィールド | 型 | 内容 |
|---|---|---|
| `ruling_id` | str | 一意 ID（引用時の provenance ID） |
| `record_id` | str | 案件への参照 |
| `broadcaster_id` / `review_desk_id` | str | **scope キー（必須・部品準拠のキー名）** |
| `criteria_id` / `criteria_version` | str | どの規則条項への裁定か |
| **`axis`** | enum | `broadcast_standards` / `pmd_act` / `stealth_marketing`。**裁定行に非正規化して持つ**（下記） |
| `ruling` | enum | `approved` / `revision_instructed` / `rejected` |
| `revision_note` | str | 修正指示の内容（「〜を〜に変更のうえ承認」） |
| `rationale` | str | 裁定理由 |
| `escalated` | bool | 考査責任者へエスカレーションされたか |
| `operator_edited` | bool | `/feedback` 由来の人手確定シグナル |

**「類似案件」の判定定義**: 以下を**すべて満たす**ものを類似とする。
1. 同一 scope（`broadcaster_id` + `review_desk_id`）— **異なる局の裁定は決して混ぜない**
2. `axis` が一致（同じ規則軸の論点）
3. `program_type` が一致、または `content_summary` の意味的近傍が閾値以上

> 3 のみ（意味的近傍のみ）での想起は行わない。**規則軸が違う裁定を「似ている」として提示すると
> 誤誘導になる**ため、1・2 を必須の前置フィルタとする。

> **🔴 `axis` 絞り込みの実装方式（部品の制約に合わせる）。**
> `axis` は本来 `review_criteria`（config 側）の属性であり `criteria_id` から join しないと引けない。
> しかし `scoped-retrieval` の `RetrievalBackend.search()` の引数は
> **`broadcaster_id` / `review_desk_id` / `query` / `top_k` のみ**で、**axis を渡す口が無い**。
> `ScopedRetrievalNode` の外から per-request に axis を渡すことはできない。
>
> したがって次の 2 点を組み合わせる:
> 1. **`ruling_records` に `axis` を非正規化して保持する**（join なしで WHERE に使えるようにする）
> 2. **axis ごとに backend インスタンスを分け、`ScopedRetrievalNode` も axis ごとに用意する**
>    （backend は construction 時に自分の `axis` を束縛し、`search()` 内で
>    `WHERE broadcaster_id=? AND review_desk_id=? AND axis=<束縛値>` を必ず適用する）。
>    Stage2 は 3 軸ぶんのノードを順に呼び、結果を `axis` 付きでマージする。
>
> ⚠️ **axis を LLM のクエリ文字列から推測させない**（`query` に axis を混ぜて検索させる方式は不可）。
> 束縛値は決定論的に決まるものであり、自由文からの推測に落とすと前置フィルタの意味が失われる。
> `criteria_version` 改定で `axis` が変わることは 3 軸固定のため想定しない。

**PII 非保持設計**: 出演者・企画関係者の氏名は保持せず、**属性表記**（例: 「出演者A」「制作担当」）に
置換して格納する。原文は `payload-store` に TTL 付きで保管し State / KB には `payload_ref` のみを載せる。

#### 🔴 保持期間・削除方針（裁定記録は無期限保持）

| 対象 | 保持 |
|---|---|
| `ruling_records` / `broadcast_review_records` | **無期限保持**。期限削除・想起対象からの自動除外は**行わない** |
| `payload-store` の台本原文 | **短命 TTL・期限で削除**（下記契約。KB とは別で、原文は蓄積対象ではない） |

> **🔴 2 つの保持方針は別物であり、混同しない**。
> **裁定記録（redact 済み・ref のみ）= 無期限**、**台本原文（生の機密）= 短命で確実に消す**。
> 「KB を無期限保持する」という判断は、**原文を無期限に持つことを一切意味しない**。

#### 🔴 `payload-store` のライフサイクル契約（原文＝生の機密）

| 項目 | 内容 |
|---|---|
| **保持期間** | `payload_ttl_seconds`（`config/config.yaml`）。**既定 3600 秒（1 時間）**。**カバーすべきは「投入 → 初回 `pre_process` が原文を消費するまで」だけ**でよい（HITL 往復は原文を必要としないため、TTL が往復時間を跨ぐ必要はない）。それ以上は延ばさない |
| **HITL との整合** | **HITL の往復は原文を必要としない**（resume は checkpoint 済みの `validated_input` / `hitl_draft` から再開する）。したがって **TTL が HITL 往復より短くても問題ない** — TTL を HITL の所要時間に合わせて延ばす必要はない。原文の再閲覧が要る場合は、グラフ経路ではなく別立ての「原文閲覧フロー」（delivery gate 側・認証＋scope＋権限＋監査つき）で提供する |
| **設定オーナー** | 顧客の考査部門（`schema_owner`）。**上限値をテンプレ側で強制**し、無制限・極端に長い値を設定できないようにする |
| **アクセス境界** | `broadcaster_id` + `review_desk_id` + `session_id` に bind。**unscoped `get()` は実装しない**（部品の Do Not Remove 事項）。scope 不一致は取得不可 |
| **🔴 single-use の消費者は「初回 `pre_process` のみ」** | 取得は**1 回**（replay 防止）。**その 1 回を消費してよいのは初回の `pre_process` だけ**と定める。<br>・**retry 経路**（`route()` の `status=retry` → `pre_process` 再実行）は **原文を再取得しない** — checkpoint 済みの `validated_input`（サニタイズ済み）を再利用する。`pre_process` は `validated_input` が既に存在する場合、payload 取得をスキップする<br>・**HITL resume 経路**も同様に**再取得しない**（`hitl_draft` / `validated_input` から再開）<br>これを定めないと、単一使用と retry/resume が衝突して 2 回目で必ず失敗する |
| **期限後の動作** | TTL 経過後は **`payload_ref` が解決不能**になる（`None` ではなく明示的に失効として扱い、`status=error` で fail-closed）。**期限切れ後に原文を復元できてはならない** |
| **削除の監査** | 削除・失効の**事実のみ**を監査イベントに記録（`payload_expired` / `payload_deleted` + `payload_ref`）。**原文および原文断片は記録しない** |
| **暗号化** | 保存時暗号化はデプロイ基盤（DB/ストア）側の責務とし、**アプリ層で平文の原文を長期保持しない**ことで担保する。鍵管理は `ctx.secrets` 経由 |

**根拠**: 放送考査は「10 年前に同種の企画で揉めた」という遠い過去の裁定が効く領域であり、
蓄積型 moat は件数と履歴の長さが価値そのもの。**期限削除は moat を自ら削る**ため既定では行わない。

**改正跨ぎの扱い**: 削除の代わりに `criteria_stale` フラグで鮮度を示す。
想起時は**その裁定が下された時点の `criteria_version`** を必ず併記し、
「現行基準では判断が変わりうる」ことを考査担当者が判断できる状態にする（自動再判定はしない）。

> **留保**: 顧客のデータ保持ポリシーで期限削除が要件になる場合は、
> **削除を config で後付けする**（`retention_years` を追加し、既定は無期限＝`null`）。
> 消したデータは戻らないため、**既定を「消さない」側に置く**設計とする。

### 🔴 原文と State の分離契約（checkpoint 漏洩の防止）

`validated_input` は `GraphNode` が内側グラフへ渡す既定フィールドであり、**外側・内側の両方の
checkpoint に永続化される**。ここに台本原文を入れると「原文を State に載せない」という設計が破れる。

| 段階 | 何が載るか |
|---|---|
| entry（`src/api/server.py`） | 台本原文は**リクエストボディから直接 `payload-store` へ**。認証済み scope に bind し TTL 付きで保管 → `payload_ref` を発行 |
| `user_input` / graph 入力 envelope | **`payload_ref` + `mode` + 検証済み `broadcaster_id` / `review_desk_id` のみ**（原文なし） |
| `pre_process` | `payload_ref` で原文を解決 → **S-2 サニタイズ（PII 属性表記化・未公開企画名置換）** → その結果だけを `validated_input` に入れる |
| `validated_input` | **サニタイズ済みテキスト**。生原文ではない |
| `hitl_draft` | 上記 HITL 節の allowlist のみ |

> つまり **原文が checkpoint に入る経路を構造的に作らない**。PB-2 / PB-5 では
> 「invoke 実行後の checkpoint に生 PII・原文断片が存在しないこと」を assert する。

### #5 S-3 二重ゲート設計の確定 ✅

> **🔴 位置づけ**: この S-3 統制は **「製品安全／非権威的出力の緩和策」**
> であって、**薬機法・景表法・弁護士法72条のコンプラ遵守を確立するものではない**。
> スキーマ排除・断定語ブロックは製品の文言を制約できるが、法令適合そのものを保証しない。
> **基準の妥当性と出力ポリシーの検証は放送局の法務／コンプラ部門（`schema_owner`）が行う。**

**第1層 — 出力スキーマからの可否フィールド構造的排除**

出力スキーマに「放送可」「考査 OK」に相当するフィールドを**定義しない**。AG が出すのは
`review_issues`（確認対象論点）と根拠であって、可否の決定ではない。

```
✅ 出力に含む: review_issues[] / disclosure_map / escalation_flags / evidence_map / retrieved_records
❌ 出力に含まない（スキーマに存在しない）: broadcast_ok / review_passed / is_compliant / verdict （bool 含む）
```

→ **schema テストで「これらのキーが出力に存在しないこと」を assert する**
（`docs/03_test_spec.md` §Security Acceptance Tests **SEC-01**）。

> 本設計の各セキュリティ契約は `docs/03_test_spec.md` の **SEC-01〜SEC-14** に 1:1 で対応させてある
> （可否排除=SEC-01 / 断定 lexicon=SEC-02 / 引用検証=SEC-03,04,05 / baseline 正常系=SEC-06 /
> scope 分離=SEC-07,08 / 原文・HITL の checkpoint 非格納=SEC-09,10 / HITL 契約=SEC-11,12 /
> 入力形式=SEC-13 / エラー時非開示=SEC-14）。全件 PASS が受入条件。

**第2層 — 断定 lexicon**

NFKC 正規化 + casefold 後に断定表現を検知する（`schema_owner` = 顧客考査部門が語彙を管理）。
`legal_output_lexicon` 方式を採用する。

検知対象例: 「放送してよい」「問題ない」「薬機法上クリア」「〜で可」「対応で可」「基準を満たす」
「違反する」「抵触する」（**該当の断定も同様に遮断** — 非該当だけでなく該当 side の断定も考査責任者の専権）

→ 検知時は **raise**（S-3 は raise 規約。自動 S-4 `node_error` が発火する）。

**第3層 — claim→source 対応検証**（`evidence-gate`）

各 `review_issue` に以下を**必須フィールド**とする:

| フィールド | 必須 | 内容 |
|---|---|---|
| `issue_id` | ✅ | 論点の一意 ID（Stage4 の紐づけ・HITL 訂正の指定に使う） |
| `input_span_id` | ✅ | 台本／統合案のどの箇所に対する論点か |
| `criteria_id` | ✅ | どの規則条項か |
| `criteria_version` | ✅ | 基準バージョン |
| **`axis`** | ✅ | **`broadcast_standards` / `pmd_act` / `stealth_marketing` の 3 値 enum**。Stage4 の決定論判定と `ruling_id` の axis 一致検証に使う。**3 値以外は fail-closed** |
| **`requires_human_review`** | ✅ | `bool`。Stage3 が付与し、**Stage4 のエスカレーション導出が消費する**。ステマ告示軸は当面既定 `true`（§#3） |
| `policy_source` | ✅ | **`baseline`（公開規則そのまま）／ `tenant_override`（自局内規で上書き）の 2 値** |
| `tenant_policy_id` | 条件付き | **`policy_source=tenant_override` のときのみ必須**。`baseline` のときは `null` |
| `ruling_id` | — | 過去裁定がある場合のみ（任意） |

> **`axis` と `requires_human_review` は Stage4 の入力契約**である。欠落や不正値（3 値以外・非 bool）は
> **fail-closed**（Stage4 が黙って空のエスカレーション一覧を出す事故を防ぐ）。

> **🔴 `tenant_policy_id` を無条件必須にしない。** `review_criteria` 側で `tenant_policy_id` は
> 任意（局内規で上書きした場合のみ持つ）である。これを `review_issue` 側で無条件必須にすると、
> **局内規による上書きが無い素の公開規則（放送基準の大半）では有効な論点を 1 件も生成できず、
> 全件 fail-closed になる**。`policy_source` で由来を明示し、`baseline` を正当な状態として扱う。

- **規則 ID なしの法的主張は fail-closed**
- `requires_human_review` は**補助的位置づけ**に留め、根拠欠落時の既定動作は fail-closed とする

> **🔴 `evidence-gate` が実際にやること／やらないこと（過大主張しない）**
>
> | 検証 | 担い手 |
> |---|---|
> | 引用 ID が Stage1/2 の実レコード集合に**実在するか**（捏造 ID 検出） | ✅ `evidence-gate`（部品） |
> | ID の文字種チェック・`asserted_attrs` と実レコード属性の一致 | ✅ `evidence-gate`（部品） |
> | **論点本文と根拠が意味的に対応しているか** | ❌ **部品はやらない**（意味的照合は非実装） |
>
> したがって「実在するが当該入力箇所と無関係な根拠」を**意味的に**検知することはできない。
> 代わりに **テンプレ側で決定論的な対応関係チェックを実装する**:
>
> 1. **`input_span_id` の実在検証** — 当該 span が今回の入力から実際に切り出された span 集合に含まれるか
> 2. **`criteria_id` × `axis` の一致** — `review_issue.axis` と引用した `criteria_id` の `axis` が一致するか
> 3. **`ruling_id` の axis / scope 一致** — 引用した過去裁定の `axis` が論点の `axis` と一致し、
>    かつ同一 scope に属するか
>
> これらは**すべて ID 照合であり意味解釈を含まない**。不一致は fail-closed。
> 意味的な妥当性（この論点にこの条項を当てるのが妥当か）は**考査責任者の判断に委ねる**
> — AG は機械検証できる範囲を超えて「根拠が正しい」と主張しない。

**非結論的観測契約**: AG は「該当する／違反する」と結論を断定せず、`review_issue`（確認対象論点）と
根拠を出力する。該当の確定は考査責任者に残す。

---

## テナント（scope）境界契約

> **scope キーの正体**: `parts/README.md` の定義に従い、これは「AG 横断のマルチテナント境界」ではなく
> **1 顧客の KB 内で混ぜてはいけない区画を絞る行キー**である。放送局グループには系列局・考査区分という
> **1 顧客内で混ぜてはいけない実在の区画**があるため、本テンプレは scope キーを**採用する**。

> **🔴 State キー名は全部品で同じ宣言にする。** `entry-adapter` / `scoped-retrieval` /
> `payload-store` / `ingest-row-pipeline` の宣言式 `scope_keys` を **`("broadcaster_id", "review_desk_id")`** に統一する。
> retrieval は State の宣言キーを読み、payload-store / ingest は `AuthContext.scope` を constructor/API に
> 明示注入する。ドメイン名（`broadcaster_id` / `review_desk_id`）へ一部だけ変えると fail-closed になるため、
> **State キーは宣言どおり `broadcaster_id` / `review_desk_id` とし、
> ドメイン上の意味を下表で対応づける**。

| 階層 | **State キー（部品準拠）** | ドメイン上の実体 |
|---|---|---|
| 第1 | **`broadcaster_id`** | 放送局・系列局（`broadcaster_id` 相当） |
| 第2 | **`review_desk_id`** | 考査区分（番組考査 / CM 考査。`review_desk_id` 相当） |

**契約:**
- 値は**検証済み `request.state` 由来**で per-request に解決する（`entry-adapter` の思想）。
  entry で認証した結果を `broadcaster_id` / `review_desk_id` として graph 入力 envelope に載せる
- **クライアントヘッダに名乗らせない**（ヘッダ由来 scope は禁止 — 過去テンプレの Critical 指摘）
- **config 固定値 + `"default"` 無言フォールバックは禁止**
- **欠落時は fail-closed**（検索せず `status=error`）
- Stage2 検索は scope で必ず絞る。**局 A のクエリが局 B の裁定を返さないことを PB テストで assert**
- クロステナント学習なし（局間で裁定情報を混在させない）
- **`retrieved_records` の safe projection**: `scoped-retrieval` の既定 allowlist は
  `ruling_id` / `criteria_id` / `criteria_version` を落とすため、**これらを
  `TOP_LEVEL_SAFE_KEYS` に明示追加する**（provenance に必須のため）。scope は部品が検証済み値を
  metadata へ注入するため、`METADATA_SAFE_KEYS` を scope 名で広げない

---

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, permissions, credential handle）
- [x] ConnectionPolicy（retry/timeout strategy）
- [x] SecurityViolationError
- [x] **S-2**: `_extra_security_gate_input()` — 企画関係者 PII の属性表記化・`confidential_business_info` 分類・非テキスト入力の reject
- [x] **S-3**: `_extra_security_gate_output()` — 可否フィールド排除 assert・断定 lexicon・claim→source 照合・PII 再スキャン
- [x] **S-4**: `emit_trace_event()` — 各 `execute()` 内で最低 1 件のドメインイベント
      （`node_start` / `node_complete` / `node_error` は framework が自動発火するため**重複させない**）

> **S-2/S-3 のノード種別による挙動:**
> - `FunctionNode` サブクラス（pre_process / post_process）→ framework `@final` ゲートが自動実行。
>   拡張は `_extra_security_gate_input/output()` **のみ**（`_security_gate_*` の直定義は import 時 `TypeError`）
> - `GraphNode`（main）→ 既定は no-op だが、本テンプレは **`_extra_security_gate_output()` で
>   provenance 検証を行う**（内側 subgraph 境界での検証。`evidence-gate` の配置先）

### `confidential_business_info` の扱い（S-2 拡張）

スポンサー企業名・商品名・未公開企画名は PII ではないが**営業機密**である。

> **🔴 ref 化は State に入る前に行う（入口で一度だけ）。**
> スポンサー企業名・商品名・未公開企画名は、**`pre_process` の S-2 および `/ingest` の
> サニタイズ段で `sponsor_ref` / `product_ref` / 属性表記へ置換してから** State に載せる。
> これにより `validated_input`・`content_summary`・KB 書込・LLM 送信のいずれにも
> **実名が到達しない**。実名は下記「実名マスタ」にのみ存在する。

| 局面 | 方針 |
|---|---|
| State / checkpoint | 参照 ID（**`sponsor_ref` / `product_ref`**）と属性表記のみ。実名は持たない |
| `validated_input` / `content_summary` | ref 置換後の文字列。**実名を含まない** |
| KB 書込（`broadcast_review_records` / `ruling_records`） | ref のみ。**実名を KB に書かない**（＝検索でヒットしない＝想起経由で漏れない） |
| 監査ログ（`emit_trace_event`） | **実名を payload に載せない**。検知事実と分類のみ |
| **外部 LLM 送信** | **実名を送らない。** スポンサー企業名・商品名も **ref / 属性表記のまま**送信する（当てはめ判定は「スポンサーが誰か」ではなく「表現が規則に触れるか」で成立するため、実名は判定に不要） |
| HITL（`hitl_draft` / `hitl_feedback`） | ref のみ（allowlist 済み）。実名を載せない |
| **クライアント応答のみ** | **応答シリアライズ時に実名を解決して表示**（下記 name resolution 契約） |

**実名マスタ**: `sponsor_ref` → 企業名の対応表は **scope 別の独立ストア**として保持し、
**KB（検索対象）には含めない**。検索・想起・LLM のいずれの経路からも到達しない。

#### 🔴 name resolution 契約（出力時のみ実名解決）

考査担当者の使い勝手のため、**クライアント応答でのみ** `sponsor_ref` → スポンサー実名を解決する。
ただし「S-3 通過後に機密が乗る経路」を作らないため、以下を厳守する。

> **🔴 これは単なるシリアライザではなく「delivery gate（配信ゲート）」である**。
> S-3 を通過した出力に**機密データを新たに載せる唯一の箇所**であるため、
> **それ自体が独立したセキュリティゲート**として設計・検証される。

| 規約 | 内容 |
|---|---|
| **🔴 解決位置 = グラフの外（delivery gate）** | **`post_process` では解決しない。** `formatted_output` は **State フィールドであり checkpoint に永続化される**ため、そこに実名を書くと「checkpoint に残さない」契約と両立しない。解決は **graph 実行が完了し最終 checkpoint が書かれた後**、`src/api/server.py` の**配信ゲート**で行う（State を一切変更しない ephemeral な変換） |
| **S-3 との順序** | S-3 は従来どおり `post_process` 内で全段実行（可否フィールド排除 → 断定 lexicon → claim→source 照合 → PII/機密再スキャン）。**name resolution はその全段の後、かつグラフ外**。ID を実名に置換するだけで**新たな自由文を生成しない**（LLM を通さない）ため、解決後に断定 lexicon を再適用する必要はない |
| **🔴 呼び出し元の認可（3 条件すべて必須）** | ① **認証済みの呼び出し元**であること ② 解決対象 ref の **`broadcaster_id` + `review_desk_id` が当該リクエストの検証済み scope と一致**すること ③ 呼び出し元が**実名閲覧権限（`view_sponsor_names` ロール／パーミッション）を明示的に保持**すること。**同一 scope であることは実名を見てよい根拠にならない**（考査部門内でも閲覧権限を分けうる）。**いずれか 1 つでも判定不能・不在なら fail-closed（ref のまま返す）** |
| **解決範囲** | **allowlist**: `sponsor_ref` → スポンサー企業名 / `product_ref` → 商品名 の 2 種のみ。**未公開企画名は解決しない**（放送前漏洩の影響が最も大きいため属性表記のまま） |
| **解決元** | 同一 scope の実名マスタのみ（KB とは別ストア）。**別 scope の ID は解決せず ID のまま返す** |
| **🔴 解決値の非永続化（全経路）** | 解決後の実名を **State / checkpoint / 応答キャッシュ / 監査ログ / トレース / 例外メッセージ・スタックトレース のいずれにも残さない**。配信ゲートはレスポンス書き出しの直前でのみ実名を保持し、**キャッシュ層を通す場合は ref 版をキャッシュし解決は毎回行う**（解決済み応答をキャッシュしない） |
| **🔴 解決失敗時の動作** | マスタ照会の失敗・タイムアウト・権限判定不能・例外のいずれも **ref のまま返して応答は成功させる**（fail-closed かつ可用性維持）。**エラー応答に解決値を含めない**。失敗は `name_resolution_failed`（件数のみ）として監査に記録し、**実名も ref の値も例外メッセージに載せない** |
| **監査ログ** | 解決の**事実**（`resolved_ref_count` / `name_resolution_failed`）のみ記録。**解決後の実名は載せない** |
| **未解決時** | マスタに無い / 別 scope / 権限なしの場合は **ID をそのまま表示**（fail-closed。推測で名前を作らない） |

> **責務分界**: この解決層は「ID → 登録済みの名前」の決定論的な置換に限る。
> 名寄せ・曖昧一致・LLM による補完は**行わない**（誤った企業名を出す事故を防ぐ）。

### Composition Pattern

- **Pattern**: **GraphNode（subgraph）** — `main` スロットに内側 domain_workflow_graph を composition
- **Composition target**: `src/graph/domain_workflow_graph.py`（`BaseGraph` 継承のカスタムトポロジ）
- **Error propagation strategy**: **propagate**（内側エラーは外側へ伝播させる）
  > **🔴 エラー時のルーティング（SDK 実挙動）**: `AgentBaseGraph.route()` は `status=SUCCESS` のみ
  > `post_process` へ送り、**`ERROR` / `TIMEOUT` / `CANCELLED` / `AWAITING_HUMAN` は `finalize` へ直行する**
  > （SDK の AgentBaseGraph リファレンス）。したがって **`post_process` は実行されない** —
  > 「post_process がエラーをパススルーする」設計は成立しない。
  > **エラー時のユーザ向け応答は `finalize` の出力で確定させる**。`post_process` の S-3 ゲートは
  > 通らないため、**エラー応答には考査論点・過去裁定・原文断片を一切含めない**
  > （エラーメッセージは定型文＋`correlation_id` のみ）。
  >
  > **これを既定の `FinalizeNode` 任せにしない** — 既定実装は State に残った部分生成物
  > （`review_issues` / `retrieved_records` / `formatted_output` の書きかけ）を
  > そのまま載せうる。**`FinalizeNode` を継承した安全 finalizer を実装し、
  > `status=error` のときは allowlist（`status` / `error_code` / `correlation_id`）**のみ**を返し、
  > `error_log` の生文言も客先に返さない（監査ログには残す）。
  > `post_process` 自身が raise した場合（S-3 が断定表現を検知した等）も同じ経路を通る。
- **`propagate_hitl`**: **True**（本テンプレは HITL 有効 — 内側の `interrupt()` を外側パイプラインへ伝播させる）
- `merge_output()` は**モードスコープで明示マッピング**する（内側の生フィールドを外側へ素通ししない）

### HITL 設計（D6 `interrupt()`）

考査担当者と AG の往復で論点を詰める refine ループを提供する。

> **🔴 ランタイム設定と静的マニフェストを分離する。**
> `memory_enabled` / `hitl.*` / `max_retry` / `timeout_s` は `config/config.yaml` に置き、
> `config/agent.yaml` は静的マニフェストに限定する。

```yaml
# config/config.yaml
memory_enabled: true    # 必須 — checkpointer が無いと ConfigError
hitl:
  enabled: true
  max_hitl: 8           # 安全弁（circuit breaker）
```

> ⚠️ **🔴 PB-7 の設定判定は fail-closed とする。**
> `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` は `config/config.yaml` を先に読み、
> 必要な場合だけ `config/agent.yaml` へフォールバックする。ファイル不在、YAML パース失敗、例外時は
> 自動免除せずテストを失敗させ、pytest 出力で PB-7 が skip されていないことを確認する。

**契約:**
- `interrupt()` は **必ず `state.get("hitl_allowed", True)` でガードする**。
  `hitl_allowed=False`（`GraphNode` 経由の自動化パイプライン）では interrupt せず処理を続行する。
  **ガードなしの `interrupt()` は本番デッドロックを起こす blocking defect**
- `hitl_draft` に下書きを保持し、resume 時の LLM 再実行を防ぐ
- **🔴 `hitl_draft` は checkpoint に永続化される** — `post_process` の S-3 ゲートは
  `interrupt()` の**後**に走るため、**S-3 を通っていない生データが checkpoint に残りうる**。
  したがって **`interrupt()` に渡す前にサニタイズする**:
  - `hitl_draft` は **allowlist 形式**（`review_issues` の `criteria_id` / `input_span_id` /
    要約済み `issue_text` / `policy_source` のみ）。**台本原文・スポンサー実名・未公開企画名・
    企画関係者の生 PII を入れない**
  - 原文参照が必要な場合は `payload_ref` + span 位置のみを載せ、実体は out-of-state に置く。
    **ただしこの ref はグラフからの再取得には使わない**（single-use は初回 `pre_process` が消費済み）。
    考査担当者が原文を参照する必要がある場合は、**AG のグラフ経路ではなく
    別立ての「原文閲覧フロー」**（name resolution と同じ delivery gate 側で、
    認証＋scope 一致＋閲覧権限を検証し、**読み取り専用・監査記録つき**で提供）とする。
    これを AG の invoke 経路に混ぜない（グラフ内に原文を再流入させないため）。
    > **🔴 single-use との関係を明確化する**: 原文閲覧フローは **`payload-store` の
    > single-use `get()` を消費しない**。実装は次のいずれかとする:
    > **(i) 閲覧フローは `payload-store` を読まない**（顧客の原本管理システム側で参照する）— 既定・推奨。
    > AG は原文の再配信責務を負わない
    > **(ii) 別権限の read-only API として実装**する場合は、`payload-store` に
    > **single-use カウンタを消費しない読み取り専用パス**を設け、`view_raw_payload` 権限＋
    > scope 一致＋**毎回の監査記録**を必須とする（TTL 内のみ・TTL 後は復元不能）。
    > **どちらの場合も invoke/resume の single-use セマンティクスには影響しない。**
  - **🔴 `hitl_feedback` は framework がノードコード実行前に State へ書く** —
    つまり「resume 後にサニタイズする」では**サニタイズ前の値がすでに checkpoint に載っている**。
    したがって**入口（resume を受ける API 層）で制約する**:
    - `/resume` エンドポイントは **構造化アクションのみ受理**する
      （`{"action": "approve"|"correct"|"reject", "corrections": [{"issue_id", "criteria_id", "note"}]}`）
    - **自由文フィールドは長さ上限＋PII/機密スキャンを入口で適用**し、
      違反は **resume を受理せず 4xx で拒否**する（State に載せない）
    - 原文引用が必要な訂正は `payload_ref` + span 指定で受け、実体は載せない
    - `/resume` は `Operation.RESUME` を宣言し、**verified middleware identity のみ**を通す。
      read-only の standalone token は `/invoke` 専用であり resume には使わない
    - `/resume` は checkpoint の `session_id` / `caller_id` を再照合し、外側 snapshot の
      `next` と `subgraph_thread_id` がともに pending の場合だけ `agent.resume()` を呼ぶ。
      完了後（`snapshot.next == ()`）の同一 thread への再送は **409** とし、古い feedback を
      再適用しない。判定と resume は process-local の per-thread lock 内で直列化する
      （multi-worker 構成では共有 checkpoint 層の lease/CAS が必要）
    - terminal resume 後は process-local の delivery scope entry を削除する。複数 round で
      再び `awaiting_human` になった場合だけ entry を維持する。同期 SDK API を event loop 上で
      実行しないよう、FastAPI route は同期 `def` として threadpool で実行する
- **S-3 provenance ゲートは PII/機密ゲートではない** — 引用 ID の実在検証と
  機密データ検知は別の統制であり、両方を post_process で通す
- `HitlStatus` は **approved / corrected / rejected の 3 分岐すべてを処理する**
  （`corrected` を無視すると修正分岐が黙って落ちる）
- `hitl_count` は framework 専有（テンプレは書き込まない）。`hitl_allowed` も同様に書き込まない
- **内側 checkpointer は外側と別インスタンス**にする（`cat2-graph-skeleton` の Do Not Remove 事項）
- PB-7（HITL interrupt 伝播）が**必須**となる（`hitl.enabled: true` のため auto-waive されない）

---

## 近接ユースケースとの境界

類似する業務フローとの混同を避けるため、対象メディア、規範、入力、知識ベースの境界を明示する。

| 対象 | 境界 |
|---|---|
| **Advertiser Claim Substantiation** | **メディア分割**: 本件は放送を対象とし、Web 広告・記事広告・雑誌などの非放送広告は対象外。本件は放送固有ロジック（業態考査・CM 量規制・番組編集基準）と放送基準152条を中核に持つ。知識ベースは自局の考査裁定、入力は番組アウトラインと台本であり、広告主の表示と実証資料を扱う審査とは分離する。 |
| **Rights & Clip Clearance** | **扱う規範が別**: 権利処理は許諾記録との台帳照合、本件は放送基準・広告規制への当てはめ判定を扱う。LLM の役割も資産抽出ではなく規則への当てはめである。 |
| **Podcast Compliance** | 放送局自身のポッドキャストや配信プロパティが同一ポリシー配下にある場合、その台本・広告読み原稿のテキスト事前レビューだけを含む。第三者ポッドキャスト、配信プラットフォームの規約審査、音声解析、配信後監査は対象外。 |
| **Athlete Sponsorship Disclosure** | アスリート個人のスポンサー契約開示ではなく、放送局の番組考査を対象とする。 |
| **Production-plan Risk Recall** | 規則への当てはめと過去判断の想起という構造は共通するが、ドメイン、規範、知識ベースは別物である。 |

---

## 空 KB / cold start の出力契約

蓄積型の知識ベースは**初日は空**である。

| 状態 | 挙動 |
|---|---|
| `ruling_records` が空 | **放送基準準拠の当てはめ判定と論点提示は動作する**（グレースフルデグレード）。<br>過去裁定セクションは空とし、`degradation_reason="empty_ruling_kb"` を返す |
| scope 内に類似案件なし | 同上。`status=success` + `degradation_reason` で返し、**システムエラーと区別する** |
| scope 解決不能 | **`status=error`（fail-closed）** — 空振りとは明確に区別する |

---

## Import Isolation Confirmation
- [x] テンプレートは agenticstar-platform SDK（Level 0）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定 4 段パイプラインであり、エージェントが次アクションを自己決定する自律ループではない |
| Composition pattern | Standalone | GraphNode | **GraphNode** | 3 モード分岐と 4 段パイプラインを内側 graph に閉じ、外側は 5 ノード backbone に収める（Cat2 標準） |
| HITL | 無効 | 有効 | **有効** | 考査担当者と AG の往復で論点を詰める業務に対応する。`memory_enabled: true` とし、PB-7 を必須化する |
| scope キー | 不採用（single-tenant） | 採用 | **採用** | 放送局グループには系列局・考査区分という 1 顧客内の実在区画があるため、テナント分離を必須とする |
| 判定軸の数 | 4 軸（+健康増進法） | 3 軸固定 | **3 軸固定** | 対象範囲の拡大を防ぎ、健康増進法は薬機法系の補助参照に留める |
| 完パケ試写 | 含む | 対象外 | **対象外** | 完成映像の解析が必要になるため、テキスト事前考査に構造的に閉じる |
| 可否判定の出力 | `verdict` フィールドを持つ | 構造的に排除 | **構造的に排除** | 最終考査判断は考査責任者の専権。非弁行為・法的断定リスクの第1層防御 |
| **検索バックエンド** | SDK の `BaseVectorStore` / `BaseDatabase` を使う | **標準 `sqlite3` で自前実装（暫定）** | **自前 SQLite（暫定）**（2026-08-13 に明文化） | SDK の `PgVectorStore` / `PineconeStore` / `PostgresClient` / `MySQLClient` は **1.0.1 時点でも `NotImplementedError` の STUB**。動く永続化層が SDK に無いため標準 `sqlite3` で代替する。**実装が提供されたら差し替える前提**で、①`ReviewRepository` Protocol を境界に置き ②構築点を `wire_services` の 1 箇所に集約し ③`tests/integration/test_retrieval_port_contract.py` を差し替えの受入基準とする。当初この選択は DDR に記録されていなかった |
| **想起のマッチング方式** | 審査本文を 1 つの `LIKE` パターンにする（旧実装） | 語＋2-gram に割り、重なり数で順位付け | **語＋2-gram＋最低一致数**（2026-08-13） | 旧実装は過去要約と審査本文が**一字一句一致**しない限り 0 件で、実務入力では蓄積した裁定に一度も到達しなかった（実測）。当初は「n-gram を入れない」方針だったが、**日本語の審査本文は空白で区切られない**ため語分割だけでは文全体が 1 語になり、`本番組ではサプリメントの効能を紹介します` で 0 件のままだった（敵対的レビューで実測）。**7 文字以上の語だけ 2-gram に展開**し、形態素解析・辞書は入れない。誤想起は最低一致数 3 で抑える |
| 検索語の予算 | 先頭 N 語で切る | **全体から等間隔サンプリング** | **等間隔**（2026-08-13） | 番組概要は「最後に〜を扱います」と関連部分が末尾に来るため、先頭切り詰めでは落ちる（100 term / 予算 64 の実入力で確認） |
| `LIKE` ワイルドカード | 素通し | エスケープ + `ESCAPE` 句 | **エスケープ**（2026-08-13） | 審査本文に `%` が含まれると同一 scope の全裁定にヒットした（実測）。scope は保たれるが想起が無制限になる |
| 部品適用範囲 | 中核のみ | ready 部品を全面適用 | **全面適用** | 既知の実バグ・レビュー指摘（unscoped get() leak・checkpoint raw payload 残り等）を先回りで潰す |

---

## 実装要件

- `config/agent.yaml` の `base_type` / `class` に実値を設定する
- **`config/config.yaml`** に `memory_enabled: true` / `hitl.enabled: true` / `hitl.max_hitl: 8` を設定する。
  `max_retry` / `timeout_s` もランタイム設定としてここに置く
- **`config/review_criteria.yaml`** に放送基準152条の条項マッピング・薬機法・ステマ告示を置く。
  `agent.yaml` には置かない
- **🔴 PB-7 スタブの skip 判定を修正**（`config.yaml` も読む / 例外時は fail させる）。
  PB-7 が skip されずに実行されることを pytest 出力で確認
- **🔴 `mode_specs["invoke"]` に `review_issues` / `disclosure_map` / `escalation_flags` / `criteria_records` を追加**
- **🔴 `output-envelope.format_invoke()` を override**（既定は `state["result"]` を読むため空になる）
- **🔴 `required_fields_by_mode["invoke"]` を強化**（`review_issues` / `evidence_map` を必須化）
- **🔴 安全 finalizer の実装**（エラー時は allowlist のみ返す）
- **🔴 `/resume` 入口の構造化制約 + middleware-only 認証・scope 解決 + pending/replay gate**
- **🔴 axis 別 backend / `ScopedRetrievalNode` の構成**（axis を construction 時に束縛）
- **🔴 決定論的な claim→source 対応チェックの実装**（`input_span_id` 実在 / `axis` 一致 / `ruling_id` の scope・axis 一致）
- **🔴 name resolution = delivery gate の実装** — `src/api/server.py`（グラフ外）に置く。
  **認可 3 条件**（認証済み・scope 一致・`view_sponsor_names` 権限）を満たす場合のみ解決し、
  1 つでも欠ければ ref のまま。allowlist 2 種・未登録は ID のまま・
  **State/checkpoint/応答キャッシュ/監査ログ/トレース/例外に解決値を残さない**・
  解決失敗時は ref のまま応答成功（エラー応答に解決値を載せない）
- **🔴 `payload_ttl_seconds` の実装** — 既定 3600 秒・テンプレ側で上限強制・
  scope + session bind・single-use・**失効後は復元不能**（`status=error`）・
  `payload_expired` / `payload_deleted` の監査イベント（原文断片は載せない）
- **🔴 ref 化を入口で実施** — `pre_process` S-2 と `/ingest` サニタイズ段で
  スポンサー名／商品名を `sponsor_ref` / `product_ref` へ置換してから State に載せる。
  実名マスタは **KB とは別の scope 別ストア**（検索対象に含めない）
- `retention_years` は **既定 `null`（無期限）** で config に用意（削除は後付け可能にしておく）
- 断定 lexicon の初期語彙
- `program_type` の統制語彙と version 方針
- 各部品の adaptation point（`TOP_LEVEL_SAFE_KEYS` / `METADATA_SAFE_KEYS` に
  `ruling_id` / `criteria_id` / `criteria_version` / `axis` を追加、`free_text_paths` 等）の実値化
