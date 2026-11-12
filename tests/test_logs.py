"""Retained diagnostics preserve their sink, redaction and typed fields; stdout stays command data."""

import io
import json
import logging
import os
import subprocess
import sys

import pytest
from rich.console import Console
from rich.logging import RichHandler

from hktvmall_mcp import build_identity, cli, logs


def event(record=None, original=None):
    return json.loads(
        logs.JsonFormatter(original).format(
            record or logging.makeLogRecord({"msg": "retained", "name": "hktvmall", "levelname": "INFO"})
        )
    )


def assert_context(payload):
    assert payload.items() >= build_identity.log_context().items()
    assert payload["level"] and payload["component"]


def test_structured_collisions_are_preserved_losslessly_and_types_survive():
    original = logging.Formatter(
        '{"service":"existing","service_":"also existing","version":7,"component":[1,2],'
        '"level":false,"nested":{"code":42},"instance":null,"msg":"retained"}',
        validate=False,
    )
    payload = event(original=original)
    assert_context(payload)
    assert payload["service_"] == "also existing" and payload["service__"] == "existing"
    assert payload["version_"] == 7 and payload["component_"] == [1, 2]
    assert payload["level_"] is False and payload["instance_"] is None
    assert payload["nested"] == {"code": 42} and payload["msg"] == "retained"


def test_plain_record_keeps_typed_extras_and_displaces_identity_fields():
    record = logging.makeLogRecord(
        {
            "name": "hktvmall.tools",
            "msg": "read complete",
            "service": "upstream",
            "items": 3,
            "ok": True,
            "details": {"page": 2},
            "ts": 12,
            "ts_": "existing",
        }
    )
    payload = event(record)
    assert_context(payload)
    assert payload["service_"] == "upstream" and payload["items"] == 3 and payload["ok"] is True
    assert payload["details"] == {"page": 2} and payload["ts"] == 12 and payload["ts_"] == "existing"


def test_original_formatter_exception_output_is_retained_without_raw_extras():
    record = logging.makeLogRecord({"name": "hktvmall", "msg": "private-message", "credential": "private-extra"})
    try:
        raise ValueError("private-exception")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    payload = event(record, logging.Formatter("[redacted]", validate=False))
    assert payload["msg"].startswith("[redacted]\nTraceback")
    assert "ValueError: private-exception" in payload["msg"]
    assert "private-message" not in payload["msg"] and "credential" not in payload


def test_redacting_formatter_can_remove_exception_and_extras():
    class Redacted(logging.Formatter):
        def format(self, record):
            return '{"msg":"safe","count":2}'

    record = logging.makeLogRecord(
        {"name": "hktvmall", "msg": "private-message", "credential": "private-extra", "exc_text": "private-trace"}
    )
    payload = event(record, Redacted())
    assert payload["count"] == 2 and "private" not in json.dumps(payload)


@pytest.fixture
def clean_logging(monkeypatch):
    root = logging.getLogger()
    old_loggers = {
        logger: (logger.handlers[:], logger.level, logger.propagate)
        for logger in [root, *(v for v in logging.Logger.manager.loggerDict.values() if isinstance(v, logging.Logger))]
    }
    fallback = logging.lastResort.formatter
    root.handlers = []
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    yield
    for logger, (handlers, level, propagate) in old_loggers.items():
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
    logging.lastResort.setFormatter(fallback)


def test_setup_preserves_sink_filters_levels_and_is_idempotent(clean_logging):
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("safe %(message)s"))
    handler.addFilter(lambda record: record.msg != "filtered")
    root = logging.getLogger()
    root.addHandler(handler)
    handlers = root.handlers[:]
    logs.setup()
    formatter = handler.formatter
    logs.setup()
    assert root.handlers == handlers and handler.formatter is formatter and handler.level == logging.ERROR
    logger = logging.getLogger("hktvmall")
    logger.error("retained")
    logger.error("filtered")
    logger.info("below sink level")
    payload = json.loads(sink.getvalue())
    assert payload["msg"] == "safe retained"
    assert_context(payload)
    for name in ("uvicorn.access", "mcp.server.lowlevel.server", "httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_dedicated_asyncio_sink_keeps_redaction_filters_and_threshold(clean_logging):
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("safe retained", validate=False))
    handler.addFilter(lambda record: record.msg != "filtered")
    logger = logging.getLogger("asyncio")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.DEBUG)
    logs.setup()
    logs.setup()
    logger.warning("below threshold")
    logger.error("filtered")
    logger.error("synthetic-private")
    payload = json.loads(sink.getvalue())
    assert payload["msg"] == "safe retained" and payload["component"] == "asyncio"
    assert handler.level == logging.ERROR and "synthetic-private" not in sink.getvalue()
    assert_context(payload)


@pytest.mark.parametrize("port", ["-1", "70000"])
@pytest.mark.parametrize("role", ["server", "login"])
def test_invalid_numeric_ports_have_only_contextual_diagnostics(tmp_path, port, role):
    environment = {name: value for name, value in os.environ.items() if not name.startswith("HKTVMALL_")}
    environment.update(HKTVMALL_STATE_DIR=str(tmp_path), MCP_BEARER_TOKEN="synthetic-bearer-at-least-sixteen")
    if role == "server":
        command = [sys.executable, "-m", "hktvmall_mcp", "serve", "--port", port]
    else:
        command = [sys.executable, "-c", "from hktvmall_login.app import main; main()"]
        environment.update(HKTVMALL_LOGIN_TOKEN="synthetic-login-token-at-least-thirty-two", PORT=port)
    result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=30)
    assert result.returncode == 1 and not result.stdout
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert events
    expected = build_identity.log_context()
    expected.pop("instance")
    assert all(event.items() >= expected.items() for event in events)
    assert len({event["instance"] for event in events}) == 1
    assert "OverflowError" in events[-1]["msg"]


def test_rich_sink_becomes_one_json_line_and_keeps_quiet_and_filters(clean_logging):
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False)
    handler = RichHandler(console=console, show_time=False, show_level=False, show_path=False)
    handler.addFilter(lambda record: record.msg != "filtered")
    root = logging.getLogger()
    root.addHandler(handler)
    logs.setup()
    logger = logging.getLogger("hktvmall")
    logger.error("retained")
    logger.error("filtered")
    console.quiet = True
    logger.error("quiet")
    payload = json.loads(sink.getvalue())
    assert payload["msg"].strip() == "retained"
    assert_context(payload)


def test_cli_argument_diagnostics_are_identified_without_echoing_private_input(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--private-session=synthetic-secret"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic-secret" not in captured.err
    assert_context(json.loads(captured.err))


def test_login_startup_refusal_keeps_json_on_stderr(monkeypatch, capsys):
    from hktvmall_login import app

    monkeypatch.delenv("HKTVMALL_LOGIN_TOKEN", raising=False)
    with pytest.raises(SystemExit) as error:
        app.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    payload = json.loads(captured.err)
    assert "HKTVMALL_LOGIN_TOKEN" in payload["msg"]
    assert_context(payload)


def test_maintenance_parser_keeps_private_arguments_out_of_diagnostics(capsys):
    from hktvmall_maintenance import cli as maintenance

    with pytest.raises(SystemExit) as error:
        maintenance.main(["synthetic-private-action"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic-private-action" not in captured.err
    assert_context(json.loads(captured.err))
