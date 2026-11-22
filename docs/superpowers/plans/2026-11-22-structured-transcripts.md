# Structured Run Transcripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture each worker CLI's structured event stream, normalize it into one transcript model, persist it per run, and render a faithful per-worker transcript (commands, tool calls, outputs, tokens) in the Work drawer.

**Architecture:** Each CLI is switched to its structured output mode. A per-driver parser normalizes heterogeneous formats into one `TranscriptEvent` model. The process layer reassembles stdout lines, feeds the parser live, and appends normalized events to a `*-transcript.jsonl` beside the raw `.log`. A generic server endpoint tails that file; the UI polls it. Final-text/session extraction is reworked to parse the structured stream from full stdout (not the truncated transcript file).

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest; static HTML + Alpine.js + Tailwind (no build step).

**Spec:** `docs/specs/structured-transcripts-design.md`

## Global Constraints

- Transcript capture/parse failure MUST NEVER fail or crash a task — catch and log, same as existing run/worklog updates (`observability must never fail the task`).
- `extract_response_text` / `extract_session` parse the **full untruncated `result.stdout`**, never the persisted (truncated) transcript file. Task-runner call sites keep passing `result.stdout`/`result.stderr`; `ProcessResult` and the `ExecProcess` protocol are unchanged.
- No PTY. Container exec stays non-TTY + `demux=True`; only stdout is fed to the parser.
- No redaction anywhere — transcript stored, served, and rendered raw.
- Per-event body truncation cap: 16 KB (mark truncation in `meta`). Transcript file served under the existing `log_dir()` path-safety (`_safe_file`).
- Follow existing patterns: Pydantic models in `server/models.py`, SQLite migration via an `_ensure_*_columns` helper in `server/db.py`, driver methods on `WorkerDriver`.

## Review Focus

- **NDJSON line split across stream chunks** — a `tool_use` JSON object arriving in two reader chunks must parse as one event, not two raw fragments. (Task 7 tests.)
- **Malformed / non-JSON stdout line** (e.g. a stray log line, opencode stderr noise) — must become `kind:"raw"`, never raise. (Tasks 1, 7 tests.)
- **Empty / absent transcript file** on the endpoint — a run with no `transcript_path` or an empty file returns `[]`, not 500. (Task 9 tests.)
- **Long final assistant message** (> 16 KB) — fact/intent extraction still recovers it exactly because it parses full stdout, even though the on-disk transcript event is truncated. (Tasks 3–5 extraction tests use an oversized message.)
- **`after_seq` past the end** — endpoint returns `[]`, and `after_seq` larger than the file's line count does not error. (Task 9 tests.)

---

### Task 1: Transcript event model + base parser

**Files:**
- Create: `cairn/src/cairn/dispatcher/workers/transcript.py`
- Modify: `cairn/src/cairn/dispatcher/workers/base.py` (default `new_transcript_parser`)
- Test: `cairn/tests/test_transcript_parsers.py`

**Interfaces:**
- Produces:
  - `TranscriptEvent` dataclass: `kind: str`, `tool: str | None = None`, `title: str | None = None`, `text: str = ""`, `meta: dict = {}`; method `to_dict() -> dict`. `kind` ∈ `system|assistant_text|thinking|tool_call|tool_result|usage|result|error|raw`.
  - `class TranscriptParser` base: `feed(self, line: str) -> list[TranscriptEvent]` (default: one `raw` event per non-empty line), `finish(self) -> list[TranscriptEvent]` (default `[]`).
  - `RawParser(TranscriptParser)`: inherits the default behavior (explicit subclass for `new_transcript_parser` fallback).
  - helper `parse_json_lines(text: str) -> list[dict]`: JSON-decode each non-empty line, skip undecodable lines.
  - helper `truncate(text: str, cap: int = 16384) -> tuple[str, bool]`: returns `(clipped, was_truncated)`.
  - default method on `WorkerDriver` (base.py): `new_transcript_parser(self) -> TranscriptParser | None` returning `None` (overridden by the four real drivers; mock/unknown drivers get no transcript, which Task 8 guards).

