.PHONY: bootstrap check lock

TEST_ARGS ?=
bootstrap:
	uv sync --locked
check:
	./scripts/test.sh $(TEST_ARGS)
# After editing dependencies in pyproject.toml: refresh uv.lock and the images' hash locks.
lock:
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header --quiet -o requirements-runtime.lock
	uv export --frozen --no-dev --extra login --no-emit-project --no-header --quiet -o requirements-login.lock
