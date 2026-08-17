"""Offline contract tests for s11's user-scoped profile and preferences."""

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
def s11():
    stub_dir = ROOT / "tests" / "stubs"
    sys.path.insert(0, str(stub_dir))
    saved_anthropic = sys.modules.pop("anthropic", None)
    old_model = os.environ.get("MODEL_ID")
    os.environ["MODEL_ID"] = "offline-test-model"
    module_name = "s11_user_memory_test_module"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, ROOT / "s11_user_memory" / "code.py"
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


def _establish_identity(memory) -> None:
    memory.save_identity(
        soul="# Soul\n\nBe useful.",
        assistant_identity="# Identity\n\nName: WorkBuddy",
        profile={"name": "Alice", "call_them": "Alice"},
    )


def test_profile_patch_is_explicit_partial_and_restart_safe(s11, tmp_path: Path) -> None:
    root = tmp_path / "user-memory"
    memory = s11.UserMemory(root, user_id="alice@example.com")

    first = memory.update_profile(
        {"name": "Alice", "call_them": "Alice", "timezone": "UTC+8"}
    )
    repeated = memory.update_profile({"name": "Alice"})
    changed = memory.update_profile({"call_them": "A", "timezone": None})

    assert first.changed == ("call_them", "name", "timezone")
    assert repeated.unchanged == ("name",)
    assert changed.changed == ("call_them", "timezone")
    recovered = s11.UserMemory(root, user_id="alice@example.com")
    assert recovered.read_profile() == {"name": "Alice", "call_them": "A"}
    assert "Call them: A" in recovered.user_path.read_text(encoding="utf-8")


def test_preference_key_deduplicates_retries_and_replaces_stale_value(
    s11, tmp_path: Path
) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")

    created = memory.set_preference(
        "response.language", "Chinese", updated_at="2026-08-09T01:00:00Z"
    )
    unchanged = memory.set_preference(
        "response.language", "Chinese", updated_at="2026-08-09T02:00:00Z"
    )
    updated = memory.set_preference(
        "response.language", "English", updated_at="2026-08-09T03:00:00Z"
    )

    assert created.status is s11.WriteStatus.CREATED
    assert unchanged.status is s11.WriteStatus.UNCHANGED
    assert unchanged.revision == 1
    assert updated.status is s11.WriteStatus.UPDATED
    assert updated.previous_value == "Chinese"
    assert updated.revision == 2
    assert memory.read_memory().count("response.language") == 1
    assert "English" in memory.read_memory()
    assert "Chinese" not in memory.read_memory()


def test_two_users_share_a_root_without_sharing_state(s11, tmp_path: Path) -> None:
    root = tmp_path / "user-memory"
    alice = s11.UserMemory(root, user_id="alice")
    bob = s11.UserMemory(root, user_id="bob")

    alice.update_profile({"name": "Alice"})
    alice.set_preference("editor.indent", "tabs")
    bob.update_profile({"name": "Bob"})
    bob.set_preference("editor.indent", "spaces")

    assert alice.base_dir != bob.base_dir
    assert alice.read_profile() == {"name": "Alice"}
    assert bob.read_profile() == {"name": "Bob"}
    assert alice.list_preferences()[0].value == "tabs"
    assert bob.list_preferences()[0].value == "spaces"
    assert "Bob" not in alice.get_context_for_agent()


