from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("CAIRN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CAIRN_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "runs test",
            "origin": "origin",
            "goal": "goal",
            "bootstrap_enabled": False,
        },
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _create_run(client: TestClient, project_id: str, log_path: str | None) -> dict:
    response = client.post(
        f"/projects/{project_id}/runs",
        json={
            "id": "run_test",
            "task_type": "explore_execute",
            "worker": "tester",
            "intent_id": "i001",
            "status": "running",
            "started_at": "2026-10-24T16:00:00Z",
            "log_path": log_path,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_run_lifecycle_and_log_tail(client: TestClient, tmp_path: Path) -> None:
    project_id = _create_project(client)
    log_path = tmp_path / "logs" / project_id / "run_test.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("line one\nline two\nline three\n", encoding="utf-8")

    run = _create_run(client, project_id, str(log_path))
    assert run["status"] == "running"

    listed = client.get(f"/projects/{project_id}/runs")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == ["run_test"]

    tail = client.get("/runs/run_test/log", params={"tail": 2})
    assert tail.status_code == 200
    assert tail.text == "line two\nline three\n"

    patched = client.patch(
        "/runs/run_test",
        json={"status": "completed", "finished_at": "2026-10-24T16:01:00Z"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "completed"


def test_log_path_outside_log_root_is_rejected(client: TestClient) -> None:
    project_id = _create_project(client)
    _create_run(client, project_id, "/etc/passwd")

    response = client.get("/runs/run_test/log")
    assert response.status_code == 403


def test_worklog_and_artifact_listing(client: TestClient, tmp_path: Path) -> None:
    project_id = _create_project(client)
    log_dir = tmp_path / "logs"
    worklog = log_dir / project_id / "WORKLOG.md"
    worklog.parent.mkdir(parents=True)
    worklog.write_text("## work\n", encoding="utf-8")

    _create_run(client, project_id, None)
    patched = client.patch("/runs/run_test", json={"worklog_path": str(worklog)})
    assert patched.status_code == 200

    worklog_response = client.get("/runs/run_test/worklog")
    assert worklog_response.status_code == 200
    assert worklog_response.text == "## work\n"

    artifact = tmp_path / "artifacts" / project_id / "notes" / "a.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("artifact\n", encoding="utf-8")

    listed = client.get(f"/projects/{project_id}/artifacts")
    assert listed.status_code == 200
    assert listed.json() == [{"path": "notes/a.txt", "size": len("artifact\n")}]

    downloaded = client.get(f"/projects/{project_id}/artifacts/notes/a.txt")
    assert downloaded.status_code == 200
    assert downloaded.text == "artifact\n"

    traversal = client.get(f"/projects/{project_id}/artifacts/../../etc/passwd")
    assert traversal.status_code == 404


def test_create_and_get_run_round_trips_transcript_path(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/runs",
        json={
            "id": "run_tp",
            "task_type": "explore_execute",
            "worker": "tester",
            "started_at": "2026-10-24T16:00:00Z",
            "transcript_path": "/tmp/a.jsonl",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["transcript_path"] == "/tmp/a.jsonl"
    assert client.get("/runs/run_tp").json()["transcript_path"] == "/tmp/a.jsonl"

    patched = client.patch("/runs/run_tp", json={"transcript_path": "/tmp/b.jsonl"})
    assert patched.status_code == 200, patched.text
    assert client.get("/runs/run_tp").json()["transcript_path"] == "/tmp/b.jsonl"


def _create_run_with_transcript(client: TestClient, transcript_path: str | None) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/runs",
        json={
            "id": "run_tr",
            "task_type": "explore_execute",
            "worker": "tester",
            "intent_id": "i001",
            "status": "running",
            "started_at": "2026-10-24T16:00:00Z",
            "transcript_path": transcript_path,
        },
    )
    assert response.status_code == 201, response.text


def _write_transcript(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "logs" / "p" / "run_tr-transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_get_transcript_returns_events(client: TestClient, tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path,
        [
            '{"kind": "text", "text": "a", "seq": 99}',
            '{"kind": "tool", "tool": "bash", "text": "b"}',
            '{"kind": "text", "text": "c"}',
        ],
    )
    _create_run_with_transcript(client, str(path))
    response = client.get("/runs/run_tr/transcript")
    assert response.status_code == 200
    body = response.json()
    assert [e["seq"] for e in body] == [0, 1, 2]
    assert [e["text"] for e in body] == ["a", "b", "c"]
    assert body[1]["tool"] == "bash"


def test_get_transcript_after_seq_pages(client: TestClient, tmp_path: Path) -> None:
    path = _write_transcript(tmp_path, ['{"text": "a"}', '{"text": "b"}', '{"text": "c"}'])
    _create_run_with_transcript(client, str(path))
    body = client.get("/runs/run_tr/transcript", params={"after_seq": 0}).json()
    assert [e["seq"] for e in body] == [1, 2]
    assert client.get("/runs/run_tr/transcript", params={"after_seq": 2}).json() == []
    limited = client.get("/runs/run_tr/transcript", params={"after_seq": 0, "limit": 1}).json()
    assert [e["seq"] for e in limited] == [1]


def test_get_transcript_missing_file_returns_empty(client: TestClient, tmp_path: Path) -> None:
    _create_run_with_transcript(client, None)
    response = client.get("/runs/run_tr/transcript")
    assert response.status_code == 200
    assert response.json() == []
    client.patch(
        "/runs/run_tr",
        json={"transcript_path": str(tmp_path / "logs" / "nope-transcript.jsonl")},
    )
    response = client.get("/runs/run_tr/transcript")
    assert response.status_code == 200
    assert response.json() == []


def test_get_transcript_rejects_path_outside_log_dir(client: TestClient) -> None:
    _create_run_with_transcript(client, "/etc/passwd")
    assert client.get("/runs/run_tr/transcript").status_code == 403


def test_get_transcript_skips_nonfinite_and_deeply_nested_lines(
    client: TestClient, tmp_path: Path
) -> None:
    path = _write_transcript(
        tmp_path,
        [
            '{"text": "a"}',
            '{"x": NaN}',
            '{"x": Infinity}',
            '{"x": -Infinity}',
            "[" * 2000 + "]" * 2000,
            '{"text": "f"}',
        ],
    )
    _create_run_with_transcript(client, str(path))
    response = client.get("/runs/run_tr/transcript")
    assert response.status_code == 200
    assert [(e["seq"], e["text"]) for e in response.json()] == [(0, "a"), (5, "f")]


def test_get_transcript_skips_malformed_lines(client: TestClient, tmp_path: Path) -> None:
    path = _write_transcript(tmp_path, ['{"text": "a"}', "not json", "[1]", '{"text": "c"}'])
    _create_run_with_transcript(client, str(path))
    body = client.get("/runs/run_tr/transcript").json()
    assert [(e["seq"], e["text"]) for e in body] == [(0, "a"), (3, "c")]
