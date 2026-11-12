"""Verify the installed application and native MCP offline before promotion."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import distribution, version
from pathlib import Path

from fastmcp import Client

from hktvmall_mcp.build_identity import BUILD
from hktvmall_mcp.config import Settings
from hktvmall_mcp.server import CARDS_HTML, build_accounts, create_server


def check(product_version: str, revision: str) -> None:
    direct = json.loads(distribution("hktvmall-mcp").read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable"), "Probe requires an installed release artifact"
    assert BUILD.version == version("hktvmall-mcp") == product_version
    assert BUILD.source_revision == revision
    assert f"HKTVmall v{product_version}" in CARDS_HTML and f'title="{revision}"' in CARDS_HTML
    with tempfile.TemporaryDirectory() as directory:
        server = create_server(build_accounts(Settings(state_dir=Path(directory))))

        async def native_identity():
            for mode in ("legacy", "auto"):
                async with Client(server, mode=mode) as client:
                    assert client.server_info.version == product_version

        asyncio.run(native_identity())
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith(("HKTVMALL_", "MCP_", "PYTHONPATH"))
        }
        environment.update(VERSION="0.0.0", VCS_REF="f" * 40, HKTVMALL_STATE_DIR=directory)
        for module, args, code in (
            ("hktvmall_mcp", ["--invalid=synthetic-private-argument"], 2),
            ("hktvmall_login", [], 1),
            ("hktvmall_maintenance.cli", ["synthetic-private-argument"], 2),
        ):
            result = subprocess.run(
                [sys.executable, "-m", module, *args],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == code and not result.stdout
            assert "synthetic-private-argument" not in result.stderr
            events = [json.loads(line) for line in result.stderr.splitlines()]
            assert events, f"Missing retained diagnostic for {module}"
            for event in events:
                assert event["service"] == BUILD.service
                assert event["version"] == product_version and event["source_revision"] == revision
                assert event["instance"] and event["level"] and event["component"]


if __name__ == "__main__":
    check(*sys.argv[1:])
