from __future__ import annotations

from cairn.dispatcher.workers.transcript import (
    RawParser,
    TranscriptEvent,
    parse_json_lines,
    truncate,
)


def test_raw_parser_emits_one_event_per_line():
    p = RawParser()
    events = p.feed("a\n") + p.feed("b\n")
    assert [e.kind for e in events] == ["raw", "raw"]
    assert [e.text for e in events] == ["a", "b"]


def test_raw_parser_ignores_blank_lines():
    p = RawParser()
    assert p.feed("   \n") == []
    assert p.feed("") == []
    assert p.finish() == []


def test_parse_json_lines_skips_malformed():
    assert parse_json_lines('{"x":1}\nnot json\n{"y":2}') == [{"x": 1}, {"y": 2}]


def test_truncate_marks_when_over_cap():
    out, cut = truncate("x" * 20000)
    assert len(out) == 16384
    assert cut is True
    assert truncate("abc") == ("abc", False)


def test_event_to_dict():
    d = TranscriptEvent(kind="tool_call", tool="Bash", text="ls").to_dict()
    assert d == {"kind": "tool_call", "tool": "Bash", "title": None, "text": "ls", "meta": {}}


# --- claude code ---

import json as _json
from pathlib import Path as _Path

from cairn.dispatcher.config import WorkerConfig as _WorkerConfig
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.transcript import ClaudeTranscriptParser

_CLAUDE_FIXTURE = _Path(__file__).parent / "fixtures" / "transcripts" / "claudecode.jsonl"


def _claude_worker() -> _WorkerConfig:
    return _WorkerConfig(
        name="c", type="claudecode", task_types=["explore"], max_running=1, priority=0,
        env={"CAIRN_HOST_AUTH": "1"},
    )


def test_claude_parser_maps_tool_use_and_result():
    p = ClaudeTranscriptParser()
    events = []
    for line in _CLAUDE_FIXTURE.read_text().splitlines():
        events += p.feed(line)
    events += p.finish()
    assert [e.kind for e in events] == [
        "system",
        "tool_call",
        "tool_result",
        "assistant_text",
        "result",
    ]
    assert events[0].title == "init"
    assert events[1].tool == "Bash"
    assert _json.loads(events[1].text) == {"command": "echo hello-fixture"}
    assert events[2].text == "hello-fixture"
    assert events[2].meta["is_error"] is False
    assert events[3].text.strip()
    assert "total_cost_usd" in events[-1].meta and "usage" in events[-1].meta
    assert events[-1].text.strip()


def test_claude_parser_never_raises_on_garbage():
    p = ClaudeTranscriptParser()
    assert [e.kind for e in p.feed("not json")] == ["raw"]
    assert p.feed("  ") == []
    assert [e.kind for e in p.feed("[1,2]")] == ["raw"]


def test_claude_parser_truncates_tool_result():
    p = ClaudeTranscriptParser()
    line = _json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "y" * 20000, "is_error": True}]}})
    (ev,) = p.feed(line)
    assert len(ev.text) == 16384
    assert ev.meta["is_error"] is True


def test_claude_extract_response_text_from_result():
    stdout = _CLAUDE_FIXTURE.read_text()
    expected = [_json.loads(l) for l in stdout.splitlines() if _json.loads(l)["type"] == "result"][0]["result"]
    assert expected
    assert ClaudeCodeDriver().extract_response_text(stdout, "") == expected


def test_claude_extract_response_text_oversized_result():
    big = "z" * 50000
    stdout = _json.dumps({"type": "result", "result": big}) + "\n"
    assert ClaudeCodeDriver().extract_response_text(stdout, "") == big


def test_claude_extract_response_text_fallbacks():
    d = ClaudeCodeDriver()
    a = _json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}})
    assert d.extract_response_text(a + "\n", "") == "onetwo"
    assert d.extract_response_text("plain output", "") == "plain output"


def test_claude_extract_response_text_empty_result_falls_back_to_assistant_text():
    a = _json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})
    r = _json.dumps({"type": "result", "result": ""})
    assert ClaudeCodeDriver().extract_response_text(a + "\n" + r + "\n", "") == "hello"


def test_claude_execute_and_conclude_argv_use_stream_json():
    d = ClaudeCodeDriver()
    for argv in (d.build_execute(_claude_worker(), "p", "sid").argv, d.build_conclude(_claude_worker(), "p", "sid")):
        assert "--output-format" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in argv
        assert "--include-partial-messages" in argv
        assert argv[-2:] == ["--", "p"]


_CLAUDE_THINKING_FIXTURE = _Path(__file__).parent / "fixtures" / "transcripts" / "claudecode-thinking.jsonl"