- [ ] **Step 1: Write failing tests** in `test_transcript_parsers.py`:
  - `test_raw_parser_emits_one_event_per_line` — `feed("a\n")` + `feed("b\n")` yields two `raw` events with `text` `"a"`,`"b"`.
  - `test_raw_parser_ignores_blank_lines` — blank/whitespace line yields `[]`.
  - `test_parse_json_lines_skips_malformed` — input `'{"x":1}\nnot json\n{"y":2}'` → `[{"x":1},{"y":2}]`.
  - `test_truncate_marks_when_over_cap` — string of 20000 chars → length 16384 and `True`.
- [ ] **Step 2: Run** `pytest cairn/tests/test_transcript_parsers.py -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `transcript.py` with the interfaces above.
- [ ] **Step 4: Run** the same tests — Expected: PASS.
- [ ] **Step 5: Commit** `feat(transcript): add normalized transcript event model and base parser`.

---

### Task 2: DB migration, models, protocol client for `transcript_path`

**Files:**
- Modify: `cairn/src/cairn/server/db.py` (runs CREATE TABLE + new `_ensure_run_columns`)
- Modify: `cairn/src/cairn/server/models.py` (`Run`, `CreateRunRequest`, `UpdateRunRequest`)
- Modify: `cairn/src/cairn/dispatcher/protocol/client.py` (`create_run`, `update_run`)
- Test: `cairn/tests/test_db_migrations.py`, `cairn/tests/test_runs_api.py`

**Interfaces:**
- Produces: `Run.transcript_path: str | None`, `CreateRunRequest.transcript_path: str | None`, `UpdateRunRequest.transcript_path: str | None`; `create_run(..., transcript_path: str | None = None)` and `update_run(..., transcript_path: str | None = None)` — both mirror the existing `log_path`/`worklog_path` handling.

- [ ] **Step 1: Write failing tests**:
  - in `test_db_migrations.py`: `test_runs_gets_transcript_path_column` — open a DB whose `runs` table lacks `transcript_path`, call `configure`, assert the column exists via `PRAGMA table_info(runs)`.
  - in `test_runs_api.py`: `test_create_and_get_run_round_trips_transcript_path` — POST a run with `transcript_path`, GET it back, assert equal; PATCH `transcript_path`, GET, assert updated.
- [ ] **Step 2: Run** both — Expected: FAIL.
- [ ] **Step 3: Implement**: add `transcript_path TEXT` to the `runs` CREATE TABLE; add `_ensure_run_columns(conn)` (same shape as `_ensure_project_columns`) and call it from `configure`; add the field to the three models; add the param to `create_run`/`update_run` (include in payload only when not `None`, like `worklog_path`).
- [ ] **Step 4: Run** both — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runs): persist transcript_path across db, models, protocol`.

---

### Task 3: Claude adapter — stream-json argv, parser, extraction

**Files:**
- Modify: `cairn/src/cairn/dispatcher/workers/adapters/claudecode.py`
- Modify: `cairn/src/cairn/dispatcher/workers/transcript.py` (add `ClaudeTranscriptParser`)
- Create fixture: `cairn/tests/fixtures/transcripts/claudecode.jsonl`
- Test: `cairn/tests/test_transcript_parsers.py`, `cairn/tests/test_contracts_and_drivers.py`

**Interfaces:**
- Consumes: `TranscriptEvent`, `TranscriptParser`, `parse_json_lines`, `truncate` (Task 1).
- Produces: `ClaudeCodeDriver.new_transcript_parser() -> ClaudeTranscriptParser`; reworked `extract_response_text`.

- [ ] **Step 1: Capture fixture** — run `claude -p --output-format stream-json --verbose -- "reply with the single word hi"` and save stdout to the fixture. Confirm whether lines arrive incrementally (note in commit message). Mapping to assert: `type=="system"`→`system`; `type=="assistant"` content blocks → `text`→`assistant_text`, `tool_use`→`tool_call` (`tool`=name, `text`=`json.dumps(input)`); `type=="user"` `tool_result`→`tool_result` (`meta.is_error`); `type=="result"`→`result` (`meta` = `total_cost_usd`, `usage`).
- [ ] **Step 2: Write failing tests**:
  - `test_claude_parser_maps_tool_use_and_result` — feed the fixture lines, assert the sequence of `kind`/`tool` values and that the `result` event carries cost/usage in `meta`.
  - `test_claude_extract_response_text_from_result` — `extract_response_text(fixture_text, "")` returns the final assistant text (from the `result` event, fallback concatenated `assistant` text).
  - `test_claude_execute_argv_uses_stream_json` — `build_execute(...).argv` contains `--output-format`, `stream-json`, `--verbose`.
