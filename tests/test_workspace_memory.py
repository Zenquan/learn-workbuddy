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


def _append_pair(
    memory,
    content: str,
    *,
    memory_key: str,
    first_day: int,
    prefix: str,
    importance: int = 4,
) -> None:
    for offset in range(2):
        memory.append_daily_log(
            content,
            kind="decision",
            importance=importance,
            memory_key=memory_key,
            recorded_at=_old(first_day + offset),
            fact_id=f"{prefix}{offset + 1}",
        )


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


def test_directory_fsync_falls_back_without_opening_directory(
    s10, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_opened(*_args, **_kwargs) -> None:
        raise AssertionError("directory fsync fallback must not call os.open")

    monkeypatch.setattr(s10, "_DIRECTORY_FSYNC_SUPPORTED", False)
    monkeypatch.setattr(s10.os, "open", fail_if_opened)

    s10._fsync_directory(tmp_path)


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


def test_keyed_fact_validates_conflict_domain_and_writes_current_schema(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")

    fact = memory.append_daily_log(
        "Use Python 3.12.",
        kind="decision",
        importance=5,
        memory_key=" Runtime.Python-Version ",
        recorded_at=_old(1),
        fact_id="runtime1",
    )

    assert fact.memory_key == "runtime.python-version"
    assert fact.schema_version == 2
    stored = json.loads(
        memory.daily_log_path(_old(1).date()).read_text(encoding="utf-8")
    )
    assert stored["memory_key"] == "runtime.python-version"
    with pytest.raises(ValueError, match="memory_key"):
        memory.append_daily_log(
            "Invalid key.",
            kind="decision",
            memory_key="Runtime Python Version",
        )

    write_memory = next(
        tool
        for tool in s10.MemoryAwareAgent._build_tools()
        if tool["name"] == "write_memory"
    )
    key_schema = write_memory["input_schema"]["properties"]["memory_key"]
    assert key_schema["pattern"] == s10.MEMORY_KEY_PATTERN.pattern
    assert "memory_key" not in write_memory["input_schema"]["required"]


def test_confirmed_new_value_supersedes_without_deleting_old_evidence(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log(
        "Use Python 3.11.",
        kind="decision",
        importance=5,
        memory_key="runtime.python-version",
        recorded_at=_old(1),
        fact_id="python-old",
    )
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    first = memory.distill(as_of=as_of)
    assert first.created == 1

    memory.append_daily_log(
        "Use Python 3.12.",
        kind="decision",
        importance=5,
        memory_key="runtime.python-version",
        recorded_at=_old(10),
        fact_id="python-new-1",
    )
    unconfirmed = memory.distill(as_of=as_of)
    assert unconfirmed.superseded == 0
    assert "Use Python 3.11." in memory.read_memory_md()
    assert "Use Python 3.12." not in memory.get_context_for_agent()

    memory.append_daily_log(
        "Use Python 3.12.",
        kind="decision",
        importance=4,
        memory_key="runtime.python-version",
        recorded_at=_old(11),
        fact_id="python-new-2",
    )
    confirmed = memory.distill(as_of=as_of)
    assert confirmed.created == 1
    assert confirmed.superseded == 1
    assert confirmed.conflicts == 0

    payload = json.loads(memory.curated_file.read_text(encoding="utf-8"))
    entries = sorted(payload["entries"], key=lambda entry: entry["revision"])
    old, active = entries
    assert payload["schema_version"] == 2
    assert old["status"] == "superseded"
    assert old["superseded_by"] == active["key"]
    assert active["status"] == "active"
    assert active["revision"] == 2
    assert active["supersedes"] == old["key"]
    assert active["evidence_ids"] == ["python-new-1", "python-new-2"]
    assert len(memory.read_all_facts()) == 3

    rendered = memory.read_memory_md()
    assert "Use Python 3.12." in rendered
    assert "Use Python 3.11." not in rendered
    assert "Use Python 3.11." not in memory.get_context_for_agent()


def test_stale_evidence_cannot_roll_back_active_revision(s10, tmp_path: Path) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log(
        "Use Python 3.11.",
        kind="decision",
        importance=5,
        memory_key="runtime.python-version",
        recorded_at=_old(1),
        fact_id="old-original",
    )
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    memory.distill(as_of=as_of)
    _append_pair(
        memory,
        "Use Python 3.12.",
        memory_key="runtime.python-version",
        first_day=10,
        prefix="new",
    )
    memory.distill(as_of=as_of)

    _append_pair(
        memory,
        "Use Python 3.11.",
        memory_key="runtime.python-version",
        first_day=5,
        prefix="late-old",
        importance=5,
    )
    report = memory.distill(as_of=as_of)

    assert report.superseded == 0
    assert report.stale == 2
    entries = memory._load_curated()
    assert len(entries) == 2
    assert next(entry for entry in entries if entry.status == "active").content == (
        "Use Python 3.12."
    )


def test_equal_strength_conflict_fails_closed_and_is_observable(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.append_daily_log(
        "Use SQLite.",
        kind="decision",
        importance=5,
        memory_key="storage.database",
        recorded_at=_old(1),
        fact_id="database-current",
    )
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    memory.distill(as_of=as_of)
    for content, prefix in (("Use Postgres.", "pg"), ("Use MySQL.", "mysql")):
        _append_pair(
            memory,
            content,
            memory_key="storage.database",
            first_day=10,
            prefix=prefix,
        )

    report = memory.distill(as_of=as_of)

    assert report.conflicts == 1
    assert report.superseded == 0
    assert report.created == 0
    assert "Use SQLite." in memory.read_memory_md()
    assert "Use Postgres." not in memory.get_context_for_agent()
    assert "Use MySQL." not in memory.get_context_for_agent()
    assert len(memory._load_curated()) == 1


def test_supersession_is_idempotent_and_recovers_in_a_new_instance(
    s10, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    writer = s10.WorkspaceMemory(root)
    writer.append_daily_log(
        "Use npm.",
        kind="decision",
        importance=5,
        memory_key="frontend.package-manager",
        recorded_at=_old(1),
        fact_id="npm",
    )
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    writer.distill(as_of=as_of)
    _append_pair(
        writer,
        "Use pnpm.",
        memory_key="frontend.package-manager",
        first_day=10,
        prefix="pnpm",
    )
    writer.distill(as_of=as_of)
    before = writer.curated_file.read_text(encoding="utf-8")

    rerun = writer.distill(as_of=as_of)
    recovered = s10.WorkspaceMemory(root)
    entries = recovered._load_curated()

    assert rerun.created == 0
    assert rerun.updated == 0
    assert rerun.superseded == 0
    assert writer.curated_file.read_text(encoding="utf-8") == before
    assert len(entries) == 2
    assert next(entry for entry in entries if entry.status == "active").revision == 2
    assert "Use pnpm." in recovered.get_context_for_agent()
    assert "Use npm." not in recovered.get_context_for_agent()


def test_schema_one_curated_state_migrates_on_next_successful_distill(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    memory.curated_file.parent.mkdir(parents=True, exist_ok=True)
    memory.curated_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": memory.workspace_id,
                "entries": [
                    {
                        "key": "legacy-entry",
                        "kind": "decision",
                        "content": "Keep the legacy decision.",
                        "first_seen": "2025-12-01T12:00:00Z",
                        "last_seen": "2025-12-01T12:00:00Z",
                        "evidence_ids": ["legacy-evidence"],
                        "occurrences": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert "Keep the legacy decision." in memory.read_memory_md()
    legacy = memory._load_curated()[0]
    assert legacy.revision == 1
    assert legacy.status == "active"
    assert legacy.memory_key is None

    memory.append_daily_log(
        "Add a new convention.",
        kind="convention",
        importance=5,
        recorded_at=_old(2),
        fact_id="new-evidence",
    )
    memory.distill(as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    migrated = json.loads(memory.curated_file.read_text(encoding="utf-8"))

    assert migrated["schema_version"] == 2
    migrated_legacy = next(
        entry for entry in migrated["entries"] if entry["key"] == "legacy-entry"
    )
    assert migrated_legacy["revision"] == 1
    assert migrated_legacy["status"] == "active"
    assert migrated_legacy["supersedes"] is None


def test_corrupt_lifecycle_with_two_active_revisions_is_rejected(
    s10, tmp_path: Path
) -> None:
    memory = s10.WorkspaceMemory(tmp_path / "project")
    base = {
        "kind": "decision",
        "first_seen": "2026-01-01T12:00:00Z",
        "last_seen": "2026-01-01T12:00:00Z",
        "occurrences": 1,
        "memory_key": "runtime.python-version",
        "status": "active",
        "supersedes": None,
        "superseded_by": None,
    }
    memory.curated_file.parent.mkdir(parents=True, exist_ok=True)
    memory.curated_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workspace_id": memory.workspace_id,
                "entries": [
                    {
                        **base,
                        "key": "revision-one",
                        "content": "Use Python 3.11.",
                        "revision": 1,
                        "evidence_ids": ["one"],
                    },
                    {
                        **base,
                        "key": "revision-two",
                        "content": "Use Python 3.12.",
                        "revision": 2,
                        "evidence_ids": ["two"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(s10.MemoryCorruptionError, match="multiple active revisions"):
        memory.read_memory_md()
