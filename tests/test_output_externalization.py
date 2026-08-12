"""S13 artifact evidence and context-pointer contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s13():
    name = "s13_output_externalization_test_module"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "s13_output_externalization" / "code.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def test_externalized_result_separates_body_pointer_and_reference(s13, tmp_path: Path) -> None:
    raw_output = "first line\n" + "private-body\n" * 4_000
    externalizer = s13.ToolResultExternalizer(tmp_path / "session-7")

    result = externalizer.externalize(
        raw_output,
        "bash",
        summary="Search output with repeated private rows.",
    )
    artifact = result.artifact

    assert artifact.path.read_text(encoding="utf-8") == raw_output
    assert artifact.content_sha256 == hashlib.sha256(raw_output.encode()).hexdigest()
    assert artifact.source.source_id.startswith("artifact:session-7:tool_result_001.txt:")
    assert artifact.source.source_type == "artifact"
    assert artifact.source_tool == "bash"
    assert artifact.summary == "Search output with repeated private rows."
    assert artifact.source.source_id in result.context_text
    assert str(artifact.path) in result.context_text
    assert artifact.content_sha256 in result.context_text


def test_memory_reference_never_copies_artifact_body(s13, tmp_path: Path) -> None:
    secret_body = "credential-like-output-that-must-stay-in-the-artifact" * 2_000
    externalizer = s13.ToolResultExternalizer(tmp_path / "session")
    artifact = externalizer.externalize(
        secret_body,
        "search",
        summary="Credential scan output; inspect the referenced artifact if authorized.",
    ).artifact

    memory_reference = artifact.for_memory()
    payload = memory_reference.to_dict()

    assert not hasattr(memory_reference, "content")
    assert "content" not in payload
    assert secret_body not in str(payload)
    assert payload["summary"] == artifact.summary
    assert payload["artifact_path"] == str(artifact.path)
    assert payload["source"] == artifact.source.to_dict()


def test_recreated_externalizer_does_not_overwrite_referenced_evidence(
    s13, tmp_path: Path
) -> None:
    session_dir = tmp_path / "stable-session"
    first = s13.ToolResultExternalizer(session_dir).externalize(
        "first evidence",
        "search",
        summary="First result.",
    ).artifact
    replacement = s13.ToolResultExternalizer(session_dir)
    second = replacement.externalize(
        "second evidence",
        "search",
        summary="Second result.",
    ).artifact

    assert first.path.name == "tool_result_001.txt"
    assert second.path.name == "tool_result_002.txt"
    assert first.path.read_text(encoding="utf-8") == "first evidence"
    assert second.path.read_text(encoding="utf-8") == "second evidence"
    assert first.source.source_id != second.source.source_id


def test_artifact_reads_are_bounded_to_owned_directory(s13, tmp_path: Path) -> None:
    externalizer = s13.ToolResultExternalizer(tmp_path / "session")
    outside = tmp_path / "outside.txt"
    outside.write_text("not owned by this externalizer", encoding="utf-8")

    with pytest.raises(s13.ArtifactAccessError, match="outside"):
        externalizer.read_from_disk(outside)
    with pytest.raises(ValueError, match="offset"):
        externalizer.read_from_disk(
            externalizer.tool_results_dir / "missing.txt", offset=-1
        )


def test_reference_read_fails_closed_after_artifact_tampering(s13, tmp_path: Path) -> None:
    externalizer = s13.ToolResultExternalizer(tmp_path / "session")
    artifact = externalizer.externalize(
        "trusted evidence",
        "search",
        summary="Trusted search evidence.",
    ).artifact
    artifact.path.write_text("replaced evidence", encoding="utf-8")

    with pytest.raises(s13.ArtifactIntegrityError, match="digest mismatch"):
        externalizer.read_artifact(artifact)


def test_invalid_summary_fails_before_creating_artifact(s13, tmp_path: Path) -> None:
    externalizer = s13.ToolResultExternalizer(tmp_path / "session")

    with pytest.raises(ValueError, match="summary"):
        externalizer.externalize("large output", "search", summary="  ")

    assert list(externalizer.tool_results_dir.iterdir()) == []