- [ ] **Step 3: Run** — Expected: FAIL.
- [ ] **Step 4: Implement**: add the three flags to `build_execute` and `build_conclude`; implement `ClaudeTranscriptParser.feed` (stateful line parse via `parse_json_lines` per line) and `extract_response_text` (parse stream-json, prefer `result`); add `new_transcript_parser`.
- [ ] **Step 5: Run** — Expected: PASS.
- [ ] **Step 6: Commit** `feat(claudecode): stream-json transcript parser and extraction`.

---

### Task 4: Codex adapter — `--json` argv, parser, extraction, session

**Files:**
- Modify: `cairn/src/cairn/dispatcher/workers/adapters/codex.py`
- Modify: `cairn/src/cairn/dispatcher/workers/transcript.py` (add `CodexTranscriptParser`)
- Create fixture: `cairn/tests/fixtures/transcripts/codex.jsonl`
- Test: `test_transcript_parsers.py`, `test_contracts_and_drivers.py`

**Interfaces:**
- Produces: `CodexDriver.new_transcript_parser()`; reworked `extract_response_text`; `extract_session` reads the session id from a JSON event, falling back to the existing `session_pattern` regex on stderr.

- [ ] **Step 1: Capture fixture** — `codex exec --json --dangerously-bypass-approvals-and-sandbox -- "reply with the single word hi"`; save JSONL. Pin the event field names (agent message, tool/command events, session/thread id) from the captured output.
- [ ] **Step 2: Write failing tests**:
  - `test_codex_parser_maps_events` — fixture → expected `kind`/`tool` sequence.
  - `test_codex_extract_response_text` — returns final agent message text.
  - `test_codex_extract_session_from_event` — session id recovered from the JSON event; and `test_codex_extract_session_regex_fallback` — when no JSON id, the stderr regex still works.
  - `test_codex_execute_argv_has_json` — argv contains `--json` for both `build_execute` and `build_conclude`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement**. **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(codex): jsonl transcript parser, extraction, event-based session`.

---

### Task 5: OpenCode adapter — `--format json`, parser, extraction

**Files:**
- Modify: `cairn/src/cairn/dispatcher/workers/adapters/opencode.py`
- Modify: `cairn/src/cairn/dispatcher/workers/transcript.py` (add `OpenCodeTranscriptParser`)
- Create fixture: `cairn/tests/fixtures/transcripts/opencode.json`
- Test: `test_transcript_parsers.py`, `test_contracts_and_drivers.py`

**Interfaces:**
- Produces: `OpenCodeDriver.new_transcript_parser()`; reworked `extract_response_text`.

- [ ] **Step 1: Capture fixture** — `opencode run --standalone --auto --format json -- "reply with the single word hi"` (via the same auth-import wrapper if needed); save output. **Confirm streamed-per-event vs single final document** and record it; if final-only, the parser's `feed` buffers and `finish()` emits all events (transcript populates at run end — acceptable per spec).
- [ ] **Step 2: Write failing tests**:
  - `test_opencode_parser_maps_events` — fixture → expected `kind`/`tool` sequence (or, if final-doc, `finish()` emits them).
  - `test_opencode_extract_response_text` — returns final message text.
  - `test_opencode_execute_argv_uses_json` — `_argv(...)` contains `--format`,`json` and not `default`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement**. **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(opencode): json transcript parser and extraction`.

---

### Task 6: Pi adapter — transcript parser (reuse existing event mapping)

**Files:**
- Modify: `cairn/src/cairn/dispatcher/workers/adapters/pi.py` (add `new_transcript_parser`)
- Modify: `cairn/src/cairn/dispatcher/workers/transcript.py` (add `PiTranscriptParser`)
- Create fixture: `cairn/tests/fixtures/transcripts/pi.jsonl`
- Test: `test_transcript_parsers.py`

**Interfaces:**
- Produces: `PiDriver.new_transcript_parser()`. `extract_response_text`/`extract_session` stay as-is (already event-based); argv unchanged (already `--mode json`).

