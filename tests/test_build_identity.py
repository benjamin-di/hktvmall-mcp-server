"""Artifact identity is strict, offline and shared by the App and native MCP handshake."""

import asyncio
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from hktvmall_mcp import __version__, build_identity, server
from hktvmall_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a123456789" * 4
GIT = ["git", "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.com", "-c", "commit.gpgsign=false"]
STAMP = "hktvmall_mcp/build_identity.json"


def test_publisher_stamps_only_valid_full_source_and_product_version(tmp_path):
    (tmp_path / "src/hktvmall_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion="{__version__}"\n')
    embed = runpy.run_path(str(ROOT / "scripts/embed-build-identity.py"))["embed"]
    for version, revision in (("wrong", REVISION), (__version__, "a123456"), (__version__, "unknown")):
        with pytest.raises(ValueError):
            embed(tmp_path, version, revision)
        assert not (tmp_path / "src/hktvmall_mcp/build_identity.json").exists()
    embed(tmp_path, "v" + __version__, REVISION.upper())
    identity = build_identity.load(tmp_path / "src/hktvmall_mcp/build_identity.json")
    assert (identity.service, identity.version, identity.source_revision) == ("hktvmall-mcp", __version__, REVISION)


def _git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _source_tree(target):
    """This checkout's packaging inputs, without generated stamps or caches."""
    target.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "hatch_build.py", ".gitignore"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, target / name)
    shutil.copytree(ROOT / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__", "build_identity.json"))
    return target


def _committed(target):
    _source_tree(target)
    _git("init", "-q", "-b", "main", cwd=target)
    _git("add", "-A", cwd=target)
    _git("commit", "-qm", "synthetic source", cwd=target)
    return _git("rev-parse", "HEAD", cwd=target)


def _uv_build(project, out, **environment):
    return subprocess.run(
        ["uv", "build", "--offline", "--out-dir", str(out), str(project)],
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, **environment},
    )


def _packaged_stamps(dist):
    found = {}
    for wheel in dist.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            found.update({name: json.loads(archive.read(name)) for name in archive.namelist() if name.endswith(STAMP)})
    for sdist in dist.glob("*.tar.gz"):
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                if member.name.endswith(STAMP):
                    found[member.name.split("/", 1)[1]] = json.load(archive.extractfile(member))
    return found


def test_clean_checkout_builds_embed_their_commit_over_stale_and_inherited_identity(tmp_path):
    source = tmp_path / "source"
    revision = _committed(source)
    decoy = tmp_path / "decoy"
    _git("clone", "-q", str(source), str(decoy), cwd=tmp_path)
    _git("commit", "-q", "--allow-empty", "-m", "decoy", cwd=decoy)
    runpy.run_path(str(ROOT / "scripts/embed-build-identity.py"))["embed"](source, __version__, "b" * 40)
    # The default build makes the sdist, then the wheel from that sdist.
    result = _uv_build(source, source / "dist", GIT_DIR=str(decoy / ".git"), GIT_WORK_TREE=str(decoy))
    assert result.returncode == 0, result.stderr
    identity = {"service": "hktvmall-mcp", "version": __version__, "source_revision": revision}
    assert _packaged_stamps(source / "dist") == {STAMP: identity, f"src/{STAMP}": identity}
    runpy.run_path(str(ROOT / "scripts/check-build-distributions.py"))["check"](source, __version__, revision)


def test_noneditable_builds_without_committed_identity_are_refused(tmp_path):
    dirty = tmp_path / "dirty"
    _committed(dirty)
    (dirty / "src/hktvmall_mcp/uncommitted.py").write_text("VALUE = 1\n")
    result = _uv_build(dirty, tmp_path / "dirty-dist")
    assert result.returncode != 0 and "requires committed source" in result.stderr
    exported = _source_tree(tmp_path / "exported")
    result = _uv_build(exported, tmp_path / "exported-dist")
    assert result.returncode != 0 and "Missing build identity" in result.stderr
    assert not list((tmp_path / "exported-dist").glob("*.whl"))


@pytest.mark.parametrize(
    "case", ["local-exclude", "configured-exclude", "unavailable-git", "skip-worktree", "assume-unchanged", "sparse"]
)
def test_source_build_refuses_uncommitted_packaged_files_and_unverifiable_checkout(tmp_path, case):
    source = tmp_path / "source"
    _committed(source)
    environment = {}
    if case == "unavailable-git":
        runpy.run_path(str(ROOT / "scripts/embed-build-identity.py"))["embed"](source, __version__, "b" * 40)
        commands = tmp_path / "bin"
        commands.mkdir()
        (commands / "git").write_text("#!/bin/sh\nexit 128\n")
        (commands / "git").chmod(0o755)
        environment["PATH"] = str(commands) + os.pathsep + os.environ["PATH"]
    elif case in {"skip-worktree", "assume-unchanged", "sparse"}:
        name = "src/hktvmall_mcp/ui.py"
        if case == "sparse":
            _git("sparse-checkout", "set", "--no-cone", "/*", f"!/{name}", cwd=source)
        else:
            with (source / name).open("a") as stream:
                stream.write("\nSYNTHETIC_UNCOMMITTED = True\n")
            _git("update-index", f"--{case}", name, cwd=source)
        assert _git("status", "--porcelain", cwd=source) == ""
    else:
        name = "src/hktvmall_mcp/uncommitted.py"
        (source / name).write_text("VALUE = 1\n")
        if case == "local-exclude":
            (source / ".git/info/exclude").write_text(name + "\n")
        else:
            excludes = tmp_path / "machine-excludes"
            excludes.write_text(name + "\n")
            _git("config", "core.excludesFile", str(excludes), cwd=source)
    result = _uv_build(source, tmp_path / "dist", **environment)
    assert result.returncode != 0, result.stderr
    assert not list((tmp_path / "dist").glob("*.whl"))


