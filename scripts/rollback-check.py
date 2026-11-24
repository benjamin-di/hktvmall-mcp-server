"""Prove rollback to the previous release on disposable synthetic state.

The previous release's code and locked dependencies are exported from its tag
into an isolated virtualenv and run in a separate interpreter against state
written by this source, and the other way round. The receipt is
`.artifacts/rollback-check.json` (see fleet release_compatibility).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / ".artifacts"
RECEIPT = ARTIFACTS / "rollback-check.json"
EVIDENCE = ARTIFACTS / "rollback-check" / "evidence.json"

SID_ACTIVE = "A" * 43
SID_PENDING = "P" * 43
SID_REVOKED = "R" * 43
SID_ROLLBACK = "B" * 43
SID_NEXT = "N" * 43
FIXED_NOW = 1_800_000_000.0
SYNTHETIC_KEY = "A" * 43 + "="

SIDE = r"""
import base64
import importlib.metadata
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import hktvmall_mcp
from hktvmall_maintenance import human_state
from hktvmall_mcp.accounts import seal, unseal
from hktvmall_mcp.connections_state import ConnectionState
from hktvmall_mcp.snapshot import parse_snapshot
from signin_window.oidc import BrowserStore

op, state_dir, expected_root = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]).resolve()
if not Path(hktvmall_mcp.__file__).resolve().is_relative_to(expected_root):
    raise SystemExit(f"unexpected module path: {hktvmall_mcp.__file__}")
sw_version = importlib.metadata.version("signin-window")
now = float(sys.argv[4])
key = base64.b64decode("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

def store():
    return BrowserStore(state_dir / "browser-sessions", clock=lambda: now)

def tables(s):
    with sqlite3.connect(s.path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))

out = {"signin_window": sw_version}
if op == "seed-sessions":
    s = store()
    s.save(sys.argv[5], {"issuer": "https://id.example", "sub": "owner-a", "expires": now + 3600})
    s.save(
        sys.argv[6],
        {"_state_abc": {"nonce": "n1"}, "next": "/connections/", "pending_expires": now + 600},
    )
    out["tables"] = tables(s)
elif op == "seed-with-revocation":
    s = store()
    s.save_if_not_revoked(
        sys.argv[5],
        {"issuer": "https://id.example", "sub": "owner-a", "expires": now + 3600, "_family": "fam-active"},
    )
    s.save_if_not_revoked(
        sys.argv[6],
        {"_state_xyz": {"nonce": "n2"}, "next": "/connections/", "pending_expires": now + 600},
    )
    s.save_if_not_revoked(
        sys.argv[7],
        {"issuer": "https://id.example", "sub": "owner-b", "expires": now + 3600, "_family": "fam-revoked"},
    )
    s.revoke(sys.argv[7])
    out["tables"] = tables(s)
    with sqlite3.connect(s.path) as conn:
        out["revocations"] = conn.execute("SELECT COUNT(*) FROM revocations").fetchone()[0]
elif op == "read-sessions":
    s = store()
    out["tables"] = tables(s)
    out["loaded"] = {sid: s.load(sid) for sid in sys.argv[5:]}
elif op == "write-and-delete":
    s = store()
    s.save(sys.argv[5], {"issuer": "https://id.example", "sub": "owner-prev", "expires": now + 3600})
    s.delete(sys.argv[6])
    out["loaded"] = s.load(sys.argv[5])
    out["deleted"] = s.load(sys.argv[6])
elif op == "rotate-and-revoke":
    s = store()
    ok = s.save_if_not_revoked(
        sys.argv[6],
        {"issuer": "https://id.example", "sub": "owner-prev", "expires": now + 3600, "_family": "fam-rot"},
        old_sid=sys.argv[5],
    )
    out["rotated"] = ok
    out["loaded_new"] = s.load(sys.argv[6])
    out["loaded_old"] = s.load(sys.argv[5])
    s.revoke(sys.argv[6])
    out["after_revoke"] = s.load(sys.argv[6])
elif op == "controls-init":
    cs = ConnectionState(state_dir / "controls", "example", require_existing=False)
    cs.initialize()
    cs.bind({"issuer": "https://id.example", "sub": sys.argv[5]})
    out["control"] = cs.read()
elif op == "controls-read":
    out["control"] = ConnectionState(state_dir / "controls", "example", require_existing=True).read()
elif op == "controls-update":
    cs = ConnectionState(state_dir / "controls", "example", require_existing=True)
    epoch = cs.suspend()
    cs.finish(epoch)
    out["control"] = cs.read()
