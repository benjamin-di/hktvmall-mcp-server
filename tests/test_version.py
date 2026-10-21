import tomllib
from pathlib import Path

import hktvmall_mcp


def test_reported_version_is_the_released_package_version() -> None:
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert hktvmall_mcp.__version__ == pyproject["project"]["version"]
