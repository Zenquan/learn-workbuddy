#!/usr/bin/env python3
"""s12_cloud_memory - stored remote memory and query-scoped recall.

s10 and s11 define durable state owned by a workspace and a user.  This
chapter adds a remote/service boundary and, more importantly, separates two
objects that are often both called "memory":

* ``StoredMemory`` is a durable record with owner and source provenance.
* ``RecallHit`` is a scored candidate produced for one ``RecallQuery``.

A hit is not a second stored memory and it is not automatically trusted prompt
context.  The teaching ranker is intentionally small (lexical overlap plus a
recency feature) so query, source, score, and selection remain inspectable.  A
production provider can replace the ranker without changing those contracts.

Usage:
    python s12_cloud_memory/code.py --demo
    python s12_cloud_memory/code.py
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping


# Machine-readable learning path metadata.  The chapter preserves s11's user
# ownership boundary while distinguishing persistent evidence from a temporary
# retrieval view.
PROGRESSION = {
    "chapter": "s12_cloud_memory",
    "builds_on": ["s11_user_memory"],
    "adds": [
        "source-bearing remote memory records",
        "query-scoped recall hits",
        "explainable retrieval scores",
    ],
    "preserves": ["workspace and user memory ownership boundaries"],
}


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_workbuddy.chapter_demo import maybe_run_chapter_demo
from mini_workbuddy.chapter_demo import prepare_chapter_provider

maybe_run_chapter_demo(__file__, PROGRESSION)
prepare_chapter_provider()

from anthropic import Anthropic
from dotenv import load_dotenv
from mini_workbuddy.paths import tutorial_workbuddy_home


try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass


load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


WORKDIR = Path.cwd()
MODEL = os.environ.get("MODEL_ID")
client: Anthropic | None = None


def runtime_client() -> tuple[Anthropic, str]:
    """Create the provider client only when the online loop actually needs it.

    Storage, recall, rendering, and cross-chapter walkthroughs are pure local
    contracts.  Requiring a provider key while importing those types would
    make an otherwise keyless lesson impossible to compose or test.
    """

    global client
    model = os.environ.get("MODEL_ID") or MODEL
    if not model:
        raise RuntimeError(
            "MODEL_ID is not set. Copy .env.example to .env and fill in "
            "the provider key and MODEL_ID (see README quick start)."
        )
    if client is None:
        client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    return client, model


SCHEMA_VERSION = 1
MAX_RECALL_LIMIT = 10
MAX_RECORD_CHARS = 20_000
DEFAULT_REMOTE_ROOT = tutorial_workbuddy_home() / "remote-memory"


class RemoteMemoryError(RuntimeError):
    """Base class for remote-memory contract failures."""


class RemoteMemoryValidationError(RemoteMemoryError):
    """Raised before malformed memory or query data crosses the boundary."""


class RemoteMemoryScopeError(RemoteMemoryError):
    """Raised when a record belongs to another remote user scope."""


class RemoteMemoryCorruptionError(RemoteMemoryError):
    """Raised when a durable record cannot be decoded safely."""


class MemoryKind(str, Enum):
    """Remote records have different selection policies."""

    PROFILE = "profile"
    CONVERSATION = "conversation"


@dataclass(frozen=True)
class MemorySource:
    """Where a durable record came from, independent of its current storage."""

    source_id: str
    source_type: str
    title: str
    captured_at: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "MemorySource":
        try:
            return cls(
                source_id=str(payload["source_id"]),
                source_type=str(payload["source_type"]),
                title=str(payload.get("title", "")),
                captured_at=str(payload["captured_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteMemoryCorruptionError(f"invalid memory source: {exc}") from exc


@dataclass(frozen=True)
class StoredMemory:
    """One immutable, source-bearing record in remote durable storage."""

    memory_id: str
    user_scope: str
    kind: MemoryKind
    content: str
    summary: str
    source: MemorySource
    stored_at: str
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "StoredMemory":
        try:
            return cls(
                memory_id=str(payload["memory_id"]),
                user_scope=str(payload["user_scope"]),
                kind=MemoryKind(str(payload["kind"])),
                content=str(payload["content"]),
                summary=str(payload["summary"]),
                source=MemorySource.from_dict(dict(payload["source"])),
                stored_at=str(payload["stored_at"]),
                schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteMemoryCorruptionError(f"invalid stored memory: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


@dataclass(frozen=True)
class RecallQuery:
    """A self-contained retrieval request scoped to one user and one turn."""

    query_id: str
    text: str
    user_scope: str
    limit: int
    issued_at: str


@dataclass(frozen=True)
class RecallHit:
    """A query-dependent candidate; never a new durable memory record."""

    query_id: str
    memory_id: str
    rank: int
    snippet: str
    source: MemorySource
    score: float
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "memory_id": self.memory_id,
            "rank": self.rank,
            "snippet": self.snippet,
            "source": asdict(self.source),
            "score": self.score,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True)
class RecallResult:
    """Complete observable output of one retrieval operation."""

    query: RecallQuery
    hits: tuple[RecallHit, ...]
    searched_records: int

    def to_dict(self) -> dict[str, object]:
        return {
            "query": asdict(self.query),
            "hits": [hit.to_dict() for hit in self.hits],
            "searched_records": self.searched_records,
        }


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemoteMemoryCorruptionError(f"invalid timestamp: {value!r}") from exc
    return _utc(parsed)


def _scope_id(user_id: str) -> str:
    normalized = user_id.strip().casefold()
    if not normalized:
        raise RemoteMemoryValidationError("user_id must not be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _clean_text(value: object, *, field_name: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        raise RemoteMemoryValidationError(f"{field_name} must not be empty")
    if len(text) > max_chars:
        raise RemoteMemoryValidationError(
            f"{field_name} exceeds the {max_chars}-character teaching limit"
        )
    return text


def _validate_source(source: MemorySource) -> MemorySource:
    """Reject anonymous or temporally invalid provenance before persistence."""

    return MemorySource(
        source_id=_clean_text(source.source_id, field_name="source_id", max_chars=500),
        source_type=_clean_text(
            source.source_type, field_name="source_type", max_chars=100
        ),
        title=_clean_text(source.title, field_name="source title", max_chars=1_000),
        captured_at=_iso(_parse_timestamp(source.captured_at)),
    )


def _tokenize(text: str) -> set[str]:
    """Return deterministic lexical features for English and Chinese text.

    This is a transparent offline baseline, not a claim about a production
    retrieval stack.  English uses word tokens; consecutive Chinese text also
    contributes character bi-grams so short topical queries can overlap.
    """

    normalized = text.casefold()
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    for segment in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(segment) == 1:
            terms.add(segment)
        else:
            terms.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return terms


class RemoteMemoryStore:
    """Append-only local simulation of a user-scoped remote memory service.

    The JSONL file is durable evidence.  Recall never writes to it: search
    results are derived views that live only for one query.  A real service can
    implement the same methods over an API or database without changing the
    query/hit contract taught below.
    """

    def __init__(self, path: Path, *, user_id: str = "local-user"):
        self.path = Path(path).expanduser().resolve()
        self.user_id = user_id.strip()
        self.user_scope = _scope_id(user_id)

    def append(
        self,
        *,
        kind: MemoryKind,
        content: str,
        summary: str,
        source: MemorySource,
        memory_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredMemory:
        """Persist one immutable record and reject duplicate identifiers."""

        record = StoredMemory(
            memory_id=memory_id or uuid.uuid4().hex,
            user_scope=self.user_scope,
            kind=MemoryKind(kind),
            content=_clean_text(
                content, field_name="memory content", max_chars=MAX_RECORD_CHARS
            ),
            summary=_clean_text(summary, field_name="memory summary", max_chars=2_000),
            source=_validate_source(source),
            stored_at=_iso(stored_at),
        )
        existing_ids = {item.memory_id for item in self.read_all()}
        if record.memory_id in existing_ids:
            raise RemoteMemoryValidationError(
                f"duplicate memory_id: {record.memory_id}"
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read_all(self) -> list[StoredMemory]:
        if not self.path.exists():
            return []
        records: list[StoredMemory] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RemoteMemoryCorruptionError(
                    f"invalid JSON at line {line_number}: {exc.msg}"
                ) from exc
            record = StoredMemory.from_dict(payload)
            if record.user_scope != self.user_scope:
                raise RemoteMemoryScopeError(
                    f"memory {record.memory_id} belongs to another user scope"
                )
            records.append(record)
        if len({record.memory_id for record in records}) != len(records):
            raise RemoteMemoryCorruptionError("duplicate memory_id in durable store")
        return records

    def latest_profile(self) -> StoredMemory | None:
        """Select the newest profile snapshot without treating it as recall."""

        profiles = [record for record in self.read_all() if record.kind is MemoryKind.PROFILE]
        if not profiles:
            return None
        return max(
            profiles,
            key=lambda record: (_parse_timestamp(record.source.captured_at), record.memory_id),
        )


class RecallEngine:
    """Create ranked conversation candidates for one explicit query.

    The score is intentionally explainable:

    ``0.85 * query-term coverage + 0.15 * recency``

    There is no hidden reranker.  This lets the lesson focus on the retrieval
    boundary; a later implementation may add embeddings or reranking while
    preserving ``RecallQuery`` and ``RecallHit``.
    """

    def __init__(self, store: RemoteMemoryStore):
        self.store = store

    def recall(
        self,
        text: str,
        *,
        limit: int = 5,
        query_id: str | None = None,
        as_of: datetime | None = None,
    ) -> RecallResult:
        query_text = _clean_text(text, field_name="recall query", max_chars=2_000)
        if not 1 <= int(limit) <= MAX_RECALL_LIMIT:
            raise RemoteMemoryValidationError(
                f"limit must be between 1 and {MAX_RECALL_LIMIT}"
            )
        current = _utc(as_of)
        query = RecallQuery(
            query_id=query_id or uuid.uuid4().hex,
            text=query_text,
            user_scope=self.store.user_scope,
            limit=int(limit),
            issued_at=_iso(current),
        )
        query_terms = _tokenize(query.text)
        if not query_terms:
            raise RemoteMemoryValidationError("recall query has no searchable terms")

        candidates: list[tuple[float, datetime, StoredMemory, tuple[str, ...]]] = []
        conversation_records = [
            record
            for record in self.store.read_all()
            if record.kind is MemoryKind.CONVERSATION
        ]
        for record in conversation_records:
            record_terms = _tokenize(f"{record.summary} {record.content}")
            matched = tuple(sorted(query_terms & record_terms))
            if not matched:
                continue
            coverage = len(matched) / len(query_terms)
            captured_at = _parse_timestamp(record.source.captured_at)
            age_days = max((current - captured_at).total_seconds() / 86_400, 0.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = round(0.85 * coverage + 0.15 * recency, 6)
            candidates.append((score, captured_at, record, matched))

        # Stable secondary keys make the offline lesson deterministic when two
        # records receive the same simple score.
        candidates.sort(
            key=lambda item: (-item[0], -item[1].timestamp(), item[2].memory_id)
        )
        hits = tuple(
            RecallHit(
                query_id=query.query_id,
                memory_id=record.memory_id,
                rank=rank,
                snippet=record.summary,
                source=record.source,
                score=score,
                matched_terms=matched,
            )
            for rank, (score, _captured_at, record, matched) in enumerate(
                candidates[: query.limit], start=1
            )
        )
        return RecallResult(
            query=query,
            hits=hits,
            searched_records=len(conversation_records),
        )


def render_recall_context(result: RecallResult) -> str:
    """Render selected hits for prompt assembly without hiding provenance."""

    if not result.hits:
        return ""
    lines = [
        f'<recalled_context query_id="{html.escape(result.query.query_id)}" '
        f'query="{html.escape(result.query.text)}">'
    ]
    for hit in result.hits:
        lines.extend(
            [
                (
                    f'  <hit rank="{hit.rank}" score="{hit.score:.6f}" '
                    f'memory_id="{html.escape(hit.memory_id)}" '
                    f'source_id="{html.escape(hit.source.source_id)}">'
                ),
                f"    {html.escape(hit.snippet)}",
                "  </hit>",
            ]
        )
    lines.append("</recalled_context>")
    return "\n".join(lines)


def build_system_prompt(store: RemoteMemoryStore) -> str:
    """Inject the latest profile snapshot, not the entire remote store."""

    profile = store.latest_profile()
    profile_block = ""
    if profile:
        profile_block = (
            f'<remote_profile memory_id="{html.escape(profile.memory_id)}" '
            f'source_id="{html.escape(profile.source.source_id)}" '
            f'captured_at="{html.escape(profile.source.captured_at)}">\n'
            f"{html.escape(profile.content)}\n"
            "</remote_profile>\n\n"
        )
    return profile_block + f"""You are a coding agent at {WORKDIR}.

