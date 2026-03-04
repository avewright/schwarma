"""
live_demo.py -- DEPRECATED.  Use hub_demo.py instead.

    python examples/hub_demo.py

This file runs an in-process Exchange with a self-hosted SSE event log.
That event log is superseded by the hub dashboard at http://localhost:8741.

To run the hub-connected demo (recommended):
    docker compose up -d
    python examples/hub_demo.py

The remainder of this file is kept for reference only.

--------------------------------------------------------------------
live_demo.py -- Schwarma live collaborative demo in your browser.

Runs a real multi-agent exchange where every agent is backed by your LLM.
Watch the whole workflow (post → claim → solve → review → accept) live.

Usage
-----
    # MiniMax via Anthropic SDK (set your own base_url + model as needed)
    python examples/live_demo.py \\
        --api-key  YOUR_KEY \\
        --base-url https://api.minimaxi.chat/v1 \\
        --model    MiniMax-Text-01

    # Standard Anthropic
    python examples/live_demo.py \\
        --api-key  sk-ant-... \\
        --model    claude-3-5-haiku-20241022

Then open:  http://localhost:7741
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ── ensure package is importable from repo root ──────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dotenv() -> None:
    """Load .env from the repo root into os.environ (no external deps)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from schwarma import (
    Agent,
    AgentCapability,
    Exchange,
    ExchangeConfig,
    Problem,
    ProblemTag,
    Review,
    ReviewType,
    ReviewVerdict,
)
from schwarma.events import EventKind

# ── global SSE queue ─────────────────────────────────────────────────
# All SSE subscribers share new events from here.
_sse_queues: list[queue.Queue] = []
_sse_lock = threading.Lock()
# Cached latest full-state SSE payload so late-connecting browsers can
# immediately see the current leaderboard / problem list.
_last_state_payload: str | None = None


def _broadcast(event_type: str, data: dict) -> None:
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        for q in _sse_queues:
            q.put_nowait(payload)


# ── HTML page ────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en" class="h-full bg-gray-950">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Schwarma Live Demo</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: {
      extend: {
        fontFamily: { mono: ['JetBrains Mono', 'Fira Code', 'monospace'] }
      }
    }
  }
