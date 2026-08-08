"""Offline contract tests for s10's workspace log-and-distill memory."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s10():
    stub_dir = ROOT / "tests" / "stubs"
    sys.path.insert(0, str(stub_dir))
    saved_anthropic = sys.modules.pop("anthropic", None)
    old_model = os.environ.get("MODEL_ID")
    os.environ["MODEL_ID"] = "offline-test-model"
    module_name = "s10_workspace_memory_test_module"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, ROOT / "s10_workspace_memory" / "code.py"
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


def _old(day: int) -> datetime:
    return datetime(2026, 1, day, 12, tzinfo=timezone.utc)


def test_workspace_scope_isolated_and_log_is_append_only(s10, tmp_path: Path) -> None:
    first = s10.WorkspaceMemory(tmp_path / "project-a")
    second = s10.WorkspaceMemory(tmp_path / "project-b")

    first.append_daily_log("Use SQLite WAL.", kind="decision", fact_id="a1")
    first.append_daily_log("Keep paths relative.", kind="convention", fact_id="a2")
    second.append_daily_log("Use Postgres.", kind="decision", fact_id="b1")

    assert first.workspace_id != second.workspace_id
    assert [fact.fact_id for fact in first.read_daily_facts()] == ["a1", "a2"]
    assert [fact.fact_id for fact in second.read_daily_facts()] == ["b1"]
    assert first.today_log_path().read_text(encoding="utf-8").count("\n") == 2
    assert ".learn_workbuddy/memory/daily" in first.today_log_path().as_posix()


def test_distill_gate_is_explainable_idempotent_and_keeps_evidence(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log(
        "Use SQLite WAL.", kind="decision", importance=5, recorded_at=_old(1), fact_id="d1"
    )
    memory.append_daily_log(
        "Keep paths relative.", kind="convention", importance=2, recorded_at=_old(2), fact_id="c1"
    )
    memory.append_daily_log(
        " keep   paths relative. ", kind="convention", importance=2, recorded_at=_old(3), fact_id="c2"
    )
    memory.append_daily_log(
        "One-off build passed.", kind="outcome", importance=5, recorded_at=_old(4), fact_id="o1"
    )
    memory.append_daily_log(
        "Possible rare pitfall.", kind="pitfall", importance=2, recorded_at=_old(5), fact_id="p1"
    )
    report = memory.distill(
        policy=s10.DistillPolicy(minimum_age_days=30),
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    assert report == s10.DistillReport(
        scanned=5, eligible=3, created=2, updated=0, skipped=2
    )
    assert len(memory.list_logs()) == 5  # distillation never deletes evidence
    rendered = memory.read_memory_md()
    assert "Use SQLite WAL." in rendered
    assert "Keep paths relative." in rendered
    assert "One-off build passed." not in rendered
    assert memory.distill(
        policy=s10.DistillPolicy(minimum_age_days=30),
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ).created == 0
    entries = json.loads(memory.curated_file.read_text(encoding="utf-8"))["entries"]
    assert sorted(entry["occurrences"] for entry in entries) == [1, 2]


def test_atomic_replace_preserves_previous_curated_file_on_failure(
    s10, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log(
        "First decision.", kind="decision", importance=5, recorded_at=_old(1), fact_id="d1"
    )
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    memory.distill(as_of=as_of)
    previous = memory.curated_file.read_text(encoding="utf-8")
    memory.append_daily_log(
        "Second decision.", kind="decision", importance=5, recorded_at=_old(2), fact_id="d2"
    )
    real_replace = s10.os.replace

    def fail_curated_replace(source, destination) -> None:
        if Path(destination) == memory.curated_file:
            raise OSError("simulated crash before rename")
        real_replace(source, destination)

    monkeypatch.setattr(s10.os, "replace", fail_curated_replace)
    with pytest.raises(OSError, match="simulated crash"):
        memory.distill(as_of=as_of)
    assert memory.curated_file.read_text(encoding="utf-8") == previous
    assert list(memory.memory_dir.glob(".*.tmp")) == []


def test_new_process_view_recovers_facts_and_curated_context(s10, tmp_path: Path) -> None:
    root = tmp_path / "project"
    writer = s10.WorkspaceMemory(root)
    writer.append_daily_log(
        "Never store secrets in memory.",
        kind="convention",
        importance=5,
        recorded_at=_old(1),
        fact_id="safe1",
    )
    writer.distill(as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))

    recovered = s10.WorkspaceMemory(root)
    assert recovered.read_all_facts()[0].fact_id == "safe1"
    assert "Never store secrets in memory." in recovered.get_context_for_agent()

    # curated.json is canonical: a stale human-facing projection is repaired
    # before it can be injected into a replacement runtime.
    recovered.memory_file.write_text("stale projection", encoding="utf-8")
    assert "Never store secrets in memory." in recovered.read_memory_md()
    assert "stale projection" not in recovered.memory_file.read_text(encoding="utf-8")
    assert "# Recent Workspace Facts" not in recovered.get_context_for_agent(recent_limit=0)


def test_reader_ignores_only_an_unterminated_corrupt_tail(s10, tmp_path: Path) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log("Valid fact.", fact_id="valid")
    with memory.today_log_path().open("ab") as handle:
        handle.write(b'{"fact_id":')
    assert [fact.fact_id for fact in memory.read_daily_facts()] == ["valid"]
