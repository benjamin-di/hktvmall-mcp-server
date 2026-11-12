"""Exercise native report failures, real pytest output and process-group cancellation."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import runpy
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Native Linux worker supervisor")

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "ci-check.py"
spec = importlib.util.spec_from_file_location("ci_check", HELPER)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize("status", [0, 7, 124, 143])
def test_command_status_and_bounded_log(tmp_path, status):
    log = tmp_path / "command.log"
    code = "import sys; print('x' * 100000); sys.exit(int(sys.argv[1]))"
    assert ci.command([sys.executable, "-c", code, str(status)], log, dict(os.environ)) == status
    assert log.stat().st_size == ci.LOG_BYTES


def test_unwritable_log_preserves_failure(tmp_path):
    # A file used as a directory fails even under root, unlike chmod-based tests.
    blocked = tmp_path / "blocked"
    blocked.touch()
    assert ci.command([sys.executable, "-c", "raise SystemExit(9)"], blocked / "log", dict(os.environ)) == 9
    assert ci.command([sys.executable, "-c", "pass"], blocked / "log", dict(os.environ)) == 0


def test_unwritable_console_preserves_command_status(tmp_path, monkeypatch):
    class BrokenConsole:
        @property
        def buffer(self):
            return self

        def write(self, _data):
            raise OSError("synthetic broken output")

        def flush(self):
            pass

    monkeypatch.setattr(ci.sys, "stdout", BrokenConsole())
    assert ci.command([sys.executable, "-c", "print('synthetic')"], tmp_path / "log", dict(os.environ)) == 0
    assert (tmp_path / "log").read_text() == "synthetic\n"


def test_junit_replaces_only_report_options(tmp_path):
    options = ci.junit_options('-k "keep this" --junitxml old.xml --junit-xml=also-old.xml -x', tmp_path / "new.xml")
    assert "old.xml" not in options
    assert "keep this" in options and "-x" in options
    assert str(tmp_path / "new.xml") in options


def test_real_pytest_reports_are_per_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    # Even a broken library destination must be replaced with private scratch.
    monkeypatch.setenv("PYTEST_ADDOPTS", "--junitxml=/unavailable/stage.xml")
    case = tmp_path / "case.py"
    case.write_text("def test_pass(): pass\ndef test_failure(): assert False\n")
    monkeypatch.setattr(
        ci,
        "commands",
        lambda *args: [
            ("pytest", [sys.executable, "-m", "pytest", "--noconftest", "-p", "no:cacheprovider", str(case)])
        ],
    )
    assert ci.run("offline") == 1
    case.write_text("def test_new_pass(): pass\n")
    assert ci.run("offline") == 0
    suites = list((tmp_path / ".artifacts/hktvmall-ci").glob("*/junit.xml"))
    assert len(suites) == 2
    results = sorted(
        (int(ET.parse(p).getroot()[0].get("tests")), int(ET.parse(p).getroot()[0].get("failures"))) for p in suites
    )
    assert results == [(1, 0), (2, 1)]


@pytest.mark.parametrize(
    "status,xml,state",
    [(0, "", "missing"), (9, "", "missing"), (0, "broken", "unreadable"), (8, "broken", "unreadable")],
)
def test_missing_or_malformed_junit_does_not_change_status(tmp_path, monkeypatch, status, xml, state):
    monkeypatch.setattr(ci, "ROOT", tmp_path)

    def command(argv, log, environment):
        import shlex

        report = next(
            item.split("=", 1)[1]
            for item in shlex.split(environment["PYTEST_ADDOPTS"])
            if item.startswith("--junitxml=")
        )
        if xml:
            Path(report).write_text(xml)
        return status

    monkeypatch.setattr(ci, "command", command)
    assert ci.run("offline") == status
    diagnostics = next((tmp_path / ".artifacts/hktvmall-ci").glob("*/status.json"))
    assert json.loads(diagnostics.read_text())["junit"] == state
    assert not list((tmp_path / ".artifacts/hktvmall-ci").glob("*/junit.xml"))


def test_unwritable_artifacts_do_not_change_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / ".artifacts").touch()
    case = tmp_path / "case.py"
    monkeypatch.setattr(
        ci,
        "commands",
        lambda *args: [
            ("pytest", [sys.executable, "-m", "pytest", "--noconftest", "-p", "no:cacheprovider", str(case)])
        ],
    )
    case.write_text("def test_pass(): pass\n")
    assert ci.run("offline") == 0
    case.write_text("def test_fail(): assert False\n")
    assert ci.run("offline") == 1


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_cancellation_fences_output_holding_children(tmp_path, number):
    pidfile = tmp_path / "pids"
    child = (
        "import os,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    # The direct command exits after spawning a child holding its output pipe.
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    driver = (
        "import importlib.util,os,sys; "
        f"s=importlib.util.spec_from_file_location('ci', {str(HELPER)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"sys.exit(m.command([sys.executable, '-c', {parent!r}], None, dict(os.environ)))"
    )
    process = subprocess.Popen([sys.executable, "-B", "-c", driver], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pid = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists(), "command child did not start"
        pid = int(pidfile.read_text())
        process.send_signal(number)
        process.communicate(timeout=10)
        assert process.returncode == 128 + number
        # A reparented zombie may await init; it no longer executes or owns pipes.
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_original_source_receipt_gate_and_offline_browser_contract(tmp_path):
    assert ci.commands("offline") == [("make-check", ["make", "check"])]
    environment = {"CI_TEST_IMAGE": "synthetic:fixture"}
    build, acceptance = ci.commands("paste", environment)
    assert build[1][:6] == ["docker", "build", "--file", "Dockerfile.login", "--tag", "synthetic:fixture"]
    assert build[1][-1] == "."
    arguments = dict(
        value.split("=", 1)
        for flag, value in zip(build[1][6:-1:2], build[1][7:-1:2], strict=True)
        if flag == "--build-arg"
    )
    assert arguments["VCS_REF"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ci.ROOT, text=True).strip()
    (tmp_path / "src/hktvmall_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_bytes((ci.ROOT / "pyproject.toml").read_bytes())
    embed = runpy.run_path(str(ci.ROOT / "scripts/embed-build-identity.py"))["embed"]
    embed(tmp_path, arguments["VERSION"], arguments["VCS_REF"])
    assert json.loads((tmp_path / "src/hktvmall_mcp/build_identity.json").read_text()) == {
        "service": "hktvmall-mcp",
        "version": arguments["VERSION"],
        "source_revision": arguments["VCS_REF"],
    }
    assert acceptance[1][-1] == "scripts/test_paste_e2e.py"
    gate = HELPER.with_name("test.sh").read_text()
    assert "trap finish EXIT" in gate
    assert "source-identity.py" in gate and "cmp -s" in gate
    assert ".venv/bin/ruff check src tests scripts" in gate
    assert "requirements-runtime.lock" in gate and "requirements-login.lock" in gate


def test_paste_container_is_offline_and_exact_source_fixture_only(monkeypatch):
    spec = importlib.util.spec_from_file_location("paste_report", HELPER.with_name("test_paste_e2e.py"))
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    monkeypatch.setenv("CI_TEST_IMAGE", "synthetic:fixture")
    monkeypatch.setenv("CI_TEST_CONTAINER", "synthetic-fixture")
    captured = []

    class Result:
        returncode = 0

    def run(argv, **kwargs):
        captured.append(argv)
        return Result()

    monkeypatch.setattr(wrapper.subprocess, "run", run)
    wrapper.test_disposable_paste()
    argv = captured[0]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--name") + 1] == "synthetic-fixture"
    mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "-v"]
    assert len(mounts) == 2 and all(mount.endswith(":ro") for mount in mounts)
    assert all("docker.sock" not in item and ".docker" not in item for item in argv)
    Result.returncode = 19
    with pytest.raises(pytest.fail.Exception, match="exited 19"):
        wrapper.test_disposable_paste()


def test_cleanup_is_bounded_and_cannot_replace_result(monkeypatch):
    calls = []

    def fail(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ci.subprocess, "run", fail)
    ci.cleanup_docker("synthetic-fixture", "synthetic:fixture")
    assert len(calls) == 2
    assert calls[0][0] == ["docker", "rm", "--force", "synthetic-fixture"]
    assert all(options["timeout"] == 5 for _, options in calls)


def test_cancellation_during_teardown_keeps_cleanup_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setattr(ci, "commands", lambda *args: [("synthetic", ["not-executed"])])
    monkeypatch.setattr(ci, "command", lambda *args: 0)
    cleaned = []

    def cleanup(name, image):
        cleaned.append((name, image))
        os.kill(os.getpid(), signal.SIGTERM)
        cleaned.append("finished")

    monkeypatch.setattr(ci, "cleanup_docker", cleanup)
    assert ci.run("paste") == 128 + signal.SIGTERM
    assert cleaned[-1] == "finished"


def test_failed_build_does_not_start_browser_but_still_cleans(tmp_path, monkeypatch):
    original_commands = ci.commands
    build_commands = original_commands("paste", {"CI_TEST_IMAGE": "synthetic:fixture"})
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setattr(ci, "commands", lambda *_: build_commands)
    called, cleaned = [], []

    def command(argv, log, environment):
        called.append(argv)
        return 17

    monkeypatch.setattr(ci, "command", command)
    monkeypatch.setattr(ci, "cleanup_docker", lambda *args: cleaned.append(args))
    assert ci.run("paste") == 17
    assert len(called) == 1 and "build" in called[0]
    assert len(cleaned) == 1
