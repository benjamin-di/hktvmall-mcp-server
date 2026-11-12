"""Run the original synthetic paste journey inside the source-built login image."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_disposable_paste():
    # Only the existing synthetic journey and provider fixture cross this read-only
    # mount. Runtime code and locked packages come from this source's login build.
    argv = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--network",
        "none",
        "--name",
        os.environ["CI_TEST_CONTAINER"],
        "-v",
        f"{ROOT / 'scripts/paste_e2e.py'}:/tmp/paste_e2e.py:ro",
        "-v",
        f"{ROOT / 'tests/oidc_provider.py'}:/tmp/oidc_provider.py:ro",
        "--entrypoint",
        "python",
        os.environ["CI_TEST_IMAGE"],
        "/tmp/paste_e2e.py",
    ]
    # pytest's status reports this genuine command result. Avoid serializing
    # argument/environment exceptions or any transient browser state.
    try:
        status = subprocess.run(argv, check=False).returncode
    except OSError:
        pytest.fail("Synthetic paste command could not start", pytrace=False)
    else:
        if status:
            pytest.fail(f"Synthetic paste command exited {status}", pytrace=False)