def test_claude_parser_captures_thinking_deltas():
    p = ClaudeTranscriptParser()
    events = []
    for line in _CLAUDE_THINKING_FIXTURE.read_text().splitlines():
        events += p.feed(line)
    events += p.finish()
    assert [e.kind for e in events] == [
        "system",
        "thinking",
        "assistant_text",
        "tool_call",
        "tool_result",
        "result",
    ]
    thinking = [e for e in events if e.kind == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].text == "Let me check the port."
    # no empty thinking events leaked from the redacted assembled block
    assert all(e.text for e in thinking)
    assert [e for e in events if e.kind == "assistant_text"][0].text == "Scanning now."
    assert [e for e in events if e.kind == "tool_call"][0].tool == "Bash"
    # rate_limit_event / tool_progress no longer produce raw
    assert not any(e.kind == "raw" for e in events)


def test_claude_parser_skips_empty_thinking_block():
    p = ClaudeTranscriptParser()
    redacted = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "", "signature": "x"}]}})
    assert p.feed(redacted) == []
    nonempty = _json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"}]}})
    assert [e.kind for e in p.feed(nonempty)] == ["thinking"]


def test_claude_parser_quiets_rate_limit_and_tool_progress():
    p = ClaudeTranscriptParser()
    assert p.feed(_json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})) == []
    assert p.feed(_json.dumps({"type": "tool_progress", "toolUseID": "t"})) == []


def test_claude_parser_flushes_unterminated_thinking_on_finish():
    # a run killed mid-thinking (no content_block_stop) still yields the buffered text
    p = ClaudeTranscriptParser()
    assert p.feed(_json.dumps({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}})) == []
    assert p.feed(_json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "partial"}}})) == []
    flushed = p.finish()
    assert [e.kind for e in flushed] == ["thinking"]
    assert flushed[0].text == "partial"


def test_claude_driver_provides_parser():
    assert isinstance(ClaudeCodeDriver().new_transcript_parser(), ClaudeTranscriptParser)


# --- codex ---

from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.transcript import CodexTranscriptParser

_CODEX_FIXTURE = _Path(__file__).parent / "fixtures" / "transcripts" / "codex.jsonl"


def _codex_worker(**env) -> _WorkerConfig:
    return _WorkerConfig(
        name="x", type="codex", task_types=["explore"], max_running=1, priority=0,
        env=env or {"CAIRN_HOST_AUTH": "1"},
    )


def test_codex_parser_maps_events():
    p = CodexTranscriptParser()
    events = []
    for line in _CODEX_FIXTURE.read_text().splitlines():
        events += p.feed(line)
    events += p.finish()
    assert [e.kind for e in events] == [
        "system", "thinking", "tool_call", "tool_result", "assistant_text", "usage",
    ]
    assert events[0].meta == {"session_id": "thread_fixture"}
    assert events[2].tool == "command" and events[2].text == "echo hello-fixture"
    assert events[3].text == "hello-fixture\n" and events[3].meta["exit_code"] == 0
    assert events[5].meta["usage"]["output_tokens"] == 5


def test_codex_parser_never_raises_on_garbage():
    p = CodexTranscriptParser()
    for line in ("not json", "[1,2]", '{"type":"item.completed","item":5}', '{"type":"zzz"}'):
        assert [e.kind for e in p.feed(line)] == ["raw"]


def test_codex_extract_response_text():
    out = _CODEX_FIXTURE.read_text()
    assert CodexDriver().extract_response_text(out, "") == "hi"
    assert CodexDriver().extract_response_text("plain output", "") == "plain output"


def test_codex_extract_response_text_oversized():
    big = "z" * 40000
    out = _json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": big}})
    assert CodexDriver().extract_response_text(out + "\n", "") == big


def test_codex_extract_session_from_event():
    out = _CODEX_FIXTURE.read_text()
    assert CodexDriver().extract_session(None, out, "") == "thread_fixture"
    assert CodexDriver().extract_session("keep", out, "") == "keep"


def test_codex_extract_session_regex_fallback():
    assert CodexDriver().extract_session(None, "no json", "session id: abc-123") == "abc-123"


def test_codex_argv_has_json_in_all_branches():
    host = _codex_worker()
    prov = _codex_worker(CODEX_MODEL="m", CODEX_BASE_URL="http://x", OPENAI_API_KEY="k")
    for w in (host, prov):
        d = CodexDriver()
        assert "--json" in d.build_execute(w, "p", None).argv
        assert "--json" in d.build_conclude(w, "p", "s")
    assert "--json" in CodexDriver(local=True).build_execute(prov, "p", None).argv
    assert isinstance(CodexDriver().new_transcript_parser(), CodexTranscriptParser)


