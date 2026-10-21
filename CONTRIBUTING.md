# Contributing

Thanks for helping. Issues and pull requests are welcome, especially when HKTVmall changes something and a tool
starts failing with `schema_changed`.

## Setup

```sh
git clone https://github.com/benjamin-di/hktvmall-mcp-server
cd hktvmall-mcp-server
make bootstrap   # uv sync --locked
make check       # lint, format, lock consistency, tests
```

`uv run hktvmall-mcp --help` runs your checkout. To try it against HKTVmall you need a connection from Hong Kong;
sign in with `uv run --extra login hktvmall-mcp login` and use a separate `HKTVMALL_STATE_DIR` for development.

## Rules

- **No personal data.** Never commit real account data, recorded payloads from a signed-in session, cookies, names,
  email addresses or order details, in code, tests, fixtures, docs or commit messages. Fixtures are synthetic
  (`tests/fixtures/README.md`).
- **Tests never contact HKTVmall.** Model new behaviour in the fakes (`tests/conftest.py`) and document what you
  observed in [docs/hktvmall-site.md](docs/hktvmall-site.md).
- **The safety boundary is deliberate.** Adding an HKTVmall host or path, or anything that could place, pay for or
  change an order, means changing `tests/test_safety.py` on purpose and explaining why in the pull request.
- Keep tool outputs stable: fields may be added, not renamed or removed, without a major version.
- Be gentle with HKTVmall when testing live: a guest session where it suffices, otherwise only your own account; a
  handful of requests, never lists, and commit nothing they return.

AGENTS.md has the same rules for coding agents.

## How changes land

Each release of this repository is exported from a private working repository as one
`Release X.Y.Z` commit, so pull requests are not merged here: the next release would replace
them. When a change fits, the maintainer applies it there, credits you in the release notes
and closes the pull request with a link to the release that contains it. Issues are welcome;
answers are best effort.
