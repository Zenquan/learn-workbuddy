#!/usr/bin/env python3
"""s10_workspace_memory - project-scoped log-and-distill memory.

Session transcripts (s09) preserve what happened in one conversation.  This
chapter adds a different contract: select durable project facts, keep their
append-only evidence, and derive a compact view that a later session can load.

The implementation is intentionally local and provider-neutral.  Distillation
uses explicit rules instead of an API call, so scope, idempotency, atomic
writes, and restart recovery can all be exercised offline.

Usage:
    python s10_workspace_memory/code.py --demo
    python s10_workspace_memory/code.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping


# Machine-readable learning path metadata.  The chapter keeps s09's
# append-only evidence principle, but memory is a selective derived view rather
# than a second conversation transcript.
PROGRESSION = {
    "chapter": "s10_workspace_memory",
    "builds_on": ["s09_jsonl_transcript"],
    "adds": [
        "workspace-scoped fact log",
        "policy-driven memory distillation",
        "atomic curated memory view",
    ],
    "preserves": ["append-only evidence and restart recovery"],
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


SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
MAX_FACT_CHARS = 2_000
MAX_CONTEXT_FACTS = 6
WORKDIR = Path.cwd()


class MemoryErrorBase(RuntimeError):
    """Base class for failures at the workspace-memory boundary."""


class MemoryScopeError(MemoryErrorBase):
    """Raised when persisted memory belongs to another workspace."""


class MemoryCorruptionError(MemoryErrorBase):
    """Raised for malformed durable records that cannot be ignored safely."""


class FactKind(str, Enum):
    """Small vocabulary that makes retention policy explicit.

    Decisions, conventions, and pitfalls are usually useful in later sessions.
    Outcomes are useful in the recent log but normally age out instead of being
    promoted to long-term memory.
    """

    DECISION = "decision"
    CONVENTION = "convention"
    PITFALL = "pitfall"
    OUTCOME = "outcome"


@dataclass(frozen=True)
class MemoryFact:
    """One immutable fact in a workspace's append-only daily log."""

    fact_id: str
    workspace_id: str
    recorded_at: str
    kind: str
    content: str
    source: str = "agent"
    importance: int = 3
    evidence: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "MemoryFact":
        try:
            return cls(
                fact_id=str(payload["fact_id"]),
                workspace_id=str(payload["workspace_id"]),
                recorded_at=str(payload["recorded_at"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                source=str(payload.get("source", "agent")),
                importance=int(payload.get("importance", 3)),
                evidence=dict(payload.get("evidence") or {}),
                schema_version=int(payload.get("schema_version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"invalid memory fact: {exc}") from exc


@dataclass(frozen=True)
class DistillPolicy:
    """Explainable gate from raw facts to curated project memory."""

    minimum_age_days: int = DEFAULT_RETENTION_DAYS
    minimum_importance: int = 4
    repeat_threshold: int = 2
    stable_kinds: frozenset[str] = frozenset(
        {FactKind.DECISION.value, FactKind.CONVENTION.value, FactKind.PITFALL.value}
    )

    def __post_init__(self) -> None:
        if self.minimum_age_days < 0:
            raise ValueError("minimum_age_days must be >= 0")
        if not 1 <= self.minimum_importance <= 5:
            raise ValueError("minimum_importance must be between 1 and 5")
        if self.repeat_threshold < 1:
            raise ValueError("repeat_threshold must be >= 1")


@dataclass
class CuratedMemoryEntry:
    """Compact long-term statement with links back to source facts."""

    key: str
    kind: str
    content: str
    first_seen: str
    last_seen: str
    evidence_ids: list[str]
    occurrences: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CuratedMemoryEntry":
        try:
            evidence_ids = [str(item) for item in payload.get("evidence_ids", [])]
            return cls(
                key=str(payload["key"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                first_seen=str(payload["first_seen"]),
                last_seen=str(payload["last_seen"]),
                evidence_ids=evidence_ids,
                occurrences=int(payload.get("occurrences", len(evidence_ids))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"invalid curated entry: {exc}") from exc


@dataclass(frozen=True)
class DistillReport:
    """Observable result returned to the scheduler, CLI, or audit layer."""

    scanned: int
    eligible: int
    created: int
    updated: int
    skipped: int


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryCorruptionError(f"invalid recorded_at timestamp: {value!r}") from exc
    return _as_utc(parsed)


def _normal_form(content: str) -> str:
    """Collapse cosmetic differences before grouping repeated facts."""

    return re.sub(r"\s+", " ", content).strip().casefold()


def _entry_key(kind: str, content: str) -> str:
    material = f"{kind}\0{_normal_form(content)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


class WorkspaceMemory:
    """Durable project memory with an append log and a derived compact view.

    Layout under each project root::

        .learn_workbuddy/memory/
        ├── daily/YYYY-MM-DD.jsonl   # immutable source facts
        ├── curated.json             # canonical compact state
        └── MEMORY.md                # human/prompt-facing derived view

    The teaching namespace deliberately avoids writing into a real product's
    state directory.  Resolving ``project_dir`` before deriving ``workspace_id``
    also prevents two relative paths to the same project from creating
    different logical scopes.
    """

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.workspace_id = hashlib.sha256(
            str(self.project_dir).encode("utf-8")
        ).hexdigest()[:16]
        self.memory_dir = self.project_dir / ".learn_workbuddy" / "memory"
        self.daily_dir = self.memory_dir / "daily"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.curated_file = self.memory_dir / "curated.json"
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    def daily_log_path(self, day: date) -> Path:
        """Return the scoped path for one UTC day's append-only fact log."""

        return self.daily_dir / f"{day.isoformat()}.jsonl"

    def today_log_path(self) -> Path:
        return self.daily_log_path(datetime.now(timezone.utc).date())

    def append_daily_log(
        self,
        content: str,
        *,
        kind: str | FactKind = FactKind.OUTCOME,
        importance: int = 3,
        source: str = "agent",
        evidence: Mapping[str, str] | None = None,
        recorded_at: datetime | None = None,
        fact_id: str | None = None,
    ) -> MemoryFact:
        """Append one validated fact as exactly one JSONL record.

        ``O_APPEND`` plus one ``os.write`` keeps concurrent writers from
        seeking to and overwriting the same offset.  The subsequent ``fsync``
        makes successful return mean the record reached the filesystem.  A
        crash may still leave a partial final line; readers ignore only that
        unterminated tail and reject corruption in the middle of a log.
        """

        text = re.sub(r"\s+", " ", content).strip()
        if not text:
            raise ValueError("memory fact content must not be empty")
        if len(text) > MAX_FACT_CHARS:
            raise ValueError(f"memory fact exceeds {MAX_FACT_CHARS} characters")
        if not 1 <= importance <= 5:
            raise ValueError("importance must be between 1 and 5")

        kind_value = kind.value if isinstance(kind, FactKind) else str(kind)
        if kind_value not in {item.value for item in FactKind}:
            raise ValueError(f"unknown fact kind: {kind_value}")

        timestamp = _as_utc(recorded_at)
        fact = MemoryFact(
            fact_id=fact_id or uuid.uuid4().hex,
            workspace_id=self.workspace_id,
            recorded_at=timestamp.isoformat().replace("+00:00", "Z"),
            kind=kind_value,
            content=text,
            source=source,
            importance=importance,
            evidence=dict(evidence or {}),
        )
        encoded = (json.dumps(asdict(fact), ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        path = self.daily_log_path(timestamp.date())
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError(f"short memory write: {written}/{len(encoded)} bytes")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return fact

    def list_logs(self) -> list[Path]:
        return sorted(self.daily_dir.glob("????-??-??.jsonl"))

    def _read_log(self, path: Path) -> list[MemoryFact]:
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        facts: list[MemoryFact] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                is_partial_tail = index == len(lines) and not line.endswith("\n")
                if is_partial_tail:
                    break
                raise MemoryCorruptionError(f"{path.name}:{index}: invalid JSON") from exc
            fact = MemoryFact.from_dict(payload)
            if fact.schema_version != SCHEMA_VERSION:
                raise MemoryCorruptionError(
                    f"{path.name}:{index}: unsupported schema {fact.schema_version}"
                )
            if fact.workspace_id != self.workspace_id:
                raise MemoryScopeError(
                    f"{path.name}:{index} belongs to workspace {fact.workspace_id}"
                )
            facts.append(fact)
        return facts

    def read_daily_facts(self, day: date | None = None) -> list[MemoryFact]:
        target = day or datetime.now(timezone.utc).date()
        return self._read_log(self.daily_log_path(target))

    def read_all_facts(self) -> list[MemoryFact]:
        facts = [fact for path in self.list_logs() for fact in self._read_log(path)]
        return sorted(facts, key=lambda item: (item.recorded_at, item.fact_id))

    def _atomic_write_text(self, path: Path, content: str) -> None:
        """Replace a complete file or leave the previous version untouched."""

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            # fsync the directory entry as well as the file contents.  Without
            # this step, a power loss may preserve the temporary file data but
            # lose the rename that made it canonical.
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_curated(self) -> list[CuratedMemoryEntry]:
        if not self.curated_file.exists():
            return []
        try:
            payload = json.loads(self.curated_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryCorruptionError("curated.json is not valid JSON") from exc
        if payload.get("workspace_id") != self.workspace_id:
            raise MemoryScopeError("curated memory belongs to another workspace")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise MemoryCorruptionError("unsupported curated memory schema")
        return [CuratedMemoryEntry.from_dict(item) for item in payload.get("entries", [])]

    def _render_memory(self, entries: Iterable[CuratedMemoryEntry]) -> str:
        groups: dict[str, list[CuratedMemoryEntry]] = {
            kind.value: [] for kind in FactKind if kind is not FactKind.OUTCOME
        }
        for entry in entries:
            groups.setdefault(entry.kind, []).append(entry)

        labels = {
            FactKind.DECISION.value: "Decisions",
            FactKind.CONVENTION.value: "Conventions",
            FactKind.PITFALL.value: "Pitfalls",
        }
        lines = [
            "# Workspace Memory",
            "",
            "Derived from append-only project facts. Edit the source log or policy, not this view.",
        ]
        for kind in (
            FactKind.DECISION.value,
            FactKind.CONVENTION.value,
            FactKind.PITFALL.value,
        ):
            items = sorted(groups.get(kind, []), key=lambda item: (item.content, item.key))
            if not items:
                continue
            lines.extend(["", f"## {labels[kind]}", ""])
            for item in items:
                lines.append(
                    f"- {item.content} "
                    f"(seen {item.occurrences}x; evidence: {len(item.evidence_ids)})"
                )
        lines.append("")
        return "\n".join(lines)

    def _save_curated(self, entries: list[CuratedMemoryEntry]) -> None:
        ordered = sorted(entries, key=lambda item: (item.kind, item.key))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "entries": [asdict(item) for item in ordered],
        }
        # curated.json is canonical.  MEMORY.md is a replaceable projection for
        # humans and prompt assembly; both are written through atomic rename.
        self._atomic_write_text(
            self.curated_file,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self._atomic_write_text(self.memory_file, self._render_memory(ordered))

    def read_memory_md(self) -> str:
        if not self.curated_file.exists():
            # A pre-migration teaching workspace may only have MEMORY.md.
            return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""

        # curated.json is canonical.  If a process stopped after committing it
        # but before refreshing MEMORY.md, the next read repairs the projection
        # instead of injecting stale memory into a new session.
        rendered = self._render_memory(self._load_curated())
        current = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else None
        if current != rendered:
            self._atomic_write_text(self.memory_file, rendered)
        return rendered

    def distill(
        self,
        *,
        policy: DistillPolicy | None = None,
        as_of: datetime | None = None,
    ) -> DistillReport:
        """Promote eligible facts without deleting their source evidence.

        A stable fact becomes long-term memory when it is old enough and is
        either explicitly important or independently repeated.  Existing
        evidence ids make reruns idempotent.  Repeated observations update the
        same deterministic entry instead of producing near-duplicate bullets.
        """

        active_policy = policy or DistillPolicy()
        cutoff = _as_utc(as_of) - timedelta(days=active_policy.minimum_age_days)
        existing = self._load_curated()
        by_key = {entry.key: entry for entry in existing}
        processed_ids = {
            evidence_id for entry in existing for evidence_id in entry.evidence_ids
        }

        aged = [
            fact
            for fact in self.read_all_facts()
            if _parse_timestamp(fact.recorded_at) < cutoff
        ]
        candidates = [
            fact
            for fact in aged
            if fact.kind in active_policy.stable_kinds and fact.fact_id not in processed_ids
        ]
        groups: dict[str, list[MemoryFact]] = {}
        for fact in candidates:
            groups.setdefault(_entry_key(fact.kind, fact.content), []).append(fact)

        created = 0
        updated = 0
        eligible = 0
        skipped = len(aged) - len(candidates)
        changed = False
        for key, facts in groups.items():
            current = by_key.get(key)
            qualifies = (
                current is not None
                or max(fact.importance for fact in facts) >= active_policy.minimum_importance
                or len(facts) >= active_policy.repeat_threshold
            )
            if not qualifies:
                skipped += len(facts)
                continue

            eligible += len(facts)
            timestamps = sorted(fact.recorded_at for fact in facts)
            new_ids = {fact.fact_id for fact in facts}
            if current is None:
                representative = sorted(
                    facts,
                    key=lambda fact: (-fact.importance, fact.recorded_at, fact.fact_id),
                )[0]
                by_key[key] = CuratedMemoryEntry(
                    key=key,
                    kind=representative.kind,
                    content=representative.content,
                    first_seen=timestamps[0],
                    last_seen=timestamps[-1],
                    evidence_ids=sorted(new_ids),
                    occurrences=len(new_ids),
                )
                created += 1
            else:
                merged_ids = sorted(set(current.evidence_ids) | new_ids)
                current.first_seen = min(current.first_seen, timestamps[0])
                current.last_seen = max(current.last_seen, timestamps[-1])
                current.evidence_ids = merged_ids
                current.occurrences = len(merged_ids)
                updated += 1
            changed = True

        if changed:
            self._save_curated(list(by_key.values()))

        return DistillReport(
            scanned=len(aged),
            eligible=eligible,
            created=created,
            updated=updated,
            skipped=skipped,
        )

    def get_context_for_agent(self, *, recent_limit: int = MAX_CONTEXT_FACTS) -> str:
        """Build a bounded prompt block from curated and recent memory."""

        parts: list[str] = []
        curated = self.read_memory_md().strip()
        if curated:
            parts.append(curated)

        recent = self.read_all_facts()[-recent_limit:] if recent_limit > 0 else []
        if recent:
            lines = ["# Recent Workspace Facts", ""]
            lines.extend(
                f"- [{fact.kind}] {fact.content} ({fact.recorded_at[:10]})"
                for fact in recent
            )
            parts.append("\n".join(lines))
        return "\n\n".join(parts) if parts else "(no workspace memory yet)"


class MemoryAwareAgent:
    """Minimal provider loop showing where workspace memory enters a harness.

    The memory store is independent of the provider.  The loop loads a bounded
    view before each turn and exposes one structured ``write_memory`` tool.
    Tool-use blocks, rather than a provider stop string, determine whether
    another tool-result round is required.
    """

    def __init__(self, cwd: Path, memory: WorkspaceMemory):
        self.cwd = Path(cwd).resolve()
        self.memory = memory
        self.client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
        self.model = os.getenv("MODEL_ID", "")
        if not self.model:
            raise SystemExit(
                "MODEL_ID is not set. Copy .env.example to .env and fill in "
                "ANTHROPIC_API_KEY and MODEL_ID (see README quick start)."
            )
        self.messages: list[dict] = []

    def _build_system(self) -> str:
        return f"""You are a coding agent working in {self.cwd}.

## Workspace memory
{self.memory.get_context_for_agent()}

Use write_memory only for durable project decisions, conventions, pitfalls,
or completed outcomes. Do not store greetings, guesses, secrets, or raw tool
output. Memory supplements the user-facing reply; it never replaces it."""

    @staticmethod
    def _build_tools() -> list[dict]:
        return [
            {
                "name": "bash",
                "description": "Run a shell command inside the workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            {
                "name": "write_memory",
                "description": "Append one durable, project-scoped memory fact.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [item.value for item in FactKind],
                        },
                        "importance": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                        },
                    },
                    "required": ["content", "kind", "importance"],
                },
            },
        ]

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        final_text = ""

        while True:
            response = self.client.messages.create(
                model=self.model,
                system=self._build_system(),
                messages=self.messages,
                tools=self._build_tools(),
                max_tokens=8_000,
            )
            self.messages.append({"role": "assistant", "content": response.content})
            final_text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            tool_blocks = [
                block
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
            ]
            if not tool_blocks:
                return final_text

            results = []
            for block in tool_blocks:
                if block.name == "write_memory":
                    fact = self.memory.append_daily_log(
                        str(block.input.get("content", "")),
                        kind=str(block.input.get("kind", FactKind.OUTCOME.value)),
                        importance=int(block.input.get("importance", 3)),
                        source="model_tool",
                    )
                    output = f"memory fact appended: {fact.fact_id}"
                elif block.name == "bash":
                    output = self._run_bash(str(block.input.get("command", "")))
                else:
                    output = f"Error: unknown tool {block.name}"
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
            self.messages.append({"role": "user", "content": results})

    def _run_bash(self, command: str) -> str:
        """Keep the inherited demo tool narrow; s04 owns permission policy."""

        dangerous = ("rm -rf /", "sudo", "shutdown", "reboot")
        if any(fragment in command for fragment in dangerous):
            return "Error: command blocked by the teaching safety guard"
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Error: timeout after 120 seconds"
        output = (result.stdout + result.stderr).strip()
        return output[:50_000] if output else "(no output)"


def _print_report(report: DistillReport) -> None:
    print(
        "distill: "
        f"scanned={report.scanned}, eligible={report.eligible}, "
        f"created={report.created}, updated={report.updated}, skipped={report.skipped}"
    )


def main() -> None:
    print("s10: Workspace Memory — append facts, distill durable knowledge")
    memory = WorkspaceMemory(WORKDIR)
    agent = MemoryAwareAgent(WORKDIR, memory)
    print(f"workspace: {memory.project_dir}")
    print(f"memory:    {memory.memory_dir}")
    print("commands: /memory /today /logs /distill /reset q")

    while True:
        try:
            query = input("s10 >> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if query.lower() in {"q", "quit", "exit"}:
            break
        if not query:
            continue
        if query == "/memory":
            print(memory.read_memory_md() or "(no curated workspace memory)")
            continue
        if query == "/today":
            facts = memory.read_daily_facts()
            for fact in facts:
                print(f"[{fact.kind}] {fact.content} ({fact.importance}/5)")
            if not facts:
                print("(no facts today)")
            continue
        if query == "/logs":
            for path in memory.list_logs():
                print(f"{path.name}: {len(memory._read_log(path))} facts")
            if not memory.list_logs():
                print("(no daily logs)")
            continue
        if query == "/distill":
            _print_report(memory.distill())
            continue
        if query == "/reset":
            agent.messages.clear()
            print("conversation reset; workspace memory retained")
            continue
        print(agent.chat(query))


if __name__ == "__main__":
    main()
