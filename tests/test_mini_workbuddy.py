from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from mini_workbuddy.audit import AuditLog
from mini_workbuddy.agent import MiniAgent
from mini_workbuddy.config import HarnessConfig, workspace_id
from mini_workbuddy.events import Event, EventBus
from mini_workbuddy.storage import Storage
from mini_workbuddy.tools import PermissionError, ToolRegistry


def build_runtime(home: Path, cwd: Path, threshold_kb: int = 50):
    config = HarnessConfig(root_dir=home, tool_result_threshold=threshold_kb * 1024)
    storage = Storage(config)
    events = EventBus()
    tools = ToolRegistry(config, storage)
    agent = MiniAgent(storage, tools, events)
    session = storage.create_session(str(cwd), "pytest session")
    return config, storage, events, tools, agent, session


def test_workspace_id_is_stable_and_path_safe() -> None:
    assert workspace_id("/") == "root"
    assert workspace_id("/tmp/my project") == "tmp-my_project"
    assert "/" not in workspace_id("/tmp/my project")


def test_storage_appends_and_recovers_transcript(tmp_path: Path) -> None:
    config = HarnessConfig(root_dir=tmp_path / "home")
    storage = Storage(config)
    session = storage.create_session(str(tmp_path), "storage")

    storage.append_event(session, {"type": "message", "role": "user", "content": "hello"})
    storage.append_event(session, {"type": "message", "role": "assistant", "content": "world"})

    transcript = storage.read_transcript(session)
    assert [event["content"] for event in transcript] == ["hello", "world"]
    assert storage.transcript_path(session).exists()
    assert storage.load_session(session.id).title == "storage"
    assert storage.list_sessions()[0].id == session.id


def test_storage_appends_and_reads_memory(tmp_path: Path) -> None:
    config = HarnessConfig(root_dir=tmp_path / "home")
    storage = Storage(config)

    path = storage.append_memory("workspace", "- prefer small verified steps")

    assert path.exists()
    assert "small verified steps" in storage.read_memory("workspace")


def test_audit_log_verifies_hash_chain_and_detects_tampering(tmp_path: Path) -> None:
    audit = AuditLog(HarnessConfig(root_dir=tmp_path / "home"))

    audit.append("tool_call", {"tool": "bash", "argument": "pwd"})
    audit.append("tool_result", {"tool": "bash", "exit_code": 0})

    assert audit.verify() is True

    text = audit.path.read_text(encoding="utf-8")
    audit.path.write_text(text.replace("pwd", "whoami"), encoding="utf-8")

    assert audit.verify() is False


def test_audit_log_detects_corrupt_trailing_line(tmp_path: Path) -> None:
    audit = AuditLog(HarnessConfig(root_dir=tmp_path / "home"))
    audit.append("tool_call", {"tool": "bash", "argument": "pwd"})

    with audit.path.open("a", encoding="utf-8") as fh:
        fh.write("{not-json\n")

    assert audit.verify() is False


def test_tool_search_lists_and_filters_tools(tmp_path: Path) -> None:
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path)

    all_tools = tools.run("tool_search", "", session).content
    filtered = tools.run("tool_search", "read", session).content

    assert "bash" in all_tools
    assert "read_file" in all_tools
    assert "read_file" in filtered
    assert "tool_search" not in filtered


def test_read_file_supports_relative_session_paths(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello mini harness", encoding="utf-8")
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path)

    result = tools.run("read_file", "README.md", session)

    assert result.name == "read_file"
    assert result.content == "hello mini harness"


def test_read_file_denies_paths_outside_session_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("do not read", encoding="utf-8")
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", workspace)

    with pytest.raises(PermissionError):
        tools.run("read_file", str(outside), session)

    with pytest.raises(PermissionError):
        tools.run("read_file", "../secret.txt", session)


def test_read_file_denies_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("do not read", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this filesystem")
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", workspace)

    with pytest.raises(PermissionError):
        tools.run("read_file", "linked-secret.txt", session)


def test_bash_denies_dangerous_commands(tmp_path: Path) -> None:
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path)

    with pytest.raises(PermissionError):
        tools.run("bash", "rm -rf .", session)


