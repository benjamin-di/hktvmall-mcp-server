import json

import pytest

from hktvmall_maintenance import human


def test_human_recovery_preflight_failure_is_attributed(monkeypatch, capsys):
    monkeypatch.setattr(human, "setup", lambda **kwargs: None)
    monkeypatch.delenv("HKTVMALL_MAINTENANCE_ROOT", raising=False)
    with pytest.raises(SystemExit) as failure:
        human.main(["--recover-only"])
    assert failure.value.code == 1
    data = json.loads(capsys.readouterr().err)
    assert "HKTVMALL_MAINTENANCE_ROOT" in data["msg"] and data["service"] == "hktvmall-mcp"


def test_human_recovery_argument_errors_are_attributed_and_redacted(monkeypatch, capsys):
    monkeypatch.setattr(human, "setup", lambda **kwargs: None)
    with pytest.raises(SystemExit) as failure:
        human.main(["--private-sentinel"])
    assert failure.value.code == 2
    output = capsys.readouterr().err
    assert json.loads(output)["service"] == "hktvmall-mcp" and "private-sentinel" not in output
