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
