"""S24 checks that the capstone consumes earlier chapter contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s24(tmp_path_factory):
    stub = ROOT / "tests" / "stubs"
    sys.path.insert(0, str(stub))
    old_model = os.environ.get("MODEL_ID")
    old_home = os.environ.get("WORKBUDDY_HOME")
    os.environ["MODEL_ID"] = "offline-test-model"
    os.environ["WORKBUDDY_HOME"] = str(tmp_path_factory.mktemp("s24-home"))
    name = "s24_comprehensive_contract_module"
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "s24_comprehensive" / "code.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)
        sys.path.remove(str(stub))
        if old_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = old_model
        if old_home is None:
            os.environ.pop("WORKBUDDY_HOME", None)
        else:
            os.environ["WORKBUDDY_HOME"] = old_home


def test_tool_views_derive_from_one_registry(s24) -> None:
    assert set(s24.TOOL_HANDLERS) == set(s24.TOOL_REGISTRY)
    assert {tool["name"] for tool in s24.TOOLS} == set(s24.TOOL_REGISTRY)


def test_permissions_require_real_approval_and_unknown_tools_deny(s24, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert s24.check_permission("write_file") is s24.PermissionDecision.ASK
    assert "approval required" in s24.execute_tool(
        "write_file", {"path": "note.txt", "content": "x"}
    )
    assert not (tmp_path / "note.txt").exists()
    assert s24.execute_tool(
        "write_file", {"path": "note.txt", "content": "x"}, lambda _name, _input: True
    ) == "File written"
    assert s24.check_permission("new_unregistered_tool") is s24.PermissionDecision.DENY


def test_workspace_memory_scopes_jsonl_by_project(s24, tmp_path: Path) -> None:
    first = s24.Memory(tmp_path / "a")
    second = s24.Memory(tmp_path / "b")
    first.append_workspace("first fact")
    second.append_workspace("second fact")
    assert first.workspace_id != second.workspace_id
    assert "first fact" in first.get_workspace()
    assert "second fact" not in first.get_workspace()


def test_rag_memory_harness_replays_selected_context_after_restart(
    s24,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "project-note.txt").write_text("offline harness fixture", encoding="utf-8")

    result = s24.run_rag_memory_harness(tmp_path)

    assert result["ok"] is True
    assert all(result["checks"].values()), result["checks"]
    assert result["recalled_ids"] == result["selected_ids"]
    assert '<recalled_memory user_scope="' in result["context"]
    assert f'memory_id="{result["memory_id"]}"' in result["context"]
    assert 'authority="workspace_override"' in result["context"]
    assert "source_id=" in result["context"]
    assert result["context"] in result["system_prompt"]
    assert "project-note.txt" in result["tool_output"]

    # These reads happen through fresh persistence objects inside the harness,
    # not through the in-memory instances that wrote the records.
    assert result["replayed_event_types"] == [
        "message",
        "recall_result",
        "memory_context_selected",
        "function_call_result",
        "message",
    ]
    assert "scripts/verify.py" in result["restarted_workspace_memory"]
    assert result["memory_id"] in result["restarted_remote_memory_ids"]

    manifest_path = Path(result["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["checks"] == result["checks"]
    assert Path(manifest["artifacts"]["context"]).read_text(encoding="utf-8") == result["context"]


def test_keyless_multiturn_walkthrough_uses_tool_blocks(root: Path, tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("MODEL_ID", None)
    env["WORKBUDDY_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [sys.executable, "s24_comprehensive/code.py", "--walkthrough"],
        cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout
    assert "turn 1: blocks=['tool_use']" in result.stdout
    assert "tool_result call_1" in result.stdout
    assert "walkthrough complete" in result.stdout
    assert "query -> recall -> select -> context -> tool -> transcript/memory -> restart" in result.stdout
    assert "rag memory harness: OK" in result.stdout
    assert "restart: transcript=5 events" in result.stdout
