import os
from pathlib import Path

import pytest

from .conftest import (
    TmuxEnvironment,
    create_commit,
    get_window_name,
    get_worktree_path,
    poll_until,
    run_workmux_add,
    run_workmux_command,
    write_workmux_config,
)


def test_add_creates_worktree(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates a git worktree."""
    env = isolated_tmux_server
    branch_name = "feature-worktree"

    write_workmux_config(repo_path)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify worktree in git's state
    worktree_list_result = env.run_command(["git", "worktree", "list"])
    assert branch_name in worktree_list_result.stdout

    # Verify worktree directory exists
    expected_worktree_dir = get_worktree_path(repo_path, branch_name)
    assert expected_worktree_dir.is_dir()


def test_add_creates_tmux_window(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates a tmux window with the correct name."""
    env = isolated_tmux_server
    branch_name = "feature-window"
    window_name = get_window_name(branch_name)

    write_workmux_config(repo_path)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify tmux window was created
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    existing_windows = list_windows_result.stdout.strip().split("\n")
    assert window_name in existing_windows


def test_add_executes_post_create_hooks(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` executes post_create hooks in the worktree directory."""
    env = isolated_tmux_server
    branch_name = "feature-hooks"
    hook_file = "hook_was_executed.txt"

    write_workmux_config(repo_path, post_create=[f"touch {hook_file}"])

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify hook file was created in the worktree directory
    expected_worktree_dir = get_worktree_path(repo_path, branch_name)
    assert (expected_worktree_dir / hook_file).exists()


def test_add_executes_pane_commands(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` executes commands in configured panes."""
    env = isolated_tmux_server
    branch_name = "feature-panes"
    window_name = get_window_name(branch_name)
    expected_output = "test pane command output"

    write_workmux_config(
        repo_path, panes=[{"command": f"echo '{expected_output}'; sleep 0.5"}]
    )

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify pane command output appears in the pane
    def check_pane_output():
        capture_result = env.tmux(["capture-pane", "-p", "-t", window_name])
        return expected_output in capture_result.stdout

    assert poll_until(check_pane_output, timeout=2.0), (
        f"Expected output '{expected_output}' not found in pane"
    )


def test_add_sources_shell_rc_files(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that shell rc files (.zshrc) are sourced and aliases work in pane commands."""
    env = isolated_tmux_server
    branch_name = "feature-aliases"
    window_name = get_window_name(branch_name)
    alias_output = "custom_alias_worked_correctly"

    # The environment now provides an isolated HOME directory.
    # Write the .zshrc file there.
    zshrc_content = f"""
# Test alias
alias testcmd='echo "{alias_output}"'
"""
    (env.home_path / ".zshrc").write_text(zshrc_content)

    write_workmux_config(repo_path, panes=[{"command": "testcmd; sleep 0.5"}])

    # The HOME env var is already set for the tmux server.
    # We still need to ensure the correct SHELL is used if it's non-standard.
    shell_path = os.environ.get("SHELL", "/bin/zsh")
    pre_cmds = [
        ["set-option", "-g", "default-shell", shell_path],
    ]

    # Run workmux add. No pre-run `setenv` for HOME is needed anymore.
    run_workmux_add(
        env, workmux_exe_path, repo_path, branch_name, pre_run_tmux_cmds=pre_cmds
    )

    # Verify the alias output appears in the pane
    def check_alias_output():
        capture_result = env.tmux(["capture-pane", "-p", "-t", window_name])
        return alias_output in capture_result.stdout

    assert poll_until(check_alias_output, timeout=2.0), (
        f"Alias output '{alias_output}' not found in pane - shell rc file not sourced"
    )


def test_add_from_specific_branch(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add --base` creates a worktree from a specific branch."""
    env = isolated_tmux_server
    new_branch = "feature-from-base"

    write_workmux_config(repo_path)

    # Create a commit on the current branch
    create_commit(env, repo_path, "Add base file")

    # Get current branch name
    result = env.run_command(["git", "branch", "--show-current"], cwd=repo_path)
    base_branch = result.stdout.strip()

    # Run workmux add with --base flag
    run_workmux_command(
        env,
        workmux_exe_path,
        repo_path,
        f"add {new_branch} --base {base_branch}",
    )

    # Verify worktree was created
    expected_worktree_dir = get_worktree_path(repo_path, new_branch)
    assert expected_worktree_dir.is_dir()

    # Verify the new branch contains the file from base branch
    # The create_commit helper creates a file with a specific naming pattern
    expected_file = expected_worktree_dir / "file_for_Add_base_file.txt"
    assert expected_file.exists()

    # Verify tmux window was created
    window_name = get_window_name(new_branch)
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    existing_windows = list_windows_result.stdout.strip().split("\n")
    assert window_name in existing_windows


def test_add_copies_single_file(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` copies a single file to the worktree."""
    env = isolated_tmux_server
    branch_name = "feature-copy-file"

    # Create a file in the repo root to copy
    env_file = repo_path / ".env"
    env_file.write_text("SECRET_KEY=test123")

    # Commit the file to avoid uncommitted changes
    env.run_command(["git", "add", ".env"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add .env file"], cwd=repo_path)

    # Configure workmux to copy the .env file
    write_workmux_config(repo_path, files={"copy": [".env"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the file was copied (not symlinked)
    worktree_path = get_worktree_path(repo_path, branch_name)
    copied_file = worktree_path / ".env"
    assert copied_file.exists()
    assert copied_file.read_text() == "SECRET_KEY=test123"
    # Verify it's a real file, not a symlink
    assert not copied_file.is_symlink()


def test_add_copies_multiple_files_with_glob(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` copies multiple files using glob patterns."""
    env = isolated_tmux_server
    branch_name = "feature-copy-glob"

    # Create multiple .local files in the repo root
    (repo_path / ".env.local").write_text("LOCAL_VAR=value1")
    (repo_path / ".secrets.local").write_text("API_KEY=secret")

    # Commit the files
    env.run_command(["git", "add", "*.local"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add local files"], cwd=repo_path)

    # Configure workmux to copy all .local files
    write_workmux_config(repo_path, files={"copy": ["*.local"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify both files were copied
    worktree_path = get_worktree_path(repo_path, branch_name)
    assert (worktree_path / ".env.local").exists()
    assert (worktree_path / ".env.local").read_text() == "LOCAL_VAR=value1"
    assert (worktree_path / ".secrets.local").exists()
    assert (worktree_path / ".secrets.local").read_text() == "API_KEY=secret"


def test_add_copies_file_with_parent_directories(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates parent directories when copying nested files."""
    env = isolated_tmux_server
    branch_name = "feature-copy-nested"

    # Create a nested file structure
    config_dir = repo_path / "config"
    config_dir.mkdir()
    (config_dir / "app.conf").write_text("setting=value")

    # Commit the files
    env.run_command(["git", "add", "config/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add config files"], cwd=repo_path)

    # Configure workmux to copy the nested file
    write_workmux_config(repo_path, files={"copy": ["config/app.conf"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the file and parent directory were created
    worktree_path = get_worktree_path(repo_path, branch_name)
    nested_file = worktree_path / "config" / "app.conf"
    assert nested_file.exists()
    assert nested_file.read_text() == "setting=value"
    assert (worktree_path / "config").is_dir()


def test_add_copy_directory_fails_gracefully(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that attempting to copy a directory fails with a clear error message."""
    env = isolated_tmux_server
    branch_name = "feature-copy-dir-fail"

    # Create a directory with files
    data_dir = repo_path / "data"
    data_dir.mkdir()
    (data_dir / "file.txt").write_text("content")

    # Commit the directory
    env.run_command(["git", "add", "data/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add data dir"], cwd=repo_path)

    # Configure workmux to copy the directory (should fail)
    write_workmux_config(repo_path, files={"copy": ["data"]}, env=env)

    # Run workmux add and expect it to fail
    with pytest.raises(AssertionError) as excinfo:
        run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    stderr = str(excinfo.value)
    assert "Cannot copy directory" in stderr or "Only files are supported" in stderr


def test_add_symlinks_single_file(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates a symlink for a single file."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-file"

    # Create a file in the repo root to symlink
    shared_file = repo_path / "shared.txt"
    shared_file.write_text("shared content")

    # Commit the file
    env.run_command(["git", "add", "shared.txt"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add shared file"], cwd=repo_path)

    # Configure workmux to symlink the file
    write_workmux_config(repo_path, files={"symlink": ["shared.txt"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the symlink was created
    worktree_path = get_worktree_path(repo_path, branch_name)
    symlinked_file = worktree_path / "shared.txt"
    assert symlinked_file.exists()
    assert symlinked_file.is_symlink()
    # Verify the content is accessible through the symlink
    assert symlinked_file.read_text() == "shared content"


def test_add_symlinks_directory(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates a symlink for a directory."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-dir"

    # Create a directory in the repo root to symlink
    node_modules = repo_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "package.json").write_text('{"name": "test"}')

    # Commit the directory
    env.run_command(["git", "add", "node_modules/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add node_modules"], cwd=repo_path)

    # Configure workmux to symlink the directory
    write_workmux_config(repo_path, files={"symlink": ["node_modules"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the symlink was created
    worktree_path = get_worktree_path(repo_path, branch_name)
    symlinked_dir = worktree_path / "node_modules"
    assert symlinked_dir.exists()
    assert symlinked_dir.is_symlink()
    # Verify the directory contents are accessible
    assert (symlinked_dir / "package.json").exists()


def test_add_symlinks_multiple_items_with_glob(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` creates symlinks for multiple items using glob patterns."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-glob"

    # Create multiple cache directories
    (repo_path / ".cache").mkdir()
    (repo_path / ".cache" / "data.txt").write_text("cache data")
    (repo_path / ".pnpm-store").mkdir()
    (repo_path / ".pnpm-store" / "index.txt").write_text("pnpm index")

    # Commit the directories
    env.run_command(["git", "add", ".cache/", ".pnpm-store/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add cache dirs"], cwd=repo_path)

    # Configure workmux to symlink using glob patterns
    write_workmux_config(repo_path, files={"symlink": [".*-store", ".cache"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify both symlinks were created
    worktree_path = get_worktree_path(repo_path, branch_name)
    assert (worktree_path / ".cache").is_symlink()
    assert (worktree_path / ".cache" / "data.txt").exists()
    assert (worktree_path / ".pnpm-store").is_symlink()
    assert (worktree_path / ".pnpm-store" / "index.txt").exists()


def test_add_symlinks_are_relative(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that created symlinks use relative paths, not absolute paths."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-relative"

    # Create a file to symlink
    test_file = repo_path / "test.txt"
    test_file.write_text("test content")

    # Commit the file
    env.run_command(["git", "add", "test.txt"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add test file"], cwd=repo_path)

    # Configure workmux to symlink the file
    write_workmux_config(repo_path, files={"symlink": ["test.txt"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the symlink uses the correct relative path
    worktree_path = get_worktree_path(repo_path, branch_name)
    symlinked_file = worktree_path / "test.txt"
    assert symlinked_file.is_symlink()

    # Verify the symlink points to the correct relative path
    source_file = repo_path / "test.txt"
    expected_target = os.path.relpath(source_file, symlinked_file.parent)
    link_target = os.readlink(symlinked_file)
    assert link_target == expected_target, (
        f"Symlink target incorrect. Expected: {expected_target}, Got: {link_target}"
    )


def test_add_symlink_replaces_existing_file(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that symlinking replaces an existing file at the destination."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-replace"

    # Create a file to symlink
    source_file = repo_path / "source.txt"
    source_file.write_text("source content")

    # Commit the file
    env.run_command(["git", "add", "source.txt"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add source file"], cwd=repo_path)

    # Configure workmux to symlink the file
    write_workmux_config(repo_path, files={"symlink": ["source.txt"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Now manually create a regular file at the symlink location in the worktree
    worktree_path = get_worktree_path(repo_path, branch_name)
    dest_file = worktree_path / "source.txt"

    # Remove the existing symlink and create a regular file
    dest_file.unlink()
    dest_file.write_text("replaced content")
    assert not dest_file.is_symlink()

    # Run workmux add again on a different branch to trigger symlink creation again
    # This simulates the --force-files behavior
    branch_name_2 = "feature-symlink-replace-2"
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name_2)

    # Verify the file was replaced with a symlink
    worktree_path_2 = get_worktree_path(repo_path, branch_name_2)
    dest_file_2 = worktree_path_2 / "source.txt"
    assert dest_file_2.is_symlink()
    assert dest_file_2.read_text() == "source content"


def test_add_symlink_with_nested_structure(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that symlinking works with nested directory structures."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-nested"

    # Create a nested directory structure
    nested_dir = repo_path / "lib" / "cache"
    nested_dir.mkdir(parents=True)
    (nested_dir / "data.db").write_text("database content")

    # Commit the structure
    env.run_command(["git", "add", "lib/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add nested structure"], cwd=repo_path)

    # Configure workmux to symlink the nested directory
    write_workmux_config(repo_path, files={"symlink": ["lib/cache"]}, env=env)

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify the nested symlink was created
    worktree_path = get_worktree_path(repo_path, branch_name)
    symlinked_dir = worktree_path / "lib" / "cache"
    assert symlinked_dir.exists()
    assert symlinked_dir.is_symlink()
    assert (symlinked_dir / "data.db").read_text() == "database content"
    # Verify parent directory exists and is NOT a symlink
    assert (worktree_path / "lib").is_dir()
    assert not (worktree_path / "lib").is_symlink()


def test_add_combines_copy_and_symlink(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that copy and symlink operations can be used together."""
    env = isolated_tmux_server
    branch_name = "feature-combined-ops"

    # Create files for both copy and symlink
    (repo_path / ".env").write_text("SECRET=abc123")
    node_modules = repo_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "package.json").write_text('{"name": "test"}')

    # Commit both
    env.run_command(["git", "add", ".env", "node_modules/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add files"], cwd=repo_path)

    # Configure workmux to both copy and symlink
    write_workmux_config(
        repo_path, files={"copy": [".env"], "symlink": ["node_modules"]}, env=env
    )

    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify copy operation
    worktree_path = get_worktree_path(repo_path, branch_name)
    copied_file = worktree_path / ".env"
    assert copied_file.exists()
    assert not copied_file.is_symlink()
    assert copied_file.read_text() == "SECRET=abc123"

    # Verify symlink operation
    symlinked_dir = worktree_path / "node_modules"
    assert symlinked_dir.exists()
    assert symlinked_dir.is_symlink()
    assert (symlinked_dir / "package.json").exists()


def test_add_file_operations_with_empty_config(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that workmux add works when files config is empty or missing."""
    env = isolated_tmux_server
    branch_name = "feature-no-files"

    # Configure workmux with no file operations
    write_workmux_config(repo_path, env=env)

    # Should succeed without errors
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify worktree was created
    worktree_path = get_worktree_path(repo_path, branch_name)
    assert worktree_path.is_dir()


def test_add_file_operations_with_nonexistent_pattern(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that workmux handles glob patterns that match no files gracefully."""
    env = isolated_tmux_server
    branch_name = "feature-no-match"

    # Configure workmux with patterns that don't match anything
    write_workmux_config(
        repo_path,
        files={"copy": ["nonexistent-*.txt"], "symlink": ["missing-dir"]},
        env=env,
    )

    # Should succeed without errors (no matches is not an error)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Verify worktree was created
    worktree_path = get_worktree_path(repo_path, branch_name)
    assert worktree_path.is_dir()


def test_add_copy_with_path_traversal_fails(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` fails if a copy path attempts to traverse outside the repo."""
    env = isolated_tmux_server
    branch_name = "feature-copy-traversal"

    # Create a sensitive file outside the repository
    (repo_path.parent / "sensitive_file").write_text("secret")

    write_workmux_config(repo_path, files={"copy": ["../sensitive_file"]}, env=env)

    with pytest.raises(AssertionError) as excinfo:
        run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # The error should indicate path traversal or invalid path
    stderr = str(excinfo.value)
    assert (
        "Path traversal" in stderr
        or "outside" in stderr
        or "No such file" in stderr
        or "pattern matched nothing" in stderr
    )


def test_add_symlink_with_path_traversal_fails(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies that `workmux add` fails if a symlink path attempts to traverse outside the repo."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-traversal"

    # Create a directory outside the repository that will be matched by the glob
    (repo_path.parent / "some_dir").mkdir()
    (repo_path.parent / "some_dir" / "file.txt").write_text("outside repo")

    write_workmux_config(repo_path, files={"symlink": ["../some_dir"]}, env=env)

    with pytest.raises(AssertionError) as excinfo:
        run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # The error should indicate path traversal
    stderr = str(excinfo.value)
    assert "Path traversal" in stderr or "outside" in stderr


def test_add_symlink_overwrites_conflicting_file_from_git(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies a symlink operation overwrites a conflicting file checked out by git."""
    env = isolated_tmux_server
    branch_name = "feature-symlink-overwrite"

    # In main repo root, create the directory to be symlinked
    (repo_path / "node_modules").mkdir()
    (repo_path / "node_modules" / "dep.js").write_text("content")
    env.run_command(["git", "add", "node_modules/"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add real node_modules"], cwd=repo_path)

    # On a different branch, create a conflicting FILE with the same name
    env.run_command(["git", "checkout", "-b", "conflict-branch"], cwd=repo_path)
    env.run_command(["git", "rm", "-r", "node_modules"], cwd=repo_path)
    (repo_path / "node_modules").write_text("this is a placeholder file")
    env.run_command(["git", "add", "node_modules"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "Add conflicting file"], cwd=repo_path)

    # On main, configure workmux to symlink the directory
    env.run_command(["git", "checkout", "main"], cwd=repo_path)
    write_workmux_config(repo_path, files={"symlink": ["node_modules"]}, env=env)

    # Create a worktree from the branch with the conflicting file
    run_workmux_command(
        env,
        workmux_exe_path,
        repo_path,
        f"add {branch_name} --base conflict-branch",
    )

    # Verify the symlink exists and replaced the original file
    worktree_path = get_worktree_path(repo_path, branch_name)
    symlinked_target = worktree_path / "node_modules"
    assert symlinked_target.is_symlink()
    assert (symlinked_target / "dep.js").exists()
