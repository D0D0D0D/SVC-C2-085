# テスト仕様・実行結果

実行日: 2026-08-12

テストは外部 LLM・外部 API を呼ばない。LLM は注入 seam の fake、裁定記録は一時 SQLite を使う。
原文・実名は fixture 内だけに置き、State・checkpoint・SQL へは ref/属性表記だけが残ることを検証する。

## Framework compliance

| ID | 検証 | 結果 |
| --- | --- | --- |
| TC-01 | flat TypedDict / Pydantic 非使用 | PASS |
| TC-02 | 不正入力の SecurityViolationError | PASS |
| TC-03 | State に認証情報を置かない | PASS |
| TC-04 | InvocationContext は configurable/state 経由 | PASS |
| TC-05 | lifecycle event の重複なし | PASS |
| TC-06 | S-2 final gate の非 override | PASS |
| TC-07 | S-3 final gate の非 override | PASS |
| TC-08 | required_trust_level | PASS |
| TC-09 | S-2 の domain input check | PASS |
| TC-10 | S-3 の domain output check | PASS |
| TC-11 | 各 execute の domain trace | PASS |

## Proof of boundary

| ID | 検証 | 結果 |
| --- | --- | --- |
| PB-1 | EventEmitter への trace 発火 | PASS |
| PB-2 | State の JSON/msgpack 安全性 | PASS |
| PB-3 | SQLite への実書込・軸/Scope 検索 | PASS |
| PB-3-bis | **retrieval port 契約**（backend 実装非依存・`test_retrieval_port_contract.py`・12 件）: **空白なしの実務日本語**での想起 / 無関係の非想起 / **1 語しか共有しない文の非想起** / 重なり順 / scope 外・axis 外の非想起 / 空 KB は success・空 / scope 欠落は fail-closed / top_k 遵守 / **`LIKE` ワイルドカードで想起を広げられない** / **長文末尾の関連部分が term 予算で落ちない** | PASS |
| PB-4 | Level 0 import isolation | PASS |
| PB-5 | checkpoint に credential/Pydantic 非残留 | PASS |
| PB-6 | S-1 → S-4 → S-2 → execute → S-3 → S-4 の順序 | PASS |
| PB-7 | inner interrupt の outer graph 伝播、`hitl_allowed=false` | PASS（実行済み・skip なし） |

## Security acceptance

| ID | 検証 | 結果 |
| --- | --- | --- |
| SEC-01 | 可否フィールドの構造的排除 | PASS |
| SEC-02 | NFKC/casefold 断定 lexicon | PASS |
| SEC-03 | 捏造 cited ID の fail-closed | PASS |
| SEC-04 | span / criteria-axis / ruling scope-axis の決定論照合 | PASS |
| SEC-05, SEC-05b, SEC-05c | 根拠・Stage4 契約・human review OR 集約 | PASS |
| SEC-06 | baseline policy の正常系 | PASS |
| SEC-07, SEC-08 | tenant/org 分離と scope 欠落 fail-closed | PASS |
| SEC-09, SEC-10 | 原文・HITL draft/feedback の checkpoint 非残留 | PASS |
| SEC-10b, SEC-10c | resume の構造・認証・scope 入口検証 | PASS |
| SEC-11, SEC-12 | HITL allowed guard と approve/correct/reject | PASS |
| SEC-13 | csv/xlsx/docx allowlist と PDF/音声/動画 reject | PASS |
| SEC-14 | error finalizer の情報非開示 | PASS |
| SEC-15--SEC-19 | name resolution の scope、allowlist、監査非実名 | PASS |
| SEC-20, SEC-21 | ingest/LLM に実名を渡さない | PASS |
| SEC-22--SEC-25 | 無関係 ref、権限なし、失敗・cache/log/exception 非残留 | PASS |
| SEC-26 | `payload_store` は TTL だけに依存せず、`/invoke`・`/ingest`・`/feedback` の `put` 後を同一 scope の `try/finally: delete()` で確実削除する。正常完了、S-2 拒否、graph 例外後はいずれも `payload_ref_unknown` | PASS |
| SEC-27 | payload scope 不一致・single-use replay は fail-closed | PASS |
| SEC-28 | retry はサニタイズ済み `validated_input` を再利用し、原文を再取得しない | PASS |
| SEC-29 | HITL resume は原文を再取得しない。`correct` は一致する `issue_id` / `criteria_id` の `review_issues` 本文へ実際に反映する | PASS |
| SEC-30 | 原文閲覧フロー | N/A（方式 (i): 本テンプレは原文再配信 API を持たない） |
| RET-01 | `retention_years: null` の無期限保持 | PASS |

## Business logic

| ID | 検証 | 結果 |
| --- | --- | --- |
| BL-01 | 空 KB の graceful degradation | PASS |
| BL-02 | axis 前置フィルタ | PASS |
| BL-03 | 施行日後の規則を除外 | PASS |
| BL-04 | invoke envelope に review_issues/evidence_map | PASS |
| BL-05 | `/feedback` は同一 scope の record 所有を検証し、`ruling_records` に ruling / 非正規化 axis / `operator_edited` を書き込む。次回 `/invoke` の Stage2 が裁定を想起する | PASS |

## 共通部品接合部（parts/TESTING.md 層2）