Remote memory rules:
- The profile above is a source-bearing stored snapshot, not user-authored s11 state.
- Call recall_history only when the current task needs earlier conversation context.
- The recall query must be self-contained because retrieval cannot see this chat.
- Recall hits are candidates for this query. Check source and score before using them.
- Do not write recall hits back as new memory merely because they were retrieved.

s10 workspace memory and s11 explicit user memory remain separate ownership layers."""


def _seed_store(store: RemoteMemoryStore, *, as_of: datetime | None = None) -> None:
    """Seed deterministic offline evidence only when the simulated service is empty."""

    if store.read_all():
        return
    current = _utc(as_of)
    store.append(
        kind=MemoryKind.PROFILE,
        memory_id="profile-001",
        content=(
            "Preferred technical languages: Python and TypeScript. "
            "Response style: concise proposals before explanation."
        ),
        summary="Remote profile snapshot",
        source=MemorySource(
            source_id="profile-snapshot-001",
            source_type="profile_snapshot",
            title="Remote profile snapshot",
            captured_at=_iso(current - timedelta(days=1)),
        ),
        stored_at=current - timedelta(days=1),
    )
    examples = [
        (
            "conversation-001",
            2,
            "learn-workbuddy architecture",
            "Designed the learn-workbuddy teaching path from agent loop to desktop harness.",
        ),
        (
            "conversation-002",
            5,
            "Electron process architecture",
            "Compared main, renderer and preload responsibilities with a sidecar boundary.",
        ),
        (
            "conversation-003",
            10,
            "layered memory design",
            "Separated workspace facts, explicit user preferences and remote recall candidates.",
        ),
        (
            "conversation-004",
            15,
            "React state decision",
            "Compared Redux, Zustand and Jotai, then selected Zustand for the project.",
        ),
        (
            "conversation-005",
            20,
            "agent loop contract",
            "Defined explicit model turns, tool dispatch and terminal stop reasons.",
        ),
        (
            "conversation-006",
            30,
            "skills deferred loading",
            "Designed skill discovery before loading complete SKILL.md instructions.",
        ),
    ]
    for memory_id, age_days, title, content in examples:
        captured_at = current - timedelta(days=age_days)
        store.append(
            kind=MemoryKind.CONVERSATION,
            memory_id=memory_id,
            content=content,
            summary=content,
            source=MemorySource(
                source_id=f"transcript-{memory_id}",
                source_type="conversation_transcript",
                title=title,
                captured_at=_iso(captured_at),
            ),
            stored_at=captured_at,
        )


def create_default_store() -> RemoteMemoryStore:
    user_id = os.getenv("WORKBUDDY_USER_ID", "local-user")
    scope = _scope_id(user_id)
    store = RemoteMemoryStore(
        DEFAULT_REMOTE_ROOT / "users" / scope / "records.jsonl", user_id=user_id
    )
    _seed_store(store)
    return store


DEFAULT_STORE: RemoteMemoryStore | None = None
DEFAULT_RECALL: RecallEngine | None = None
SYSTEM: str | None = None


def default_runtime() -> tuple[RemoteMemoryStore, RecallEngine, str]:
    """Initialize seeded interactive state on first use, never on import.

    The chapter CLI still receives the same deterministic seed data.  Library
    users can import the storage and recall types without writing to a default
    home directory merely as a side effect of inspection.
    """

    global DEFAULT_STORE, DEFAULT_RECALL, SYSTEM
    if DEFAULT_STORE is None:
        DEFAULT_STORE = create_default_store()
    if DEFAULT_RECALL is None or DEFAULT_RECALL.store is not DEFAULT_STORE:
        DEFAULT_RECALL = RecallEngine(DEFAULT_STORE)
    # Prompt assembly is a view, not durable state. Rebuild it so a profile
    # written since the previous turn cannot leave the online loop stale.
    SYSTEM = build_system_prompt(DEFAULT_STORE)
    return DEFAULT_STORE, DEFAULT_RECALL, SYSTEM


def recall_history(query: str, limit: int = 5) -> str:
    """Tool adapter returning structured query, hit, source, and score fields."""

    _store, recall, _system = default_runtime()
    result = recall.recall(query, limit=int(limit))
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def safe_path(value: str) -> Path:
    path = (WORKDIR / value).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"path escapes workspace: {value}")
    return path


def run_read(path: str) -> str:
    try:
        return safe_path(path).read_text(encoding="utf-8")[:5_000]
    except Exception as exc:
        return f"Error: {exc}"


def run_bash(command: str) -> str:
    # Permission policy is taught in s04.  Keep the standalone memory lesson
    # from demonstrating obvious host-wide destructive commands.
    if any(fragment in command for fragment in ("rm -rf /", "sudo", "shutdown", "reboot")):
        return "Error: dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: timeout (30s)"
    output = (result.stdout + result.stderr).strip()
    return output[:5_000] if output else "(no output)"


TOOLS = [
    {
        "name": "recall_history",
        "description": (
            "Search source-bearing remote conversation records. Retrieval cannot see "
            "the current chat, so query must restate the needed historical topic. "
            "Returned hits are query-scoped candidates with source and score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Self-contained description of the historical context needed.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RECALL_LIMIT,
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 file inside the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


TOOL_HANDLERS = {
    "recall_history": recall_history,
    "bash": run_bash,
    "read_file": run_read,
}


def agent_loop(messages: list[dict]) -> None:
    """Run the standard tool loop with structured recall as one tool."""

    active_client, model = runtime_client()
    _store, _recall, system = default_runtime()
    while True:
        response = active_client.messages.create(
            model=model,
            system=system,
            messages=messages,
            tools=TOOLS,
            max_tokens=8_000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            display = str(output)[:150].replace("\n", " ")
            print(f"  \033[36m> {block.name}\033[0m {display}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
        messages.append({"role": "user", "content": results})


def main() -> None:
    store, _recall, _system = default_runtime()
    print("s12: Remote Memory — stored records vs recalled context")
    print(f"user scope: {store.user_scope}")
    print(f"durable store: {store.path}")
    profile = store.latest_profile()
    if profile:
        print(
            "profile snapshot: "
            f"{profile.memory_id} from {profile.source.source_id} "
            f"at {profile.source.captured_at}"
        )
    print("Try: continue the previous layered memory design discussion")

    messages: list[dict] = []
    while True:
        try:
            query = input("s12 >> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if query.lower() in {"q", "quit", "exit"}:
            break
        if not query:
            continue
        messages.append({"role": "user", "content": query})
        agent_loop(messages)
        text = "".join(
            block.text
            for block in messages[-1]["content"]
            if getattr(block, "type", None) == "text"
        )
        print(text)


if __name__ == "__main__":
    main()
