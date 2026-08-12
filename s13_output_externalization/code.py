#!/usr/bin/env python3
from __future__ import annotations
"""
s13_output_externalization.py - Tool Output Externalization

The virtual memory swap mechanism applied to LLM context management.

When tool output is too large, don't put it in the context.
Write it to disk as a file, keep only a pointer + preview in context.

  Context Window (RAM)              Disk (Swap)
  ┌──────────────────┐              ┌──────────────────────┐
  │ tool_result:      │              │ tool-results/        │
  │   head 6KB        │  ──write──▶  │   tool_result_001.txt│
  │   ...             │              │   tool_result_002.txt│
  │   tail 24KB       │  ◀──read───  │   ...                │
  │   [full at: path] │              └──────────────────────┘
  └──────────────────┘
       ~30KB                            unlimited

  This is EXACTLY virtual memory paging:
    - Context window  = RAM (limited, fast, expensive)
    - tool-results/   = disk swap (unlimited, slow, cheap)
    - Pointer + preview = page table entry (small, points to data)
    - Read tool       = page fault handler (brings data back on demand)

Usage:
    python s13_output_externalization/code.py
"""

import argparse
import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Machine-readable learning path metadata. Tests enforce that every
# chapter declares what it inherits and what it adds.
PROGRESSION = {
    "chapter": "s13_output_externalization",
    "builds_on": ["s12_cloud_memory"],
    "adds": [
        "large output threshold",
        "source-bearing artifact references",
        "page-fault reads",
    ],
    "preserves": ["context budget mindset", "memory as a selective derived view"],
}

# Shared learning entrypoints: --demo is offline; --provider deepseek configures real API env.
import sys as _wb_sys
from pathlib import Path as _wb_Path
_WB_ROOT = _wb_Path(__file__).resolve().parents[1]
if str(_WB_ROOT) not in _wb_sys.path:
    _wb_sys.path.insert(0, str(_WB_ROOT))
from mini_workbuddy.chapter_demo import maybe_run_chapter_demo as _wb_maybe_run_chapter_demo
_wb_maybe_run_chapter_demo(__file__, PROGRESSION)


# ======================================================================
# Constants — teaching-scale env var names (illustrative pattern, not source-derived)
# ======================================================================

BASH_MAX_OUTPUT_LENGTH = 30_000          # chars — Bash output threshold
TOOL_RESULT_THRESHOLD_KB = 50            # KB — non-Bash tool threshold
HEAD_BYTES = 6 * 1024                    # 6KB head in pointer
TAIL_BYTES = 24 * 1024                   # 24KB tail in pointer
SUMMARY_MAX_CHARS = 240                  # bounded text suitable for later retrieval
ARTIFACT_SOURCE_TYPE = "artifact"


class ArtifactAccessError(ValueError):
    """An artifact read escaped the externalizer-owned tool-results directory."""


class ArtifactIntegrityError(RuntimeError):
    """Persisted artifact bytes no longer match the reference digest."""


@dataclass(frozen=True)
class ArtifactSource:
    """Stable provenance shared with the source contract in s09 and s12."""

    source_id: str
    source_type: str
    title: str
    captured_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactMemoryReference:
    """The only artifact-shaped value that may cross into a Memory policy.

    This value intentionally has no ``content`` field. The large body remains
    owned by the artifact file; Memory receives a bounded summary, a resolvable
    pointer, integrity metadata, and provenance for later retrieval.
    """

    summary: str
    artifact_path: str
    content_sha256: str
    source_tool: str
    source: ArtifactSource

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["source"] = self.source.to_dict()
        return payload


@dataclass(frozen=True)
class ArtifactReference:
    """Metadata for one immutable tool-result artifact, never its raw body."""

    path: Path
    summary: str
    source_tool: str
    source: ArtifactSource
    byte_size: int
    character_count: int
    content_sha256: str

    def for_memory(self) -> ArtifactMemoryReference:
        """Return a compact reference; do not duplicate the artifact content."""

        return ArtifactMemoryReference(
            summary=self.summary,
            artifact_path=str(self.path),
            content_sha256=self.content_sha256,
            source_tool=self.source_tool,
            source=self.source,
        )


