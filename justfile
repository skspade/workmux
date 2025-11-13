# Rust project checks

set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

# List available commands
default:
    @just --list

# Run format, clippy-fix, build, and tests
check: parallel-checks test

# Run format, clippy-fix, and build in parallel
[parallel]
parallel-checks: format clippy-fix build

# Format Rust and Python files
format:
    cargo fmt --all
    ruff format tests --quiet

# Run clippy with all warnings
clippy:
    cargo clippy -- -W clippy::all

# Auto-fix clippy warnings
clippy-fix:
    cargo clippy --fix --allow-dirty -- -W clippy::all

# Build the project
build:
    cargo build --all

# Run the application
run *ARGS:
    cargo run -- "$@"

# Run Python tests (depends on build)
test *ARGS: build
    #!/usr/bin/env bash
    set -euo pipefail
    source tests/venv/bin/activate
    if [ $# -eq 0 ]; then
        pytest tests/ -v
    else
        pytest "$@"
    fi

# Release a new patch version
release-patch:
    @just _release patch

# Release a new minor version
release-minor:
    @just _release minor

# Release a new major version
release-major:
    @just _release major

# Internal release helper
_release bump:
    @python3 scripts/release.py {{bump}}
