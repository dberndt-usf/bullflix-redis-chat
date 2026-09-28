"""Tests for the summarization add-on (summarize.py).

Run all:        pytest -v
Just TODO 1:    pytest -k pinned -v
Just TODO 2:    pytest -k trim -v
"""
import fakeredis
import pytest

from chat_demo import approx_tokens
from summarize import SummarizingStore

SYSTEM = "You are helpful."            # 16 characters = 4 tokens
PREFIX = "Summary of the earlier conversation:\n"


class FakeSummarizer:
    """Deterministic stand-in for a model. Records what it was asked to summarize."""

    def __init__(self):
        self.calls = []

    def __call__(self, previous, messages):
        self.calls.append((previous, [m.content for m in messages]))
        return " | ".join(([previous] if previous else []) + [m.content for m in messages])


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def summarizer():
    return FakeSummarizer()


@pytest.fixture
def store(r, summarizer):
    return SummarizingStore(r, ttl_seconds=60, summarizer=summarizer)


def add_messages(store, session_id, count):
    """Add count messages, alternating user/assistant: m0, m1, m2, ..."""
    for i in range(count):
        store.append(session_id, "user" if i % 2 == 0 else "assistant", f"m{i}")


def set_summary(store, session_id, summary):
    _, meta_key = store.keys(session_id)
    store.r.hset(meta_key, "summary", summary)


def contents(store, session_id):
    return [m.content for m in store.get_history(session_id)]


# ---- TODO 1: pinned ---------------------------------------------------------

def test_pinned_without_summary_is_just_the_system_prompt(store):
    assert store.pinned("s1", SYSTEM, 100) == [("system", SYSTEM)]


def test_pinned_summary_comes_after_the_system_prompt(store):
    set_summary(store, "s1", "Ada likes tea.")
    assert store.pinned("s1", SYSTEM, 100) == [
        ("system", SYSTEM),
        ("summary", PREFIX + "Ada likes tea."),
    ]


def test_pinned_summary_that_fits_exactly_is_included(store):
    set_summary(store, "s1", "x" * 40)        # with the prefix: 77 characters = 19 tokens
    assert approx_tokens(PREFIX + "x" * 40) == 19
    # Budget 23 = 4 (system) + 19 (summary): fits exactly.
    assert [label for label, _ in store.pinned("s1", SYSTEM, 23)] == ["system", "summary"]


def test_pinned_summary_that_does_not_fit_is_left_out(store):
    set_summary(store, "s1", "x" * 40)        # 19 tokens, but only 18 left after the system prompt
    assert store.pinned("s1", SYSTEM, 22) == [("system", SYSTEM)]


def test_pinned_summary_counts_against_the_context_budget(store):
    for i in range(6):                         # six messages of 10 tokens each
        store.append("s1", "user" if i % 2 == 0 else "assistant", f"{i}" * 40)
    set_summary(store, "s1", "x" * 40)        # 19 tokens

    # Budget 50: 4 (system) + 19 (summary) leaves 27, room for two messages.
    context = store.get_context("s1", 50, SYSTEM)
    assert context[0] == {"role": "system", "content": SYSTEM}
    assert context[1] == {"role": "system", "content": PREFIX + "x" * 40}
    assert [m["content"] for m in context[2:]] == ["4" * 40, "5" * 40]


# ---- TODO 2: summarize_and_trim ---------------------------------------------

def test_trim_leaves_short_sessions_alone(store, summarizer):
    add_messages(store, "s1", 4)
    store.summarize_and_trim("s1", keep_last=4)
    assert contents(store, "s1") == ["m0", "m1", "m2", "m3"]
    assert store.get_summary("s1") == ""
    assert summarizer.calls == []


def test_trim_keeps_only_the_newest_messages(store):
    add_messages(store, "s1", 8)
    store.summarize_and_trim("s1", keep_last=2)
    assert contents(store, "s1") == ["m6", "m7"]


def test_trim_summarizes_the_older_messages(store, summarizer):
    add_messages(store, "s1", 8)
    store.summarize_and_trim("s1", keep_last=2)
    assert summarizer.calls == [("", ["m0", "m1", "m2", "m3", "m4", "m5"])]
    assert store.get_summary("s1") == "m0 | m1 | m2 | m3 | m4 | m5"


def test_trim_builds_on_the_previous_summary(store, summarizer):
    add_messages(store, "s1", 4)
    store.summarize_and_trim("s1", keep_last=2)       # summary: m0 | m1
    store.append("s1", "user", "m4")
    store.append("s1", "assistant", "m5")
    store.summarize_and_trim("s1", keep_last=2)       # folds in m2, m3
    assert summarizer.calls[1] == ("m0 | m1", ["m2", "m3"])
    assert store.get_summary("s1") == "m0 | m1 | m2 | m3"
    assert contents(store, "s1") == ["m4", "m5"]


def test_trim_keeps_a_message_added_while_summarizing(store):
    add_messages(store, "s1", 6)

    def slow_summarizer(previous, messages):
        # Another message arrives while the "model" is still working.
        store.append("s1", "user", "late")
        return "summary"

    store.summarizer = slow_summarizer
    store.summarize_and_trim("s1", keep_last=2)
    assert contents(store, "s1") == ["m4", "m5", "late"]


def test_trim_refreshes_the_expiry_on_both_keys(store, r):
    add_messages(store, "s1", 6)
    messages_key, meta_key = store.keys("s1")
    r.expire(messages_key, 5)
    r.expire(meta_key, 5)
    store.summarize_and_trim("s1", keep_last=2)
    assert 55 < r.ttl(messages_key) <= 60
    assert 55 < r.ttl(meta_key) <= 60


def test_end_session_also_deletes_the_summary(store, r):
    add_messages(store, "s1", 6)
    store.summarize_and_trim("s1", keep_last=2)
    store.end_session("s1")
    assert r.keys("chat:s1:*") == []
