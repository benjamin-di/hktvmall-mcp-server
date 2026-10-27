"""One default-account profile/snapshot commit, recovered before any native writer.

The fixed paths belong to the existing custodian. PREPARED always restores the
accepted pair; COMMITTED only finishes cleanup. The reader sees the shared
pending marker and refuses access until recovery completes.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
from pathlib import Path

from signin_window.automation.profiles import prepare_profile
from signin_window.state import atomic_private_write, read_private_file

from hktvmall_mcp.errors import HktvError


def profile_path(root):
    return root / "browser-profiles" / "hktvmall" / "default"


def pending_path(control):
    return control.path.with_suffix(".pending")


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exchange(first, second):
    """Linux atomic directory exchange; no delete-then-rename authority gap."""
    native = ctypes.CDLL(None, use_errno=True)
    operation = native.renameat2
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(first), -100, os.fsencode(second), 2):
        raise OSError(ctypes.get_errno(), "profile exchange failed")
    sync_dir(first.parent)


def identity(path):
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
        raise ValueError("private profile required")
    return [info.st_dev, info.st_ino]


def sync_tree(root):
    for path in root.rglob("*"):
        if not path.is_symlink() and stat.S_ISREG(path.stat().st_mode):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for folder in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), reverse=True):
        sync_dir(folder)
    sync_dir(root)
    sync_dir(root.parent)


def _write(path, value):
    atomic_private_write(path, json.dumps(value, separators=(",", ":")).encode())


def _read(path):
    return json.loads(read_private_file(path, max_bytes=8192))


def _restore(destination, backup, existed):
    if existed:
        atomic_private_write(destination, read_private_file(backup, max_bytes=512 * 1024))
    else:
        destination.unlink(missing_ok=True)
        sync_dir(destination.parent)


def recover(root: Path, export: Path, control):
    """Caller holds cli.lease(root). No new snapshot is ever published here."""
    folder = root / "human-commit"
    journal = folder / "journal.json"
    marker = pending_path(control)
    if folder.exists() or folder.is_symlink():
        info = folder.lstat()
        if folder.is_symlink() or not folder.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private commit directory required")
    if journal.is_symlink() or marker.is_symlink():
        raise ValueError("invalid commit authority")
    if not journal.exists():
        # A marker is written before PREPARED, before any accepted state changes.
        if marker.exists():
            if _read(marker) != {"version": 1, "phase": "preparing"}:
                raise ValueError("unrecognized pending commit")
            marker.unlink()
            sync_dir(marker.parent)
        # A process can die while a candidate browser is open, before PREPARED.
        # A native owner that cannot be proven dead keeps recovery closed.
        for orphan in profile_path(root).parent.glob("human-*"):
            if not re.fullmatch(r"human-[0-9a-f]{32}", orphan.name):
                raise ValueError("unrecognized recovery candidate")
            identity(orphan)
            prepare_profile(orphan)
            shutil.rmtree(orphan)
        return
    record = _read(journal)
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "version",
            "phase",
            "candidate",
            "accepted_identity",
            "candidate_identity",
            "snapshot_existed",
            "state_existed",
            "epoch",
        }
        or record["version"] != 1
        or record["phase"] not in ("prepared", "committed")
        or not re.fullmatch(r"human-[0-9a-f]{32}", str(record["candidate"]))
        or any(type(record[key]) is not bool for key in ("snapshot_existed", "state_existed"))
    ):
        raise ValueError("invalid human commit journal")
    accepted = profile_path(root)
    candidate = accepted.parent / record["candidate"]
    state = root / "identity-targets" / "hktvmall.json"
    # Hold the control lock through rollback/cleanup as well as normal commits.
    with control.edit():
        if record["phase"] == "committed" and (
            identity(accepted) != record["candidate_identity"] or identity(candidate) != record["accepted_identity"]
        ):
            raise ValueError("profile commit ownership changed")
        if record["phase"] == "prepared":
            current = identity(accepted)
            if current == record["candidate_identity"]:
                if identity(candidate) != record["accepted_identity"]:
                    raise ValueError("profile commit ownership changed")
                exchange(accepted, candidate)
            elif current != record["accepted_identity"] or identity(candidate) != record["candidate_identity"]:
                raise ValueError("profile commit ownership changed")
            _restore(export / "snapshot.json", folder / "previous-snapshot", record["snapshot_existed"])
            _restore(state, folder / "previous-state", record["state_existed"])
        # Never rewrite a committed snapshot, even if an owner has since disconnected.
        marker.unlink(missing_ok=True)
        sync_dir(marker.parent)
        journal.unlink()
        sync_dir(folder)
    if candidate.exists():
        shutil.rmtree(candidate)
    for name in ("previous-snapshot", "previous-state"):
        (folder / name).unlink(missing_ok=True)


def commit(root, export, control, epoch, candidate, snapshot, state, generation, hold_id, *, fault=lambda _: None):
    """Commit a closed, independently verified candidate under the existing profile lease."""
    accepted = profile_path(root)
    if candidate.parent != accepted.parent or not re.fullmatch(r"human-[0-9a-f]{32}", candidate.name):
        raise ValueError("fixed candidate directory required")
    folder = root / "human-commit"
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077:
        raise ValueError("private commit directory required")
    previous_state = root / "identity-targets" / "hktvmall.json"
    previous_state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sync_tree(candidate)
    record = {
        "version": 1,
        "phase": "prepared",
        "candidate": candidate.name,
        "accepted_identity": identity(accepted),
        "candidate_identity": identity(candidate),
        "snapshot_existed": (export / "snapshot.json").exists(),
        "state_existed": previous_state.exists(),
        "epoch": epoch,
    }
    with control.edit() as data:
        if data["state"] != "active" or data["epoch"] != epoch or pending_path(control).exists():
            raise HktvError("connection_disconnected")
        if record["snapshot_existed"]:
            atomic_private_write(
                folder / "previous-snapshot", read_private_file(export / "snapshot.json", max_bytes=512 * 1024)
            )
        if record["state_existed"]:
            atomic_private_write(folder / "previous-state", read_private_file(previous_state, max_bytes=512 * 1024))
        _write(pending_path(control), {"version": 1, "phase": "preparing"})
        _write(folder / "journal.json", record)
        fault("prepared")
        exchange(accepted, candidate)
        fault("profile")
        atomic_private_write(export / "snapshot.json", snapshot)
        fault("snapshot")
        state.record_publish("hktvmall", generation)
        if hold_id is not None and not state.clear_hold("hktvmall", hold_id):
            raise ValueError("recovery hold changed")
        fault("state")
        record["phase"] = "committed"
        _write(folder / "journal.json", record)
        fault("committed")
    recover(root, export, control)
