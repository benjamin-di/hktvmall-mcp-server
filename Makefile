.PHONY: bootstrap check lock rollback-check

TEST_ARGS ?=
bootstrap:
	uv sync --locked
check:
	./scripts/test.sh $(TEST_ARGS)
	@if [ -f publish/config.toml ] && command -v publish-public >/dev/null 2>&1; then publish-public --check-head .; fi
rollback-check:
	uv run --frozen python scripts/rollback-check.py
# After editing dependencies in pyproject.toml: refresh uv.lock and the images' hash locks.
lock:
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header --quiet -o requirements-runtime.lock
	uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o requirements-login.lock
	uv run --frozen python scripts/export-patchright-lock.py -o requirements-patchright.lock
