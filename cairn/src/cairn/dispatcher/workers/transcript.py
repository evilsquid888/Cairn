from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

TRANSCRIPT_KINDS = (
    "system",
    "assistant_text",
    "thinking",
    "tool_call",
    "tool_result",
    "usage",
    "result",
    "error",
    "raw",
)

EVENT_BODY_CAP = 16384


@dataclass(slots=True)
class TranscriptEvent:
    kind: str
    tool: str | None = None
    title: str | None = None
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "title": self.title,
            "text": self.text,
            "meta": self.meta,
        }


class TranscriptParser:
    """Turns worker stdout lines into normalized transcript events."""

    def feed(self, line: str) -> list[TranscriptEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        return [TranscriptEvent(kind="raw", text=stripped)]

    def finish(self) -> list[TranscriptEvent]:
        return []


class RawParser(TranscriptParser):
    """Fallback parser: one raw event per non-empty line."""


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def truncate(text: str, cap: int = EVENT_BODY_CAP) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(json.dumps(item))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)


class ClaudeTranscriptParser(TranscriptParser):
    """Normalizes Claude Code `--output-format stream-json` lines."""

    def feed(self, line: str) -> list[TranscriptEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
            events = self._map(obj) if isinstance(obj, dict) else None
        except Exception:
            events = None
        if events is None:
            return [TranscriptEvent(kind="raw", text=truncate(stripped)[0])]
        return events

    def _map(self, obj: dict[str, Any]) -> list[TranscriptEvent] | None:
        kind = obj.get("type")
        if kind == "system":
            meta: dict[str, Any] = {}
            if obj.get("model") is not None:
                meta["model"] = obj["model"]
            if isinstance(obj.get("tools"), list):
                meta["tools"] = len(obj["tools"])
            if obj.get("session_id") is not None:
                meta["session_id"] = obj["session_id"]
            return [TranscriptEvent(kind="system", title=obj.get("subtype"), meta=meta)]
        if kind in ("assistant", "user"):
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                return None
            out: list[TranscriptEvent] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if kind == "assistant" and btype == "text":
                    out.append(TranscriptEvent(kind="assistant_text", text=truncate(str(block.get("text", "")))[0]))
                elif kind == "assistant" and btype == "thinking":
                    out.append(TranscriptEvent(kind="thinking", text=truncate(str(block.get("thinking", "")))[0]))
                elif kind == "assistant" and btype == "tool_use":
                    text, cut = truncate(json.dumps(block.get("input", {})))
                    out.append(
                        TranscriptEvent(
                            kind="tool_call",
                            tool=block.get("name"),
                            text=text,
                            meta={"truncated": True} if cut else {},
                        )
                    )
                elif kind == "user" and btype == "tool_result":
                    text, cut = truncate(_block_text(block.get("content")))
                    meta = {"is_error": bool(block.get("is_error", False))}
                    if cut:
                        meta["truncated"] = True
                    out.append(TranscriptEvent(kind="tool_result", text=text, meta=meta))
            return out
        if kind == "result":
            text, cut = truncate(str(obj.get("result") or ""))
            meta = {"total_cost_usd": obj.get("total_cost_usd"), "usage": obj.get("usage")}
            if cut:
                meta["truncated"] = True
            return [TranscriptEvent(kind="result", text=text, meta=meta)]
        return None


class CodexTranscriptParser(TranscriptParser):
    """Normalizes `codex exec --json` JSONL events."""

    def feed(self, line: str) -> list[TranscriptEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
            events = self._map(obj) if isinstance(obj, dict) else None
        except Exception:
            events = None
        if events is None:
            return [TranscriptEvent(kind="raw", text=truncate(stripped)[0])]
        return events

    def _map(self, obj: dict[str, Any]) -> list[TranscriptEvent] | None:
        kind = obj.get("type")
        if kind == "thread.started":
            meta = {"session_id": obj["thread_id"]} if obj.get("thread_id") is not None else {}
            return [TranscriptEvent(kind="system", title="session", meta=meta)]
        if kind == "turn.started":
            return []
        if kind == "turn.completed":
            return [TranscriptEvent(kind="usage", meta={"usage": obj.get("usage")})]
        if kind in ("turn.failed", "error"):
            err = obj.get("error")
            msg = err.get("message") if isinstance(err, dict) else obj.get("message") or err
            return [TranscriptEvent(kind="error", text=truncate(str(msg or ""))[0])]
        if kind in ("item.started", "item.updated", "item.completed"):
            item = obj.get("item")
            if not isinstance(item, dict):
                return None
            itype = item.get("type")
            done = kind == "item.completed"
            if itype == "command_execution":
                if kind == "item.updated":
                    return []
                if not done:
                    text, cut = truncate(str(item.get("command", "")))
                    return [
                        TranscriptEvent(
                            kind="tool_call", tool="command", text=text,
                            meta={"truncated": True} if cut else {},
                        )
                    ]
                text, cut = truncate(str(item.get("aggregated_output") or ""))
                meta: dict[str, Any] = {"exit_code": item.get("exit_code")}
                if cut:
                    meta["truncated"] = True
                return [TranscriptEvent(kind="tool_result", text=text, meta=meta)]
            if not done:
                return []
            if itype == "agent_message":
                return [TranscriptEvent(kind="assistant_text", text=truncate(str(item.get("text", "")))[0])]
            if itype == "reasoning":
                return [TranscriptEvent(kind="thinking", text=truncate(str(item.get("text", "")))[0])]
            return None
        return None


class OpenCodeTranscriptParser(TranscriptParser):
    """Normalizes `opencode run --format json` NDJSON events (one JSON object per line)."""

    def feed(self, line: str) -> list[TranscriptEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
            events = self._map(obj) if isinstance(obj, dict) else None
        except Exception:
            events = None
        if events is None:
            return [TranscriptEvent(kind="raw", text=truncate(stripped)[0])]
        return events

    def _map(self, obj: dict[str, Any]) -> list[TranscriptEvent] | None:
        kind = obj.get("type")
        if kind == "error":
            err = obj.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            return [TranscriptEvent(kind="error", text=truncate(str(msg or ""))[0])]
        part = obj.get("part")
        if not isinstance(part, dict):
            return None
        if kind == "step_start":
            meta = {"session_id": obj["sessionID"]} if obj.get("sessionID") is not None else {}
            return [TranscriptEvent(kind="system", title="step", meta=meta)]
        if kind == "text":
            return [TranscriptEvent(kind="assistant_text", text=truncate(str(part.get("text", "")))[0])]
        if kind == "step_finish":
            return [
                TranscriptEvent(
                    kind="usage",
                    meta={
                        "tokens": part.get("tokens"),
                        "cost": part.get("cost"),
                        "reason": part.get("reason"),
                    },
                )
            ]
        if kind == "tool_use":
            state = part.get("state")
            if not isinstance(state, dict):
                return None
            inp = state.get("input")
            if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                call_text, cut_in = truncate(inp["command"])
            else:
                call_text, cut_in = truncate(json.dumps(inp if inp is not None else {}))
            out: list[TranscriptEvent] = [
                TranscriptEvent(
                    kind="tool_call", tool=part.get("tool"), text=call_text,
                    meta={"truncated": True} if cut_in else {},
                )
            ]
            status = state.get("status")
            if status in ("completed", "error"):
                body = state.get("output") if status == "completed" else state.get("error")
                text, cut = truncate(str(body or ""))
                meta: dict[str, Any] = {"status": status, "is_error": status == "error"}
                md = state.get("metadata")
                if isinstance(md, dict) and md.get("exit") is not None:
                    meta["exit"] = md["exit"]
                if cut:
                    meta["truncated"] = True
                out.append(TranscriptEvent(kind="tool_result", text=text, meta=meta))
            return out
        return None


class PiTranscriptParser(TranscriptParser):
    """Normalizes `pi --mode json` NDJSON events.

    Assistant text is taken from `turn_end` only (`agent_end` repeats the same messages).
    Tool events follow pi's `tool_execution_start`/`tool_execution_end` shape.
    """

    _QUIET = frozenset({
        "agent_start", "turn_start", "message_start", "message_end", "agent_end",
        "agent_settled", "auto_retry_start", "entry_appended", "tool_execution_update",
    })

    def feed(self, line: str) -> list[TranscriptEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
            events = self._map(obj) if isinstance(obj, dict) else None
        except Exception:
            events = None
        if events is None:
            return [TranscriptEvent(kind="raw", text=truncate(stripped)[0])]
        return events

    def _map(self, obj: dict[str, Any]) -> list[TranscriptEvent] | None:
        kind = obj.get("type")
        if kind == "session":
            meta = {"session_id": obj["id"]} if obj.get("id") is not None else {}
            return [TranscriptEvent(kind="system", title="session", meta=meta)]
        if kind in self._QUIET:
            return []
        if kind == "turn_end":
            msg = obj.get("message")
            if not isinstance(msg, dict):
                return None
            out: list[TranscriptEvent] = []
            content = msg.get("content")
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    out.append(TranscriptEvent(kind="assistant_text", text=truncate(str(item["text"]))[0]))
                elif item.get("type") == "thinking" and item.get("thinking"):
                    out.append(TranscriptEvent(kind="thinking", text=truncate(str(item["thinking"]))[0]))
            if msg.get("stopReason") == "error" or msg.get("errorMessage"):
                out.append(TranscriptEvent(kind="error", text=truncate(str(msg.get("errorMessage") or "error"))[0]))
            usage = msg.get("usage")
            if isinstance(usage, dict) and any(isinstance(v, (int, float)) and v for v in usage.values()):
                out.append(TranscriptEvent(kind="usage", meta={"usage": usage}))
            return out
        if kind == "auto_retry_end":
            if obj.get("success"):
                return []
            return [TranscriptEvent(kind="error", text=truncate(str(obj.get("finalError") or "retry failed"))[0])]
        if kind == "tool_execution_start":
            args = obj.get("args")
            if isinstance(args, dict) and isinstance(args.get("command"), str):
                text, cut = truncate(args["command"])
            else:
                text, cut = truncate(json.dumps(args if args is not None else {}))
            return [
                TranscriptEvent(
                    kind="tool_call", tool=obj.get("toolName"), text=text,
                    meta={"truncated": True} if cut else {},
                )
            ]
        if kind == "tool_execution_end":
            result = obj.get("result")
            content = result.get("content") if isinstance(result, dict) else result
            body = _block_text(content) if isinstance(content, list) else str(content or "")
            text, cut = truncate(body)
            meta: dict[str, Any] = {"is_error": bool(obj.get("isError"))}
            if cut:
                meta["truncated"] = True
            return [TranscriptEvent(kind="tool_result", tool=obj.get("toolName"), text=text, meta=meta)]
        return None
