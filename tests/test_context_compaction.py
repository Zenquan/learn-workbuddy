"""S14 compaction must be lossy only for disposable prompt messages."""

from __future__ import annotations

import copy
import importlib.util
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s14():
    name = "s14_context_compact_test_module"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "s14_context_compact" / "code.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def _durable_state(s14):
    return s14.DurableContextState(
        facts=(
            s14.DurableFact(
                fact_id="fact-db",
                content="Workspace persistence uses SQLite WAL.",
                source_pointer="transcript:session-42:17",
                last_confirmed_at="2026-08-12T09:30:00+08:00",
            ),
        ),
        pending_items=(
            s14.PendingItem(
                item_id="pending-migration",
                description="Run the migration rollback test.",
                source_pointer="artifact:session-42:tool_result_003.txt:abc123",
                last_confirmed_at="2026-08-12T10:00:00+08:00",
            ),
        ),
        retrieval_evidence=(
            s14.RetrievalEvidence(
                memory_id="memory-db",
                source_id="transcript:session-42:17",
                source_type="transcript",
                source_title="Persistence decision",
                captured_at="2026-08-12T09:30:00+08:00",
                score=0.923456,
                source_rank=1,
                conflict_key="architecture:persistence",
            ),
        ),
    )


def _long_messages() -> list[dict]:
    messages: list[dict] = [
        {"role": "user", "content": "Keep the original request."}
    ]
    for index in range(8):
        messages.append(
            {
                "role": "assistant" if index % 2 == 0 else "user",
                "content": f"turn-{index} " + ("detail " * 80),
            }
        )
    messages.insert(
        2,
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "large-result " * 200,
                    "_read_path": "src/app.py",
                }
            ],
        },
    )
    return messages


def test_compaction_carries_durable_state_exactly_and_does_not_mutate_input(
    s14, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s14, "TOKEN_THRESHOLD", 20)
    monkeypatch.setattr(s14, "MAX_TOOL_RESULT_TOKENS", 8)
    monkeypatch.setattr(s14, "KEEP_RECENT_TURNS", 2)
    messages = _long_messages()
    original_messages = copy.deepcopy(messages)
    state = _durable_state(s14)

    result = s14.compact_context(
        messages,
        state,
        summarizer=lambda _: "Old turns compressed without owning durable facts.",
        verbose=False,
    )

    assert result.durable_state is state
    assert result.durable_state == _durable_state(s14)
    assert messages == original_messages
    assert result.messages != original_messages
    assert result.tokens_after < result.tokens_before
    assert result.applied_layers


def test_untrusted_summary_cannot_rewrite_fact_pending_source_or_confirmation(
    s14, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s14, "TOKEN_THRESHOLD", 1)
    monkeypatch.setattr(s14, "MAX_TOOL_RESULT_TOKENS", 1)
    monkeypatch.setattr(s14, "KEEP_RECENT_TURNS", 6)
    state = _durable_state(s14)
    summary_calls: list[str] = []

    def adversarial_summary(conversation: str) -> str:
        summary_calls.append(conversation)
        return "Persistence uses plain JSON. There is no pending migration work."

    result = s14.compact_context(
        _long_messages(),
        state,
        summarizer=adversarial_summary,
        verbose=False,
    )
    rendered = s14.render_durable_context(result.durable_state)

    assert summary_calls
    assert "Persistence uses plain JSON" in str(result.messages)
    assert "Workspace persistence uses SQLite WAL." in rendered
    assert "Run the migration rollback test." in rendered
    assert "transcript:session-42:17" in rendered
    assert "artifact:session-42:tool_result_003.txt:abc123" in rendered
    assert "memory-db" in rendered
    assert "score=0.923456" in rendered
    assert "conflict_winner=architecture:persistence" in rendered
    assert "2026-08-12T09:30:00+08:00" in rendered
    assert "2026-08-12T10:00:00+08:00" in rendered


def test_durable_items_are_immutable_and_require_source_and_zoned_time(s14) -> None:
    state = _durable_state(s14)

    with pytest.raises(FrozenInstanceError):
        state.facts[0].content = "Rewritten by compaction"
    with pytest.raises(ValueError, match="source_pointer"):
        s14.DurableFact("fact", "content", " ", "2026-08-12T10:00:00+08:00")
    with pytest.raises(ValueError, match="timezone"):
        s14.PendingItem("todo", "description", "source-1", "2026-08-12T10:00:00")


def test_durable_state_normalizes_mutable_collections(s14) -> None:
    fact = _durable_state(s14).facts[0]
    caller_owned_facts = [fact]

    state = s14.DurableContextState(facts=caller_owned_facts)
    caller_owned_facts.clear()

    assert state.facts == (fact,)
    assert state.pending_items == ()


def test_duplicate_durable_ids_fail_before_prompt_assembly(s14) -> None:
    fact = _durable_state(s14).facts[0]
    evidence = _durable_state(s14).retrieval_evidence[0]

    with pytest.raises(ValueError, match="fact ids"):
        s14.DurableContextState(facts=(fact, fact))
    with pytest.raises(ValueError, match="retrieval evidence memory ids"):
        s14.DurableContextState(retrieval_evidence=(evidence, evidence))


def test_retrieval_evidence_rejects_untraceable_ranking_metadata(s14) -> None:
    values = {
        "memory_id": "memory-1",
        "source_id": "transcript-1",
        "source_type": "transcript",
        "source_title": "Decision",
        "captured_at": "2026-08-18T09:00:00+08:00",
        "score": 0.8,
        "source_rank": 1,
    }

    with pytest.raises(ValueError, match="score"):
        s14.RetrievalEvidence(**{**values, "score": 1.1})
    with pytest.raises(ValueError, match="source_rank"):
        s14.RetrievalEvidence(**{**values, "source_rank": 0})
    with pytest.raises(ValueError, match="captured_at.*timezone"):
        s14.RetrievalEvidence(
            **{**values, "captured_at": "2026-08-18T09:00:00"}
        )


def test_failed_or_empty_summary_keeps_original_messages(s14) -> None:
    messages = _long_messages()

    failed, failed_saved = s14.generate_summary(
        messages,
        summarizer=lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    empty, empty_saved = s14.generate_summary(messages, summarizer=lambda _: " ")

    assert failed is messages
    assert empty is messages
    assert failed_saved == 0
    assert empty_saved == 0
