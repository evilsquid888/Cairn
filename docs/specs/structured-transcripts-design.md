# Structured Run Transcripts — Design

Date: 2026-11-22
Status: Proposed (awaiting approval)

## Intent / Why

Cairn's web observability is too thin to tell what a worker is actually doing. Each
run surfaces two things:

1. A raw `.log` — stdout/stderr of the CLI exec, streamed live to a host file and served
   by `GET /runs/{id}/log`.
2. A `WORKLOG.md` — a markdown file the **agent writes about itself** inside the container,
   copied out **only when the run ends** and served by `GET /runs/{id}/worklog`.

The gap is not the log plumbing — it is that the CLIs are invoked in their **human output
modes** (`claude -p`, `opencode run --format default`, `codex exec`), so the `.log` captures
only the final assistant text, never the tool calls, bash commands, file edits, reasoning, or
token/cost accounting. The `WORKLOG.md` is the agent's self-summary, not ground truth, and it
is not live.

Every CLI we use can instead emit a **structured event stream** (one event per step: tool
call, tool result, assistant message, usage). The goal of this work is to capture that stream,
normalize it into a single transcript model, persist it, and render a faithful per-worker
transcript — commands, tool calls, outputs, tokens — in the existing Work drawer, refreshed
near-live.

This keeps the harness architecture (host-auth inheritance, maintained off-the-shelf agents,
swappable workers). The user explicitly chose this over an inline-API agent loop.

## Goals

- Capture the **structured event stream** from all four real adapters (claudecode, opencode,
  codex, pi) in addition to the raw log.
- Normalize heterogeneous CLI formats into **one transcript event model** so the server and UI
  are format-agnostic.
- Persist the normalized transcript per run and serve it via a new endpoint.
- Render a readable transcript in the existing Work drawer: assistant text, tool/command
  calls with their inputs, tool results/outputs, and per-run usage/cost — refreshed by polling
  (near-live), matching the drawer's current auto-refresh.
- Keep fact/intent extraction and session resume working after the output-format switch.

## Non-goals

- No SSE/WebSocket push. The user chose **poll/tail refresh**; the UI extends its existing
  periodic `refreshWork()`.
- No inline-API agent loop. We stay on the CLI harness.
- No change to the blackboard protocol (Facts/Intents/Hints) or the scheduler.
- No new retention/rotation policy beyond today's file-size/tail caps.
- `mock` adapter needs no structured mode (tests may add a trivial one if convenient).

## Anti-goals (would make this a failure even if it "works")

- Breaking fact/intent extraction: the switch to structured output changes stdout format, and
  the response text that feeds `output_parser` must still be recovered exactly.
- Breaking session resume (`extract_session`) for codex (currently regex on stderr) and the
  seed-session drivers.
- Letting transcript capture/parse failure **fail or crash a task**. Observability must never
  break a run — same rule already applied to run/worklog updates.

## Constraints

- Container execution reads the exec stream demuxed (stdout/stderr split) in a reader thread
  (`ManagedProcess._read_stream`), already writing raw bytes live to the `.log`. Structured
  events arrive on **stdout** as NDJSON; stderr may still carry logs (e.g. opencode
  `--print-logs`). Both continue to land in the raw `.log`.
- NDJSON lines can span stream chunks, so parsing needs **line reassembly**, not per-chunk
  parsing.
- Local execution (`LocalProcess`) must get the same treatment as container execution.
- The server must stay decoupled from dispatcher driver internals (today it imports no
  drivers). Normalization therefore happens **in the dispatcher**, and the server serves a
  generic, already-normalized file.
- **Buffering / liveness policy.** Container exec runs non-TTY with `demux=True`, so stdout and
  stderr arrive split and clean. Some CLIs block-buffer stdout when it is a pipe, so "near-live"
  is best-effort per adapter: wrap commands with `stdbuf -oL` / CLI unbuffer flags where they
  exist; if a CLI still block-buffers, its transcript populates at run end (still a large
  improvement). We do **not** introduce a PTY to force flushing, because that collapses the
  stdout/stderr split and interleaves logs (e.g. opencode `--print-logs`) into the NDJSON parser.
  Actual delivery timing is confirmed at build time alongside event shapes.

## CLI structured-output facts (verified via `--help` on host 2026-11-22)