def test_bash_uses_workspace_scoped_environment(monkeypatch, tmp_path: Path) -> None:
    """Tool commands must not inherit ambient credentials from the harness."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/private-agent.sock")
    monkeypatch.setenv("WORKBUDDY_TEST_SECRET", "parent-only")
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path)
    command = (
        "printf '%s\\n%s\\n%s\\n%s\\n%s\\n' "
        '"${OPENAI_API_KEY-unset}" "${ANTHROPIC_API_KEY-unset}" '
        '"${SSH_AUTH_SOCK-unset}" "${WORKBUDDY_TEST_SECRET-unset}" '
        '"$HOME|$PWD|${PATH:+set}"'
    )

    result = tools.run("bash", command, session)

    assert result.content.splitlines() == [
        "unset",
        "unset",
        "unset",
        "unset",
        f"{tmp_path.resolve()}|{tmp_path.resolve()}|set",
    ]


def test_large_tool_output_externalizes_to_disk(tmp_path: Path) -> None:
    _, storage, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path, threshold_kb=1)
    command = "python3 -c \"print('x' * 5000)\""

    result = tools.run("bash", command, session)

    assert result.externalized_path is not None
    externalized = Path(result.externalized_path)
    assert externalized.exists()
    assert externalized.read_text(encoding="utf-8").startswith("x")
    assert "Full output written to:" in result.content
    assert externalized.parent == storage.tool_result_path(session, result.tool_call_id).parent


def test_tool_call_ids_are_unique_under_fast_repeated_calls(tmp_path: Path) -> None:
    _, _, _, tools, _, session = build_runtime(tmp_path / "home", tmp_path)

    ids = {tools.run("tool_search", "", session).tool_call_id for _ in range(200)}

    assert len(ids) == 200


def test_agent_records_user_tool_result_and_assistant_events(tmp_path: Path) -> None:
    _, storage, _, _, agent, session = build_runtime(tmp_path / "home", tmp_path)

    result = agent.prompt(session, "pwd")
    transcript = storage.read_transcript(session)

    assert result["toolResults"][0]["name"] == "bash"
    assert [event["type"] for event in transcript] == ["message", "tool_result", "message"]
    assert transcript[0]["role"] == "user"
    assert transcript[-1]["role"] == "assistant"


def test_agent_writes_audit_entries_when_enabled(tmp_path: Path) -> None:
    config = HarnessConfig(root_dir=tmp_path / "home")
    storage = Storage(config)
    events = EventBus()
    tools = ToolRegistry(config, storage)
    audit = AuditLog(config)
    agent = MiniAgent(storage, tools, events, audit)
    session = storage.create_session(str(tmp_path), "audit session")

    agent.prompt(session, "pwd")

    actions = [entry.action for entry in audit.read_entries()]
    assert actions == ["user_prompt", "tool_call", "tool_result", "assistant_message"]
    assert audit.verify() is True


def test_agent_unknown_prompt_returns_help_without_tool_call(tmp_path: Path) -> None:
    _, storage, _, _, agent, session = build_runtime(tmp_path / "home", tmp_path)

    result = agent.prompt(session, "hello?")
    transcript = storage.read_transcript(session)

    assert result["toolResults"] == []
    assert "Try:" in result["answer"]
    assert [event["type"] for event in transcript] == ["message", "message"]


def test_event_to_sse_is_valid_event_stream() -> None:
    event = Event("session_update", {"content": "hello"})

    payload = event.to_sse().decode("utf-8")

    assert payload.startswith("event: session_update\n")
    assert 'data: {"content": "hello"}' in payload


def test_event_bus_delivers_to_active_subscriber() -> None:
    bus = EventBus()
    delivered: queue.Queue[Event] = queue.Queue()

    def consume_one() -> None:
        subscriber = bus.subscribe()
        try:
            delivered.put(next(subscriber))
        finally:
            subscriber.close()

    thread = threading.Thread(target=consume_one)
    thread.start()
    deadline = time.time() + 2
    while not bus._subscribers and time.time() < deadline:
        time.sleep(0.01)
    assert bus._subscribers

    bus.publish("session_update", {"n": 1})

    event = delivered.get(timeout=2)
    thread.join(timeout=2)

    assert event.name == "session_update"
    assert event.data == {"n": 1}
    assert not thread.is_alive()
