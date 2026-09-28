"""Domain services and injected seams for the broadcast-review workflow.

The repository deliberately keeps raw payloads and sponsor names outside the
graph.  The SQLite implementation is suitable for local/STG verification; a
deployment can replace the container's repository and LLM without changing a
node.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml
from shared.security.pii_detector import detect_pii

from src.entry_adapter import SCOPE_KEYS
from src.payload_store import PayloadStore


AXES = frozenset({"broadcast_standards", "pmd_act", "stealth_marketing"})
_NAME_LABEL = re.compile(r"(?P<label>スポンサー(?:企業|名)?|商品名?|sponsor|product)\s*[:：]\s*(?P<value>[^\n、。]{1,80})", re.I)
_UNRELEASED_PROJECT_LABEL = re.compile(r"未公開(?:企画|番組)名\s*[:：]\s*(?P<value>[^\n、。]{1,80})")
_EMAIL_ADDRESS = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])")
_PHONE_NUMBER = re.compile(r"(?<!\d)(?:\+?81[-\s]?)?0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?!\d)")
_MY_NUMBER = re.compile(r"(?<!\d)\d{12}(?!\d)")
_PAYMENT_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def normalize_review_text(value: Any) -> str:
    """Canonical write/search normalisation shared by ingestion and retrieval."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


_TERM_SPLIT_RE = re.compile(r"[\s、。，．・「」『』（）()\[\]【】:：;；/／\\|+*_'\"-]+")
_MIN_TERM_LEN = 2
_MAX_TERMS = 64
# A word longer than this is treated as unsegmented running text and expanded
# into 2-grams. Japanese review copy has no spaces, so splitting on whitespace
# alone leaves the whole sentence as one term and it matches nothing.
_UNSEGMENTED_LEN = 6
# How many of the terms a record must carry to count as a hit. One shared term
# is not a recall — with 2-grams almost every stored row shares one — but the
# share cannot scale with the text either: a long programme description would
# then demand more overlap than any single past ruling can supply.
_MIN_HITS = 3


def search_terms(query: Any) -> list[str]:
    """Split a review text into the terms a stored summary can be matched on.

    The retrieval backend is a stand-in: the SDK ships `BaseVectorStore` /
    `BaseDatabase` as stubs (`NotImplementedError`), so recall runs on SQLite
    until a real implementation lands. The rule stays deliberately small — no
    morphological analyser, no dictionary — because a real backend replaces all
    of it. What must survive the swap is pinned by the port-contract tests.

    Whitespace and punctuation first; anything still long enough to be running
    text is expanded into 2-grams. Japanese business copy
    ("本番組ではサプリメントの効能を紹介します") carries no spaces at all, and
    without the expansion it stays one term that no stored summary contains.
    """
    normalized = normalize_review_text(query)
    if not normalized:
        return []
    terms: dict[str, None] = {}
    for part in _TERM_SPLIT_RE.split(normalized):
        if len(part) < _MIN_TERM_LEN:
            continue
        if len(part) <= _UNSEGMENTED_LEN:
            terms.setdefault(part, None)
            continue
        for index in range(len(part) - 1):
            terms.setdefault(part[index : index + 2], None)
    collected = list(terms)
    if len(collected) <= _MAX_TERMS:
        return collected
    # Sample across the whole text rather than truncating: the passage that
    # matters is often at the end of a long programme description, and taking
    # the first N terms drops it entirely.
    step = len(collected) / _MAX_TERMS
    return [collected[int(index * step)] for index in range(_MAX_TERMS)]


