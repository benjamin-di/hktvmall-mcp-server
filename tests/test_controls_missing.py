"""Lost authority must never turn an adopted account back into an active one."""

import shutil

import pytest

from hktvmall_mcp.connections_state import ConnectionState
from hktvmall_mcp.errors import HktvError


def construct(folder, **kwargs):
    return ConnectionState(folder, "default", **kwargs)


def bootstrap(folder):
    return construct(folder, require_existing=False).initialize()


def adopt(folder):
    bootstrap(folder)
    control = construct(folder)
    epoch = control.suspend(identity={"issuer": "https://id.example.com", "sub": "owner"})
    control.finish(epoch)
    return control


def test_saved_disconnect_cannot_be_recreated_after_record_loss(tmp_path):
    folder = tmp_path / "state"
    control = adopt(folder)
    control.path.unlink()
    with pytest.raises(HktvError):
        construct(folder).require_active()
    with pytest.raises(HktvError):
        bootstrap(folder)


@pytest.mark.parametrize("setting", ["1", "invalid", ""])
def test_hosted_missing_or_replaced_state_requires_explicit_adoption(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("CONNECTIONS_REQUIRE_CONTROLS", setting)
    folder = tmp_path / "state"
    with pytest.raises(HktvError):
        construct(folder).read()
    bootstrap(folder)
    construct(folder).require_active()
    control = adopt(folder)
    before = control.path.read_bytes()
    bootstrap(folder)
    assert control.path.read_bytes() == before
    shutil.rmtree(folder)
    folder.mkdir(mode=0o700)
    with pytest.raises(HktvError):
        construct(folder).require_active()