elif op == "app-seed":
    person_dir = state_dir / "people" / "example"
    person_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    verified_at = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    blob = seal(
        key,
        "example",
        {
            "schema": 1,
            "target": "hktvmall",
            "generation": 1,
            "email": "example@example.invalid",
            "user_agent": "Synthetic Browser",
            "verified_at": verified_at,
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": "synthetic-cookie",
                    "domain": "www.hktvmall.com",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                }
            ],
        },
    )
    session_file = person_dir / "session.bin"
    session_file.write_bytes(blob)
    session_file.chmod(0o600)
    cs = ConnectionState(state_dir / "controls", "example", require_existing=False)
    cs.initialize()
    epoch = cs.read()["epoch"]
    accepted = human_state.profile_path(state_dir)
    accepted.mkdir(parents=True, mode=0o700, exist_ok=True)
    candidate_name = "human-" + "a" * 32
    candidate = accepted.parent / candidate_name
    candidate.mkdir(mode=0o700, exist_ok=True)
    journal_dir = state_dir / "human-commit"
    journal_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    human_state._write(
        journal_dir / "journal.json",
        {
            "version": 1,
            "phase": "committed",
            "candidate": candidate_name,
            "accepted_identity": human_state.identity(candidate),
            "candidate_identity": human_state.identity(accepted),
            "snapshot_existed": False,
            "state_existed": False,
            "epoch": epoch,
        },
    )
    out["seeded"] = True
elif op == "app-read":
    raw = unseal(key, "example", (state_dir / "people" / "example" / "session.bin").read_bytes())
    snap = parse_snapshot(raw, "example@example.invalid", now)
    journal = human_state._read(state_dir / "human-commit" / "journal.json")
    out["email"] = snap.email
    out["generation"] = snap.generation
    out["journal"] = journal
elif op == "app-recover":
    export = state_dir / "export"
    export.mkdir(mode=0o700, exist_ok=True)
    cs = ConnectionState(state_dir / "controls", "example", require_existing=True)
    human_state.recover(state_dir, export, cs)
    out["journal_exists"] = (state_dir / "human-commit" / "journal.json").exists()
    out["candidate_exists"] = (human_state.profile_path(state_dir).parent / ("human-" + "a" * 32)).exists()
else:
    raise SystemExit(f"unknown op: {op}")

