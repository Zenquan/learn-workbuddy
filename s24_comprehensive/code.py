#!/usr/bin/env python3
from __future__ import annotations
"""
s24_comprehensive.py - Capstone: All Mechanisms in One Loop

This is the final lesson. It integrates all 20 mechanisms from s01-s23
into a single agent loop, showing how they fit together.

Mechanisms integrated:
  s01  Agent Loop          — while True core loop
  s02  Tool Dispatch       — TOOL_HANDLERS dispatch map
  s04  Permission Hooks    — pre-tool permission check
  s10  Workspace Memory    — append-only daily log
  s11  User Memory         — MEMORY.md preferences
  s12  Cloud Memory        — simulated cloud profile
  s14  Context Compact     — simplified compaction
  s15  Prompt Assembly     — runtime system prompt assembly
  s16  Skills System       — skill directory listing
  s18  Experts System      — expert package loading
  s19  Visualizer          — SVG output detection
  s20  Result Presentation — file artifact creation
  s21  SQLite Database     — session + usage persistence
  s22  Automation Scheduler— (referenced, not fully active)
  s23  Audit & Sandbox     — SHA256 hash chain + command safety

The core insight: "循环属于 agent。机制属于 harness。"
The loop doesn't change. The mechanisms orbit around it.

Usage:
    python s24_comprehensive/code.py
"""



# Machine-readable learning path metadata. Tests enforce that every
# chapter declares what it inherits and what it adds.
PROGRESSION = {'chapter': 's24_comprehensive',
 'builds_on': ['s23_audit_sandbox'],
 'adds': ['integrated mini harness', 'end-to-end agent pipeline', 'all-layer wiring'],
 'preserves': ['all previous chapter mechanisms']}

# Shared learning entrypoints: --demo is offline; --provider deepseek configures real API env.
import sys as _wb_sys
from pathlib import Path as _wb_Path
_WB_ROOT = _wb_Path(__file__).resolve().parents[1]
if str(_WB_ROOT) not in _wb_sys.path:
    _wb_sys.path.insert(0, str(_WB_ROOT))
from mini_workbuddy.chapter_demo import maybe_run_chapter_demo as _wb_maybe_run_chapter_demo
_wb_maybe_run_chapter_demo(__file__, PROGRESSION)
from mini_workbuddy.chapter_demo import prepare_chapter_provider as _wb_prepare_chapter_provider
_wb_prepare_chapter_provider()
import os, sys, time, json, hashlib, sqlite3, subprocess, tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Callable

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from dotenv import load_dotenv
from mini_workbuddy.paths import tutorial_workbuddy_home

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

OFFLINE_WALKTHROUGH = "--walkthrough" in sys.argv
if OFFLINE_WALKTHROUGH:
    Anthropic = None
else:
    from anthropic import Anthropic
client = None if Anthropic is None else Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ.get("MODEL_ID") or ("offline-walkthrough" if OFFLINE_WALKTHROUGH else None)
if not MODEL:
    raise SystemExit(
        "MODEL_ID is not set. Copy .env.example to .env and fill in "
        "ANTHROPIC_API_KEY and MODEL_ID (see README quick start)."
    )

# ============================================================
# LAYER 1: Persistence (s21 SQLite + s23 Audit)
# ============================================================

DB_DIR = (Path(tempfile.mkdtemp(prefix="learn-workbuddy-s24-"))
          if OFFLINE_WALKTHROUGH else tutorial_workbuddy_home())
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "workbuddy.db"
AUDIT_DIR = DB_DIR / "audit-log"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
USER_MEMORY = DB_DIR / "MEMORY.md"
GENESIS_HASH = "0" * 64