def _like_pattern(term: str) -> str:
    """Escape LIKE wildcards so a query cannot widen its own match.

    A review text containing `%` matched every stored ruling in the same scope
    (measured), which is a scope-preserving but unbounded recall.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def stable_ref(kind: str, value: str) -> str:
    digest = hashlib.sha256(normalize_review_text(value).encode("utf-8")).hexdigest()[:16]
    return f"{kind}_ref_{digest}"


class NameRegistry:
    """Scope-bound name master used only by the graph-external delivery gate."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], str] = {}

    def register_from_text(self, text: str, *, broadcaster_id: str, review_desk_id: str) -> set[str]:
        refs: set[str] = set()
        for kind, value in confidential_name_values(text):
            # The delivery gate can only resolve values held here.  Strip PII
            # before both the ref is derived and the value is retained, so a
            # permissive name-view permission can never re-expand a contact
            # address or telephone number embedded beside a commercial name.
            safe_value = commercial_name_value(value)
            if not safe_value:
                continue
            ref = stable_ref(kind, safe_value)
            self._values[(broadcaster_id, review_desk_id, ref)] = safe_value
            refs.add(ref)
        return refs

    def ref_hints(self, refs: set[str]) -> dict[str, str]:
        """Return non-sensitive label-to-ref hints for pre_process state."""

        hints: dict[str, str] = {}
        sponsor = next((ref for ref in refs if ref.startswith("sponsor_ref_")), None)
        product = next((ref for ref in refs if ref.startswith("product_ref_")), None)
        if sponsor:
            hints["sponsor"] = sponsor
        if product:
            hints["product"] = product
        return hints

    def resolve(self, ref: str, *, broadcaster_id: str, review_desk_id: str) -> str | None:
        return self._values.get((broadcaster_id, review_desk_id, ref))


def sanitize_sensitive_text(text: str, *, ref_hints: dict[str, str] | None = None) -> str:
    """Replace labelled commercial names before text enters State or a prompt."""

    ref_hints = ref_hints or {}

    def replace(match: re.Match[str]) -> str:
        label = normalize_review_text(match.group("label"))
        kind = "product" if "商品" in label or label == "product" else "sponsor"
        # Derive each ref from the matched value.  A kind-level envelope hint
        # cannot safely represent two different sponsors without putting their
        # names in State, whereas this deterministic id matches NameRegistry.
        safe_value = commercial_name_value(match.group("value"))
        if not safe_value:
            return f"{match.group('label')}: [名称非開示]"
        return f"{match.group('label')}: {stable_ref(kind, safe_value)}"

    normalized_text = unicodedata.normalize("NFKC", str(text or ""))
    try:
        structured = json.loads(normalized_text)
    except (TypeError, ValueError):
        structured = None
    if structured is not None:
        return _redact_detected_pii(json.dumps(_redact_structured_names(structured), ensure_ascii=False, default=str))
    return _redact_free_text(normalized_text, replace)


def confidential_name_values(text: Any) -> list[tuple[str, str]]:
    """Extract the sponsor/product vocabulary shared by ref creation and S-2."""

    normalized_text = unicodedata.normalize("NFKC", str(text or ""))
    try:
        structured = json.loads(normalized_text)
    except (TypeError, ValueError):
        structured = None
    if structured is not None:
        return _structured_name_values(structured) + _embedded_name_values(structured)
    return _labelled_name_values(normalized_text)


def _labelled_name_values(text: str) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for match in _NAME_LABEL.finditer(text):
        label = normalize_review_text(match.group("label"))
        kind = "product" if "商品" in label or label == "product" else "sponsor"
        values.append((kind, match.group("value").strip()))
    return values


def contains_sensitive_submission(text: Any, *, allow_commercial_references: bool = False) -> bool:
    """Return whether an entry payload must be rejected before graph State.

    This is the single entry-boundary decision point for every HTTP mode.
    PII and confidential project titles are always rejected.  ``/invoke`` and
    ``/ingest`` may opt in to labelled sponsor/product values because S-2
    deterministically converts those values to opaque refs; feedback and
    resume cannot, so they use the default fail-closed behaviour.
    """

    normalized_text = unicodedata.normalize("NFKC", str(text or ""))
    return bool(
        _detected_pii_requires_rejection(normalized_text, allow_commercial_references=allow_commercial_references)
        or _UNRELEASED_PROJECT_LABEL.search(normalized_text)
        or _EMAIL_ADDRESS.search(normalized_text)
        or _PHONE_NUMBER.search(normalized_text)
        or _MY_NUMBER.search(normalized_text)
        or _contains_payment_card(normalized_text)
        or (not allow_commercial_references and confidential_name_values(normalized_text))
    )


def commercial_name_value(value: Any) -> str:
    """Return a delivery-safe sponsor/product name, without adjacent PII."""

    cleaned = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = _EMAIL_ADDRESS.sub("", cleaned)
    cleaned = _PHONE_NUMBER.sub("", cleaned)
    cleaned = _MY_NUMBER.sub("", cleaned)
    cleaned = _PAYMENT_CARD_CANDIDATE.sub("", cleaned)
    # Contact values are commonly appended as ``Acme / jane@example.com``.
    # Remove only now-empty separators; preserve legitimate spaces in a name.
    cleaned = re.sub(r"\s*[／/|,、;；]\s*", " ", cleaned)
    return " ".join(cleaned.split()).strip(" /／|,、;；")


