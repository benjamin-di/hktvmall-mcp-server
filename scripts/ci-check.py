"""Native check phases; retain only bounded synthetic diagnostics, never session state."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

LOG_BYTES = 64 * 1024
ROOT = Path(__file__).resolve().parents[1]
UV_RUN = ["uv", "run", "--frozen", "--no-sync"]
GATES = ("offline", "paste")


class Cancelled(BaseException):
    def __init__(self, number):
        self.number = number


def stop(process):
    """Bound teardown of the whole command group, including output-holding children."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def notice(message):
    with contextlib.suppress(OSError):
        print(message, flush=True)


def retain(path, data):
    if path is None:
        return False
    try:
        path.write_bytes(data)
    except OSError:
        notice(f"Diagnostic unavailable: {path.name}")
        return False
    return True


def command(argv, log, environment):
    """Stream output, bound its retained tail and preserve failures/cancellation."""
    process = None
    tail = bytearray()
    previous, pending = {}, []
    status = 127

    def cancel(number, _frame):
        pending.append(number)
        if process is not None:
            raise Cancelled(number)

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, cancel)
        if pending:
            raise Cancelled(pending[0])
        process = subprocess.Popen(
            argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
        )
        if pending:
            raise Cancelled(pending[0])
        while chunk := os.read(process.stdout.fileno(), 4096):
            tail.extend(chunk)
            del tail[:-LOG_BYTES]
            with contextlib.suppress(OSError):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        status = process.wait()
        if status < 0:
            status = 128 - status
    except Cancelled as interrupted:
        status = 128 + interrupted.number
    except OSError as error:
        # Do not serialize the environment, command arguments or exception text.
        notice(f"Command could not run ({type(error).__name__})")
        status = 127 if isinstance(error, FileNotFoundError) else 126
    finally:
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        if process is not None:
            stop(process)
            process.stdout.close()
        retain(log, tail)
        for number, handler in previous.items():
            signal.signal(number, handler)
    return status


def junit_options(options, report):
    """Replace the library's default with this invocation's private pytest output."""
    remaining, skip = [], False
    for item in shlex.split(options):
        if skip:
            skip = False
        elif item in ("--junitxml", "--junit-xml"):
            skip = True
        elif not item.startswith(("--junitxml=", "--junit-xml=")):
            remaining.append(item)
    if report is not None:
        remaining.append(f"--junitxml={report}")
    return shlex.join(remaining + ["--durations=25", "--durations-min=1", "--tb=short", "--show-capture=no"])


def report_state(report):
    try:
        root = ET.parse(report).getroot()
    except FileNotFoundError:
        return "missing"
    except (ET.ParseError, OSError):
        return "unreadable"
    return "present" if root.tag in ("testsuite", "testsuites") else "unreadable"


def commands(gate, environment=None, report=None):
    environment = environment or os.environ
    if gate == "offline":
        return [("make-check", ["make", "check", *([f"TEST_ARGS=--junitxml={report}"] if report else [])])]
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return [
        (
            "login-image-build",
            [
                "docker",
                "build",
                "--file",
                "Dockerfile.login",
                "--tag",
                environment["CI_TEST_IMAGE"],
                "--build-arg",
                f"VERSION={version}",
                "--build-arg",
                f"VCS_REF={revision}",
                ".",
            ],
        ),
        ("paste-acceptance", [*UV_RUN, "pytest", "-s", "scripts/test_paste_e2e.py"]),
    ]


def cleanup_docker(name, image):
    # No credentials or Docker socket are passed to the synthetic container.
    # Teardown is advisory and cannot replace a command failure or cancellation.
    for argv in (["docker", "rm", "--force", name], ["docker", "image", "rm", image]):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                start_new_session=True,
            )


