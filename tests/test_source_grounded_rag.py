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
    assert len(index["documents"]) == 4
    assert any(item["unsafe_reason"] for item in index["chunks"])
    assert report["passed"] is True
    assert len(report["case_results"]) == 4


def rag_version() -> int:
    # CLI 产物的公开格式版本保持显式，避免测试依赖导入 fixture 生命周期。
    return 1
