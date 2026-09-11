"""Source-grounded RAG 的离线来源、索引、安全和预算契约。"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "examples" / "source_grounded_rag" / "code.py"
FIXTURES = CODE.parent / "fixtures"


@pytest.fixture(scope="module")
def rag():
    module_name = "source_grounded_rag_test_module"
    spec = importlib.util.spec_from_file_location(module_name, CODE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(module_name, None)


def _copy_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURES / "corpus", corpus)
    return corpus


def _index(rag, corpus: Path, tmp_path: Path):
    index = rag.SourceIndex(corpus, tmp_path / "state" / "source-index.json")
    report = index.sync()
    return index, report


def test_heading_aware_chunks_preserve_contiguous_source_lines(rag) -> None:
    text = "# Root\n\nintro\n\n## Child\n\nfirst paragraph\n\nsecond paragraph\n"

    chunks = rag.chunk_markdown(
        document_id="doc_example",
        source_path="guide.md",
        text=text,
        max_chars=120,
    )

    child = next(chunk for chunk in chunks if chunk.heading_path == ("Root", "Child"))
    assert child.citation.startswith("guide.md#L5-L")
    assert child.text == "\n".join(
        text.splitlines()[child.start_line - 1 : child.end_line]
    ).strip()
    assert len(child.chunk_id) == len("chk_") + 20
    assert len(child.content_hash) == 64


@pytest.mark.parametrize("opening,closing", [
    ("```python", "```"), ("~~~python", "~~~~"),
    ("   ````python", "  ````` \t"), ("~~~a`b", "~~~"),
])
def test_fenced_code_comments_do_not_change_heading_path(rag, opening, closing) -> None:
    source = f"# Root\n\n{opening}\n# Comment\nprint(1)\n{closing}\n\nAfter code.\n\n## Child\n\nDetails."
    chunks = rag.chunk_markdown(document_id="doc_test", source_path="test.md", text=source)
    assert [chunk.heading_path for chunk in chunks] == [("Root",), ("Root", "Child")]
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 8
    assert chunks[1].start_line == 10
    for chunk in chunks:
        assert chunk.text == "\n".join(source.splitlines()[chunk.start_line - 1:chunk.end_line]).strip()


@pytest.mark.parametrize("false_close", ["```", "~~~~", "```` trailing", "    ````", ""])
def test_unclosed_fence_keeps_later_headings_in_code(rag, false_close) -> None:
    source = f"# Root\n````python\n# Comment\n{false_close}\n## Still code\n"
    chunks = rag.chunk_markdown(document_id="doc_test", source_path="test.md", text=source)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ("Root",)
    assert "## Still code" in chunks[0].text


@pytest.mark.parametrize("not_opening", ["``python", "```a`b", "    ```", "text ```"])
def test_invalid_fence_does_not_hide_real_heading(rag, not_opening) -> None:
    source = f"# Root\n{not_opening}\n## Child\nDetails."
    chunks = rag.chunk_markdown(document_id="doc_test", source_path="test.md", text=source)
    assert chunks[-1].heading_path == ("Root", "Child")


def test_version_two_rebuilds_legacy_fenced_chunks(rag, tmp_path, monkeypatch) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "code.md").write_text("# Root\n```python\n# Comment\n```\n## Child\nDetails.", encoding="utf-8")
    path = tmp_path / "index.json"
    # Save actual old-style wrong sections, not merely a new index with an old label.
    with monkeypatch.context() as patch:
        patch.setattr(rag, "INDEX_VERSION", 2)
        patch.setattr(rag, "_section_ranges", lambda lines: [
            (1, 2, ("Root",)), (3, 4, ("Comment",)), (5, 6, ("Comment", "Child")),
        ])
        rag.SourceIndex(corpus, path).sync()
    index = rag.SourceIndex(corpus, path)
    assert rag.OfflineBM25Retriever(index).search("Comment").hits == ()
    assert index.sync().documents_updated == 1
    assert [chunk.heading_path for chunk in index.chunks] == [("Root",), ("Root", "Child")]
    assert json.loads(path.read_text())["version"] == 3
    assert rag.SourceIndex(corpus, path).sync().documents_unchanged == 1


def test_chunk_identity_depends_on_content_not_line_position(rag) -> None:
    original = "# Stable\n\nSame paragraph.\n"
    shifted = "\n\n# Stable\n\nSame paragraph.\n"

    first = rag.chunk_markdown(
        document_id="doc_stable", source_path="stable.md", text=original
    )
    second = rag.chunk_markdown(
        document_id="doc_stable", source_path="stable.md", text=shifted
    )

    assert first[0].text == second[0].text
    assert first[0].chunk_id == second[0].chunk_id
    assert first[0].start_line != second[0].start_line


def test_initial_and_unchanged_sync_reuse_chunks(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, first = _index(rag, corpus, tmp_path)
    original_ids = tuple(chunk.chunk_id for chunk in index.chunks)

    second = index.sync()

    assert first.documents_added == 4
    assert first.documents_unchanged == 0
    assert second.documents_added == 0
    assert second.documents_updated == 0
    assert second.documents_unchanged == 4
    assert tuple(chunk.chunk_id for chunk in index.chunks) == original_ids
    assert second.generation == first.generation + 1


@pytest.mark.parametrize("failure_point", ["write", "replace"])
@pytest.mark.parametrize("change", ["initial", "add", "update", "delete", "settings"])
def test_failed_publication_preserves_state_and_can_retry(
    rag, tmp_path: Path, monkeypatch, failure_point: str, change: str,
) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    index = rag.SourceIndex(corpus, path)
    if change != "initial":
        index.sync()
    if change == "add":
        (corpus / "aaa-new.md").write_text("# New\n\nmemory publication\n", encoding="utf-8")
    elif change == "update":
        source = corpus / "layered-memory.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nNew memory fact.\n", encoding="utf-8")
    elif change == "delete":
        (corpus / "layered-memory.md").unlink()
    elif change == "settings":
        index.max_chars = 120

    def state(current):
        # Copy mutable containers so premature mutation cannot alter the oracle.
        return (
            current.generation, dict(current.documents), current.chunks,
            [dict(item) for item in current.tombstones], current._indexed_max_chars,
        )

    previous = state(index)
    disk_before = path.read_bytes() if path.exists() else None
    temporary = path.with_suffix(".json.tmp")
    original_write = Path.write_text

    def fail_write(target, *args, **kwargs):
        if target == temporary:
            # Even a partially written temporary file must not publish live state.
            original_write(target, "{", encoding="utf-8")
            raise OSError("injected publication failure")
        return original_write(target, *args, **kwargs)

    def fail_replace(*args, **kwargs):
        raise OSError("injected publication failure")

    with monkeypatch.context() as patch:
        if failure_point == "write":
            patch.setattr(Path, "write_text", fail_write)
        else:
            patch.setattr(rag.os, "replace", fail_replace)
        # Retry under the same failure too: generation and tombstones must not drift.
        for _ in range(2):
            with pytest.raises(OSError, match="injected publication failure"):
                index.sync()
            assert state(index) == previous
            assert (path.read_bytes() if path.exists() else None) == disk_before
            reopened = rag.SourceIndex(corpus, path, max_chars=index.max_chars)
            assert state(reopened) == previous
            result = rag.OfflineBM25Retriever(index).search("memory")
            assert result == rag.OfflineBM25Retriever(reopened).search("memory")
            assert all(hit.chunk.source_path != "aaa-new.md" for hit in result.hits)

    report = index.sync()
    assert report.generation == previous[0] + 1
    assert report.documents_added == (4 if change == "initial" else int(change == "add"))
    assert report.documents_updated == (4 if change == "settings" else int(change == "update"))
    assert report.documents_deleted == int(change == "delete")
    assert report.documents_unchanged == {
        "initial": 0, "add": 4, "update": 3, "delete": 3, "settings": 0,
    }[change]
    assert len(index.tombstones) == int(change == "delete")
    assert state(rag.SourceIndex(corpus, path, max_chars=index.max_chars)) == state(index)
    assert not temporary.exists()


@pytest.mark.parametrize("old_size,new_size", [(900, 120), (120, 900)])
@pytest.mark.parametrize("restart", [False, True])
def test_changed_chunk_size_rebuilds_unchanged_documents(
    rag, tmp_path: Path, old_size: int, new_size: int, restart: bool,
) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    index = rag.SourceIndex(corpus, path, max_chars=old_size)
    index.sync()
    previous_ids = {chunk.chunk_id for chunk in index.chunks}
    if restart:
        index = rag.SourceIndex(corpus, path, max_chars=new_size)
    else:
        index.max_chars = new_size

    report = index.sync()
    fresh = rag.SourceIndex(corpus, tmp_path / "fresh.json", max_chars=new_size)
    fresh.sync()

    assert report.documents_updated == 4
    assert report.documents_unchanged == 0
    assert index.chunks == fresh.chunks
    assert {chunk.chunk_id for chunk in index.chunks} != previous_ids
    assert all(index.validate_chunk(chunk)[0] for chunk in index.chunks)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["max_chars"] == new_size
    reloaded = rag.SourceIndex(corpus, path, max_chars=new_size)
    assert reloaded.sync().documents_unchanged == 4
    assert reloaded.chunks == fresh.chunks


def test_same_chunk_size_after_restart_reuses_chunks(rag, tmp_path: Path, monkeypatch) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    index = rag.SourceIndex(corpus, path, max_chars=120)
    index.sync()
    reloaded = rag.SourceIndex(corpus, path, max_chars=120)

    def unexpected_rechunk(**kwargs):
        raise AssertionError("unchanged source and settings must reuse chunks")

    monkeypatch.setattr(rag, "chunk_markdown", unexpected_rechunk)
    assert reloaded.sync().documents_unchanged == 4
    assert reloaded.chunks == index.chunks


def test_legacy_index_rechunks_once_without_guessing_settings(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    original = rag.SourceIndex(corpus, path, max_chars=120)
    original.sync()
    # Version 1 did not save the setting; its chunks need not use the default.
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 1
    payload.pop("max_chars", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    legacy = rag.SourceIndex(corpus, path, max_chars=900)
    assert not legacy.validate_chunk(legacy.chunks[0])[0]
    report = legacy.sync()
    fresh = rag.SourceIndex(corpus, tmp_path / "fresh.json", max_chars=900)
    fresh.sync()
    assert report.documents_updated == 4
    assert legacy.chunks == fresh.chunks
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == rag.INDEX_VERSION
    assert rag.SourceIndex(corpus, path).sync().documents_unchanged == 4


def test_changed_settings_require_sync_before_retrieval(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    rag.SourceIndex(corpus, path, max_chars=900).sync()
    changed = rag.SourceIndex(corpus, path, max_chars=120)
    result = rag.OfflineBM25Retriever(changed).search("memory")
    assert result.hits == ()
    assert any("chunk settings" in reason for reason in result.rejected.values())
    changed.sync()
    assert rag.OfflineBM25Retriever(changed).search("memory").hits


@pytest.mark.parametrize("invalid", [True, 119, 120.5, "120", None])
def test_chunk_settings_are_validated_at_index_boundaries(rag, tmp_path: Path, invalid) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    with pytest.raises(rag.RagContractError, match="max_chars"):
        rag.SourceIndex(corpus, path, max_chars=invalid)
    index = rag.SourceIndex(corpus, path)
    index.sync()
    index.max_chars = invalid
    before = path.read_bytes()
    with pytest.raises(rag.RagContractError, match="max_chars"):
        index.sync()
    assert path.read_bytes() == before
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["max_chars"] = invalid
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rag.RagContractError, match="max_chars"):
        rag.SourceIndex(corpus, path)


@pytest.mark.parametrize("version", [2, 3])
def test_versioned_index_requires_saved_chunk_settings(rag, tmp_path: Path, version) -> None:
    corpus = _copy_corpus(tmp_path)
    path = tmp_path / "index.json"
    rag.SourceIndex(corpus, path).sync()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = version
    del payload["max_chars"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rag.RagContractError, match="index.max_chars"):
        rag.SourceIndex(corpus, path)


def test_changed_document_replaces_old_chunks(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, _first = _index(rag, corpus, tmp_path)
    path = corpus / "agent-harness.md"
    document_id = "doc_" + rag._sha256("agent-harness.md")[:20]
    old_ids = set(index.documents[document_id].chunk_ids)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## Recovery\n\nA denied operation can be retried after a new explicit grant.\n",
        encoding="utf-8",
    )

    report = index.sync()
    new_ids = set(index.documents[document_id].chunk_ids)

    assert report.documents_updated == 1
    assert report.documents_unchanged == 3
    assert new_ids != old_ids
    assert all(chunk.chunk_id not in old_ids for chunk in index.chunks if chunk.document_id != document_id)


def test_deleted_document_is_removed_and_tombstoned(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, _first = _index(rag, corpus, tmp_path)
    deleted_path = corpus / "layered-memory.md"
    document_id = "doc_" + rag._sha256("layered-memory.md")[:20]
    deleted_path.unlink()

    report = index.sync()

    assert report.documents_deleted == 1
    assert document_id not in index.documents
    assert all(chunk.document_id != document_id for chunk in index.chunks)
    tombstone = index.tombstones[-1]
    assert tombstone["document_id"] == document_id
    assert tombstone["source_path"] == "layered-memory.md"
    assert tombstone["deleted_generation"] == report.generation


def test_fixture_evaluation_passes_source_and_safety_metrics(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, report = _index(rag, corpus, tmp_path)
    evaluation = rag.evaluate(
        index,
        rag.OfflineBM25Retriever(index),
        rag.load_cases(FIXTURES / "cases.json"),
    )

    assert report.unsafe_chunks >= 1
    assert evaluation.passed is True
    assert evaluation.metrics.recall_at_k == 1
    assert evaluation.metrics.citation_precision == 1
    assert evaluation.metrics.stale_citation_rate == 0
    assert evaluation.metrics.negative_abstention_accuracy == 1
    assert evaluation.metrics.forbidden_source_rate == 0
    assert evaluation.metrics.unsafe_evidence_rate == 0
    assert evaluation.metrics.prompt_budget_violation_rate == 0


def test_prompt_injection_marks_the_whole_document_unsafe(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, _report = _index(rag, corpus, tmp_path)
    malicious = [
        chunk for chunk in index.chunks if chunk.source_path == "untrusted-note.md"
    ]

    result = rag.OfflineBM25Retriever(index).search(
        "retrieved evidence system prompt instructions",
        top_k=5,
        prompt_budget_chars=2400,
    )

    assert malicious
    assert all(chunk.unsafe_reason for chunk in malicious)
    assert all(chunk.chunk_id in result.rejected for chunk in malicious)
    assert "untrusted-note.md" not in {hit.chunk.source_path for hit in result.hits}
    assert "Ignore previous instructions" not in result.evidence_prompt
    assert result.evidence_prompt.startswith(rag.PROMPT_GUARD)


def test_source_change_after_indexing_fails_closed(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, _report = _index(rag, corpus, tmp_path)
    source = corpus / "rag-security.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nchanged after indexing\n",
        encoding="utf-8",
    )

    result = rag.OfflineBM25Retriever(index).search(
        "stale evidence source digest citation",
        top_k=5,
        prompt_budget_chars=2400,
    )

    stale_chunks = [chunk for chunk in index.chunks if chunk.source_path == "rag-security.md"]
    assert stale_chunks
    assert all(
        result.rejected[chunk.chunk_id] == "source document changed after indexing"
        for chunk in stale_chunks
    )
    assert "rag-security.md" not in {hit.chunk.source_path for hit in result.hits}


def test_budget_keeps_complete_evidence_blocks(rag, tmp_path: Path) -> None:
    corpus = _copy_corpus(tmp_path)
    index, _report = _index(rag, corpus, tmp_path)
    retriever = rag.OfflineBM25Retriever(index)
    roomy = retriever.search(
        "permission tool audit artifact",
        top_k=4,
        prompt_budget_chars=4000,
    )
    assert len(roomy.hits) >= 2
    first_only_budget = len(rag.PROMPT_GUARD) + 2 + len(roomy.hits[0].prompt_block)

    bounded = retriever.search(
        "permission tool audit artifact",
        top_k=4,
        prompt_budget_chars=first_only_budget,
    )

    assert len(bounded.hits) == 1
    assert bounded.prompt_chars == first_only_budget
    assert bounded.evidence_prompt.endswith("</evidence>")
    assert any(reason == "prompt budget exceeded" for reason in bounded.rejected.values())


@pytest.mark.parametrize(
    "source_path",
    ["", "../secret.md", "C:/secret.md"],
)
def test_anonymous_or_escaping_source_metadata_is_rejected(rag, source_path: str) -> None:
    payload = {
        "chunk_id": "chunk",
        "document_id": "document",
        "source_path": source_path,
        "heading_path": ["Heading"],
        "start_line": 1,
        "end_line": 1,
        "content_hash": "digest",
        "text": "evidence",
        "unsafe_reason": None,
    }

    with pytest.raises(rag.RagContractError):
        rag.SourceChunk.from_dict(payload)


def test_index_cannot_be_reused_for_another_corpus(rag, tmp_path: Path) -> None:
    first_corpus = _copy_corpus(tmp_path / "first")
    index_path = tmp_path / "shared" / "index.json"
    rag.SourceIndex(first_corpus, index_path).sync()
    second_corpus = _copy_corpus(tmp_path / "second")

    with pytest.raises(rag.RagContractError, match="corpus_root"):
        rag.SourceIndex(second_corpus, index_path)


def test_cli_writes_machine_readable_index_and_report(tmp_path: Path) -> None:
    output = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable,
            str(CODE),
            "--corpus",
            str(FIXTURES / "corpus"),
            "--cases",
            str(FIXTURES / "cases.json"),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout
    assert "RESULT: OK" in result.stdout
    index = json.loads((output / "source-index.json").read_text(encoding="utf-8"))
    report = json.loads(
        (output / "source-grounded-rag-report.json").read_text(encoding="utf-8")
    )
    assert index["version"] == rag_version()
    assert index["max_chars"] == 900
    assert len(index["documents"]) == 4
    assert any(item["unsafe_reason"] for item in index["chunks"])
    assert report["passed"] is True
    assert len(report["case_results"]) == 4


def rag_version() -> int:
    # CLI 产物的公开格式版本保持显式，避免测试依赖导入 fixture 生命周期。
    return 3
