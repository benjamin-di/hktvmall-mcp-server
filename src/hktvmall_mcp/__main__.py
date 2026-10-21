"""``python -m hktvmall_mcp``: the ``hktvmall-mcp`` command (``cli``)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