def test_canonical_scope_marker_rejects_cross_user_file_copy(s11, tmp_path: Path) -> None:
    root = tmp_path / "user-memory"
    alice = s11.UserMemory(root, user_id="alice")
    bob = s11.UserMemory(root, user_id="bob")
    alice.set_preference("response.detail", "concise")
    bob.preferences_path.write_text(
        alice.preferences_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(s11.UserScopeError, match="another user scope"):
        bob.list_preferences()


def test_context_contains_user_state_but_not_workspace_state(s11, tmp_path: Path) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    _establish_identity(memory)
    memory.set_preference("response.language", "Chinese")

    context = memory.get_context_for_agent()

    assert "## User profile" in context
    assert "response.language" in context
    assert "workspace" not in context.casefold()
    assert ".learn_workbuddy/memory" not in context


def test_projection_is_repaired_from_canonical_preferences(s11, tmp_path: Path) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    memory.set_preference("response.language", "Chinese")
    memory.memory_path.write_text("stale projection", encoding="utf-8")

    assert "response.language" in memory.read_memory()
    assert "stale projection" not in memory.memory_path.read_text(encoding="utf-8")


def test_preference_json_is_stable_and_addressable(s11, tmp_path: Path) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    memory.set_preference("response.detail", "concise")
    memory.set_preference("editor.indent", "tabs")

    payload = json.loads(memory.preferences_path.read_text(encoding="utf-8"))

    assert payload["user_scope"] == memory.scope_id
    assert [item["key"] for item in payload["preferences"]] == [
        "editor.indent",
        "response.detail",
    ]


def test_expired_preference_stays_canonical_but_never_reaches_prompt(
    s11, tmp_path: Path
) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    _establish_identity(memory)
    memory.set_preference(
        "response.detail",
        "verbose during onboarding",
        source="transcript",
        source_event_id="session-7:event-42",
        updated_at="2026-08-01T09:00:00+08:00",
        expires_at="2026-08-02T09:00:00+08:00",
    )
    before_expiry = datetime(2026, 8, 2, 0, 59, 59, tzinfo=timezone.utc)
    at_expiry = datetime(2026, 8, 2, 1, 0, 0, tzinfo=timezone.utc)

    preference = memory.list_preferences()[0]
    assert preference.updated_at == "2026-08-01T01:00:00Z"
    assert preference.expires_at == "2026-08-02T01:00:00Z"
    assert preference.source_event_id == "session-7:event-42"
    assert preference.status(as_of=before_expiry) is s11.PreferenceStatus.ACTIVE
    assert preference.status(as_of=at_expiry) is s11.PreferenceStatus.EXPIRED
    assert "response.detail" in memory.get_context_for_agent(as_of=before_expiry)
    assert "response.detail" not in memory.get_context_for_agent(as_of=at_expiry)

    # Time-travel reads are pure and expired records remain auditable.
    current_projection = memory.memory_path.read_text(encoding="utf-8")
    assert "response.detail" in memory.read_memory(as_of=before_expiry)
    assert memory.memory_path.read_text(encoding="utf-8") == current_projection
    payload = json.loads(memory.preferences_path.read_text(encoding="utf-8"))
    assert len(payload["preferences"]) == 1


def test_lifecycle_and_provenance_changes_are_revisioned_but_retries_are_not(
    s11, tmp_path: Path
) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    common = {
        "source": "transcript",
        "source_event_id": "event-1",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    created = memory.set_preference(
        "response.language",
        "Chinese",
        updated_at="2026-08-01T00:00:00Z",
        **common,
    )
    retry = memory.set_preference(
        "response.language",
        "Chinese",
        updated_at="2026-08-02T00:00:00Z",
        **common,
    )
    extended = memory.set_preference(
        "response.language",
        "Chinese",
        source="transcript",
        source_event_id="event-1",
        updated_at="2026-08-03T00:00:00Z",
        expires_at="2099-02-01T00:00:00Z",
    )
    new_evidence = memory.set_preference(
        "response.language",
        "Chinese",
        source="transcript",
        source_event_id="event-2",
        updated_at="2026-08-04T00:00:00Z",
        expires_at="2099-02-01T00:00:00Z",
    )

    assert created.status is s11.WriteStatus.CREATED
    assert retry.status is s11.WriteStatus.UNCHANGED
    assert retry.revision == 1
    assert extended.status is s11.WriteStatus.UPDATED
    assert extended.revision == 2
    assert new_evidence.status is s11.WriteStatus.UPDATED
    assert new_evidence.revision == 3
    recovered = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    assert recovered.list_preferences()[0].source_event_id == "event-2"

    with pytest.raises(s11.StalePreferenceUpdateError, match="newer canonical"):
        recovered.set_preference(
            "response.language",
            "English",
            source="transcript",
            source_event_id="stale-event",
            updated_at="2026-08-03T12:00:00Z",
        )
    assert recovered.list_preferences()[0].value == "Chinese"


def test_timestamps_and_source_event_ids_fail_closed(s11, tmp_path: Path) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")

    with pytest.raises(s11.UserMemoryValidationError, match="timezone"):
        memory.set_preference(
            "response.detail",
            "concise",
            updated_at="2026-08-01T00:00:00",
        )
    with pytest.raises(s11.UserMemoryValidationError, match="later than"):
        memory.set_preference(
            "response.detail",
            "concise",
            updated_at="2026-08-02T00:00:00Z",
            expires_at="2026-08-02T00:00:00Z",
        )
    with pytest.raises(s11.UserMemoryValidationError, match="source_event_id"):
        memory.set_preference(
            "response.detail",
            "concise",
            source_event_id="event id with spaces",
        )
    with pytest.raises(s11.UserMemoryValidationError, match="as_of"):
        memory.list_active_preferences(as_of=datetime(2026, 8, 1))


def test_schema_one_preferences_migrate_on_next_observable_update(
    s11, tmp_path: Path
) -> None:
    memory = s11.UserMemory(tmp_path / "user-memory", user_id="alice")
    memory.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    memory.preferences_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "user_scope": memory.scope_id,
                "preferences": [
                    {
                        "key": "response.language",
                        "value": "Chinese",
                        "source": "explicit",
                        "updated_at": "2026-08-01T00:00:00Z",
                        "revision": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    legacy = memory.list_preferences()[0]
    assert legacy.expires_at is None
    assert legacy.source_event_id is None
    memory.set_preference(
        "response.language",
        "Chinese",
        updated_at="2026-08-02T00:00:00Z",
        expires_at="2099-01-01T00:00:00Z",
        source_event_id="event-migration",
    )
    migrated = json.loads(memory.preferences_path.read_text(encoding="utf-8"))

    assert migrated["schema_version"] == 2
    record = migrated["preferences"][0]
    assert record["revision"] == 2
    assert record["expires_at"] == "2099-01-01T00:00:00Z"
    assert record["source_event_id"] == "event-migration"


def test_model_tool_can_set_expiry_but_cannot_forge_source_event_id(s11) -> None:
    tool = next(
        item
        for item in s11.IdentityAwareAgent._build_tools()
        if item["name"] == "save_user_preference"
    )
    properties = tool["input_schema"]["properties"]

    assert properties["expires_at"]["format"] == "date-time"
    assert "source_event_id" not in properties