| ID | 本テンプレでの具体的検証 | 結果 |
| --- | --- | --- |
| I-4 / I-5 | `broadcaster_id` / `review_desk_id` は client 値を捨て、認証済み scope をトップレベルへ注入。allowlist 外 scalar と raw records は scoped payload-store へ退避 | PASS |
| I-7 | 実 `PayloadStore` を adapter なしで `IngestRowPipelineNode` に注入し、scope + session 付き `resolve(..., consume=True)` から SQLite へ書込 | PASS |
| I-8 | State に正しい scope/session を偽装しても、constructor の認証済み context が無ければ `ValueError` | PASS |
| R-1 | `SQLiteReviewRepository` に実レコードを書き、実 SQL の複合 scope + axis WHERE から 1 件取得 | PASS |
| R-2 / R-5 | 別区画は非表示。正常 hit は保存行由来 scope を返す。scope を落とす adapter は全 hit reject + graceful degradation | PASS |
| V-1 | 正常 provenance は PASS、malformed claim / 非実在 ID / 誤帰属は `SecurityViolationError` | PASS |
| L-4 / L-5 | LLM 未設定のみ deterministic degradation。provider/parse 失敗は stable `error_code` で error、例外文字列は非露出 | PASS |
| N-1--N-5 | client header scope 禁止、token 境界、verified state 優先、payload put/invoke/delete の順序 | PASS |
| N-6 | 実 HTTP で `invoke→READ_INVOKE`、`ingest→WRITE_INGEST`、`feedback→WRITE_FEEDBACK`、`resume→RESUME` を直列確認。standalone token は write/resume 403 | PASS |
| HR-2 | 実 HTTP の `awaiting_human → resume success` 後、同一 thread の再 resume は 409。terminal 時に delivery scope entry を削除 | PASS |
| P-1--P-4 / P-6 | unscoped read 不在、scope/session 境界、replay/TTL、finally delete、consumer protocol は scoped resolve のみ | PASS |

## LLM 応答契約

- `review_issues` の必須 9 フィールド、enum、文字数上限、closed-key 契約を module 定数から `response_contract` として生成し、prompt に埋め込む。
- `test_prompt_response_contract.py` は prompt 内の宣言だけから応答を組み立て、Stage3 validator → Stage4 assembler → PostProcess projection / S-3 まで通す。
- 設計上の独立不変条件として 9 フィールド名と公開 invoke envelope の業務フィールド名をテスト側に固定する。
- 未宣言キー `model_invented_verdict` を含む応答は `llm_output_contract_failed` で fail-closed。

## 変異検査

- 変異: `src/graph/domain_workflow_graph.py` の unknown-key reject（`set(issue).issubset(allowed_fields)`）を実ファイルから一時除去。
- 実在確認: `rg` で `MUTATION PROBE` が対象行に存在し、元 guard が不存在であることを確認。
- 結果: `tests/unit/test_prompt_response_contract.py` は **1 failed, 1 passed**（unknown-key reject テストが赤）。
- 復元: `apply_patch` で guard を戻し、`rg` で guard 実在 / mutation marker 不在を確認後 **2 passed**。

## retrieval backend 差し替えの受入基準（2026-08-13 追加）

現行 backend は **SQLite（暫定）**。SDK の `BaseVectorStore` / `BaseDatabase` は 1.0.1 時点でも
`NotImplementedError` の STUB で、動く永続化層が無いため標準 `sqlite3` で代替している。

**実装が提供されたら `tests/integration/test_retrieval_port_contract.py` の `_backend()` を差し替え、
このファイルをそのまま流すことが受入基準**（SQL・`LIKE`・語分割のどれにも依存しない条件で書いてある）。
`repository.search_axis(...)` を直接叩く既存テスト（`test_broadcast_security.py` ほか）は
SQLite アダプタ固有の API を固定しているもので、差し替え時には書き直しになる。

> 🔴 **契約テストの入力は空白を入れない**。初版は `本番組では サプリメント の 効能 を 紹介します` と
> 人手で区切っており、実務の日本語（`本番組ではサプリメントの効能を紹介します`）では 0 件のままだった
> ことを敵対的レビューが実測で検出した。既存 PB-3 と同じ「入力の選び方で欠陥が隠れる」型。
>
> 🔴 旧実装は審査本文を**まるごと 1 つの `LIKE` パターン**にしていたため、過去要約と
> 一字一句一致しない限り 0 件だった。実測 3 ケース（完全一致=1件 / 語は重なるが文が違う=0件 /
> 語も重ならない=0件）で確認し、語＋重なり数の方式へ変更。既存 PB-3 は「過去要約と審査本文が
> 完全一致」という条件で組まれていたため緑のまま欠陥を通していた。

## 実行結果

```text
PYTHONPATH="$PWD/.venv/lib/python3.11/site-packages:$PWD:$PWD/src" .venv/bin/pytest tests/ -q --tb=line
444 passed, 1 skipped, 3 failed

PYTHONPATH="$PWD/.venv/lib/python3.11/site-packages:$PWD:$PWD/src" .venv/bin/pytest tests/ -q --tb=line --ignore=tests/parts/ingest-file-sanitizer
437 passed, 1 skipped
```

skip は方式 (i) を採用した SEC-30（原文閲覧 API を実装しないため N/A）。3 failed は `.venv` に `defusedxml==0.7.1` が無いため、部品 v0.1.1 が setup error として顕在化させた DOCX 3 ケースである。`openpyxl==3.1.5` も未導入のため、対応宣言済み XLSX 正常系は未検証。この実行記録では依存パッケージを追加していない。PDF reject・CSV、防御部品、PB-7 は実行済み。

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['review_issues', 'disclosure_map', 'escalation_flags', 'evidence_map', 'cited_source_ids', 'criteria_records', 'mode', 'gate_kind'] が非空。縮退理由なし |

本番の LLM はデプロイ側が `config["llm"]` に注入する `BaseLLM` 実装で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。実 LLM への接続情報が必要なため CI には含めない。