</script>
<style>
  body { font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace; }
  .fade-in { animation: fadeIn .4s ease; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }
  .pulse-dot { animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .scrollbar::-webkit-scrollbar { width:4px; }
  .scrollbar::-webkit-scrollbar-track { background:transparent; }
  .scrollbar::-webkit-scrollbar-thumb { background:#374151; border-radius:2px; }
  .status-OPEN    { color:#60a5fa; }
  .status-CLAIMED { color:#fbbf24; }
  .status-SOLVED  { color:#a78bfa; }
  .status-CLOSED  { color:#34d399; }
  .status-REJECTED{ color:#f87171; }
  .status-NEEDS_REVISION { color:#fb923c; }
</style>
</head>
<body class="dark h-full text-gray-100">

<!-- Header -->
<header class="fixed top-0 left-0 right-0 bg-gray-900 border-b border-gray-800 z-10 px-6 py-3 flex items-center justify-between">
  <div class="flex items-center gap-3">
    <span class="text-2xl">🌀</span>
    <span class="font-bold text-lg tracking-tight text-white">Schwarma</span>
    <span class="text-gray-500 text-sm">/ live demo</span>
  </div>
  <div class="flex items-center gap-4">
    <div id="conn-status" class="flex items-center gap-2 text-sm text-yellow-400">
      <span class="pulse-dot inline-block w-2 h-2 rounded-full bg-yellow-400"></span>
      connecting…
    </div>
    <div id="agent-count" class="text-sm text-gray-400">0 agents</div>
  </div>
</header>

<!-- Main grid -->
<div class="pt-14 h-screen flex overflow-hidden">

  <!-- Left: event log -->
  <div class="w-1/2 flex flex-col border-r border-gray-800">
    <div class="px-4 py-2 bg-gray-900 border-b border-gray-800 text-xs text-gray-500 uppercase tracking-widest">Event Log</div>
    <div id="log" class="flex-1 overflow-y-auto scrollbar p-3 space-y-1 text-xs font-mono"></div>
  </div>

  <!-- Right: agents + problems -->
  <div class="w-1/2 flex flex-col overflow-hidden">

    <!-- Agents panel -->
    <div class="border-b border-gray-800">
      <div class="px-4 py-2 bg-gray-900 border-b border-gray-800 text-xs text-gray-500 uppercase tracking-widest">Agents</div>
      <div id="agents" class="p-3 grid grid-cols-2 gap-2 text-xs"></div>
    </div>

    <!-- Problems panel -->
    <div class="flex flex-col flex-1 overflow-hidden">
      <div class="px-4 py-2 bg-gray-900 border-b border-gray-800 text-xs text-gray-500 uppercase tracking-widest">Problems</div>
      <div id="problems" class="flex-1 overflow-y-auto scrollbar p-3 space-y-2 text-xs"></div>
    </div>

  </div>
</div>

<!-- Phase banner (floats) -->
<div id="phase-banner" class="hidden fixed bottom-6 left-1/2 -translate-x-1/2 bg-indigo-600 text-white px-6 py-2 rounded-full text-sm font-semibold shadow-lg transition-all"></div>

<script>
const LOG_COLORS = {
  PROBLEM_POSTED:   '#60a5fa',
  PROBLEM_CLAIMED:  '#fbbf24',
  PROBLEM_SOLVED:   '#a78bfa',
  SOLUTION_ACCEPTED:'#34d399',
  SOLUTION_REJECTED:'#f87171',
  SOLUTION_REVISION_REQUESTED:'#fb923c',
  REVIEW_SUBMITTED: '#94a3b8',
  TRIAGE_ASSIGNED:  '#6b7280',
  AGENT_REGISTERED: '#86efac',
  DUPLICATE_DETECTED:'#f59e0b',
  narration:        '#e879f9',
  phase:            '#818cf8',
  llm_call:         '#67e8f9',
  error:            '#fca5a5',
};

const agents  = {};   // id → {name, rep, solved, reviewed, active}
const problems = {};  // id → {title, status, bounty, tags, solver_name}

let eventSource;

function connect() {
  eventSource = new EventSource('/events');
  eventSource.onopen = () => {
    document.getElementById('conn-status').innerHTML =
      '<span class="inline-block w-2 h-2 rounded-full bg-green-400"></span> live';
    document.getElementById('conn-status').className = 'flex items-center gap-2 text-sm text-green-400';
  };
  eventSource.onerror = () => {
    document.getElementById('conn-status').innerHTML =
      '<span class="inline-block w-2 h-2 rounded-full bg-red-400"></span> disconnected';
    document.getElementById('conn-status').className = 'flex items-center gap-2 text-sm text-red-400';
    setTimeout(connect, 3000);
  };

  // Listen to named event types from the server
  ['exchange', 'narration', 'phase', 'llm_call', 'state', 'error'].forEach(type => {
    eventSource.addEventListener(type, e => {
      const d = JSON.parse(e.data);
      if (type === 'state') { handleState(d); return; }
      if (type === 'phase') { showPhase(d.text); }
      appendLog(type === 'exchange' ? d.kind : type, d.text || d.kind, d);
      if (type === 'exchange') handleExchange(d);
    });
  });
}

function appendLog(kind, text, data) {
  const log = document.getElementById('log');
  const color = LOG_COLORS[kind] || '#9ca3af';
  const ts = new Date().toLocaleTimeString('en-US', {hour12:false});
  const div = document.createElement('div');
  div.className = 'fade-in flex gap-2 py-0.5 border-b border-gray-900';
  div.innerHTML = `
    <span class="text-gray-600 flex-none">${ts}</span>
    <span class="flex-none font-semibold" style="color:${color}; min-width:220px">${kind}</span>
    <span class="text-gray-300 truncate">${escHtml(text)}</span>`;
  log.prepend(div);
  // Limit to 200 entries
  while (log.children.length > 200) log.removeChild(log.lastChild);
}

function handleExchange(d) {
  if (d.kind === 'AGENT_REGISTERED' && d.agent) {
    agents[d.agent.id] = { ...d.agent, rep: 0, solved: 0, reviewed: 0, active: 0 };
    renderAgents();
  }
  if (d.kind === 'PROBLEM_POSTED' && d.problem) {
    problems[d.problem.id] = { ...d.problem, status: 'OPEN' };
    renderProblems();
  }
  if (d.kind === 'PROBLEM_CLAIMED' && d.problem_id) {
    if (problems[d.problem_id]) { problems[d.problem_id].status = 'CLAIMED'; problems[d.problem_id].solver_name = d.solver_name; }
    renderProblems();
  }
  if (d.kind === 'PROBLEM_SOLVED' && d.problem_id) {
    if (problems[d.problem_id]) problems[d.problem_id].status = 'SOLVED';
    renderProblems();
  }
  if (d.kind === 'SOLUTION_ACCEPTED' && d.problem_id) {
    if (problems[d.problem_id]) problems[d.problem_id].status = 'CLOSED';
    renderProblems();
  }
  if (d.kind === 'SOLUTION_REJECTED' && d.problem_id) {
    if (problems[d.problem_id]) problems[d.problem_id].status = 'OPEN'; // re-opened
    renderProblems();
  }
  if (d.kind === 'SOLUTION_REVISION_REQUESTED' && d.problem_id) {
    if (problems[d.problem_id]) problems[d.problem_id].status = 'NEEDS_REVISION';
    renderProblems();
  }
}

function handleState(d) {
  // Full snapshot update
  if (d.agents) {
    for (const a of d.agents) agents[a.id] = { ...agents[a.id], ...a };
    document.getElementById('agent-count').textContent = `${d.agents.length} agents`;
    renderAgents();
  }
  if (d.problems) {
    for (const p of d.problems) problems[p.id] = { ...problems[p.id], ...p };
    renderProblems();
  }
}

function showPhase(text) {
  const b = document.getElementById('phase-banner');
  b.textContent = text;
  b.classList.remove('hidden');
  clearTimeout(b._t);
  b._t = setTimeout(() => b.classList.add('hidden'), 4000);
}

function renderAgents() {
  const el = document.getElementById('agents');
  el.innerHTML = Object.values(agents).map(a => {
    const bar = Math.min(100, Math.max(4, a.rep || 0));
    return `<div class="bg-gray-800 rounded-lg p-2">
      <div class="flex justify-between items-center mb-1">
        <span class="font-semibold text-white">${escHtml(a.name)}</span>
        <span class="text-green-400 font-mono">${a.rep ?? 0} pts</span>
      </div>
      <div class="w-full bg-gray-700 rounded-full h-1.5 mb-1">
        <div class="bg-green-400 h-1.5 rounded-full transition-all duration-500" style="width:${bar}%"></div>
      </div>
      <div class="flex gap-3 text-gray-500">
        <span>✓ ${a.solved ?? 0} solved</span>
        <span>👁 ${a.reviewed ?? 0} reviewed</span>
        <span>⚡ ${a.active ?? 0} active</span>
      </div>
    </div>`;
  }).join('');
}

function renderProblems() {
  const el = document.getElementById('problems');
  el.innerHTML = Object.values(problems).map(p => {
    const statusClass = `status-${p.status}`;
    const tags = (p.tags || []).map(t => `<span class="bg-gray-700 px-1.5 py-0.5 rounded text-gray-400">${t}</span>`).join(' ');
    return `<div class="bg-gray-800 rounded-lg p-3 fade-in">
      <div class="flex justify-between items-start mb-1">
        <span class="font-semibold text-white">${escHtml(p.title)}</span>
        <span class="font-mono font-bold text-yellow-400">${p.bounty ?? 0} pts</span>
      </div>
      <div class="flex items-center gap-2 mb-1">
        <span class="font-bold ${statusClass}">${p.status}</span>
        ${p.solver_name ? `<span class="text-gray-500">→ ${escHtml(p.solver_name)}</span>` : ''}
      </div>
      <div class="flex gap-1 flex-wrap">${tags}</div>
    </div>`;
  }).join('');
}

function escHtml(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

connect();
</script>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence access logs
        pass

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q: queue.Queue = queue.Queue()
            with _sse_lock:
                _sse_queues.append(q)
                # Immediately replay the latest state so the browser
                # is never stuck on a blank page.
                if _last_state_payload is not None:
                    q.put_nowait(_last_state_payload)
            try:
                while True:
                    try:
                        chunk = q.get(timeout=20)
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Send a keepalive comment
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _sse_lock:
                    _sse_queues.remove(q)
        else:
            self.send_error(404)


def _run_server(port: int) -> None:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.serve_forever()


# ── LLM solver factory ────────────────────────────────────────────────

def make_solver(
    name: str,
    role: str,
    *,
    api_key: str,
    base_url: str | None,
    model: str,
):
    """Return an async solver callback that calls the LLM."""
    try:
        import anthropic as _anthropic
    except ImportError:
        print("[ERROR] anthropic package not found. Install with: pip install anthropic")
        sys.exit(1)

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = _anthropic.Anthropic(**kwargs)

    system = textwrap.dedent(f"""
        You are {name}, an AI agent in the Schwarma peer-review exchange.
        Your role: {role}
        Be concise -- limit responses to 3-6 sentences. No markdown unless asked.
    """).strip()

    async def solver(description: str, ctx: dict) -> str:
        # Announce the call to the UI
        _broadcast("llm_call", {"text": f"{name} is thinking… ({model})"})

        revision = ctx.get("revision_feedback", "")
        content = description
        if revision:
            content += f"\n\n[Revision feedback]: {revision}"

        loop = asyncio.get_event_loop()

        def _call():
            resp = client.messages.create(
                model=model,
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            return resp.content[0].text

        try:
            result = await loop.run_in_executor(None, _call)
        except Exception as exc:
            result = f"[LLM error: {exc}]"
            _broadcast("error", {"text": f"{name} LLM error: {exc}"})

        _broadcast("llm_call", {"text": f"{name} responded ({len(result)} chars)"})
        return result

    return solver


# ── Demo orchestrator ─────────────────────────────────────────────────

def _phase(text: str) -> None:
    print(f"\n{'-'*60}\n  {text}\n{'-'*60}")
    _broadcast("phase", {"text": text})


def _narrate(text: str) -> None:
    print(f"  >>  {text}")
    _broadcast("narration", {"text": text})


def _push_state(exchange: Exchange, solver_names: dict) -> None:
    """Send a full state snapshot to the UI."""
    agents_list = []
    for a in exchange.agents:
        agents_list.append({
            "id": str(a.id),
            "name": a.name,
            "rep": exchange.ledger.balance(a.id),
            "solved": a._total_solved,
            "reviewed": a._total_reviewed,
            "active": a.active_count,
        })

    problems_list = []
    for p in exchange._problems.values():
        problems_list.append({
            "id": str(p.id),
            "title": p.title,
            "status": p.status.name,
            "bounty": p.bounty,
            "tags": [t.name for t in p.tags],
            "solver_name": solver_names.get(str(p.id)),
        })

    global _last_state_payload
    snapshot = {"agents": agents_list, "problems": problems_list}
    _last_state_payload = f"event: state\ndata: {json.dumps(snapshot)}\n\n"
    _broadcast("state", snapshot)


async def run_demo(
    api_key: str,
    base_url: str | None,
    model: str,
    port: int,
) -> None:
    """Run the full collaborative demo."""

    # ── Event bridge: Exchange → SSE ──────────────────────────────────
    agent_registry: dict[str, str] = {}     # agent_id → name
    solver_names:   dict[str, str] = {}     # problem_id → solver name

    async def _exchange_event_handler(ev) -> None:
        kind = ev.kind.name

        extra: dict[str, Any] = {
            "kind": kind,
            "problem_id": str(ev.problem_id) if ev.problem_id else None,
            "solution_id": str(ev.solution_id) if ev.solution_id else None,
            "source": str(ev.source_agent_id) if ev.source_agent_id else None,
            "target": str(ev.target_agent_id) if ev.target_agent_id else None,
        }

        # Human-readable description
        src_name = agent_registry.get(str(ev.source_agent_id), "?") if ev.source_agent_id else ""
        tgt_name = agent_registry.get(str(ev.target_agent_id), "?") if ev.target_agent_id else ""
        descriptions = {
            "PROBLEM_POSTED":    f"{src_name} posted a problem",
            "PROBLEM_CLAIMED":   f"{src_name} claimed the problem",
            "PROBLEM_SOLVED":    f"{src_name} submitted a solution",
            "SOLUTION_ACCEPTED": f"Solution ACCEPTED! Bounty paid to {src_name}",
            "SOLUTION_REJECTED": f"Solution REJECTED -- problem re-opened",
            "SOLUTION_REVISION_REQUESTED": f"Revision requested from {src_name}",
            "REVIEW_REQUESTED":  f"{tgt_name} asked to review",
            "REVIEW_SUBMITTED":  f"{src_name} submitted a review",
            "TRIAGE_ASSIGNED":   f"Triage → {tgt_name} suggested for problem",
            "AGENT_REGISTERED":  f"{src_name} joined the exchange",
            "DUPLICATE_DETECTED": "Similar archived problem detected",
        }
        extra["text"] = descriptions.get(kind, kind)

        _broadcast("exchange", extra)

    # ── Exchange setup ────────────────────────────────────────────────
    _phase("Setting up the Exchange")

    cfg = ExchangeConfig(
        reviews_required_for_accept=2,
        min_reputation_to_claim=0,
        enable_staking=False,
        enable_content_guards=False,
        enable_effort_guards=False,
        enable_similarity_check=False,
        auto_assign=True,
        tiebreaker_extra_reviews=0,
        tiebreaker_fallback="reject",
    )
    exchange = Exchange(cfg)

    # Subscribe the bridge before any agents are registered
    exchange.bus.subscribe_all(_exchange_event_handler)

    # ── Register agents ───────────────────────────────────────────────
    _phase("Registering LLM-backed agents")
    await asyncio.sleep(0.5)

    agent_configs = [
        (
            "Alice",
            "You are a senior software architect. Solve problems clearly and correctly.",
            {AgentCapability.CODE_GENERATION, AgentCapability.DEBUGGING},
        ),
        (
            "Bob",
            "You are a backend engineer specialising in Python. Write clean, working code.",
            {AgentCapability.CODE_GENERATION},
        ),
        (
            "Carol",
            "You are a code reviewer. When reviewing, give a verdict of APPROVE or REJECT "
            "with a 1-sentence reason. Keep it short.",
            {AgentCapability.CODE_REVIEW},
        ),
        (
            "Dave",
            "You are a thorough code reviewer. Approve correct solutions, reject incorrect ones. "
            "Reply with APPROVE or REJECT and a brief reason.",
            {AgentCapability.CODE_REVIEW},
        ),
    ]

    agents_map: dict[str, Any] = {}
    for agent_name, role, caps in agent_configs:
        s = make_solver(agent_name, role, api_key=api_key, base_url=base_url, model=model)
        a = Agent(name=agent_name, solver=s, capabilities=caps)
        exchange.register(a)
        agents_map[agent_name] = a
        agent_registry[str(a.id)] = agent_name

        # Announce to UI
        _broadcast("exchange", {
            "kind": "AGENT_REGISTERED",
            "text": f"{agent_name} joined the exchange",
            "agent": {
                "id": str(a.id),
                "name": agent_name,
                "rep": 0, "solved": 0, "reviewed": 0, "active": 0,
            },
        })
        _narrate(f"Registered: {agent_name}")
        _push_state(exchange, solver_names)
        await asyncio.sleep(0.3)

    alice = agents_map["Alice"]
    bob   = agents_map["Bob"]
    carol = agents_map["Carol"]
    dave  = agents_map["Dave"]

    # ── Post problems ─────────────────────────────────────────────────
    _phase("Posting problems to the Exchange")
    await asyncio.sleep(0.5)

    problem_defs = [
        Problem(
            title="Write a retry decorator",
            description=(
                "Write a Python decorator called `retry(max_attempts, delay_seconds)` "
                "that retries a failing function up to max_attempts times, "
                "waiting delay_seconds between attempts. Include a docstring."
            ),
            author_id=alice.id,
            tags=[ProblemTag.FEATURE],
            bounty=20,
        ),
        Problem(
            title="Explain async generators",
            description=(
                "Explain Python async generators in 3-4 sentences suitable for a "
                "developer who knows sync generators but is new to async/await. "
                "Include one short code example."
            ),
            author_id=alice.id,
            tags=[ProblemTag.QUESTION],
            bounty=15,
        ),
        Problem(
            title="Find the bug in this binary search",
            description=(
                "Here is a binary search implementation that sometimes returns -1 "
                "even when the target is present:\n\n"
                "def binary_search(arr, target):\n"
                "    lo, hi = 0, len(arr)\n"
                "    while lo < hi:\n"
                "        mid = (lo + hi) // 2\n"
                "        if arr[mid] == target: return mid\n"
                "        elif arr[mid] < target: lo = mid\n"
                "        else: hi = mid - 1\n"
                "    return -1\n\n"
                "Find the bug and provide the corrected version."
            ),
            author_id=alice.id,
            tags=[ProblemTag.BUG],
            bounty=25,
        ),
    ]

    posted: list[Any] = []
    for pdef in problem_defs:
        p = await exchange.post_problem(pdef)
        posted.append(p)
        _narrate(f"Posted: '{p.title}' (bounty={p.bounty})")
        _push_state(exchange, solver_names)
        await asyncio.sleep(0.4)

    # ── Solve problems ────────────────────────────────────────────────
    _phase("Agents claiming and solving problems")
    await asyncio.sleep(0.5)

    # Bob solves problems 0 and 2; Alice solves problem 1
    solver_pairs = [
        (posted[0], bob),
        (posted[1], alice),
        (posted[2], bob),
    ]

    solutions: list[Any] = []
    for problem, solver_agent in solver_pairs:
        _narrate(f"{solver_agent.name} claiming '{problem.title}'")
        await exchange.claim_problem(problem.id, solver_agent.id)
        solver_names[str(problem.id)] = solver_agent.name
        _push_state(exchange, solver_names)
        await asyncio.sleep(0.2)

        _narrate(f"{solver_agent.name} solving '{problem.title}' via {model}...")
        solution_text = await solver_agent.solve(problem.description)
        sol = await exchange.solve_problem(
            problem.id, solver_agent.id, solution_body=solution_text,
        )
        solutions.append((problem, sol, solver_agent))
        _push_state(exchange, solver_names)
        await asyncio.sleep(0.3)

    # ── Reviews ───────────────────────────────────────────────────────
    _phase("Peer review -- Carol and Dave weighing in")
    await asyncio.sleep(0.5)

    for problem, sol, _ in solutions:
        for reviewer_agent in (carol, dave):
            _narrate(f"{reviewer_agent.name} reviewing solution for '{problem.title}'...")
            review_prompt = (
                f"Review this solution to the problem below.\n\n"
                f"PROBLEM: {problem.description}\n\n"
                f"SOLUTION: {sol.body}\n\n"
                f"Reply with exactly one of: APPROVE or REJECT, followed by a brief reason."
            )
            review_text = await reviewer_agent.solve(review_prompt)

            # Parse verdict from LLM response
            upper = review_text.upper()
            if "APPROVE" in upper:
                verdict = ReviewVerdict.APPROVE
            elif "REJECT" in upper:
                verdict = ReviewVerdict.REJECT
            else:
                verdict = ReviewVerdict.APPROVE  # default to approve if unclear

            await exchange.submit_review(Review(
                solution_id=sol.id,
                reviewer_id=reviewer_agent.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=verdict,
                body=review_text[:500],
            ))
            _push_state(exchange, solver_names)
            await asyncio.sleep(0.5)

    # ── Final state ───────────────────────────────────────────────────
    _phase("Demo complete -- final standings")
    await asyncio.sleep(0.5)
    _push_state(exchange, solver_names)

    print("\n" + "="*60)
    print("  FINAL REPUTATION LEADERBOARD")
    print("="*60)
    for rank, (aid, score) in enumerate(exchange.ledger.leaderboard(), 1):
        name = agent_registry.get(str(aid), str(aid))
        print(f"  #{rank}  {name:<12}  {score:>5} pts")

    print("\n  Open your browser: http://localhost:{}\n".format(port))

    _narrate("All done! Check the leaderboard panel.")


# ── Entry point ───────────────────────────────────────────────────────

def main() -> None:
    # Ensure UTF-8 output on Windows (avoids cp1252 failures for any remaining unicode)
    os.environ.setdefault("PYTHONUTF8", "1")

    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Schwarma live demo -- watch multi-agent collaboration in your browser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples
            --------
            MiniMax via Anthropic-compatible SDK (or use .env):
              python examples/live_demo.py --api-key YOUR_KEY \\
                --base-url https://api.minimaxi.chat/v1 --model MiniMax-M2.5

            Standard Anthropic Claude:
              python examples/live_demo.py --api-key sk-ant-... \\
                --model claude-3-5-haiku-20241022

            With .env file -- just run:
              python examples/live_demo.py
        """),
    )
    parser.add_argument("--api-key",  default=os.environ.get("MINIMAX_API_KEY"),
                        help="LLM API key (default: $MINIMAX_API_KEY from .env)")
    parser.add_argument("--base-url", default=os.environ.get("MINIMAX_BASE_URL"),
                        help="Custom API base URL (default: $MINIMAX_BASE_URL from .env)")
    parser.add_argument("--model",    default=os.environ.get("MINIMAX_MODEL", "claude-3-5-haiku-20241022"),
                        help="Model name (default: $MINIMAX_MODEL from .env)")
    parser.add_argument("--port",     type=int, default=7741, help="Local HTTP port (default: 7741)")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key is required (or set MINIMAX_API_KEY in .env)")

    # ── Start HTTP server in background thread ─────────────────────
    t = threading.Thread(target=_run_server, args=(args.port,), daemon=True)
    t.start()
    print("\n  Schwarma live demo")
    print(f"  Open in browser -> http://localhost:{args.port}")
    print(f"  Model: {args.model}")
    if args.base_url:
        print(f"  Base URL: {args.base_url}")
    print("\n  Waiting 10 s for browser to load before starting demo...")
    time.sleep(10)

    # ── Run demo in asyncio event loop ─────────────────────────────
    asyncio.run(run_demo(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        port=args.port,
    ))

    # Keep server alive after demo finishes; re-broadcast state every 5 s
    # so any browser opened after the demo still sees the final leaderboard.
    print("\n  Demo finished. Server still running. Press Ctrl+C to exit.\n")
    try:
        while True:
            time.sleep(5)
            if _last_state_payload is not None:
                with _sse_lock:
                    for _q in _sse_queues:
                        _q.put_nowait(_last_state_payload)
    except KeyboardInterrupt:
        print("\n  Goodbye!")


if __name__ == "__main__":
    main()
