# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

workmux is a Rust CLI tool that orchestrates git worktrees and zellij tabs for isolated development environments. It's designed for managing parallel AI agent workflows.

## Commands

### Build & Run
```bash
cargo build                    # Build the project
cargo run -- <args>           # Run with arguments
just run <args>               # Alternative run command
```

### Testing
```bash
just check                    # Full check: format, clippy, build, unit tests, integration tests
just test                     # Run all Python integration tests (requires venv setup)
just test tests/test_workmux_add.py::test_name  # Run single test
just unit-tests               # Run Rust unit tests only
```

### Linting & Formatting
```bash
just format                   # Format Rust and Python code
just clippy                   # Run clippy (fails on warnings)
just clippy-fix              # Auto-fix clippy warnings
```

### Test Environment Setup
The integration tests use pytest and require a Python venv:
```bash
python -m venv tests/venv
source tests/venv/bin/activate && pip install -r tests/requirements.txt
```

## Architecture

### Source Structure (`src/`)

- **`main.rs`** - Entry point, initializes logger and runs CLI
- **`cli.rs`** - Clap argument parsing and command dispatch
- **`config.rs`** - Configuration loading (global `~/.config/workmux/config.yaml` + project `.workmux.yaml`)
- **`git.rs`** - Git operations (worktree management, branch operations, merge strategies)
- **`zellij.rs`** - Zellij operations (tab creation, command execution)
- **`template.rs`** - MiniJinja templating for branch names and prompts
- **`prompt.rs`** - AI prompt handling (inline, file, editor) with YAML frontmatter parsing

### Command Layer (`src/command/`)
CLI command handlers that parse args and delegate to workflow layer:
- `add.rs` - Create worktree + zellij tab
- `merge.rs` - Merge branch and cleanup
- `remove.rs` - Remove worktree without merging
- `list.rs` - List worktrees with status
- `open.rs` - Open zellij tab for existing worktree

### Workflow Layer (`src/workflow/`)
Business logic implementation:
- `create.rs` - Worktree creation logic
- `setup.rs` - Post-creation setup (file ops, pane layout, hooks)
- `merge.rs` - Merge strategies (standard, rebase, squash)
- `cleanup.rs` - Resource cleanup (worktree, branch, zellij tab)
- `context.rs` - Shared workflow context

### Other Modules
- **`claude.rs`** - Claude-specific config file management (`~/.claude.json` pruning)
- **`github.rs`** - GitHub CLI (`gh`) integration for PR checkout
- **`cmd.rs`** - Shell command execution utilities

## Testing Approach

Integration tests are in Python (`tests/`) using pytest. They test the CLI end-to-end by:
1. Creating temporary git repos and zellij sessions
2. Running `workmux` commands via subprocess
3. Verifying git/zellij state after operations

`conftest.py` contains extensive fixtures for test setup (temp repos, zellij sessions, mock configurations).

**Note**: The integration tests need to be updated to use zellij instead of tmux. Currently they are designed for tmux and will need modification to work with zellij's different CLI interface.
