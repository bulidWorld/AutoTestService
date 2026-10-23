"""Task list result filtering, historical summaries and pagination."""

from datetime import UTC, datetime

import pytest
import test_task_business as fixtures
from fastapi.testclient import TestClient
from test_task_business import schema_check

from app.main import create_app
from app.repository.projects import Projects
from app.repository.rest_task import listTasks

database = fixtures.database


def test_task_list_results(database, monkeypatch):
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda self: None)
    with database() as db:
        repository = Projects(db)
        pid = repository.one("INSERT INTO projects(name) VALUES('list') RETURNING id")["id"]
        other = repository.one("INSERT INTO projects(name) VALUES('other') RETURNING id")["id"]
        ids = {}
        for name in ("success", "never", "failed", "queued", "running", "interrupted"):
            ids[name] = repository.one(
                "INSERT INTO tasks(project_id,name,base_url,updated_at) "
                "VALUES(%s,%s,'http://localhost','2026-01-01') RETURNING id",
                (pid, name),
            )["id"]
        repository.one(
            "INSERT INTO tasks(project_id,name,base_url) VALUES(%s,'foreign','http://localhost') "
            "RETURNING id",
            (other,),
        )
        started = datetime(2026, 1, 2, tzinfo=UTC)

        def run(name, status, day, start=started):
            repository.one(
                "INSERT INTO task_runs(task_id,project_id,status,config_snapshot,created_at,"
                "started_at,finished_at) VALUES(%s,%s,%s,'{}',%s,%s,%s) RETURNING id",
                (
                    ids[name],
                    pid,
                    status,
                    datetime(2026, 1, day, tzinfo=UTC),
                    start,
                    started if status in {"failed", "succeeded"} else None,
                ),
            )

        run("success", "failed", 1)
        run("success", "succeeded", 2)
        # Equal creation times use the newer run ID.
        run("failed", "succeeded", 2)
        run("failed", "failed", 2)
        run("running", "failed", 2)
        run("queued", "queued", 3, None)
        run("running", "running", 3)
        run("interrupted", "failed", 3, None)

        def page(**query):
            return listTasks(repository, {"project_id": pid}, query, None, None)

        result = page()
        assert result["total"] == 6
        assert [t["name"] for t in result["items"]] == [
            "failed",
            "interrupted",
            "never",
            "queued",
            "running",
            "success",
        ]
        by_name = {t["name"]: t for t in result["items"]}
        assert by_name["failed"]["last_execution_time"] == started
        assert by_name["failed"]["has_active_run"] is False
        assert by_name["interrupted"]["last_execution_time"] == datetime(2026, 1, 3, tzinfo=UTC)
        assert by_name["never"]["last_execution_time"] is None
        assert by_name["never"]["last_execution_result"] == "not_executed"
        for name in ("queued", "running"):
            assert by_name[name]["last_execution_result"] == "queued"
            assert by_name[name]["has_active_run"] is True
        assert by_name["queued"]["last_execution_time"] == datetime(2026, 1, 3, tzinfo=UTC)
        assert by_name["running"]["last_execution_time"] == started
        for status, count in (("failed", 2), ("not_executed", 1), ("queued", 2), ("succeeded", 1)):
            filtered = page(last_execution_result=status, page_size=1)
            assert filtered["total"] == count
            assert len(filtered["items"]) == 1
            assert filtered["items"][0]["last_execution_result"] == status
        assert page(page=2, page_size=2)["items"] == result["items"][2:4]
        assert page(last_execution_result="")["total"] == 6
        assert page(name="FAIL", last_execution_result="failed")["total"] == 1
        assert page(name="FAIL", last_execution_result="succeeded")["total"] == 0
        # Check the actual HTTP contract with the same isolated, uncommitted data.
        db.commit()

    with TestClient(create_app()) as client:
        for status in ("failed", "queued"):
            response = schema_check(
                client.get(
                    f"/api/v1/projects/{pid}/tasks", params={"last_execution_result": status}
                ),
                "TaskPage",
            )
            assert response["total"] == 2
            assert all(t["last_execution_result"] == status for t in response["items"])


@pytest.mark.parametrize("value", ["success", "running", "失败", "unknown"])
def test_task_list_rejects_invalid_result(tmp_path, monkeypatch, value):
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda self: None)
    config = tmp_path / "settings.yaml"
    config.write_text(f"logging:\n  directory: {tmp_path / 'logs'}\n")
    monkeypatch.setenv("APP_CONFIG", str(config))
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/projects/61/tasks", params={"last_execution_result": value})
    error = schema_check(response, "Error", 400)
    assert error["issues"][0]["location"] == "query.last_execution_result"