| Adapter | Change | Emits |
|---|---|---|
| claudecode | add `--output-format stream-json --verbose` | NDJSON: `system/init`, `assistant` (text + `tool_use`), `user` (`tool_result`), `result` (usage, cost) |
| opencode | `--format default` → `--format json` | JSON output (per-help `default|json`); **shape/streaming to be confirmed at build time** — may be final-document rather than per-event streamed. `--print-logs` available for live stderr. |
| codex | add `--json` to `codex exec` | JSONL events ("Print events to stdout as JSONL") |
| pi | **none** — already `--mode json` | NDJSON: `session`, `turn_end`, `agent_end`, tool events (already parsed by `PiDriver`) |

Open verification item for the implementer (build-time, cheap): run each CLI once on a trivial
prompt and capture the exact event JSON to pin down field names, and specifically confirm
whether `opencode --format json` **streams per-event** or emits a **single final document**.
If opencode is final-only, its transcript is populated at run end (still a large improvement);
all others are near-live. This does not change the architecture.

## Approaches considered

**A. Server parses the raw `.log` on read.**
The server would need to know each run's driver type and implement all four format parsers.
Rejected: couples the server to dispatcher internals and duplicates parsing; server currently
imports no drivers.

**B. Dispatcher normalizes live; server serves a generic normalized file. (CHOSEN)**
Each driver exposes a stateful event parser. A line-reassembling sink in the process layer
feeds complete stdout lines to that parser as they arrive; normalized events are appended to a
per-run `*-transcript.jsonl` while the raw bytes still stream to `.log`. The server gains a
generic `GET /runs/{id}/transcript` that tails the normalized jsonl (format-agnostic). The UI
renders structured events. The same normalized events also drive `extract_response_text` /
`extract_session`, replacing today's raw-stdout heuristics. Chosen: keeps the server dumb,
keeps normalization next to the drivers that understand each format, and is near-live for poll.

**C. Harness-as-server + SSE subscription.**
Run e.g. opencode in server mode and subscribe to its event stream. Rejected: uneven across
CLIs (`claude -p` is one-shot), and the user chose poll over push.

## Design (Approach B)

### 1. Normalized transcript event model

One object per line in `*-transcript.jsonl`, also the shape returned by the transcript
endpoint:

```
{
  "seq":   int,            # monotonic per run
  "ts":    ISO-8601,       # when the dispatcher parsed it
  "kind":  "system" | "assistant_text" | "thinking" | "tool_call"
           | "tool_result" | "usage" | "result" | "error" | "raw",
  "tool":  str | null,     # e.g. "bash", "read", "edit"  (tool_call/result)
  "title": str | null,     # short label for the UI row
  "text":  str,            # human-readable body (message / command / output)
  "meta":  object          # tokens, cost_usd, exit_code, duration_ms, is_error, ids …
}
```

