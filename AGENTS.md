# Working on hktvmall-mcp

Instructions for contributors and coding agents. `AGENTS.md` is the only agent instruction file.

**This repository is public.** Never commit personal data or anything about a particular deployment: no real names,
email addresses or account details, no recorded payloads from a real HKTVmall account, no addresses, order numbers,
basket contents or purchase history, no host names, networks or infrastructure of where it runs, and no anecdotes
about who used it when. Deployment-specific configuration and runbooks belong with the deployment, not here. This
applies to code comments, docs, tests, fixtures and commit messages alike.

## Verify

`make bootstrap`, then `make check`: ruff, the format check, the lock-consistency check (`requirements-runtime.lock`
and `requirements-login.lock` must equal `uv export` of `uv.lock`; run `make lock` after a dependency change) and
pytest, with a source-bound receipt under `.artifacts/`. Documentation-only changes need `git diff --check` and link
checks.

Tests never reach HKTVmall: `tests/conftest.py` fakes the site, including the quirks in
[docs/hktvmall-site.md](docs/hktvmall-site.md). Fixtures are synthetic (`tests/fixtures/README.md`): shaped like
HKTVmall's responses, with invented values.

## Boundaries

- `tests/test_safety.py` is the safety boundary: a new HKTVmall host or path, a network import outside `site.py`, or
  any order-changing, payment, voucher, address, profile or list endpoint is a deliberate change there, never a side
  effect. Order history is read-only (the list and an order's page).
- `tests/test_login_safety.py`: the sign-in window shows HKTVmall's page and automates nothing on it.
- Tool results never carry exception text or cookies; errors use the fixed `NEXT_STEP` table in `errors.py`.
- Live checks against HKTVmall: a guest session where it suffices, otherwise only your own account; a handful of
  paced requests, never lists (out of scope), and nothing they return is committed.

## Releases

A `vX.Y.Z` tag whose version matches `pyproject.toml` runs `.github/workflows/release.yml`: the tests, then both
images (smoke-tested, pushed to GHCR, promoted from an immutable `:sha-<12>` candidate) and a GitHub release.