print(json.dumps(out, sort_keys=True))
"""


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def require(condition: bool, message: object = "rollback check failed") -> None:
    if not condition:
        raise AssertionError(str(message))


class Check:
    def __init__(self, previous_root: Path, previous_py: Path, state: Path):
        self.sides = {
            "previous": (previous_py, previous_root),
            "current": (Path(sys.executable), ROOT),
        }
        self.state = state
        self.log: list[dict] = []

    def run(self, side: str, op: str, *args: str) -> dict:
        python_bin, root = self.sides[side]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(python_bin), "-B", "-c", SIDE, op, str(self.state), str(root), str(FIXED_NOW), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.log.append(
            {
                "side": side,
                "op": op,
                "args": list(args),
                "exit": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        )
        if result.returncode:
            raise AssertionError(f"{side} {op} failed: {result.stderr.strip()[-600:]}")
        return json.loads(result.stdout)


def preserved_files(state: Path) -> dict[str, str]:
    targets = (
        state / "people" / "example" / "session.bin",
        state / "human-commit" / "journal.json",
    )
    return {str(p.relative_to(state)): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}


def fresh(base: Path) -> Path:
    state = Path(tempfile.mkdtemp(dir=base))
    state.chmod(0o700)
    return state


def scenarios(previous_root: Path, previous_py: Path, base: Path):
    results: dict[str, str] = {}
    checks: list[tuple[str, Check]] = []

    def scenario(name: str, body):
        check = Check(previous_root, previous_py, fresh(base))
        checks.append((name, check))
        try:
            body(check)
        except Exception as error:
            check.log.append({"failure": f"{type(error).__name__}: {error}"})
            results[name] = "failed"
        else:
            results[name] = "passed"

    def upgrade(c: Check):
        seeded = c.run("previous", "seed-sessions", SID_ACTIVE, SID_PENDING)
        require(seeded["signin_window"] == "0.8.4", seeded)
        require(seeded["tables"] == ["sessions"], seeded["tables"])
        upgraded = c.run("current", "read-sessions", SID_ACTIVE, SID_PENDING)
        require(upgraded["signin_window"] == "0.10.0", upgraded)
        require(set(upgraded["tables"]) == {"sessions", "revocations", "session_families"}, upgraded["tables"])
        require(upgraded["loaded"][SID_ACTIVE]["sub"] == "owner-a", upgraded["loaded"])
        require(upgraded["loaded"][SID_PENDING]["_state_abc"] == {"nonce": "n1"}, upgraded["loaded"])

    def previous_read(c: Check):
        seeded = c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        require(set(seeded["tables"]) == {"sessions", "revocations", "session_families"}, seeded["tables"])
        require(seeded["revocations"] >= 1, seeded)
        seen = c.run("previous", "read-sessions", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        require(seen["signin_window"] == "0.8.4", seen)
        require(seen["loaded"][SID_ACTIVE]["sub"] == "owner-a", seen["loaded"])
        require(seen["loaded"][SID_PENDING]["_state_xyz"] == {"nonce": "n2"}, seen["loaded"])
        require(seen["loaded"][SID_REVOKED] == {}, seen["loaded"])

    def previous_write(c: Check):
        c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        written = c.run("previous", "write-and-delete", SID_ROLLBACK, SID_ACTIVE)
        require(written["loaded"]["sub"] == "owner-prev", written)
        require(written["deleted"] == {}, written)
        seen = c.run("current", "read-sessions", SID_ROLLBACK, SID_ACTIVE, SID_PENDING)
        require(seen["loaded"][SID_ROLLBACK]["sub"] == "owner-prev", seen["loaded"])
        require(seen["loaded"][SID_ACTIVE] == {}, seen["loaded"])
        require(seen["loaded"][SID_PENDING]["_state_xyz"] == {"nonce": "n2"}, seen["loaded"])
        rotated = c.run("current", "rotate-and-revoke", SID_ROLLBACK, SID_NEXT)
        require(rotated["rotated"] is True, rotated)
        require(rotated["loaded_old"] == {}, rotated)
        require(rotated["loaded_new"]["sub"] == "owner-prev", rotated)
        require(rotated["after_revoke"] == {}, rotated)

    def controls(c: Check):
        created = c.run("current", "controls-init", "owner-a")["control"]
        seen = c.run("previous", "controls-read")["control"]
        require(seen == created, (seen, created))
        updated = c.run("previous", "controls-update")["control"]
        require(updated["state"] == "disconnected", updated)
        after = c.run("current", "controls-read")["control"]
        require(after == updated, (after, updated))

    def journal(c: Check):
        c.run("previous", "app-seed")
        before = preserved_files(c.state)
        prev_app = c.run("previous", "app-read")
        require(
            prev_app["email"] == "example@example.invalid" and prev_app["journal"]["phase"] == "committed", prev_app
        )
        c.run("previous", "seed-sessions", SID_ACTIVE, SID_PENDING)
        c.run("current", "read-sessions", SID_ACTIVE, SID_PENDING)
        curr_app = c.run("current", "app-read")
        require(curr_app["email"] == prev_app["email"] and curr_app["journal"] == prev_app["journal"], curr_app)
        c.run("current", "seed-with-revocation", SID_ACTIVE, SID_PENDING, SID_REVOKED)
        c.run("previous", "write-and-delete", SID_ROLLBACK, SID_ACTIVE)
        c.run("current", "read-sessions", SID_ROLLBACK, SID_PENDING)
        after_app = c.run("previous", "app-read")
        require(after_app["email"] == prev_app["email"] and after_app["journal"] == prev_app["journal"], after_app)
        require(preserved_files(c.state) == before, "application stores changed across upgrade and rollback")
        recovered = c.run("previous", "app-recover")
        require(recovered["journal_exists"] is False and recovered["candidate_exists"] is False, recovered)

    scenario("upgrade", upgrade)
    scenario("previous-read", previous_read)
    scenario("previous-write", previous_write)
    scenario("controls-preserved", controls)
    scenario("delivery-journal-preserved", journal)
    return results, {name: check.log for name, check in checks}


def main() -> int:
    started = datetime.now(UTC)
    source = git("rev-parse", "HEAD")
    compatibility = json.loads((ROOT / "release" / "compatibility.json").read_text(encoding="utf-8"))
    rollback_versions = (compatibility.get("state") or {}).get("rollback_versions") or []
    if not rollback_versions:
        raise RuntimeError("release/compatibility.json must declare state.rollback_versions")
    previous = str(rollback_versions[-1])
    tag = f"v{previous}"
    ARTIFACTS.mkdir(exist_ok=True)
    RECEIPT.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="rollback-check-") as temporary:
        base = Path(temporary)
        previous_root = base / "previous"
        previous_root.mkdir(mode=0o700)
        archive = subprocess.run(
            ["git", "-C", str(ROOT), "archive", tag, "pyproject.toml", "uv.lock", "README.md", "hatch_build.py", "src"],
            check=True,
            capture_output=True,
        )
        with tarfile.open(fileobj=BytesIO(archive.stdout)) as bundle:
            bundle.extractall(previous_root, filter="data")
        sync_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(previous_root / ".venv")}
        subprocess.run(["uv", "sync", "--locked", "--no-dev", "--quiet"], cwd=previous_root, env=sync_env, check=True)
        previous_py = previous_root / ".venv" / "bin" / "python"
        os.chmod(base, 0o700)
        results, log = scenarios(previous_root, previous_py, base)
    EVIDENCE.parent.mkdir(exist_ok=True)
    EVIDENCE.write_text(json.dumps({"previous_tag": tag, "source_sha": source, "scenarios": log}, indent=1) + "\n")
    passed = all(value == "passed" for value in results.values())
    receipt = {
        "schema": 1,
        "source_sha": source,
        "previous_version": previous,
        "status": "passed" if passed else "failed",
        "scenarios": results,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "artifacts": [
            {"path": str(EVIDENCE.relative_to(ROOT)), "sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()}
        ],
    }
    RECEIPT.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(json.dumps(results, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
