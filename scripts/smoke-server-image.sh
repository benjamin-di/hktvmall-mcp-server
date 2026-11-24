#!/usr/bin/env bash
# Offline smoke for the exact hktvmall-mcp server candidate digest.
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

# Smoke: the server builds both session modes without a network, refuses HTTP without a bearer, and
# answers its own health check (the image's HEALTHCHECK) on a read-only root as uid 1000.
docker run --rm --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 \
  -e HKTVMALL_SESSION_SNAPSHOT=/session/snapshot.json -e HKTVMALL_EXPECTED_EMAIL=smoke@example.invalid \
  -e HKTVMALL_SESSION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
  --entrypoint python "$image" -c 'from hktvmall_mcp.server import build_accounts, create_server; from hktvmall_mcp.config import Settings; a = build_accounts(Settings.from_env()); assert create_server(a).name == "hktvmall" and [s.account_id for s in a.services()] == ["default"]'

rc=0
timeout 120 docker run --rm --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 "$image" || rc=$?
[ "$rc" = 1 ] || { echo "ERROR: expected a refusal without MCP_BEARER_TOKEN, got exit $rc" >&2; exit 1; }

name="smoke-hktvmall-${vcs_ref:0:12}-$$"
docker run -d --name "$name" --network none --read-only --tmpfs /tmp --tmpfs /state:uid=1000 \
  -e MCP_BEARER_TOKEN=smoke-token-0123456789abcdef "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 30); do
  docker exec "$name" hktvmall-mcp health && break
  sleep 1
done
docker exec "$name" hktvmall-mcp health || { docker logs "$name" >&2; exit 1; }
