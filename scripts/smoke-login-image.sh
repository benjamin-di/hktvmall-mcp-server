#!/usr/bin/env bash
# Offline smoke for the exact hktvmall-mcp-login browser candidate digest.
set -euo pipefail
image="${1:?image digest required}"
vcs_ref="${2:?source revision required}"
version="${3:?source version required}"
[[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || { echo 'An exact image digest is required' >&2; exit 2; }
[[ "$vcs_ref" =~ ^[0-9a-f]{40}$ ]] || { echo 'A full source revision is required' >&2; exit 2; }

docker pull -q "$image" >/dev/null
docker run --rm --network none --read-only --tmpfs /tmp \
  --mount "type=bind,src=$PWD/scripts/check-build-identity.py,dst=/tmp/check-build-identity.py,readonly" \
  --entrypoint python "$image" /tmp/check-build-identity.py "$version" "$vcs_ref"

# Smoke: the browser, display, remote-desktop server, viewer and CJK fonts are there; the service
# refuses to start without its bearer; it never runs as root.
docker run --rm --network none --entrypoint sh "$image" -c '
  set -e
  [ "$(id -u)" = 1000 ]
  python -c "import hktvmall_login.app, hktvmall_login.window, signin_window.service, patchright; assert hktvmall_login.app.policy_installed()"
  google-chrome-stable --version >/dev/null
  command -v Xvfb >/dev/null && command -v x11vnc >/dev/null
  ls /ms-patchright | grep -q chromium
  [ -f /opt/novnc/vnc.html ] && [ -f /opt/novnc/core/rfb.js ]
  python -c "from signin_window.relay import PASTE_JS; assert b\"./app/ui.js\" in PASTE_JS"
  ls /usr/share/fonts/opentype/noto/ | grep -q CJK'

rc=0
timeout 60 docker run --rm --network none "$image" || rc=$?
[ "$rc" = 1 ] || { echo "ERROR: expected a refusal without HKTVMALL_LOGIN_TOKEN, got exit $rc" >&2; exit 1; }

# End to end, offline: signed OIDC, owner isolation and the live view in Google Chrome, with Ctrl+V
# typing the clipboard into the window (scripts/paste_e2e.py; a stand-in page instead of HKTVmall'\''s).
timeout 300 docker run --rm --network none \
  --tmpfs /home/login:rw,uid=1000,gid=1000,mode=0700,size=64m \
  -e CHROME_PATH=/usr/bin/google-chrome-stable \
  -v "$PWD/scripts/paste_e2e.py:/tmp/paste_e2e.py:ro" \
  -v "$PWD/tests/oidc_provider.py:/tmp/oidc_provider.py:ro" \
  --entrypoint python "$image" /tmp/paste_e2e.py

bash scripts/check-maintenance-image.sh "$image"