# --- opencode ---

from cairn.dispatcher.workers.adapters.opencode import OpenCodeDriver
from cairn.dispatcher.workers.transcript import OpenCodeTranscriptParser

_OC_FIXTURE = _Path(__file__).parent / "fixtures" / "transcripts" / "opencode.jsonl"


def test_opencode_parser_maps_events():
    p = OpenCodeTranscriptParser()
    events = []
    for line in _OC_FIXTURE.read_text().splitlines():
        events += p.feed(line)
    events += p.finish()
    assert [e.kind for e in events] == [
        "system", "tool_call", "tool_result", "usage", "system", "assistant_text", "usage",
    ]
    assert events[1].tool == "shell" and events[1].text == "echo hello-fixture"
    assert events[2].text == "hello-fixture\n" and events[2].meta["exit"] == 0
    assert events[5].text == "hi"
    assert events[6].meta["tokens"]["output"] == 3


def test_opencode_parser_never_raises_on_garbage():
    p = OpenCodeTranscriptParser()
    for line in ("not json", "[1,2]", '{"type":"text","part":5}', '{"type":"zzz"}'):
        assert [e.kind for e in p.feed(line)] == ["raw"]


def test_opencode_extract_response_text():
    out = _OC_FIXTURE.read_text()
    assert OpenCodeDriver().extract_response_text(out, "") == "hi"
    assert OpenCodeDriver().extract_response_text("plain output", "") == "plain output"


def test_opencode_extract_response_text_oversized():
    big = "z" * 40000
    out = _json.dumps({"type": "text", "part": {"type": "text", "text": big}})
    assert OpenCodeDriver().extract_response_text(out + "\n", "") == big


def _oc_line(type_, **part):
    return _json.dumps({"type": type_, "part": {"type": type_, **part}})


def test_opencode_extract_response_text_joins_final_step_text_parts():
    big = "y" * 20000
    out = "\n".join([
        _oc_line("step_start"),
        _oc_line("text", text="earlier step"),
        _oc_line("step_finish"),
        _oc_line("step_start"),
        _oc_line("text", text="first block"),
        _oc_line("text", text=big),
        _oc_line("step_finish"),
    ])
    assert OpenCodeDriver().extract_response_text(out, "") == "first block\n" + big


def test_opencode_extract_response_text_no_step_start_joins_all():
    out = "\n".join([_oc_line("text", text="a"), _oc_line("text", text="b")])
    assert OpenCodeDriver().extract_response_text(out, "") == "a\nb"


def test_opencode_argv_uses_json_and_provides_parser():
    argv = OpenCodeDriver().build_execute(
        _WorkerConfig(name="x", type="opencode", task_types=["explore"], max_running=1, priority=0),
        "p", "ses_x",
    ).argv
    i = argv.index("--format")
    assert argv[i + 1] == "json" and "default" not in argv
    assert isinstance(OpenCodeDriver().new_transcript_parser(), OpenCodeTranscriptParser)


from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.transcript import PiTranscriptParser

_PI_FIXTURE = _Path(__file__).parent / "fixtures" / "transcripts" / "pi.jsonl"


def test_pi_parser_maps_events():
    p = PiTranscriptParser()
    events = []
    for line in _PI_FIXTURE.read_text().splitlines():
        events += p.feed(line)
    events += p.finish()
    assert [e.kind for e in events] == ["system", "tool_call", "tool_result", "assistant_text", "usage"]
    assert events[0].title == "session" and events[0].meta["session_id"] == "sess-fixture-0001"
    assert events[1].tool == "bash" and events[1].text == "echo hello-fixture"
    assert events[2].text == "hello-fixture\n" and events[2].meta["is_error"] is False
    assert events[3].text == "hi"
    assert events[4].meta["usage"]["output"] == 3


def test_pi_parser_errors_and_garbage():
    p = PiTranscriptParser()
    for line in ("not json", "[1]", '{"type":"zzz"}', '{"type":"turn_end","message":5}'):
        assert [e.kind for e in p.feed(line)] == ["raw"]
    err = p.feed('{"type":"turn_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"boom"}}')
    assert [e.kind for e in err] == ["error"] and err[0].text == "boom"
    assert p.feed('{"type":"agent_start"}') == []


def test_pi_driver_provides_parser_and_keeps_extraction():
    assert isinstance(PiDriver().new_transcript_parser(), PiTranscriptParser)
    assert PiDriver().extract_response_text(_PI_FIXTURE.read_text(), "") == "hi"
    assert PiDriver().extract_session(None, _PI_FIXTURE.read_text(), "") == "sess-fixture-0001"
