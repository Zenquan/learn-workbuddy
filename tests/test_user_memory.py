"""Offline contract tests for s11's user-scoped profile and preferences."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
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