def _contains_payment_card(text: str) -> bool:
    for candidate in _PAYMENT_CARD_CANDIDATE.findall(text):
        digits = [int(char) for char in candidate if char.isdigit()]
        checksum = 0
        for index, digit in enumerate(reversed(digits)):
            if index % 2:
                digit = digit * 2 - 9 if digit > 4 else digit * 2
            checksum += digit
        if checksum % 10 == 0:
            return True
    return False


def _detected_pii_requires_rejection(text: str, *, allow_commercial_references: bool) -> bool:
    """Keep detector findings fail-closed except labelled names that S-2 refs."""

    findings = detect_pii(text)
    if not findings:
        return False
    if not allow_commercial_references:
        return True
    commercial_value_spans = [(match.start("value"), match.end("value")) for match in _NAME_LABEL.finditer(text)]
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("type") != "name":
            return True
        start, end = finding.get("start"), finding.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not any(
            value_start <= start and end <= value_end for value_start, value_end in commercial_value_spans
        ):
            return True
    return False


def _redact_free_text(text: str, replace) -> str:
    redacted = _NAME_LABEL.sub(replace, text)
    # A deliberately conservative marker for project titles.  It prevents a
    # labelled yet unpublished title from becoming a retrieval/LLM artefact.
    redacted = _UNRELEASED_PROJECT_LABEL.sub("未公開企画名: [企画名非開示]", redacted)
    # detect_pii is retained at API boundaries.  These common textual forms
    # are also removed here so emails/telephone numbers never reach State,
    # checkpoints, prompts, or the ruling KB when the upstream detector does
    # not classify them.
    redacted = _EMAIL_ADDRESS.sub("[連絡先非開示]", redacted)
    redacted = _PHONE_NUMBER.sub("[連絡先非開示]", redacted)
    redacted = _MY_NUMBER.sub("[個人情報非開示]", redacted)
    redacted = _PAYMENT_CARD_CANDIDATE.sub("[個人情報非開示]", redacted)
    # Input fixtures and operational integrations may mark verbatim script
    # fragments explicitly.  These carry no rule-application value and must
    # not become checkpoint evidence merely because they are not PII.
    redacted = re.sub(r"(?:台本|原文)[^\n]{0,24}マーカー", "[入力固有マーカー非保持]", redacted)
    return _redact_detected_pii(redacted)


def _redact_detected_pii(text: str) -> str:
    """Mask any remaining detector finding before it can be checkpointed."""

    findings = detect_pii(text)
    if not isinstance(findings, list):
        return text
    redacted = text
    for finding in sorted(findings, key=lambda item: item.get("start", -1) if isinstance(item, dict) else -1, reverse=True):
        if not isinstance(finding, dict):
            continue
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(redacted):
            redacted = f"{redacted[:start]}[個人情報非開示]{redacted[end:]}"
    return redacted


