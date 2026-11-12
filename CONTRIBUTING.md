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

## Build identity

Every noneditable wheel and sdist carries `src/hktvmall_mcp/build_identity.json`. Built from a clean Git checkout,
including `uvx --from git+<repository URL>` and `uv build`, `hatch_build.py` stamps that commit; a wheel built from an sdist
keeps the stamp the sdist ships. Builds refuse uncommitted changes, sparse checkouts and tracked inputs marked
skip-worktree or assume-unchanged. To build a local release artifact from a reviewed source checkout:

```sh
product_version="$(uv run hktvmall-mcp --version | cut -d ' ' -f2)"
source_revision="$(git rev-parse HEAD)"
uv build
python3 scripts/check-build-distributions.py "$product_version" "$source_revision"
```

Image build contexts have no Git metadata, so the Dockerfiles first run `scripts/embed-build-identity.py VERSION
VCS_REF`; a build without Git metadata or that stamp is refused. The stamp requires the exact package version and a full
hexadecimal source revision. Both image publishers and the PyPI workflow verify the installed identity, App footer and
native MCP handshake before promotion. The distribution check also compares packaged Python and HTML bytes with the
selected source. For an installed artifact, run `scripts/check-build-identity.py VERSION FULL_REVISION` with that
artifact's Python interpreter.

An editable development checkout reports revision `unknown`, and builds never write a stamp into it. Missing or
malformed installed identity fails closed. Only the build reads Git; at runtime neither environment variables nor Git
identify an installed build. A stamp written by `embed-build-identity.py` is ignored by Git and never packaged from a
Git checkout, but editable runs read it, so remove it after use.

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