- [ ] **Step 1: Capture fixture** — `pi --mode json -p "reply with the single word hi"` (minimal flags); save NDJSON. Map `session`→`system`(session id in meta), tool events→`tool_call`/`tool_result`, `turn_end`/`agent_end` assistant text→`assistant_text`.
- [ ] **Step 2: Write failing test** `test_pi_parser_maps_events` — fixture → expected normalized sequence.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement** `PiTranscriptParser` reusing the existing `_iter_events` shape; `new_transcript_parser`. **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(pi): transcript parser over existing json events`.

---

### Task 7: Live capture plumbing in the process layer

**Files:**
- Modify: `cairn/src/cairn/dispatcher/runtime/process.py` (`ManagedProcess`)
- Modify: `cairn/src/cairn/dispatcher/runtime/local_process.py` (`LocalProcess`)
- Modify: `cairn/src/cairn/dispatcher/runtime/backend.py` (`ExecutionBackend.build_exec_process` signature)
- Modify: `cairn/src/cairn/dispatcher/runtime/containers.py` and `local_backend.py` (`build_exec_process`)
- Test: `cairn/tests/test_local_execution.py`

**Interfaces:**
- Consumes: `TranscriptParser` (Task 1).
- Produces: `build_exec_process(..., log_path=None, transcript_path: str | None = None, transcript_parser: TranscriptParser | None = None)` on both backends and the Protocol; `ManagedProcess`/`LocalProcess` accept `transcript_path` + `transcript_parser`.
- Behavior: a stdout-only line buffer accumulates text; on each stdout chunk, complete `\n`-terminated lines are fed to `transcript_parser.feed`; returned events are stamped with a monotonic `seq` (int, from an instance counter — single stdout writer thread) and `ts` (ISO-8601 UTC) and appended as JSON lines to `transcript_path`. On stream close, `finish()` is flushed and any trailing partial line is fed. All parser/IO errors are caught and logged; never propagated. Raw `.log` writing is unchanged.

- [ ] **Step 1: Write failing tests** (use `LocalProcess`, no Docker):
  - `test_local_process_writes_transcript` — command `printf '{"type":"a"}\n{"type":"b"}\n'` with a stub parser that maps each dict to one `assistant_text` event → transcript file has 2 JSON lines, each with ascending `seq` and a `ts`.
  - `test_transcript_line_split_across_chunks` — stub parser; feed a command that emits a long line in pieces (or unit-test the line buffer directly) → the object parses as one event.
  - `test_transcript_parser_error_does_not_crash` — stub parser whose `feed` raises → `communicate()` still returns a normal `ProcessResult`, transcript file has no crash artifact.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**: add the line buffer + transcript writer to both process classes (mirror `_append_log`/`_close_log` with a `_transcript_lock`), feed only stdout, add the two params through both backends and the Protocol.
- [ ] **Step 4: Run** `pytest cairn/tests/test_local_execution.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(runtime): live structured transcript capture in process layer`.

---

### Task 8: Record the transcript in `run_worker_process` + wire drivers

**Files:**
- Modify: `cairn/src/cairn/dispatcher/tasks/common.py` (`run_worker_process`)
- Modify: `cairn/src/cairn/dispatcher/tasks/{explore,bootstrap,reason}.py` (pass `driver`)
- Test: `cairn/tests/test_worker_tasks.py`

**Interfaces:**
- Consumes: `driver.new_transcript_parser()` (Tasks 3–6); `build_exec_process(transcript_path=, transcript_parser=)` (Task 7); `create_run`/`update_run(transcript_path=)` (Task 2).
- Produces: `run_worker_process(..., driver: WorkerDriver | None = None)`. Creates `transcript_path = project_dir / f"{run_id}-{phase}-{worker.name}-transcript.jsonl"` (chmod 600, next to `log_path`); passes parser + path into `build_exec_process`; records `transcript_path` on `create_run` and on the final `update_run`.

- [ ] **Step 1: Write failing test** `test_run_worker_process_records_transcript` — with the local backend, a stub driver whose parser maps lines to events, and a command emitting NDJSON: assert the created run carries a `transcript_path`, the file exists with normalized events, and the run update includes it. (Extend existing `test_worker_tasks` fakes.)
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**: thread `driver` into `run_worker_process`, build the path, call `driver.new_transcript_parser()` (guard `None` → skip), pass through; update the three call sites to pass `driver` (already in scope at each site).
- [ ] **Step 4: Run** `pytest cairn/tests/test_worker_tasks.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(tasks): record per-run transcript path and feed driver parser`.

---

### Task 9: Transcript endpoint

**Files:**
- Modify: `cairn/src/cairn/server/routers/runs.py`
- Test: `cairn/tests/test_runs_api.py`

**Interfaces:**
- Produces: `GET /runs/{run_id}/transcript?after_seq:int=-1&limit:int=2000` → `list[dict]` (each the stored event plus `seq` = 0-based line index). Reads `transcript_path` via `_safe_file(..., log_dir())`; returns events whose line index `> after_seq`, capped to `limit`. No `transcript_path` or empty file → `[]`. Malformed line → skipped (not an error).

- [ ] **Step 1: Write failing tests** in `test_runs_api.py`:
  - `test_get_transcript_returns_events` — create a run whose `transcript_path` points at a temp jsonl under `log_dir()`; GET returns the parsed events with ascending `seq`.
  - `test_get_transcript_after_seq_pages` — `after_seq=0` omits line 0; `after_seq` past end → `[]`.
  - `test_get_transcript_missing_file_returns_empty` — run with no `transcript_path` → `[]` (or empty), not 500.
  - `test_get_transcript_rejects_path_outside_log_dir` — a `transcript_path` outside `log_dir()` → 403.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** the route (reuse `_get_run_or_404`, `_safe_file`; a small helper to read/paginate jsonl lines).
- [ ] **Step 4: Run** `pytest cairn/tests/test_runs_api.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): structured transcript endpoint with after_seq paging`.

---

### Task 10: UI — render the structured transcript

**Files:**
- Modify: `cairn/src/cairn/server/static/index.html`

**Interfaces:**
- Consumes: `GET /runs/{id}/transcript?after_seq=` (Task 9).

- [ ] **Step 1: Implement** in the Work drawer:
  - Add `workTranscript: []` and `workTranscriptSeq: -1` to the Alpine state; in `refreshWork()`, when `workTab === 'transcript' || workTab === 'commands'`, fetch `/runs/${id}/transcript?after_seq=${workTranscriptSeq}`, append new events, advance `workTranscriptSeq` to the last `seq`. Reset both when `openWork` changes run.
  - Replace the `<pre x-text="workLogView()">` block (lines ~1144–1146) with an event-row renderer: `assistant_text`/`thinking` as prose; `tool_call` as a labeled block (`$ <tool>` + `text`); `tool_result` as a collapsible `<details>` (show `meta.is_error`/`exit_code`); `usage`/`result` as a footer line (tokens + `meta.total_cost_usd`). Follow the sketch in the design doc §6.
  - `commands` view = the same list filtered to `tool_call`/`tool_result`.
  - Keep the Worklog tab and the raw-log **Download** (fetch `/runs/${id}/log` on demand for download). No redaction applied to transcript rendering.
- [ ] **Step 2: Verify manually** — start the app (use the `run` skill / project launch), open a project with a finished run, open the Work drawer → Transcript shows structured rows (commands, outputs, token/cost); Commands tab filters; Worklog tab unchanged; Download still yields the raw `.log`. (No automated UI test harness exists in this repo.)
- [ ] **Step 3: Commit** `feat(ui): render structured run transcript in Work drawer`.

---

### Task 11 (optional): Live WORKLOG copy

**Files:**
- Modify: `cairn/src/cairn/dispatcher/tasks/common.py`
- Test: `cairn/tests/test_worker_tasks.py`

Spec marks this deferrable (§7). Implement only if Task 10 shipped cleanly and time allows.

- [ ] **Step 1: Write failing test** `test_worklog_copied_mid_run` — a worklog present before run end is readable via the run's `worklog_path` before `finish`.
- [ ] **Step 2–4:** add a periodic `_copy_worklog` (e.g. on the heartbeat tick) guarded so failure never affects the run; run the test to green.
- [ ] **Step 5: Commit** `feat(tasks): copy WORKLOG.md live during run`.

---

### Final: whole-suite verification

- [ ] Run `pytest` (full suite) from the repo root — Expected: all green.
- [ ] Spot-run one real project end-to-end if a worker is configured, and confirm a transcript file appears under `log_dir()` and renders in the UI.