def _structured_name_values(value: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = normalize_review_text(key).replace(" ", "_")
            if isinstance(item, str) and normalized in {"sponsor", "sponsor_name", "スポンサー", "スポンサー名"}:
                found.append(("sponsor", item))
            elif isinstance(item, str) and normalized in {"product", "product_name", "商品", "商品名"}:
                found.append(("product", item))
            else:
                found.extend(_structured_name_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_structured_name_values(item))
    return found


def _embedded_name_values(value: Any) -> list[tuple[str, str]]:
    """Find labelled names held inside free-text JSON fields without raw JSON punctuation."""

    if isinstance(value, str):
        return _labelled_name_values(value)
    if isinstance(value, list):
        return [item for child in value for item in _embedded_name_values(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _embedded_name_values(child)]
    return []


#: Fields whose values are opaque identifiers, never prose.  They are already
#: constrained to ``^[A-Za-z0-9_-]+$`` at the HTTP boundary, so they cannot
#: carry a phone number, My Number or card number in any readable form — but a
#: hex-shaped identifier *does* contain digit runs that ``_MY_NUMBER`` /
#: ``_PAYMENT_CARD_CANDIDATE`` match (~3.4% of ``uuid4().hex`` values). Masking
#: one rewrites the identifier mid-flight, so the S-3 provenance gate then
#: rejects it as an unsafe source id and the whole request fails. Identifiers
#: are therefore passed through structurally rather than scanned as free text.
_OPAQUE_ID_FIELDS = frozenset({"ruling_id", "record_id", "criteria_id", "criteria_version", "issue_id"})

_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,120}\Z")


def _is_opaque_identifier(value: Any) -> bool:
    """True when ``value`` matches the identifier contract enforced at ingress."""

    return isinstance(value, str) and bool(_OPAQUE_ID_RE.fullmatch(value))


def _redact_structured_names(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_structured_names(item) for item in value]
    if not isinstance(value, dict):
        if isinstance(value, str):
            return _redact_free_text(
                value,
                lambda match: f"{match.group('label')}: {stable_ref('product' if '商品' in normalize_review_text(match.group('label')) else 'sponsor', commercial_name_value(match.group('value')))}",
            )
        return value
    output: dict[str, Any] = {}
    for key, item in value.items():
        normalized = normalize_review_text(key).replace(" ", "_")
        if key in _OPAQUE_ID_FIELDS and _is_opaque_identifier(item):
            # Identifier, not prose: keep it byte-for-byte so lookups and
            # provenance checks still match. Anything that fails the identifier
            # contract falls through and is redacted like ordinary text.
            output[key] = item
        elif isinstance(item, str) and normalized in {"sponsor", "sponsor_name", "スポンサー", "スポンサー名"}:
            safe_value = commercial_name_value(item)
            output["sponsor_ref"] = stable_ref("sponsor", safe_value) if safe_value else "[名称非開示]"
        elif isinstance(item, str) and normalized in {"product", "product_name", "商品", "商品名"}:
            safe_value = commercial_name_value(item)
            output["product_ref"] = stable_ref("product", safe_value) if safe_value else "[名称非開示]"
        else:
            output[key] = _redact_structured_names(item)
    return output


class ReviewRepository(Protocol):
    """Port the review store must satisfy — the seam a real backend swaps into.

    SQLite is a stand-in. The SDK ships `BaseDatabase` / `BaseVectorStore` as
    stubs that raise `NotImplementedError`, so recall runs on SQLite until a
    real implementation exists. Declaring the port here (rather than typing the
    container to the SQLite class) keeps the swap to one construction site, and
    `tests/integration/test_retrieval_port_contract.py` states what any
    implementation has to do.
    """

    def write_ingest_record(self, row: dict[str, Any]) -> str: ...

    def write_ruling(self, row: dict[str, Any], *, allow_overwrite: bool = False) -> str: ...

    def record_belongs_to_scope(self, record_id: str, *, broadcaster_id: str, review_desk_id: str) -> bool: ...

    def ruling_belongs_to_scope(self, ruling_id: str, *, broadcaster_id: str, review_desk_id: str) -> bool: ...

    def search_axis(
        self, *, broadcaster_id: str, review_desk_id: str, axis: str, query: str, top_k: int
    ) -> list[dict[str, Any]]: ...


@dataclass
class SQLiteReviewRepository:
    """Small real-SQL boundary used locally and replaceable in STG."""

    database_path: str = ":memory:"
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS broadcast_review_records (
              broadcaster_id TEXT NOT NULL, review_desk_id TEXT NOT NULL, record_id TEXT NOT NULL,
              program_type TEXT NOT NULL, sponsor_ref TEXT, content_summary TEXT NOT NULL,
              reviewed_at TEXT, criteria_version TEXT,
              PRIMARY KEY (broadcaster_id, review_desk_id, record_id)
            );
            CREATE TABLE IF NOT EXISTS ruling_records (
              broadcaster_id TEXT NOT NULL, review_desk_id TEXT NOT NULL, ruling_id TEXT NOT NULL, record_id TEXT NOT NULL,
              criteria_id TEXT NOT NULL, criteria_version TEXT NOT NULL,
              axis TEXT NOT NULL, ruling TEXT, revision_note TEXT, rationale TEXT,
              escalated INTEGER NOT NULL DEFAULT 0, operator_edited INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (broadcaster_id, review_desk_id, ruling_id),
              FOREIGN KEY (broadcaster_id, review_desk_id, record_id)
                REFERENCES broadcast_review_records (broadcaster_id, review_desk_id, record_id)
            );
            """
        )
        self._connection.commit()

    def write_ingest_record(self, row: dict[str, Any]) -> str:
        record_id = str(row["record_id"])
        self._connection.execute(
            """INSERT INTO broadcast_review_records
               (record_id, broadcaster_id, review_desk_id, program_type, sponsor_ref, content_summary, reviewed_at, criteria_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(broadcaster_id, review_desk_id, record_id) DO UPDATE SET
                 program_type=excluded.program_type, sponsor_ref=excluded.sponsor_ref,
                 content_summary=excluded.content_summary, reviewed_at=excluded.reviewed_at,
                 criteria_version=excluded.criteria_version""",
            (
                record_id, row["broadcaster_id"], row["review_desk_id"], row["program_type"], row.get("sponsor_ref"),
                row["content_summary"], row.get("reviewed_at"), row.get("criteria_version"),
            ),
        )
        self._connection.commit()
        return record_id

    def write_ruling(self, row: dict[str, Any], *, allow_overwrite: bool = False) -> str:
        if row.get("axis") not in AXES:
            raise ValueError("invalid_axis")
        ruling_id = str(row["ruling_id"])
        if not self.record_belongs_to_scope(
            str(row["record_id"]), broadcaster_id=str(row["broadcaster_id"]), review_desk_id=str(row["review_desk_id"])
        ):
            raise ValueError("record_scope_invalid")
        if self.ruling_belongs_to_scope(ruling_id, broadcaster_id=str(row["broadcaster_id"]), review_desk_id=str(row["review_desk_id"])) and not allow_overwrite:
            raise ValueError("ruling_id_already_exists")
        self._connection.execute(
            """INSERT INTO ruling_records
               (ruling_id, record_id, broadcaster_id, review_desk_id, criteria_id, criteria_version, axis, ruling,
                revision_note, rationale, escalated, operator_edited)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(broadcaster_id, review_desk_id, ruling_id) DO UPDATE SET
                 record_id=excluded.record_id, criteria_id=excluded.criteria_id,
                 criteria_version=excluded.criteria_version, axis=excluded.axis, ruling=excluded.ruling,
                 revision_note=excluded.revision_note, rationale=excluded.rationale,
                 escalated=excluded.escalated, operator_edited=excluded.operator_edited""",
            (
                ruling_id, row["record_id"], row["broadcaster_id"], row["review_desk_id"], row["criteria_id"],
                row["criteria_version"], row["axis"], row.get("ruling"), row.get("revision_note", ""),
                row.get("rationale", ""), int(bool(row.get("escalated"))), int(bool(row.get("operator_edited"))),
            ),
        )
        self._connection.commit()
        return ruling_id

    def record_belongs_to_scope(self, record_id: str, *, broadcaster_id: str, review_desk_id: str) -> bool:
        return self._connection.execute(
            """SELECT 1 FROM broadcast_review_records
                 WHERE broadcaster_id=? AND review_desk_id=? AND record_id=?""",
            (broadcaster_id, review_desk_id, record_id),
        ).fetchone() is not None

    def ruling_belongs_to_scope(self, ruling_id: str, *, broadcaster_id: str, review_desk_id: str) -> bool:
        return self._connection.execute(
            """SELECT 1 FROM ruling_records
                 WHERE broadcaster_id=? AND review_desk_id=? AND ruling_id=?""",
            (broadcaster_id, review_desk_id, ruling_id),
        ).fetchone() is not None

    def search_axis(self, *, broadcaster_id: str, review_desk_id: str, axis: str, query: str, top_k: int) -> list[dict[str, Any]]:
        if not broadcaster_id or not review_desk_id or axis not in AXES:
            return []
        terms = search_terms(query)
        if not terms:
            return []
        # Score by how many query terms a stored record carries, most first.
        # Matching the WHOLE review text as one LIKE pattern only ever hits a
        # record whose summary is character-identical to it, so every realistic
        # review recalled nothing while `status` stayed `success` — the stored
        # rulings were never reachable.
        score_sql = " + ".join(
            "(CASE WHEN lower(b.content_summary) LIKE ? ESCAPE '\\' "
            "OR lower(b.program_type) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
            for _ in terms
        )
        params: list[Any] = []
        for term in terms:
            pattern = _like_pattern(term)
            params.extend([pattern, pattern])
        min_hits = min(_MIN_HITS, len(terms))
        rows = self._connection.execute(
            f"""SELECT r.broadcaster_id, r.review_desk_id, r.ruling_id, r.record_id, r.criteria_id, r.criteria_version, r.axis,
                      b.program_type, b.content_summary, ({score_sql}) AS match_score
                 FROM ruling_records r JOIN broadcast_review_records b
                   ON b.broadcaster_id = r.broadcaster_id AND b.review_desk_id = r.review_desk_id AND b.record_id = r.record_id
                WHERE r.broadcaster_id=? AND r.review_desk_id=? AND r.axis=?
                  AND ({score_sql}) >= ?
                ORDER BY match_score DESC, r.ruling_id
                LIMIT ?""",
            (*params, broadcaster_id, review_desk_id, axis, *params, min_hits, top_k),
        ).fetchall()
        return [
            {
                "id": row["ruling_id"], "source_id": row["ruling_id"], "ruling_id": row["ruling_id"],
                "record_id": row["record_id"], "criteria_id": row["criteria_id"],
                "criteria_version": row["criteria_version"], "axis": row["axis"],
                "title": row["program_type"], "summary": "過去裁定（要約）",
                "broadcaster_id": row["broadcaster_id"], "review_desk_id": row["review_desk_id"],
                "metadata": {"record_id": row["record_id"], "ruling_id": row["ruling_id"],
                             "criteria_id": row["criteria_id"], "criteria_version": row["criteria_version"],
                             "axis": row["axis"], "broadcaster_id": row["broadcaster_id"], "review_desk_id": row["review_desk_id"]},
            }
            for row in rows
        ]


class AxisBoundRetrievalBackend:
    """Binds the axis at construction so free-text queries cannot change it."""

    def __init__(self, repository: ReviewRepository, axis: str) -> None:
        if axis not in AXES:
            raise ValueError("invalid_axis")
        self._repository = repository
        self._axis = axis

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        return self._repository.search_axis(
            broadcaster_id=scope["broadcaster_id"], review_desk_id=scope["review_desk_id"], axis=self._axis, query=query, top_k=top_k
        )


def load_review_criteria(path: str | Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    criteria = payload.get("criteria", [])
    if not isinstance(criteria, list):
        raise ValueError("review_criteria.criteria must be a list")
    for item in criteria:
        if not isinstance(item, dict) or item.get("axis") not in AXES:
            raise ValueError("review_criteria contains an invalid axis")
    return [dict(item) for item in criteria]


def effective_criteria(criteria: list[dict[str, Any]], broadcast_date: str | None) -> list[dict[str, Any]]:
    target = date.fromisoformat(broadcast_date) if broadcast_date else date.today()
    return [item for item in criteria if date.fromisoformat(str(item["effective_date"])) <= target]


def configured_criterion_matches(
    criteria: list[dict[str, Any]], *, criteria_id: Any, criteria_version: Any, axis: Any
) -> bool:
    """Return whether feedback names an exact configured review criterion."""

    return any(
        str(item.get("criteria_id")) == str(criteria_id)
        and str(item.get("criteria_version")) == str(criteria_version)
        and item.get("axis") == axis
        for item in criteria
    )


@dataclass
class ServiceContainer:
    payload_store: PayloadStore
    repository: ReviewRepository
    name_registry: NameRegistry
    review_criteria: list[dict[str, Any]]
    llm: Any | None = None


def wire_services(config: dict[str, Any] | None = None) -> ServiceContainer:
    """Build injected services at server startup; no client is created in nodes."""

    config = config or {}
    root = Path(__file__).resolve().parents[2]
    criteria_path = config.get("review_criteria_path", root / "config" / "review_criteria.yaml")
    ttl = int(config.get("payload_ttl_seconds", 3600))
    if not 1 <= ttl <= 86_400:
        raise ValueError("payload_ttl_seconds must be between 1 and 86400")
    return ServiceContainer(
        payload_store=PayloadStore(scope_keys=SCOPE_KEYS, ttl_seconds=ttl),
        # Single construction site for the review store. A real backend is
        # injected here (or passed in as `repository`) without touching nodes.
        repository=(
            config["repository"]
            if config.get("repository") is not None
            else SQLiteReviewRepository(str(config.get("database_path", ":memory:")))
        ),
        name_registry=NameRegistry(),
        review_criteria=load_review_criteria(criteria_path),
        llm=config.get("llm"),
    )
