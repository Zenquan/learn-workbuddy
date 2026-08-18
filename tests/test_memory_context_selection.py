"""Offline contracts for s15's memory selection and context packing path."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s14():
    module_name = "s14_retrieval_evidence_test_module"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "s14_context_compact" / "code.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(module_name, None)


@pytest.fixture(scope="module")
def s15(tmp_path_factory: pytest.TempPathFactory):
    stub_dir = ROOT / "tests" / "stubs"
    state_root = tmp_path_factory.mktemp("s15-memory-selection-state")
    sys.path.insert(0, str(stub_dir))
    saved_anthropic = sys.modules.pop("anthropic", None)
    old_model = os.environ.get("MODEL_ID")
    old_home = os.environ.get("WORKBUDDY_HOME")
    os.environ["MODEL_ID"] = "offline-test-model"
    os.environ["WORKBUDDY_HOME"] = str(state_root)
    module_name = "s15_memory_selection_test_module"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, ROOT / "s15_prompt_assembly" / "code.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(stub_dir))
        sys.modules.pop(module_name, None)
        sys.modules.pop("anthropic", None)
        if saved_anthropic is not None:
            sys.modules["anthropic"] = saved_anthropic
        if old_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = old_model
        if old_home is None:
            os.environ.pop("WORKBUDDY_HOME", None)
        else:
            os.environ["WORKBUDDY_HOME"] = old_home


def candidate(
    s15,
    memory_id: str,
    text: str,
    *,
    score: float = 0.8,
    user_scope: str = "scope-a",
    source_rank: int = 1,
    captured_at: str = "2026-08-17T08:00:00Z",
    conflict_key: str | None = None,
):
    return s15.MemoryContextCandidate(
        memory_id=memory_id,
        text=text,
        user_scope=user_scope,
        score=score,
        source_rank=source_rank,
        provenance=s15.MemoryContextProvenance(
            source_id=f"source-{memory_id}",
            source_type="conversation",
            title=f"Memory {memory_id}",
            captured_at=captured_at,
        ),
        conflict_key=conflict_key,
    )


def decision_by_id(plan):
    return {decision.memory_id: decision for decision in plan.decisions}


def test_scope_and_confidence_are_gates_before_prompt_context(s15) -> None:
    candidates = [
        candidate(s15, "selected", "Keep the release checklist", score=0.91),
        candidate(
            s15,
            "cross-scope",
            "Private data from another user",
            score=0.99,
            user_scope="scope-b",
        ),
        candidate(s15, "low", "Weak lexical coincidence", score=0.39),
    ]

    plan = s15.select_memory_context(
        candidates,
        user_scope="scope-a",
        policy=s15.MemorySelectionPolicy(min_score=0.4),
    )
    decisions = decision_by_id(plan)

    assert plan.selected_memory_ids == ("selected",)
    assert "Private data" not in plan.context
    assert "Weak lexical" not in plan.context
    assert decisions["cross-scope"].reason is s15.MemoryDecisionReason.SCOPE_MISMATCH
    assert decisions["low"].reason is s15.MemoryDecisionReason.LOW_CONFIDENCE


def test_deduplication_and_conflict_resolution_are_stable(s15) -> None:
    winner = candidate(
        s15,
        "python-new",
        "Prefer Python for automation",
        score=0.95,
        conflict_key="preference:automation-language",
    )
    duplicate = candidate(
        s15,
        "python-copy",
        "  prefer  PYTHON for automation  ",
        score=0.80,
        source_rank=2,
        conflict_key="preference:automation-language",
    )
    conflict = candidate(
        s15,
        "typescript-old",
        "Prefer TypeScript for automation",
        score=0.70,
        source_rank=3,
        conflict_key="preference:automation-language",
    )
    independent = candidate(
        s15,
        "timezone",
        "Use Asia/Shanghai timezone",
        score=0.85,
        conflict_key="preference:timezone",
    )
    policy = s15.MemorySelectionPolicy(min_score=0.1, top_k=5)

    forward = s15.select_memory_context(
        [winner, duplicate, conflict, independent],
        user_scope="scope-a",
        policy=policy,
    )
    reverse = s15.select_memory_context(
        [independent, conflict, duplicate, winner],
        user_scope="scope-a",
        policy=policy,
    )
    decisions = decision_by_id(forward)

    assert forward.context == reverse.context
    assert forward.selected_memory_ids == ("python-new", "timezone")
    assert decisions["python-copy"].reason is s15.MemoryDecisionReason.DUPLICATE_CONTENT
    assert decisions["python-copy"].related_memory_id == "python-new"
    assert decisions["typescript-old"].reason is s15.MemoryDecisionReason.CONFLICT_LOSER
    assert decisions["typescript-old"].related_memory_id == "python-new"


def test_character_budget_skips_oversized_hit_and_backfills_until_top_k(s15) -> None:
    oversized = candidate(s15, "oversized", "X" * 1_000, score=0.99)
    second = candidate(s15, "second", "Short useful memory", score=0.90)
    third = candidate(s15, "third", "Another useful memory", score=0.80)
    fourth = candidate(s15, "fourth", "Outside top k", score=0.70)
    two_small = s15.render_memory_context("scope-a", [second, third])

    plan = s15.select_memory_context(
        [fourth, oversized, third, second],
        user_scope="scope-a",
        policy=s15.MemorySelectionPolicy(
            min_score=0.1,
            top_k=2,
            max_chars=len(two_small),
            max_tokens=10_000,
        ),
    )
    decisions = decision_by_id(plan)

    assert plan.context == two_small
    assert plan.selected_memory_ids == ("second", "third")
    assert decisions["oversized"].reason is s15.MemoryDecisionReason.CHAR_BUDGET_EXCEEDED
    assert decisions["fourth"].reason is s15.MemoryDecisionReason.TOP_K_REACHED
    assert plan.used_chars == len(plan.context)


def test_injected_token_counter_enforces_target_model_budget(s15) -> None:
    expensive = candidate(s15, "expensive", "TOKEN TOKEN TOKEN", score=0.9)
    compact = candidate(s15, "compact", "TOKEN", score=0.8)

    plan = s15.select_memory_context(
        [expensive, compact],
        user_scope="scope-a",
        policy=s15.MemorySelectionPolicy(
            min_score=0.1,
            top_k=2,
            max_chars=10_000,
            max_tokens=1,
        ),
        token_counter=lambda text: text.count("TOKEN"),
    )
    decisions = decision_by_id(plan)

    assert plan.selected_memory_ids == ("compact",)
    assert decisions["expensive"].reason is s15.MemoryDecisionReason.TOKEN_BUDGET_EXCEEDED
    assert plan.used_tokens == 1


def test_recall_adapter_preserves_scope_score_rank_and_provenance(s15) -> None:
    result = SimpleNamespace(
        query=SimpleNamespace(user_scope="scope-a"),
        hits=(
            SimpleNamespace(
                memory_id="memory-1",
                snippet="Use source-grounded answers",
                rank=2,
                scope=SimpleNamespace(user_scope="scope-a"),
                provenance=SimpleNamespace(
                    source_id="transcript-7",
                    source_type="transcript",
                    title="Confirmed preference",
                    captured_at="2026-08-16T09:00:00Z",
                ),
                score_breakdown=SimpleNamespace(total=0.87),
            ),
        ),
    )

    converted = s15.memory_candidates_from_recall(
        result,
        conflict_keys={"memory-1": "preference:grounding"},
    )

    assert converted == (
        s15.MemoryContextCandidate(
            memory_id="memory-1",
            text="Use source-grounded answers",
            user_scope="scope-a",
            score=0.87,
            source_rank=2,
            provenance=s15.MemoryContextProvenance(
                source_id="transcript-7",
                source_type="transcript",
                title="Confirmed preference",
                captured_at="2026-08-16T09:00:00Z",
            ),
            conflict_key="preference:grounding",
        ),
    )


def test_selected_memory_evidence_survives_lossy_compaction(
    s14,
    s15,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = candidate(
        s15,
        "python-new",
        "Prefer Python for automation",
        score=0.95,
        conflict_key="preference:automation-language",
    )
    loser = candidate(
        s15,
        "typescript-old",
        "Prefer TypeScript for automation",
        score=0.70,
        source_rank=2,
        conflict_key="preference:automation-language",
    )
    plan = s15.select_memory_context(
        [loser, winner],
        user_scope="scope-a",
        policy=s15.MemorySelectionPolicy(min_score=0.1),
    )
    selected_ids = set(plan.selected_memory_ids)
    selected = [item for item in (loser, winner) if item.memory_id in selected_ids]
    evidence = s14.capture_retrieval_evidence(selected)
    state = s14.DurableContextState(retrieval_evidence=evidence)
    messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"turn {index}: " + "disposable detail " * 40,
        }
        for index in range(10)
    ]
    monkeypatch.setattr(s14, "TOKEN_THRESHOLD", 1)

    result = s14.compact_context(
        messages,
        state,
        summarizer=lambda _conversation: "Old memory content was compressed.",
        verbose=False,
    )
    rendered = s14.render_durable_context(result.durable_state)

    assert result.durable_state is state
    assert result.durable_state.retrieval_evidence == evidence
    assert "conversation_summary" in result.applied_layers
    assert evidence == (
        s14.RetrievalEvidence(
            memory_id="python-new",
            source_id="source-python-new",
            source_type="conversation",
            source_title="Memory python-new",
            captured_at="2026-08-17T08:00:00Z",
            score=0.95,
            source_rank=1,
            conflict_key="preference:automation-language",
        ),
    )
    assert "source-python-new" in rendered
    assert "score=0.95" in rendered
    assert "conflict_winner=preference:automation-language" in rendered
    assert "typescript-old" not in rendered


def test_empty_input_is_explicit_and_duplicate_ids_fail_closed(s15) -> None:
    empty = s15.select_memory_context([], user_scope="scope-a")

    assert empty.context == ""
    assert empty.used_chars == 0
    assert empty.used_tokens == 0
    assert empty.decisions == ()

    duplicate = candidate(s15, "same-id", "one")
    with pytest.raises(ValueError, match="duplicate memory candidate ids: same-id"):
        s15.select_memory_context(
            [duplicate, candidate(s15, "same-id", "two")],
            user_scope="scope-a",
        )

    with pytest.raises(ValueError, match="another scope"):
        s15.render_memory_context(
            "scope-a",
            [candidate(s15, "other", "private", user_scope="scope-b")],
        )

    with pytest.raises(ValueError, match="non-negative integer"):
        s15.select_memory_context(
            [candidate(s15, "counter", "context")],
            user_scope="scope-a",
            token_counter=lambda _text: -1,
        )