def run(gate):
    # The command runner handles signals while a child is alive. Between commands
    # and during Docker teardown, defer them so cleanup always gets its budget.
    pending, previous = [], {}
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, lambda number, _frame: pending.append(number))
        status = run_gate(gate, pending)
        return 128 + pending[0] if pending else status
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_gate(gate, pending):
    environment = dict(os.environ)
    environment["UV_LINK_MODE"] = "copy"
    # Only versioned downloads persist. These are the shared library's bounded
    # worker caches; the fallback lives for this disposable allocation only.
    cache = environment.get("CI_BROWSER_CACHE") or environment.get("PLAYWRIGHT_BROWSERS_PATH")
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(
        Path(cache or ROOT / ".artifacts" / "browser-downloads") / "patchright"
    )
    environment["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    output = None
    try:
        parent = ROOT / ".artifacts" / "hktvmall-ci"
        parent.mkdir(parents=True, exist_ok=True)
        # A new path for every invocation, including failed setup; never reuse XML.
        output = Path(tempfile.mkdtemp(prefix=f"{gate}-", dir=parent))
        notice(f"Synthetic diagnostics: {output.relative_to(ROOT)}")
    except OSError:
        notice("Diagnostic directory unavailable; command results remain authoritative")
    steps, status, state = [], 0, "not-applicable"
    # Never archive this directory. Browser profiles, temporary auth, HOME and XDG
    # state die here; the original acceptance also owns its disposable providers.
    with tempfile.TemporaryDirectory(prefix="hktvmall-ci-") as temporary:
        scratch = Path(temporary)
        environment["TMPDIR"] = temporary
        if gate == "paste":
            (scratch / "home").mkdir(mode=0o700)
            environment["HOME"] = str(scratch / "home")
            for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
                environment.pop(name, None)
            environment["XDG_CONFIG_HOME"] = str(scratch / "config")
            environment["XDG_CACHE_HOME"] = str(scratch / "cache")
            environment["XDG_DATA_HOME"] = str(scratch / "data")
        report = scratch / "junit.xml"
        if gate in ("offline", "paste"):
            # Generate JUnit in private writable scratch, then copy best-effort.
            # A missing/unwritable archive directory cannot break a passing suite.
            environment["PYTEST_ADDOPTS"] = junit_options(environment.get("PYTEST_ADDOPTS", ""), report)
        # Retain the source job's 20 minute work budget for each complete gate.
        # Allocation/queue time remains outside these command budgets.
        deadline = time.monotonic() + 1200
        token = uuid.uuid4().hex
        image, container = f"ci-hktvmall-login:{token}", f"ci-hktvmall-paste-{token}"
        environment["CI_TEST_IMAGE"], environment["CI_TEST_CONTAINER"] = image, container
        for name, argv in commands(gate, environment, report):
            if pending:
                status = 128 + pending[0]
                break
            notice(f"Running {gate}: {name}")
            started = time.monotonic()
            remaining = max(0.001, deadline - started)
            status = command(
                ["timeout", "--signal=TERM", "--kill-after=10s", f"{remaining:.3f}s", *argv],
                output / f"{name}.log" if output else None,
                environment,
            )
            steps.append({"step": name, "exit_code": status, "seconds": round(time.monotonic() - started, 3)})
            if status:
                break
        if gate == "paste":
            cleanup_docker(container, image)
        if gate in ("offline", "paste"):
            state = report_state(report)
            if state == "present":
                try:
                    data = report.read_bytes()
                except OSError:
                    state = "unreadable"
                else:
                    if not retain(output / "junit.xml" if output else None, data):
                        state = "not-retained"
    if pending:
        status = 128 + pending[0]
    notice(f"{gate}: exit={status}, JUnit={state}")
    retain(
        output / "status.json" if output else None,
        (json.dumps({"gate": gate, "exit_code": status, "junit": state, "steps": steps}) + "\n").encode(),
    )
    return status


if __name__ == "__main__":
    os.chdir(ROOT)
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=GATES)
    raise SystemExit(run(parser.parse_args().gate))
