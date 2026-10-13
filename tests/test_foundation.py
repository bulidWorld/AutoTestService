import pytest
import yaml
from fastapi.testclient import TestClient
from lxml import etree

from app.config.settings import ROOT, load_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def local_logs(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"logging": {"directory": str(tmp_path / "logs")}}))
    monkeypatch.setenv("APP_CONFIG", str(config))


def test_health_and_contract_are_consistent():
    contract = yaml.safe_load((ROOT / "docs/rest-project.yaml").read_text())
    with TestClient(create_app("docs/rest-project.yaml")) as client:
        assert client.get("/openapi.json").json() == contract
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert len(response.headers["X-Request-ID"]) == 32
        assert client.get("/docs").status_code == 200


def test_readiness_unavailable_is_not_healthy(monkeypatch):
    def unavailable():
        raise ConnectionError("secret must not be exposed")

    monkeypatch.setattr("app.service.health.check_database", unavailable)
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


def test_missing_handlers_bind_each_route_and_ignore_path_metadata(tmp_path):
    contract = yaml.safe_load((ROOT / "docs/rest-project.yaml").read_text())
    for path in ["/missing-one", "/missing-two"]:
        contract["paths"][path] = {
            "summary": "路径说明",
            "get": contract["paths"]["/api/v1/health"]["get"] | {"operationId": path[1:]},
        }
    path = tmp_path / "api.yaml"
    path.write_text(yaml.safe_dump(contract, allow_unicode=True))
    with TestClient(create_app(str(path))) as client:
        for route in ["/missing-one", "/missing-two"]:
            response = client.get(route)
            assert response.status_code == 501
            assert route in response.json()["detail"]


def test_invalid_contract_fails_clearly(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("paths: [")
    with pytest.raises(ValueError, match="OpenAPI"):
        create_app(str(path))


def test_configuration_paths_ignore_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings()["contract"] == "docs/rest-project.yaml"
    assert ROOT.is_absolute()


def test_schema_definitions_match_standard():
    validator = etree.XMLSchema(etree.parse(str(ROOT / "db/database.xsd")))
    for path in (ROOT / "db").glob("*.xml"):
        validator.assertValid(etree.parse(str(path)))


def test_access_logs_omit_sensitive_input():
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/v1/health?token=PRIVATE_TOKEN", headers={"Authorization": "PRIVATE_AUTH"}
        )
    from app.config.settings import absolute

    text = (absolute(load_settings()["logging"]["directory"]) / "access.log").read_text()
    assert response.headers["X-Request-ID"] in text
    assert "PRIVATE_TOKEN" not in text and "PRIVATE_AUTH" not in text


def test_stale_pid_cannot_identify_an_unrelated_process(tmp_path):
    import os
    import subprocess

    pid_file = tmp_path / "service.pid"
    pid_file.write_text(f"{os.getpid()}\n")
    identity_file = tmp_path / "service.pid.start"
    identity_file.write_text("old-start\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; PID_FILE="$2"; IDENTITY_FILE="$2.start"; active_pid',
            "bash",
            str(ROOT / "scripts/common.sh"),
            str(pid_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    # stop.sh must clean stale files without terminating the unrelated process.
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; PID_FILE="$2"; IDENTITY_FILE="$2.start"; stop_service',
            "bash",
            str(ROOT / "scripts/stop.sh"),
            str(pid_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert not pid_file.exists() and not identity_file.exists()


def test_database_creation_reports_authentication_before_creating_secret(tmp_path, monkeypatch):
    import os
    import subprocess

    config = tmp_path / "database-config.yaml"
    app_secret = tmp_path / "app.secret"
    admin_secret = tmp_path / "admin.secret"
    config.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "password_file": str(app_secret),
                    "admin": {
                        "auth": "password",
                        "host": "127.0.0.1",
                        "password_file": str(admin_secret),
                    },
                }
            }
        )
    )
    monkeypatch.setenv("APP_CONFIG", str(config))
    tools = tmp_path / "bin"
    tools.mkdir()
    psql = tools / "psql"
    psql.write_text('#!/bin/bash\necho "fe_sendauth: no password supplied" >&2\nexit 2\n')
    psql.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/db_create.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no password supplied" in result.stderr
    assert "管理员连接失败" in result.stderr
    assert str(admin_secret) in result.stderr
    assert not app_secret.exists()


def test_peer_admin_switches_os_user_and_ignores_password_file(tmp_path, monkeypatch):
    import os
    import subprocess

    admin_secret = tmp_path / "invalid-admin.secret"
    admin_secret.write_text("")
    admin_secret.chmod(0o644)
    config = tmp_path / "peer.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "admin": {
                        "auth": "peer",
                        "host": "/var/run/postgresql",
                        "system_user": "test_peer_os_user",
                        "password_file": str(admin_secret),
                    }
                }
            }
        )
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    capture = tmp_path / "sudo-args.txt"
    (tools / "sudo").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_CAPTURE"\n'
        'while [[ "$1" != -- ]]; do shift; done\nshift\nexec "$@"\n'
    )
    (tools / "psql").write_text(
        "#!/bin/bash\n[[ ${APP_DB_PASSWORD:-} == TEST_ONLY_PASSWORD ]] || exit 20\n"
        '[[ ! -v PGPASSWORD ]] || exit 21\nprintf "peer-ok\\n"\n'
    )
    for path in tools.iterdir():
        path.chmod(0o755)
    monkeypatch.setenv("APP_CONFIG", str(config))
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_CAPTURE", str(capture))
    monkeypatch.setenv("APP_DB_PASSWORD", "TEST_ONLY_PASSWORD")
    monkeypatch.setenv("PGPASSWORD", "SHOULD_NOT_BE_USED")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; psql_database ADMIN -Atc "SELECT 1"',
            "bash",
            str(ROOT / "scripts/common.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "peer-ok\n"
    assert "--user=test_peer_os_user" in capture.read_text()
    assert "--preserve-env=APP_DB_PASSWORD" in capture.read_text()
    assert "TEST_ONLY_PASSWORD" not in capture.read_text()


def test_peer_admin_rejects_tcp_host(tmp_path, monkeypatch):
    import subprocess

    config = tmp_path / "invalid-peer.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "database": {
                    "admin": {
                        "auth": "peer",
                        "host": "127.0.0.1",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("APP_CONFIG", str(config))
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; psql_database ADMIN -Atc "SELECT 1"',
            "bash",
            str(ROOT / "scripts/common.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Unix socket" in result.stderr
