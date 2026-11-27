"""Derive requirements-patchright.lock from uv.lock and requirements-login.lock.

Extracts patchright and its transitive dependency closure with exact pinned
versions, markers, and SHA-256 hashes (omitting `# via` comments so unrelated
dependency graph changes do not invalidate the cached Chromium Docker layer).
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def patchright_closure(uv_lock: Path) -> set[str]:
    data = tomllib.loads(uv_lock.read_text(encoding="utf-8"))
    by_name: dict[str, list[dict]] = {}
    for pkg in data.get("package", ()):
        by_name.setdefault(normalize(pkg["name"]), []).append(pkg)

    if "patchright" not in by_name:
        raise ValueError("patchright not found in uv.lock")

    closure: set[str] = set()
    stack = ["patchright"]
    while stack:
        current = stack.pop()
        if current in closure:
            continue
        closure.add(current)
        for pkg in by_name.get(current, ()):
            for dep in pkg.get("dependencies", ()):
                dep_name = normalize(dep["name"])
                if dep_name not in closure:
                    stack.append(dep_name)
    return closure


def parse_requirements_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_name, current_lines
        if current_name is not None:
            blocks[current_name] = current_lines
        current_name = None
        current_lines = []

    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if current_name is not None and not line.lstrip().startswith("#"):
                current_lines.append(line)
            continue
        if line.startswith("#"):
            continue
        flush()
        match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if not match:
            raise ValueError(f"Unrecognized requirement line: {line!r}")
        current_name = normalize(match.group(1))
        current_lines = [line]

    flush()
    return blocks


def render_patchright_lock(uv_lock: Path, login_lock: Path) -> str:
    closure = patchright_closure(uv_lock)
    blocks = parse_requirements_blocks(login_lock.read_text(encoding="utf-8"))
    missing = sorted(closure - blocks.keys())
    if missing:
        raise ValueError(f"Packages missing from {login_lock.name}: {', '.join(missing)}")
    lines: list[str] = []
    for name in sorted(closure):
        lines.extend(blocks[name])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv-lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--login-lock", type=Path, default=ROOT / "requirements-login.lock")
    parser.add_argument("-o", "--output", type=Path, default=None)
    args = parser.parse_args()

    rendered = render_patchright_lock(args.uv_lock, args.login_lock)
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