class Database:
    """s21: SQLite with WAL mode, sessions + usage tracking."""

    def __init__(self):
        self.db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._init_tables()

    def _init_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, cwd TEXT, title TEXT,
                status TEXT DEFAULT 'active', model TEXT,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS usage_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, model TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cost REAL, created_at TEXT);
            CREATE TABLE IF NOT EXISTS tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, tool_name TEXT,
                call_count INTEGER DEFAULT 0, updated_at TEXT);
        """)
        self.db.commit()

    def create_session(self, model: str) -> str:
        sid = f"sess_{int(time.time()*1000)}"
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO sessions (id,cwd,title,status,model,created_at,updated_at) "
            "VALUES (?,?, 'New Session','active',?,?,?)",
            (sid, os.getcwd(), model, now, now))
        self.db.commit()
        return sid

    def track_usage(self, sid, model, usage):
        cost = (usage.input_tokens / 1e6 * 3.0) + (usage.output_tokens / 1e6 * 15.0)
        self.db.execute(
            "INSERT INTO usage_tracking (session_id,model,input_tokens,output_tokens,cost,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, model, usage.input_tokens, usage.output_tokens, cost,
             datetime.now().isoformat()))
        self.db.commit()

    def record_tool(self, sid, name):
        now = datetime.now().isoformat()
        row = self.db.execute(
            "SELECT id,call_count FROM tool_usage WHERE session_id=? AND tool_name=?",
            (sid, name)).fetchone()
        if row:
            self.db.execute("UPDATE tool_usage SET call_count=?,updated_at=? WHERE id=?",
                            (row["call_count"]+1, now, row["id"]))
        else:
            self.db.execute(
                "INSERT INTO tool_usage (session_id,tool_name,call_count,updated_at) VALUES (?,?,1,?)",
                (sid, name, now))
        self.db.commit()

    def get_stats(self, sid):
        u = self.db.execute(
            "SELECT SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(cost) as cost "
            "FROM usage_tracking WHERE session_id=?", (sid,)).fetchone()
        t = self.db.execute(
            "SELECT tool_name, call_count FROM tool_usage WHERE session_id=?",
            (sid,)).fetchall()
        return {"usage": dict(u) if u else {}, "tools": [dict(r) for r in t]}

    def close(self):
        self.db.close()


class AuditLog:
    """s23: SHA256 hash chain audit log."""

    def __init__(self):
        self.path = AUDIT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        lines = self.path.read_text().strip().split("\n")
        if not lines or not lines[0]:
            return GENESIS_HASH
        try:
            return json.loads(lines[-1])["hash"]
        except (json.JSONDecodeError, KeyError):
            return GENESIS_HASH

    def append(self, action: str, params: dict, result: str):
        entry = {"timestamp": datetime.now().isoformat(),
                 "action": action, "params": params, "result": result}
        prev = self._last_hash()
        data = {k: v for k, v in entry.items()}
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False) + prev
        entry["hash"] = hashlib.sha256(payload.encode()).hexdigest()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def verify(self) -> tuple[bool, int]:
        if not self.path.exists():
            return True, 0
        entries = [json.loads(l) for l in self.path.read_text().strip().split("\n") if l]
        prev = GENESIS_HASH
        for i, e in enumerate(entries):
            data = {k: v for k, v in e.items() if k != "hash"}
            expected = hashlib.sha256(
                (json.dumps(data, sort_keys=True, ensure_ascii=False) + prev).encode()
            ).hexdigest()
            if e.get("hash") != expected:
                return False, i
            prev = e["hash"]
        return True, len(entries)

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().strip().split("\n") if l]


class Transcript:
    """s09 adapter: durable evidence is separate from live messages[]."""

    def __init__(self, session_id: str):
        self.path = DB_DIR / "transcripts" / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> None:
        sequence = 1
        if self.path.exists():
            sequence += sum(1 for line in self.path.read_text().splitlines() if line)
        record = {"schema_version": 1, "sequence": sequence,
                  "recorded_at": datetime.now(timezone.utc).isoformat(), **event}
        encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


# ============================================================
# LAYER 2: Memory (s10 Workspace + s11 User + s12 Cloud)
# ============================================================

class Memory:
    """s10-s12 adapter: workspace facts stay scoped; user memory is global."""

    def __init__(self, cwd: Path | None = None):
        self.cwd = (cwd or Path.cwd()).resolve()
        self.workspace_id = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self.workspace_dir = DB_DIR / "projects" / self.workspace_id / "memory"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_log = self.workspace_dir / f"{datetime.now(timezone.utc).date()}.jsonl"
        # s11: User memory — persistent preferences
        self.user_memory = USER_MEMORY
        self._init_files()

    def _init_files(self):
        if not self.user_memory.exists():
            self.user_memory.write_text("# User Memory\n\n## Preferences\n- Prefers concise responses\n- Uses Python\n\n")

    def append_workspace(self, entry: str):
        """s10: Append one scoped JSONL fact; distillation remains owned by s10."""
        fact = {
            "type": "workspace_fact",
            "workspace_id": self.workspace_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "content": entry,
        }
        encoded = (json.dumps(fact, ensure_ascii=False) + "\n").encode()
        descriptor = os.open(self.workspace_log, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get_workspace(self) -> str:
        if not self.workspace_log.exists():
            return ""
        facts = []
        for line in self.workspace_log.read_text().splitlines()[-6:]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("workspace_id") == self.workspace_id:
                facts.append(f"- {payload.get('content', '')}")
        return "\n".join(facts)

    def get_user_memory(self) -> str:
        """s11: User-level preferences."""
        return self.user_memory.read_text()[:1000]

    def get_cloud_profile(self) -> str:
        """s12: Simulated cloud profile (in real WorkBuddy, fetched from server)."""
        return "Cloud Profile: Software engineer, works on desktop apps, prefers TypeScript."

    def update_user_memory(self, addition: str):
        with open(self.user_memory, "a") as f:
            f.write(f"- {addition}\n")


# ============================================================
# LAYER 3: Prompt Assembly (s15) + Skills (s16) + Experts (s18)
# ============================================================

SKILLS_REGISTRY = {
    "pdf": "PDF processing skill — extract, merge, convert",
    "commit": "Git commit helper — conventional commits",
    "review-pr": "PR review assistant — code quality checks",
    "finance": "Financial data skill — stock/fund queries",
}

EXPERTS_REGISTRY = {
    "SoftwareCompany": "Software company expert — full-stack development",
    "TrendResearcher": "Trend research expert — market analysis",
    "UiDesigner": "UI design expert — design systems",
}


class PromptAssembler:
    """s15: Runtime system prompt assembly from all sources."""

    def __init__(self, memory: Memory):
        self.memory = memory
        self.expert = None

    def set_expert(self, name: str):
        if name in EXPERTS_REGISTRY:
            self.expert = name
            return True
        return False

    def assemble(self, cwd: str) -> str:
        """Assemble system prompt from all memory sources."""
        parts = []

        # Base identity
        parts.append(f"You are a coding agent at {cwd}.")
        parts.append("Act, don't over-explain. Keep responses concise.\n")

        # s11: User memory
        user_mem = self.memory.get_user_memory()
        parts.append(f"## User Preferences\n{user_mem}\n")

        # s12: Cloud profile
        cloud = self.memory.get_cloud_profile()
        parts.append(f"## Cloud Profile\n{cloud}\n")

        # s10: Workspace memory (recent context)
        workspace = self.memory.get_workspace()
        if workspace.strip():
            parts.append(f"## Recent Workspace Log\n{workspace[:500]}\n")

        # s16: Skills available
        skills_list = "\n".join(f"  - {k}: {v}" for k, v in SKILLS_REGISTRY.items())
        parts.append(f"## Available Skills\n{skills_list}\n")

        # s18: Expert (if loaded)
        if self.expert:
            parts.append(f"## Active Expert: {self.expert}\n{EXPERTS_REGISTRY[self.expert]}\n")

        # s23: Safety rules
        parts.append("""## Safety Rules
