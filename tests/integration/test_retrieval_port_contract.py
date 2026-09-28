"""What ANY review-store backend has to do — implementation-independent.

The current backend is SQLite because the SDK ships `BaseVectorStore` /
`BaseDatabase` as stubs that raise `NotImplementedError`. When a real
implementation lands, point `_backend()` at it and run this file unchanged: it
is the acceptance criteria for the swap, and nothing in it depends on SQL, on
`LIKE`, or on how terms are split.

Tests that call `repository.search_axis(...)` directly are NOT that — they pin
the SQLite adapter's own API and would be rewritten on a swap.
"""

from __future__ import annotations

import pytest

from src.scoped_retrieval import ScopedRetrievalNode
from src.services.service import AxisBoundRetrievalBackend, normalize_review_text, wire_services

AXIS = "pmd_act"
SCOPE = {"broadcaster_id": "tenant", "review_desk_id": "org"}

# A past ruling as a desk would actually have stored it: a summary in the
# reviewer's own words, not a copy of some future submission.
PAST = {
    "record_id": "record-supplement",
    "content_summary": "健康食品 サプリメント の 効能 表現 で 差戻し",
    "ruling_id": "ruling-supplement",
}
UNRELATED = {
    "record_id": "record-vessel",
    "content_summary": "船舶 名称 の 表記 ゆれ について 照会",
    "ruling_id": "ruling-vessel",
}


def _repository():
    """THE seam. Point this at a real backend and run the file unchanged.

    Swapping only the retrieval adapter would leave the new store empty — the
    records below are written through whatever this returns.
    """
    return None  # None -> wire_services builds the current (SQLite) default


def _store(records, *, scope=SCOPE, axis=AXIS):
    services = wire_services({"repository": _repository()})
    for record in records:
        services.repository.write_ingest_record(
            {
                "record_id": record["record_id"],
                "program_type": "drama",
                "content_summary": record["content_summary"],
                **scope,
            }
        )
        services.repository.write_ruling(
            {
                "ruling_id": record["ruling_id"],
                "record_id": record["record_id"],
                "criteria_id": "PMD-ACT-AD-STANDARD",
                "criteria_version": "2026.1",
                "axis": record.get("axis", axis),
                **scope,
            }
        )
    return services


def _backend(services, axis: str = AXIS):
    """The single seam. Swap the backend here; the assertions below do not move."""
    return AxisBoundRetrievalBackend(services.repository, axis)


def _recall(services, review_text: str, *, scope=SCOPE, axis: str = AXIS) -> list[str]:
    node = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=_backend(services, axis),
        normalize_query=normalize_review_text,
        top_k=5,
    )
    result = node.execute({**scope, "query": review_text})
    return [record.get("ruling_id") for record in result.get("retrieved_records") or []]


def test_a_realistic_review_recalls_a_related_ruling():
    """The review text and the stored summary share vocabulary, not wording.

    The text carries NO spaces — that is how Japanese review copy arrives.
    Splitting on whitespace alone leaves the whole sentence as one term that no
    stored summary contains, and an earlier version of this file hid that by
    spacing the input out by hand.
    """
    services = _store([PAST])

    recalled = _recall(services, "本番組ではサプリメントの効能を紹介します")

    assert "ruling-supplement" in recalled, recalled


def test_an_unrelated_ruling_is_not_recalled():
    """Otherwise 'recall everything' would satisfy the test above."""
    services = _store([PAST, UNRELATED])

    recalled = _recall(services, "本番組ではサプリメントの効能を紹介します")

    assert "ruling-vessel" not in recalled, recalled


def test_more_shared_vocabulary_ranks_higher():
    services = _store(
        [
            PAST,
            {
                # ruling_id は辞書順で PAST より前に来る。順位付けを外すとこちらが
                # 1 位になるので、このテストは「重なりで勝つ」ことを実際に見ている。
                "record_id": "record-aaa",
                "content_summary": "番組 表記 の 確認",
                "ruling_id": "ruling-aaa",
            },
        ]
    )

    recalled = _recall(services, "番組で健康食品サプリメントの効能表現を扱う")

    assert recalled, recalled
    assert recalled[0] == "ruling-supplement", f"重なりの多い裁定が上位に来ていない: {recalled}"


def test_another_scope_is_never_recalled():
    services = _store([PAST], scope={"broadcaster_id": "other", "review_desk_id": "other-org"})

    recalled = _recall(services, "サプリメントの効能")

    assert recalled == [], recalled


def test_another_axis_is_never_recalled():
    services = _store([{**PAST, "axis": "stealth_marketing"}])

    recalled = _recall(services, "サプリメントの効能")

    assert recalled == [], recalled


def test_an_empty_store_returns_no_hits_rather_than_an_error():
    services = wire_services({"repository": _repository()})

    node = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=_backend(services),
        normalize_query=normalize_review_text,
        top_k=5,
    )
    result = node.execute({**SCOPE, "query": "サプリメントの効能"})

    assert result.get("status") == "success", result
    assert (result.get("retrieved_records") or []) == []


def test_a_missing_scope_fails_closed_rather_than_searching_everything():
    services = _store([PAST])

    node = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=_backend(services),
        normalize_query=normalize_review_text,
        top_k=5,
    )
    result = node.execute({"query": "サプリメントの効能"})

    assert result.get("status") == "error", result
    assert result.get("retrieved_records") is None


@pytest.mark.parametrize("top_k", [1, 2])
def test_top_k_is_honoured(top_k: int):
    services = _store(
        [
            PAST,
            {
                "record_id": "record-second",
                "content_summary": "サプリメント の 効能 表現 について 再度 差戻し",
                "ruling_id": "ruling-second",
            },
            {
                "record_id": "record-third",
                "content_summary": "サプリメント 効能 の 誇大 表現",
                "ruling_id": "ruling-third",
            },
        ]
    )

    node = ScopedRetrievalNode(
        scope_keys=("broadcaster_id", "review_desk_id"),
        backend=_backend(services),
        normalize_query=normalize_review_text,
        top_k=top_k,
    )
    result = node.execute({**SCOPE, "query": "サプリメントの効能表現"})

    assert len(result.get("retrieved_records") or []) == top_k


def test_a_review_that_barely_overlaps_recalls_nothing():
    """A brush against one term is not a recall.

    "効能" appears in the stored summary, but nothing else does. Dropping the
    minimum-hit floor makes this ruling surface on that single shared term.
    """
    services = _store([PAST])

    assert _recall(services, "本日は効能をお伝えします") == []
    assert _recall(services, "明日の天気予報をお伝えします") == []


def test_a_query_cannot_widen_its_own_match_with_like_wildcards():
    """`%` in the review text must not turn into "everything in this scope"."""
    services = _store([PAST, UNRELATED])

    assert _recall(services, "%%") == []
    assert _recall(services, "_" * 5) == []


def test_a_relevant_passage_at_the_end_of_a_long_text_still_recalls():
    """The term budget must not silently drop the part that matters.

    This text yields 100 terms against a budget of 64, and the passage that
    matters is the last sentence — taking the first N terms drops it entirely.
    """
    services = _store([PAST])
    long_text = (
        "本日の番組では全国各地の観光名所や郷土料理や伝統芸能や地域交通や気象情報や経済動向、"
        "さらに北海道と青森と秋田の話題や京都と奈良と沖縄の文化、為替と株価の解説も幅広くお届けします。"
        "最後にサプリメントの効能表現を扱います"
    )

    assert "ruling-supplement" in _recall(services, long_text)