@dataclass(frozen=True)
class ExternalizedToolResult:
    """Context-safe pointer text plus the artifact metadata that produced it."""

    context_text: str
    artifact: ArtifactReference


# ======================================================================
# Token estimation (same rough heuristic as s18)
# ======================================================================

def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 characters ≈ 1 token."""
    return len(text) // 4


def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens in a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("content", ""))) // 4
                else:
                    total += len(str(block)) // 4
        total += 4  # role overhead
    return total


# ======================================================================
# ToolResultExternalizer — the swap mechanism
# ======================================================================

class ToolResultExternalizer:
    """Manages tool output externalization to disk.

    This is the virtual memory swap manager:
    - should_externalize() = check if data exceeds RAM limit
    - write_to_disk()      = persist immutable artifact evidence
    - make_pointer()       = create a page table entry with provenance
    - read_from_disk()     = page fault handler (bring data back on demand)

    The artifact body, context pointer, and Memory reference are deliberately
    separate representations. Keeping one raw string for all three would either
    flood the prompt or silently copy large/sensitive tool output into Memory.
    """

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.tool_results_dir = session_dir / "tool-results"
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self.externalized: list[ExternalizedToolResult] = []

    def should_externalize(self, output: str, tool_name: str) -> bool:
        """Check if output exceeds the externalization threshold.

        Bash:     > 30000 chars  (BASH_MAX_OUTPUT_LENGTH)
        Others:   > 50KB         (CODEBUDDY_TOOL_RESULT_THRESHOLD_KB)
        """
        if tool_name == "bash":
            return len(output) > BASH_MAX_OUTPUT_LENGTH
        return len(output.encode("utf-8")) > TOOL_RESULT_THRESHOLD_KB * 1024

    def _next_artifact_path(self) -> Path:
        """Reserve a new name without overwriting evidence from an earlier run.

        A process-local counter alone is unsafe: recreating the externalizer for
        the same session would start again at 001 and invalidate old pointers.
        Exclusive creation turns the filename choice into the persistence gate.
        """

        while True:
            self._counter += 1
            candidate = self.tool_results_dir / f"tool_result_{self._counter:03d}.txt"
            try:
                candidate.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                continue
            return candidate

    def write_to_disk(self, output: str) -> Path:
        """Durably write full output once and return the reserved file path.

        Files are named: tool_result_001.txt, tool_result_002.txt, ...
        """
        file_path = self._next_artifact_path()
        with file_path.open("w", encoding="utf-8") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        return file_path

    @staticmethod
    def summarize(output: str, tool_name: str) -> str:
        """Build a deterministic offline summary without retaining the body.

        Production systems may replace this with a task-aware summarizer. The
        teaching version keeps the first meaningful line and basic shape so the
        demo stays keyless, bounded, and honest about what was actually observed.
        """

        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
        first_line = " ".join(first_line.split())
        if len(first_line) > SUMMARY_MAX_CHARS:
            first_line = first_line[: SUMMARY_MAX_CHARS - 1] + "…"
        line_count = output.count("\n") + (1 if output else 0)
        prefix = f"{tool_name} output: {line_count} line(s), {len(output)} characters"
        return f"{prefix}; starts with: {first_line}" if first_line else prefix

    @staticmethod
    def _normalize_summary(summary: str) -> str:
        normalized = " ".join(summary.split())
        if not normalized:
            raise ValueError("artifact summary must not be empty")
        if len(normalized) > SUMMARY_MAX_CHARS:
            raise ValueError(f"artifact summary exceeds {SUMMARY_MAX_CHARS} characters")
        return normalized

    def _artifact_reference(
        self,
        *,
        output: str,
        file_path: Path,
        tool_name: str,
        summary: str | None,
    ) -> ArtifactReference:
        """Describe persisted evidence with a stable ID and integrity digest."""

        normalized_summary = self._normalize_summary(
            summary if summary is not None else self.summarize(output, tool_name)
        )

        encoded = output.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        captured_at = datetime.now(timezone.utc).isoformat()
        artifact_id = (
            f"artifact:{self.session_dir.name}:{file_path.name}:{digest[:12]}"
        )
        source = ArtifactSource(
            source_id=artifact_id,
            source_type=ARTIFACT_SOURCE_TYPE,
            title=f"{tool_name} output {file_path.name}",
            captured_at=captured_at,
        )
        return ArtifactReference(
            path=file_path,
            summary=normalized_summary,
            source_tool=tool_name,
            source=source,
            byte_size=len(encoded),
            character_count=len(output),
            content_sha256=digest,
        )

    def make_pointer(self, output: str, artifact: ArtifactReference) -> str:
        """Create a bounded context pointer without changing evidence ownership.

        Bash tools:    head 6KB + tail 24KB (key info at both ends)
        Other tools:   2KB preview + file path (placeholder strategy)

        Why head + tail for Bash (not just head)?
        - head: command echo, environment, first results
        - tail: error summary, exit status, final conclusion
        - middle: usually repetitive data (logs), safe to omit
        """
        size = len(output)
        header = (
            f"[Artifact: {artifact.source.source_id}]\n"
            f"Summary: {artifact.summary}\n"
            f"Source: {artifact.source_tool}; SHA-256: {artifact.content_sha256}\n"
            f"Full output: {artifact.path}\n"
        )

        if artifact.source_tool == "bash":
            head = output[:HEAD_BYTES]
            tail = output[-TAIL_BYTES:]
            omitted = max(size - HEAD_BYTES - TAIL_BYTES, 0)
            return (
                f"{header}\n{head}\n"
                f"\n... [{omitted} characters omitted, "
                f"full output at: {artifact.path}] ...\n"
                f"\n{tail}"
            )
        preview = output[:2048]
        return (
            f"{header}\n"
            f"Preview ({len(preview)} chars):\n{preview}\n"
            "Use Read tool to access full content."
        )

    def _owned_path(self, file_path: Path) -> Path:
        """Resolve only files beneath this session's artifact directory."""

        owned_root = self.tool_results_dir.resolve()
        candidate = file_path.resolve()
        if candidate.parent != owned_root:
            raise ArtifactAccessError(f"artifact is outside {owned_root}")
        return candidate

    @staticmethod
    def _render_line_range(
        content: str,
        *,
        file_name: str,
        offset: int,
        limit: int,
    ) -> str:
        """Render a bounded line window after ownership/integrity checks."""

        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        lines = content.split("\n")
        end = min(offset + limit, len(lines))
        selected = lines[offset:end]
        header = f"[reading {file_name}, lines {offset+1}-{end} of {len(lines)}]\n"
        return header + "\n".join(selected)

    def read_from_disk(self, file_path: Path, offset: int = 0, limit: int = 2000) -> str:
        """Page fault handler — bring data back from disk on demand.

        Agent calls this when it needs the full output that was externalized.
        Returns a specific line range to avoid re-flooding the context.
        """
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        owned_path = self._owned_path(file_path)
        content = owned_path.read_text(encoding="utf-8")
        return self._render_line_range(
            content,
            file_name=owned_path.name,
            offset=offset,
            limit=limit,
        )

    def read_artifact(
        self,
        artifact: ArtifactReference,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read through a reference and fail closed if evidence was replaced."""

        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        owned_path = self._owned_path(artifact.path)
        encoded = owned_path.read_bytes()
        actual_digest = hashlib.sha256(encoded).hexdigest()
        if actual_digest != artifact.content_sha256:
            raise ArtifactIntegrityError(
                f"artifact digest mismatch for {artifact.source.source_id}"
            )
        content = encoded.decode("utf-8")
        return self._render_line_range(
            content,
            file_name=owned_path.name,
            offset=offset,
            limit=limit,
        )

    def externalize(
        self,
        output: str,
        tool_name: str,
        *,
        summary: str | None = None,
    ) -> ExternalizedToolResult:
        """Persist evidence and return its context-safe representation.

        The returned metadata is sufficient for a later Memory policy to keep a
        searchable reference, but never carries the raw output body itself.
        """
        normalized_summary = self._normalize_summary(
            summary if summary is not None else self.summarize(output, tool_name)
        )
        file_path = self.write_to_disk(output)
        artifact = self._artifact_reference(
            output=output,
            file_path=file_path,
            tool_name=tool_name,
            summary=normalized_summary,
        )
        pointer = self.make_pointer(output, artifact)

        original_kb = artifact.byte_size / 1024
        pointer_kb = len(pointer.encode("utf-8")) / 1024
        saved_pct = (1 - len(pointer) / len(output)) * 100 if output else 0

        print(f"\033[33m[externalize] {file_path.name} written, "
              f"{original_kb:.1f}KB → {pointer_kb:.1f}KB in context "
              f"(saved {saved_pct:.1f}%)\033[0m")

        result = ExternalizedToolResult(context_text=pointer, artifact=artifact)
        self.externalized.append(result)
        return result


# ======================================================================
# Mock tools — simulate large outputs
# ======================================================================

def mock_grep_large() -> str:
    """Simulate a grep command that produces ~1.3MB of output."""
    lines = []
    for i in range(20000):
        if i == 15999:
            lines.append(f"src/critical.py:42:TODO: fix security vulnerability before release")
        elif i == 16000:
            lines.append(f"src/critical.py:88:TODO: add input validation for user data")
        else:
            lines.append(f"src/file_{i:05d}.py:{i}:TODO: refactor function_{i}")
    return "\n".join(lines)


def mock_grep_small() -> str:
    """Simulate a small grep command — no externalization needed."""
    return "src/main.py:10:TODO: add error handling\nsrc/utils.py:5:TODO: refactor"


def run_tool(tool_name: str, tool_input: dict) -> str:
    """Mock tool dispatcher."""
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        if "grep" in cmd and "--large" in cmd:
            return mock_grep_large()
        return mock_grep_small()
    if tool_name == "read":
        # This is the page fault handler — handled by externalizer
        return "[read tool handled separately]"
    return f"(unknown tool: {tool_name})"


# ======================================================================
# Mock LLM — scripted agent behavior
# ======================================================================

class MockLLM:
    """Simulates LLM responses with a predefined script.

    The mock LLM demonstrates:
    1. Calling a tool that produces large output
    2. Seeing the externalized pointer
    3. Deciding to "page fault" — read full output from disk
    4. Reading specific lines from the externalized file
    5. Producing a final answer
    """

    def __init__(self):
        self.turn = 0
        self.script = [
            # Turn 0: LLM calls bash with a grep that produces huge output
            {
                "type": "tool_use",
                "name": "bash",
                "input": {"command": "grep -r 'TODO' . --large"},
                "thought": "Let me search for all TODO comments in the codebase.",
            },
            # Turn 1: LLM sees externalized pointer, needs the middle part
            {
                "type": "tool_use",
                "name": "read",
                "input": {"offset": 15998, "limit": 5},
                "thought": (
                    "I see the output was externalized. The tail shows file_19995.py, "
                    "but I need to check around line 16000 for critical.py. "
                    "Triggering a page fault to read that section."
                ),
            },
            # Turn 2: LLM has the critical info, produces final answer
            {
                "type": "text",
                "text": (
                    "Found 2 critical TODOs in src/critical.py:\n"
                    "  Line 42: fix security vulnerability before release\n"
                    "  Line 88: add input validation for user data\n"
                    "These should be addressed before the next release.\n"
                    "(Also found 19998 other TODOs, see tool_result_001.txt for full list)"
                ),
            },
        ]

    def respond(self, messages: list) -> dict:
        """Return the next scripted response."""
        if self.turn >= len(self.script):
            return {"type": "text", "text": "(done)"}

        action = self.script[self.turn]
        self.turn += 1

        if action["type"] == "tool_use":
            print(f"\033[90m[llm] {action['thought']}\033[0m")
            print(f"\033[36m[llm] calling {action['name']}({action['input']})\033[0m")
        else:
            print(f"\033[90m[llm] {action['thought'] if 'thought' in action else 'producing final answer'}\033[0m")

        return action


# ======================================================================
# Agent loop with externalization
# ======================================================================

def agent_loop(
    messages: list,
    llm: MockLLM,
    externalizer: ToolResultExternalizer,
) -> list:
    """Agent loop with tool output externalization.

    After each tool execution, check if the output should be externalized.
    If yes: write to disk, replace context content with pointer.
    If no:  put the full output in context as-is.
    """
    while True:
        tokens_before = estimate_messages_tokens(messages)
        print(f"\033[90m[tokens: {tokens_before:,}]\033[0m")

        # LLM responds
        action = llm.respond(messages)

        if action["type"] == "text":
            # Model is done — append final text and exit
            messages.append({"role": "assistant", "content": action["text"]})
            print(f"\n\033[32m{action['text']}\033[0m")
            return messages

        if action["type"] == "tool_use":
            tool_name = action["name"]
            tool_input = action["input"]

            # Page fault: read from externalized disk file
            if tool_name == "read":
                if not externalizer.externalized:
                    raise ArtifactAccessError("no externalized artifact is available to read")
                artifact = externalizer.externalized[-1].artifact
                offset = tool_input.get("offset", 0)
                limit = tool_input.get("limit", 2000)

                print(f"\033[33m[page-fault] agent requested full output, "
                      f"reading {artifact.path.name} from disk "
                      f"(lines {offset+1}-{offset+limit})\033[0m")

                output = externalizer.read_artifact(artifact, offset, limit)

                # Tool result goes into context
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "name": "read", "input": tool_input}
                ]})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "content": output}
                ]})
                continue

            # Normal tool execution
            raw_output = run_tool(tool_name, tool_input)

            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "name": tool_name, "input": tool_input}
            ]})

            # --- The key step: check if externalization is needed ---
            if externalizer.should_externalize(raw_output, tool_name):
                externalized = externalizer.externalize(raw_output, tool_name)
                tool_result_content = externalized.context_text
            else:
                tool_result_content = raw_output

            messages.append({"role": "user", "content": [
                {"type": "tool_result", "content": tool_result_content}
            ]})


# ======================================================================
# Main — demonstrate the full flow
# ======================================================================

def interactive():
    """Interactive shell for trying output externalization and page-fault reads."""
    session_dir = Path(tempfile.mkdtemp(prefix="workbuddy_session_interactive_"))
    externalizer = ToolResultExternalizer(session_dir)
    print("s13: Output Externalization Interactive")
    print(f"Tool results: {externalizer.tool_results_dir}")
    print("Commands:")
    print("  small")
    print("  large")
    print("  read <offset> <limit>")
    print("  summary")
    print("  q")
    last_pointer = ""
    while True:
        try:
            line = input("s13 >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line or line.lower() in {"q", "quit", "exit"}:
            return
        if line == "small":
            output = mock_grep_small()
            if externalizer.should_externalize(output, "bash"):
                print(externalizer.externalize(output, "bash").context_text)
            else:
                print(output)
            continue
        if line == "large":
            output = mock_grep_large()
            last_pointer = externalizer.externalize(output, "bash").context_text
            print(last_pointer[:600] + "\n...[pointer truncated in console]...")
            continue
        if line.startswith("read "):
            parts = line.split()
            offset = int(parts[1]) if len(parts) > 1 else 0
            limit = int(parts[2]) if len(parts) > 2 else 20
            if not externalizer.externalized:
                print("No externalized file yet. Run: large")
                continue
            artifact = externalizer.externalized[-1].artifact
            print(externalizer.read_artifact(artifact, offset=offset, limit=limit))
            continue
        if line == "summary":
            print(f"Externalized files: {len(externalizer.externalized)}")
            for result in externalizer.externalized:
                artifact = result.artifact
                print(
                    f"  - {artifact.path} "
                    f"({artifact.character_count} chars -> "
                    f"{len(result.context_text)} pointer chars)"
                )
            if last_pointer:
                print(f"Last pointer size: {len(last_pointer)} chars")
            continue
        print("Unknown command. Use: small | large | read <offset> <limit> | summary | q")


def main():
    print("=" * 65)
    print("s13: Tool Output Externalization — 内存不够, 换到磁盘")
    print("=" * 65)
    print(f"\033[90m  Bash threshold:  {BASH_MAX_OUTPUT_LENGTH:,} chars\033[0m")
    print(f"\033[90m  Other threshold: {TOOL_RESULT_THRESHOLD_KB}KB\033[0m")
    print(f"\033[90m  Pointer format:  head {HEAD_BYTES//1024}KB + tail {TAIL_BYTES//1024}KB\033[0m")
    print()

    # Setup session directory (temp dir for demo)
    session_dir = Path(tempfile.mkdtemp(prefix="workbuddy_session_"))
    externalizer = ToolResultExternalizer(session_dir)
    llm = MockLLM()

    print(f"\033[90m  Session dir: {session_dir}\033[0m")
    print(f"\033[90m  Tool results: {externalizer.tool_results_dir}\033[0m")
    print()

    # --- Run the agent ---
    messages = [
        {"role": "user", "content": "Find all TODO comments in the codebase, highlight critical ones."}
    ]

    print("-" * 65)
    print("Turn 1: Agent calls grep (large output expected)")
    print("-" * 65)

    messages = agent_loop(messages, llm, externalizer)

    # --- Summary ---
    print("\n" + "=" * 65)
    print("Externalization Summary")
    print("=" * 65)

    for result in externalizer.externalized:
        artifact = result.artifact
        original_kb = artifact.byte_size / 1024
        pointer_kb = len(result.context_text.encode("utf-8")) / 1024
        saved_pct = (1 - len(result.context_text) / artifact.character_count) * 100
        print(f"  {artifact.path.name}: "
              f"{original_kb:.1f}KB → {pointer_kb:.1f}KB "
              f"(saved {saved_pct:.1f}%)")
        print(f"    source: {artifact.source.source_id}")
        print(f"    summary: {artifact.summary}")

    final_tokens = estimate_messages_tokens(messages)
    print(f"\n  Final context size: {final_tokens:,} tokens (~{final_tokens*4:,} chars)")

    if externalizer.externalized:
        worst = max(
            externalizer.externalized,
            key=lambda result: result.artifact.character_count,
        )
        print(f"  Without externalization: ~{worst.artifact.character_count//4:,} tokens "
              f"for that one tool call alone")

    # Show the pointer that's in the context
    print("\n" + "-" * 65)
    print("What the agent sees in context (pointer):")
    print("-" * 65)
    for msg in messages:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block["content"]
                    # Show first 200 and last 200 chars of the pointer
                    if len(content) > 500:
                        print(content[:200])
                        print(f"  ... ({len(content)} chars total in pointer) ...")
                        print(content[-200:])
                    else:
                        print(content)

    # Show the disk files
    print("\n" + "-" * 65)
    print("Disk swap area (tool-results/):")
    print("-" * 65)
    for f in sorted(externalizer.tool_results_dir.glob("*.txt")):
        size = f.stat().st_size
        print(f"  {f.name}: {size:,} bytes ({size/1024:.1f}KB)")

    print(f"\n\033[90mSession dir (can inspect): {session_dir}\033[0m")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tool output externalization demo")
    parser.add_argument("--interactive", action="store_true", help="open an interactive externalization shell")
    args = parser.parse_args()
    if args.interactive:
        interactive()
    else:
        main()