- Desktop, Downloads, Documents are HIGH-RISK zones
- Scan = read-only, don't modify
- Warn + confirm before destructive actions
- Use trash, not rm
- Max 10 files per batch""")

        return "\n".join(parts)


# ============================================================
# LAYER 4: Tool Dispatch (s02) + Permission (s04) + Sandbox (s23)
# ============================================================

BLOCKED_CMDS = ["rm -rf /", "sudo ", "shutdown", "reboot", "mkfs"]
HIGH_RISK = ["Desktop", "Downloads", "Documents"]


def classify_command(cmd: str) -> str:
    """s23: Classify command safety level."""
    for b in BLOCKED_CMDS:
        if b in cmd:
            return "BLOCKED"
    for z in HIGH_RISK:
        if z in cmd and "rm" in cmd:
            return "HIGH_RISK"
    if any(d in cmd for d in ["rm ", "rmdir", "mv ", "> "]):
        return "DESTRUCTIVE"
    first = cmd.split()[0] if cmd.split() else ""
    if first in ["ls", "cat", "head", "grep", "find", "echo", "pwd", "wc", "which"]:
        return "SAFE"
    return "UNKNOWN"


def run_bash(command: str) -> str:
    """s02+s23: Execute bash with sandbox check."""
    level = classify_command(command)
    if level in ("BLOCKED", "HIGH_RISK"):
        return f"Error: {level} — command blocked by sandbox"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (60s)"
    except Exception as e:
        return f"Error: {e}"


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class ToolDefinition:
    """One source for prompt schema, handler, and permission policy."""

    name: str
    description: str
    input_schema: dict
    permission: PermissionDecision
    handler: Callable[[dict], str]


def _scoped_path(raw: str) -> Path:
    path = (Path.cwd() / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        path.relative_to(Path.cwd().resolve())
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {raw}") from exc
    return path


def _read_file(inputs: dict) -> str:
    path = _scoped_path(inputs["path"])
    return path.read_text()[:10_000] if path.exists() else "File not found"


def _write_file(inputs: dict) -> str:
    _scoped_path(inputs["path"]).write_text(inputs["content"])
    return "File written"


def _list_files(inputs: dict) -> str:
    return "\n".join(str(path) for path in islice(_scoped_path(inputs.get("path", ".")).iterdir(), 50))


TOOL_REGISTRY = {
    item.name: item for item in [
        ToolDefinition("bash", "Run a shell command. Sandboxed and audited.",
                       {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                       PermissionDecision.ALLOW, lambda inputs: run_bash(inputs["command"])),
        ToolDefinition("read_file", "Read a workspace file.",
                       {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                       PermissionDecision.ALLOW, _read_file),
        ToolDefinition("write_file", "Write a workspace file after approval.",
                       {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                       PermissionDecision.ASK, _write_file),
        ToolDefinition("list_files", "List a workspace directory.",
                       {"type": "object", "properties": {"path": {"type": "string"}}},
                       PermissionDecision.ALLOW, _list_files),
    ]
}

# Compatibility views are derived; they cannot drift from the registry.
TOOL_HANDLERS = {name: definition.handler for name, definition in TOOL_REGISTRY.items()}
TOOLS = [{"name": item.name, "description": item.description, "input_schema": item.input_schema}
         for item in TOOL_REGISTRY.values()]


def check_permission(tool_name: str) -> PermissionDecision:
    definition = TOOL_REGISTRY.get(tool_name)
    return definition.permission if definition else PermissionDecision.DENY


def execute_tool(tool_name: str, tool_input: dict,
                 approve: Callable[[str, dict], bool] | None = None) -> str:
    """Execute only after the registry policy (and real ASK decision) permits it."""
    definition = TOOL_REGISTRY.get(tool_name)
    decision = check_permission(tool_name)
    if definition is None or decision is PermissionDecision.DENY:
        return f"Permission denied: unknown or denied tool {tool_name}"
    if decision is PermissionDecision.ASK and (approve is None or not approve(tool_name, tool_input)):
        return "Permission denied: user approval required"
    try:
        return definition.handler(tool_input)
    except (OSError, PermissionError, KeyError) as exc:
        return f"Tool failed: {exc}"


# ============================================================
# LAYER 5: Context Compaction (s14) + Visualizer (s19)
# ============================================================

def estimate_tokens(messages: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block)) // 4
                else:
                    total += len(str(block)) // 4
    return total


def compact_context(messages: list, max_tokens: int = 50000) -> list:
    """s14: Simplified context compaction — summarize old messages."""
    if estimate_tokens(messages) <= max_tokens:
        return messages

    # Keep first user message and last 6 messages, summarize the rest
    if len(messages) <= 8:
        return messages

    kept = messages[:2]  # first exchange
    summarized = messages[2:-6]
    summary = f"[Context compacted: {len(summarized)} messages summarized. " \
              f"Key points: agent was working on tasks in {os.getcwd()}.]"
    kept.append({"role": "user", "content": summary})
    kept.extend(messages[-6:])
    return kept


def detect_visualizer(content) -> str | None:
    """s19: Detect SVG/HTML content in agent output for visualizer injection."""
    if isinstance(content, list):
        for block in content:
            if getattr(block, 'type', None) == 'text':
                text = block.text
                if '<svg' in text.lower():
                    return "svg"
                if '<html' in text.lower() or '<div' in text.lower():
                    return "html"
    return None


# ============================================================
# LAYER 6: The Comprehensive Agent Loop (s01 + all mechanisms)
# ============================================================

class ComprehensiveAgent:
    """
    The capstone agent. Integrates all 20 mechanisms into one loop.

    The loop itself is the same 30-line while True from s01.
    Everything else — memory, audit, sandbox, database, skills —
    orbits around the loop without changing its structure.
    """

    def __init__(self, approval_resolver: Callable[[str, dict], bool] | None = None):
        # s21: Database
        self.db = Database()
        # s23: Audit log
        self.audit = AuditLog()
        # s10-s12: Memory
        self.memory = Memory()
        # s15: Prompt assembler
        self.prompt = PromptAssembler(self.memory)
        # Session
        self.session_id = self.db.create_session(MODEL)
        self.transcript = Transcript(self.session_id)
        self.approval_resolver = approval_resolver
        self.messages = []
        self.total_cost = 0.0

        self.audit.append("session_create", {"session_id": self.session_id}, "success")

    def run(self, user_input: str) -> str:
        """Run one complete agent interaction."""
        # Log user input to audit
        self.audit.append("user_input", {"query": user_input[:200]}, "received")

        # s10: Append to workspace memory
        self.memory.append_workspace(f"User: {user_input[:100]}")

        self.messages.append({"role": "user", "content": user_input})
        self.transcript.append({"type": "message", "role": "user", "content": user_input})

        # ── THE LOOP (s01) ──
        iterations = 0
        while True:
            iterations += 1

            # s14: Context compaction
            self.messages = compact_context(self.messages)

            # s15: Assemble system prompt
            system = self.prompt.assemble(os.getcwd())

            # API call
            response = client.messages.create(
                model=MODEL, system=system, messages=self.messages,
                tools=TOOLS, max_tokens=8000,
            )

            # s21: Usage tracking
            self.db.track_usage(self.session_id, MODEL, response.usage)
            cost = (response.usage.input_tokens / 1e6 * 3.0 +
                    response.usage.output_tokens / 1e6 * 15.0)
            self.total_cost += cost

            self.messages.append({"role": "assistant", "content": response.content})
            self.transcript.append({"type": "model_response", "blocks": len(response.content)})

            # s01: response blocks drive the loop; stop_reason is diagnostic.
            tool_blocks = [block for block in response.content
                           if getattr(block, "type", None) == "tool_use"]
            if not tool_blocks:
                if response.stop_reason == "tool_use":
                    self.audit.append("protocol_error", {"stop_reason": "tool_use"}, "no tool block")
                break

            # s02+s04+s23: Tool dispatch with permission + sandbox + audit
            results = []
            for block in tool_blocks:

                tool_name = block.name
                tool_input = block.input

                # s04: Permission check
                decision = check_permission(tool_name)
                if decision is PermissionDecision.DENY:
                    self.audit.append("permission_denied",
                                      {"tool": tool_name, "reason": decision.value}, "blocked")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": "Permission denied"})
                    continue

                # s23: Audit before execution
                self.audit.append("tool_execute",
                                  {"tool": tool_name, "input": str(tool_input)[:200]},
                                  "started")

                # s02: Dispatch
                output = execute_tool(tool_name, tool_input, self.approval_resolver)

                # s21: Record tool usage
                self.db.record_tool(self.session_id, tool_name)

                # s23: Audit after execution
                self.audit.append("tool_result",
                                  {"tool": tool_name}, output[:200])
                self.transcript.append({"type": "function_call_result", "callId": block.id,
                                        "name": tool_name, "output": output})

                # Print tool activity
                safety = "SAFE"
                if tool_name == "bash":
                    safety = classify_command(tool_input.get("command", ""))
                color = {"BLOCKED": 31, "HIGH_RISK": 31, "DESTRUCTIVE": 31,
                         "SAFE": 32, "UNKNOWN": 33}.get(safety, 33)
                cmd_preview = tool_input.get("command", str(tool_input))[:60]
                print(f"\033[{color}m  [{safety}] {tool_name}: {cmd_preview}\033[0m")
                print(f"  {output[:150]}")

                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})

            self.messages.append({"role": "user", "content": results})

        # ── Post-loop: result handling ──

        # s19: Check for visualizer content
        visual = detect_visualizer(response.content)
        if visual:
            self.audit.append("visualizer_detected", {"type": visual}, "injected")

        # s10: Update workspace memory
        final_text = ""
        if isinstance(response.content, list):
            for block in response.content:
                if getattr(block, 'type', None) == 'text':
                    final_text += block.text
        self.memory.append_workspace(f"Agent: {final_text[:100]}")

        # s20: Result presentation (simulated)
        self.audit.append("agent_complete",
                          {"iterations": iterations, "cost": self.total_cost},
                          "success")

        return final_text

    def status(self):
        """Print full agent status — all mechanisms visible."""
        stats = self.db.get_stats(self.session_id)
        audit_entries = self.audit.entries()
        valid, count = self.audit.verify()

        print(f"\n{'═'*60}")
        print(f"  Comprehensive Agent Status")
        print(f"{'═'*60}")
        print(f"  Session:     {self.session_id}")
        print(f"  Model:       {MODEL}")
        print(f"  CWD:         {os.getcwd()}")
        print(f"  Messages:    {len(self.messages)}")
        print(f"  Cost:        ${self.total_cost:.4f}")
        print(f"{'─'*60}")
        print(f"  Database:    {DB_PATH}")
        if stats["usage"]:
            u = stats["usage"]
            print(f"  Tokens:      {u['inp'] or 0} in / {u['out'] or 0} out")
        print(f"  Tools used:  {len(stats['tools'])} types")
        for t in stats["tools"]:
            print(f"    {t['tool_name']:<20} {t['call_count']} calls")
        print(f"{'─'*60}")
        print(f"  Audit log:   {self.audit.path}")
        print(f"  Audit entries: {len(audit_entries)}")
        print(f"  Chain valid: {'✓' if valid else '✗ BROKEN'}")
        print(f"{'─'*60}")
        print(f"  Workspace:   {self.memory.workspace_log}")
        print(f"  User mem:    {self.memory.user_memory}")
        print(f"  Skills:      {len(SKILLS_REGISTRY)} available")
        print(f"  Experts:     {len(EXPERTS_REGISTRY)} available")
        print(f"  Active expert: {self.prompt.expert or 'none'}")
        print(f"{'═'*60}\n")

    def close(self):
        self.db.close()
        self.audit.append("session_close", {"session_id": self.session_id}, "closed")


def offline_walkthrough() -> None:
    """Keyless two-turn trace for the integrated loop contracts."""
    from types import SimpleNamespace

    scripted = [
        SimpleNamespace(
            stop_reason="end_turn",  # deliberately contradictory diagnostic
            content=[SimpleNamespace(type="tool_use", id="call_1", name="list_files",
                                     input={"path": "."})],
        ),
        SimpleNamespace(stop_reason="end_turn",
                        content=[SimpleNamespace(type="text", text="walkthrough complete")]),
    ]
    print("s24 offline walkthrough")
    for turn, response in enumerate(scripted, start=1):
        tool_blocks = [block for block in response.content if block.type == "tool_use"]
        print(f"turn {turn}: blocks={[block.type for block in response.content]}")
        if not tool_blocks:
            print(response.content[0].text)
            break
        for block in tool_blocks:
            output = execute_tool(block.name, block.input)
            print(f"tool_result {block.id}: {output.splitlines()[0] if output else '(empty)'}")


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    if OFFLINE_WALKTHROUGH:
        offline_walkthrough()
        raise SystemExit(0)
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  s24: Comprehensive — 机制很多, 循环一个                  ║")
    print("║  All 20 mechanisms integrated into one agent loop        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("机制清单:")
    print("  s01 Agent Loop    s02 Tool Dispatch   s04 Permission")
    print("  s10 Workspace Mem s11 User Memory     s12 Cloud Memory")
    print("  s14 Context Clamp s15 Prompt Assembly s16 Skills")
    print("  s18 Experts       s19 Visualizer      s20 Result Present")
    print("  s21 SQLite DB     s22 Automation      s23 Audit & Sandbox")
    print()
    print("命令: /status | /audit | /memory | /compact | /expert <name> | q 退出\n")

    agent = ComprehensiveAgent()
    print(f"会话已创建: {agent.session_id}\n")

    try:
        while True:
            try:
                query = input("\033[36ms24 >> \033[0m").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not query:
                continue
            if query.lower() in ("q", "exit", "quit"):
                break

            if query == "/status":
                agent.status()
                continue
            if query == "/audit":
                entries = agent.audit.entries()
                print(f"\n  审计日志: {len(entries)} 条记录")
                for i, e in enumerate(entries[-15:]):
                    print(f"  {i+1:>3} {e['timestamp'][:19]} {e['action']:<20} {str(e.get('result',''))[:40]}")
                valid, count = agent.audit.verify()
                print(f"\n  链完整性: {'✓ 完整' if valid else '✗ 断裂'} ({count} 条)")
                continue
            if query == "/memory":
                print(f"\n  工作区记忆:\n{agent.memory.get_workspace()[-500:]}")
                print(f"\n  用户记忆:\n{agent.memory.get_user_memory()}")
                continue
            if query == "/compact":
                before = estimate_tokens(agent.messages)
                agent.messages = compact_context(agent.messages)
                after = estimate_tokens(agent.messages)
                print(f"  压缩: {before} → {after} tokens")
                continue
            if query.startswith("/expert "):
                name = query[8:].strip()
                if agent.prompt.set_expert(name):
                    print(f"  专家已切换: {name}")
                else:
                    print(f"  未找到专家: {name}")
                    print(f"  可用: {', '.join(EXPERTS_REGISTRY.keys())}")
                continue
            if query == "/skills":
                print("  可用技能:")
                for k, v in SKILLS_REGISTRY.items():
                    print(f"    {k}: {v}")
                continue

            # Run the comprehensive agent loop
            response = agent.run(query)
            if response:
                print(f"\n{response}")
            print(f"\n\033[90m  [成本: ${agent.total_cost:.4f}]\033[0m\n")

    finally:
        agent.close()
        print(f"\n会话已关闭。数据保存在 {DB_PATH}")
        print(f"审计日志: {agent.audit.path}")
        print(f"\n{'═'*60}")
        print("  20 课完结。循环属于 agent, 机制属于 harness。")
        print(f"{'═'*60}")