`kind: "raw"` is the fallback for any line that does not match a known event, so nothing is
silently dropped. Bodies are truncated to a per-event cap (implementer's call, ~16 KB) to bound
file growth; truncation is marked in `meta`.

### 2. Per-driver parser

Add to `WorkerDriver` (base):

```python
def new_transcript_parser(self) -> "TranscriptParser": ...
```

`TranscriptParser` is stateful (`feed(line: str) -> list[Event]`, `finish() -> list[Event]`)
so drivers can coalesce multi-line/streamed structures and assign `seq`. Implement one per
real adapter; `pi` reuses its existing `_iter_events` mapping. A default parser emits each
non-empty line as `kind: "raw"` so unknown/mock drivers degrade gracefully.

### 3. Final-text and session extraction from the stream

`extract_response_text` and `extract_session` are reworked to parse the driver's structured
format, but they run over the **full, untruncated `result.stdout`** (re-parsed at run end with
the same parser) — **not** the persisted `*-transcript.jsonl`, whose event bodies are truncated.
This keeps a single event model while guaranteeing exact recovery of the final text that feeds
`output_parser`. No change to `ProcessResult`, the `ExecProcess` protocol, or task-runner call
sites (they still pass `result.stdout`/`result.stderr`).

- claudecode: final text from the `result` event (fallback: concatenated `assistant` text).
  Session stays seed-based (unchanged).
- codex: final text from the last agent message event; **session id from a JSON event**
  instead of the stderr regex (keep the regex as fallback).
- opencode: final text from the final message in the json output.
- pi: unchanged (already event-based).

This is the **highest-risk coupling** — it feeds `output_parser` → Facts/Intents. Covered by
TDD against captured real event samples (see Testing).

### 4. Live capture plumbing

`ManagedProcess` / `LocalProcess` gain an optional **line sink** alongside the existing raw log
file:

- Reader keeps appending raw bytes to `.log` (unchanged).
- A new buffer reassembles complete `\n`-terminated stdout lines and calls the driver's parser;
  normalized events are appended (newline-delimited JSON) to the transcript file as they arrive.
- All parser/IO errors are caught and logged; they never propagate into the run result.
- `finish()` flushes any trailing buffered line at stream close.

`run_worker_process` (tasks/common.py) creates the transcript path next to `log_path`
(`{run_id}-{phase}-{worker}-transcript.jsonl` in the same per-project `log_dir`), passes the
driver's parser into `build_exec_process`, and records the path on the run.

### 5. Persistence & API

- Add `transcript_path TEXT` to the `runs` table (migration, mirroring `worklog_path`).
- `CreateRun`/`UpdateRun` models + protocol client carry `transcript_path`.
- New endpoint `GET /runs/{id}/transcript?after_seq=&limit=` returning a JSON array of
  normalized events (tail/offset so the UI fetches only new events). Reuses the existing
  `_safe_file` path-safety and size caps. The raw `.log` endpoint stays for download/debug.

### 6. UI (Work drawer)

The drawer already has a **Transcript** tab (and a hinted **commands** view) that currently
dumps raw `.log` text. Change the Transcript tab to:

- Fetch `/runs/{id}/transcript` (incrementally via `after_seq`) in `refreshWork()`.
- Render events as structured rows: assistant text as prose; `tool_call` as a labeled command
  block (e.g. `$ <bash>` / `read <path>`); `tool_result` as collapsible output; a header/footer
  showing tokens + cost from `usage`/`result`.
- The hinted **commands** view becomes "tool_call/tool_result only" (filter of the same data).
- Worklog tab unchanged. Raw log remains available via Download.
- Keep the existing auto-refresh-while-open behavior (poll). No new transport.

Text sketch of a transcript row group:

```
┌ assistant ───────────────────────────────────────────────
│ I'll scan the host for open ports, then enumerate HTTP.
├ $ bash ──────────────────────────────────────────────────
│ nmap -sV -p- 10.10.10.5
├ ⮑ result  (exit 0, 2.3s) ───────────────────────── [show] ┤
│ 22/tcp open ssh …  80/tcp open http …
└ usage: 12.4k in / 1.1k out · $0.08 ──────────────────────
```

### 6a. Redaction — none (decided)

The transcript is stored, served, and rendered **raw**: no secret/flag redaction anywhere.
This matches the raw `.log`, which is already unredacted on disk, and is the user's explicit
call for a self-hosted security tool where the operator wants ground truth (flags, creds,
tokens) visible. No redaction pass is added in the dispatcher, endpoint, or UI for the
transcript. (The existing client-side redact toggle over the raw-log blob is left untouched.)

### 7. WORKLOG liveness (small, in-scope bonus)

Keep `WORKLOG.md`, but copy it **on each poll** (or periodically) rather than only at run end,
so the Worklog tab is live too. Low risk; reuses `_copy_worklog`. Implementer may defer if it
complicates the capture path.

## Testing

- Capture **one real event sample per CLI** (trivial prompt) into fixtures; unit-test each
  parser → normalized events, and each `extract_response_text` / `extract_session` against the
  fixtures (TDD; write the parser tests first).
- Line-reassembly test: events split across arbitrary chunk boundaries.
- Robustness: malformed/partial lines yield `kind: "raw"`, never raise.
- Endpoint test: `after_seq`/`limit` paging; path safety (reuse run-router test patterns).
- Migration test (mirror existing `test_db_migrations`).
- Update `test_contracts_and_drivers`, `test_worker_tasks` for the new argv flags and
  stream-based extraction.
- Full `pytest` green before done.

## What's left to the builder

- Exact event field names per CLI (pin from captured fixtures), and the opencode
  streamed-vs-final confirmation noted above.
- Per-event truncation cap and transcript file size cap (consistent with existing `.log`/tail
  caps).
- Whether the WORKLOG-liveness bonus ships now or is deferred.
- UI rendering polish (collapse/expand, color) within the existing Tailwind/Alpine style.
```
