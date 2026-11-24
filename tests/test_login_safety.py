"""The login service's boundary, checked statically: it shows HKTVmall's login page to a person and automates
nothing on it. No click, keystroke, fill or form call exists in its code; only HKTVmall (and Facebook, for
Facebook login) can be the page shown."""

import ast
from pathlib import Path

LOGIN = Path(__file__).resolve().parents[1] / "src" / "hktvmall_login"
INPUT_CALLS = {
    "click", "dblclick", "tap", "fill", "type", "press", "press_sequentially", "check", "uncheck",
    "select_option", "set_input_files", "set_checked", "dispatch_event", "drag_to", "focus", "hover",
    "insert_text", "down", "up", "move", "wheel",
}  # fmt: skip


def test_the_login_service_never_acts_on_the_page():
    for f in sorted(LOGIN.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("keyboard", "mouse", "touchscreen"), f"{node.attr} in {f.name}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in INPUT_CALLS, f".{node.func.attr}() in {f.name}"


def test_the_window_shows_only_hktvmall_or_facebook():
    from hktvmall_login.window import DOCUMENT_HOSTS, LOGIN_URL, allowed_document

    assert DOCUMENT_HOSTS == ("hktvmall.com", "facebook.com")
    assert LOGIN_URL == "https://www.hktvmall.com/hktv/en/login"
    assert allowed_document("https://www.hktvmall.com/hktv/en/")
    assert allowed_document("https://m.facebook.com/login")
    for url in ("http://www.hktvmall.com/", "https://hktvmall.com.evil.example/", "https://evil.example/",
                "https://evilhktvmall.com/", "javascript:alert(1)", "file:///etc/passwd"):  # fmt: skip
        assert not allowed_document(url), url


def test_phone_layout_uses_genuine_browser_identity_and_filters_swiftshader_locally(monkeypatch, tmp_path):
    import asyncio
    import sys
    import types

    from hktvmall_login import window
    from hktvmall_login.window import LAYOUTS

    assert LAYOUTS["phone"] == {"size": (500, 900), "user_agent": None}
    assert LAYOUTS["desktop"] == {"size": (1280, 860), "user_agent": None}

    calls: list[dict] = []

    class StopLaunch(RuntimeError):
        pass

    class FakeChromium:
        async def launch_persistent_context(self, profile, **kwargs):
            calls.append({"profile": profile, **kwargs})
            raise StopLaunch()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    fake_pkg = types.ModuleType("patchright")
    fake_async = types.ModuleType("patchright.async_api")
    fake_async.async_playwright = FakePlaywright
    fake_pkg.async_api = fake_async
    monkeypatch.setitem(sys.modules, "patchright", fake_pkg)
    monkeypatch.setitem(sys.modules, "patchright.async_api", fake_async)

    try:
        asyncio.run(window.sign_in(profile=str(tmp_path / "local"), layout="desktop"))
    except StopLaunch:
        pass
    try:
        asyncio.run(
            window.sign_in(
                profile=str(tmp_path / "hosted"),
                layout="phone",
                channel="chrome",
                extra_args=("--window-position=0,0", "--enable-unsafe-swiftshader"),
                locked=True,
            )
        )
    except StopLaunch:
        pass

    assert len(calls) == 2
    local_call, hosted_call = calls
    assert "--enable-unsafe-swiftshader" not in local_call["args"]
    assert "--enable-unsafe-swiftshader" in (local_call["ignore_default_args"] or ())
    assert local_call["user_agent"] is None
    assert "--enable-unsafe-swiftshader" in hosted_call["args"]
    assert hosted_call["ignore_default_args"] is None
    assert hosted_call["channel"] == "chrome"
    assert hosted_call["user_agent"] is None


def test_chrome_policy_check_requires_google_chrome_policy_dir(tmp_path, monkeypatch):
    from signin_window.browser import install_policy

    from hktvmall_login import app

    cft_dir = tmp_path / "chrome_for_testing"
    chrome_dir = tmp_path / "chrome"
    install_policy((cft_dir,))
    monkeypatch.setattr(app, "CHROME_POLICY_DIR", chrome_dir)
    assert app.policy_installed() is False
    install_policy((chrome_dir,))
    assert app.policy_installed() is True
