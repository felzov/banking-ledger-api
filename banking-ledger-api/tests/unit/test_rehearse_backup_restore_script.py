"""The safety gates of scripts/rehearse-backup-restore.sh, against a stand-in `docker`.

The stand-in plays both containers (metadata and the few SQL answers the gates read) and records
every call, so these tests check what the script would do without any container or database:
plan mode never calls docker, and a cleanup drops only the recorded database on the recorded
cluster, from read-only checks.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rehearse-backup-restore.sh"
DEV = "banking-ledger-db-1"
TEST = "banking-ledger-test-db-1"
DEV_CLUSTER = "7000000000000000001"
TEST_CLUSTER = "7000000000000000002"
NAME = "ledger_restore_rehearsal_20261014120000_test"
OID = "16999"

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_DOCKER_STATE"])
calls = state / "calls.jsonl"
args = sys.argv[1:]
with calls.open("a") as log:
    log.write(json.dumps(args) + "\\n")

if args[0] == "inspect":
    fmt, container = args[2], args[3]
    answers = {{
        "{{{{.State.Running}}}}": "true",
        "{{{{.Config.Image}}}}": "postgres:18-alpine",
        '{{{{index .Config.Labels "com.docker.compose.project"}}}}': "banking-ledger",
        '{{{{index .Config.Labels "com.docker.compose.service"}}}}':
            "db" if container == "{DEV}" else "test-db",
        "{{{{json .HostConfig.Tmpfs}}}}": '{{"/var/lib/postgresql":""}}',
        "{{{{len .Mounts}}}}": "0",
    }}
    print(answers[fmt])
elif args[0] == "exec":
    container, sql = args[3], args[-1]
    if "current_setting('ledger.test_cluster'" in sql:
        print("disposable" if container == "{TEST}" else "")
    elif "pg_control_system" in sql:
        print("{DEV_CLUSTER}" if container == "{DEV}" else os.environ["FAKE_TEST_CLUSTER"])
    elif "FROM pg_database" in sql:
        if not (state / "dropped").exists():
            print(os.environ["FAKE_OID"])
    elif sql.startswith("DROP DATABASE"):
        (state / "dropped").touch()
    else:
        sys.exit(f"fake docker: unexpected SQL: {{sql}}")
else:
    sys.exit(f"fake docker: unexpected command: {{args}}")
"""


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def run(
    state: Path, *args: str, test_cluster: str = TEST_CLUSTER, oid: str = OID
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{state / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_TEST_CLUSTER": test_cluster,
        "FAKE_OID": oid,
    }
    return subprocess.run(  # noqa: S603 (the repository's own script, fixed arguments)
        [str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def calls(state: Path) -> list[list[str]]:
    log = state / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def drops(state: Path) -> list[list[str]]:
    return [call for call in calls(state) if call[-1].startswith("DROP DATABASE")]


def cleanup_args(name: str = NAME, oid: str = OID, cluster: str = TEST_CLUSTER) -> list[str]:
    return ["--cleanup", name, "--expected-oid", oid, "--expected-cluster", cluster]


def test_plan_mode_calls_no_docker(fake_docker: Path) -> None:
    backup_dir = fake_docker / "backups"
    backup_dir.mkdir(mode=0o700)

    result = run(fake_docker, "--backup-dir", str(backup_dir))

    assert result.returncode == 0, result.stderr
    assert "plan only" in result.stdout
    assert calls(fake_docker) == []
    assert list(backup_dir.iterdir()) == []


def test_cleanup_plan_mode_calls_no_docker(fake_docker: Path) -> None:
    result = run(fake_docker, *cleanup_args())

    assert result.returncode == 0, result.stderr
    assert "add --execute" in result.stdout
    assert calls(fake_docker) == []


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(cleanup_args(name="ledger"), id="development database name"),
        pytest.param(cleanup_args(name="ledger_test"), id="test database name"),
        pytest.param(["--cleanup", NAME, "--expected-cluster", TEST_CLUSTER], id="no OID"),
        pytest.param(["--cleanup", NAME, "--expected-oid", OID], id="no cluster"),
        pytest.param(cleanup_args(oid="16999 OR true"), id="non-numeric OID"),
    ],
)
@pytest.mark.parametrize("execute", [False, True], ids=["plan", "execute"])
def test_cleanup_refuses_bad_arguments_before_any_docker_call(
    fake_docker: Path, args: list[str], execute: bool
) -> None:
    result = run(fake_docker, *(["--execute"] if execute else []), *args)

    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert calls(fake_docker) == []


def test_cleanup_refuses_another_cluster(fake_docker: Path) -> None:
    result = run(fake_docker, "--execute", *cleanup_args(), test_cluster="7000000000000000003")

    assert result.returncode == 2
    assert "not the recorded" in result.stderr
    assert drops(fake_docker) == []


def test_cleanup_refuses_another_oid(fake_docker: Path) -> None:
    result = run(fake_docker, "--execute", *cleanup_args(), oid="17000")

    assert result.returncode == 2
    assert "not the recorded" in result.stderr
    assert drops(fake_docker) == []


def test_cleanup_drops_the_recorded_database_after_read_only_checks(fake_docker: Path) -> None:
    result = run(fake_docker, "--execute", *cleanup_args())

    assert result.returncode == 0, result.stderr
    assert [call[-1] for call in drops(fake_docker)] == [f'DROP DATABASE "{NAME}"']
    execs = [call for call in calls(fake_docker) if call[0] == "exec"]
    for call in execs:
        container, options = call[3], call[2]
        if call[-1].startswith("DROP DATABASE"):
            assert container == TEST
        else:
            assert "default_transaction_read_only=on" in options, call
