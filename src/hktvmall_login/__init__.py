"""hktvmall-login: the browser people log in to HKTVmall with, shown to them through a remote-desktop view.

A person connecting their HKTVmall account sees HKTVmall's own login page, running in a Chromium next to the
server, through noVNC inside the connect page (served by hktvmall-mcp). They log in exactly as on hktvmall.com;
this service automates nothing on the login (``tests/test_login_safety.py`` pins that). It only notices when
HKTVmall says signed in, checks it from the same browser, hands the session to hktvmall-mcp once and closes
the browser. It holds no other account's session and no key; only hktvmall-mcp can call it (a bearer).
``window.sign_in`` is the same window on the person's own screen, for ``hktvmall-mcp login``.
"""