@pytest.mark.parametrize(
    "change",
    [
        {"service": "other"},
        {"version": "0.0.0"},
        {"source_revision": "a123456"},
        {"source_revision": "unknown"},
        {"source_revision": REVISION.upper()},
        {"source_revision": 12},
        {"extra": True},
    ],
)
def test_malformed_or_mismatched_embedded_identity_is_rejected(tmp_path, change):
    path = tmp_path / "identity.json"
    path.write_text(
        json.dumps({"service": "hktvmall-mcp", "version": __version__, "source_revision": REVISION, **change})
    )
    with pytest.raises(ValueError, match="Invalid embedded"):
        build_identity.load(path)


def test_missing_stamp_is_permitted_only_for_uninstalled_or_editable_source(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(build_identity, "distribution", lambda _: SimpleNamespace(read_text=lambda _: None))
    with pytest.raises(ValueError, match="Missing embedded"):
        build_identity.load(path)
    monkeypatch.setattr(
        build_identity,
        "distribution",
        lambda _: SimpleNamespace(read_text=lambda _: '{"dir_info":{"editable":true}}'),
    )
    assert build_identity.load(path).source_revision == "unknown"


def test_identity_ignores_runtime_environment_and_has_a_process_instance(tmp_path):
    env = dict(os.environ, VERSION="0.0.0", VCS_REF="b" * 40, SOURCE_REVISION="c" * 40)
    code = (
        "import json; from hktvmall_mcp.build_identity import log_context; "
        "print(json.dumps([log_context(),log_context()]))"
    )
    outputs = [
        json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=tmp_path, env=env, text=True, timeout=30))
        for _ in range(2)
    ]
    first, second = outputs[0]
    assert first == second
    assert first["version"] == __version__ and first["source_revision"] == build_identity.BUILD.source_revision
    assert first["instance"] != outputs[1][0]["instance"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process instance check")
def test_forked_worker_has_its_own_instance():
    read, write = os.pipe()
    parent = build_identity.log_context()
    child = os.fork()
    if child == 0:
        os.close(read)
        os.write(write, json.dumps(build_identity.log_context()).encode())
        os.close(write)
        os._exit(0)
    os.close(write)
    with os.fdopen(read) as pipe:
        context = json.load(pipe)
    assert os.waitpid(child, 0)[1] == 0
    assert context.pop("instance") != parent.pop("instance")
    assert context == parent


def test_app_footer_and_handshakes_share_the_packaged_product_identity(tmp_path, monkeypatch):
    identity = build_identity.BuildIdentity("hktvmall-mcp", __version__, REVISION)
    monkeypatch.setattr(server, "BUILD", identity)
    html = server.product_cards_html()
    assert f"HKTVmall v{__version__} · {REVISION[:12]}</summary>" in html
    assert f"Source revision <code>{REVISION}</code>" in html
    assert f'title="{REVISION}"' in html
    assert "__VERSION__" not in html and "BUILD_IDENTITY" not in html
    assert f'appInfo: {{ name: "hktvmall-products", version: "{__version__}" }}' in html
    mcp = server.create_server(server.build_accounts(Settings(state_dir=tmp_path)))

    async def check():
        for mode in ("auto", "legacy"):
            async with Client(mcp, mode=mode) as client:
                assert client.server_info.version == identity.version

    asyncio.run(check())


def test_distribution_verifier_detects_changed_packaged_application_bytes(tmp_path):
    import tarfile
    import zipfile
    from io import BytesIO

    check = runpy.run_path(str(ROOT / "scripts/check-build-distributions.py"))["check"]
    source = tmp_path / "src/hktvmall_mcp"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('value = "selected source"\n')
    identity = json.dumps({"service": "hktvmall-mcp", "version": __version__, "source_revision": REVISION}).encode()
    dist = tmp_path / "dist"
    dist.mkdir()

    def artifacts(value):
        files = {"hktvmall_mcp/__init__.py": value, "hktvmall_mcp/build_identity.json": identity}
        with zipfile.ZipFile(dist / "app.whl", "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        with tarfile.open(dist / "app.tar.gz", "w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo("app/src/" + name)
                info.size = len(content)
                archive.addfile(info, BytesIO(content))

    artifacts((source / "__init__.py").read_bytes())
    check(tmp_path, __version__, REVISION)
    artifacts(b'wrong = "stale application"\n')
    with pytest.raises(ValueError, match="application bytes"):
        check(tmp_path, __version__, REVISION)
