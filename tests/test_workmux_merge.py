from pathlib import Path

from .conftest import (
    TmuxEnvironment,
    create_commit,
    create_dirty_file,
    get_window_name,
    get_worktree_path,
    run_workmux_add,
    run_workmux_merge,
    write_workmux_config,
    poll_until,
)


def test_merge_default_strategy_succeeds_and_cleans_up(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies a standard merge succeeds and cleans up all resources."""
    env = isolated_tmux_server
    branch_name = "feature-to-merge"
    window_name = get_window_name(branch_name)
    write_workmux_config(repo_path, env=env)

    # Branch off first, then create commits on both branches to force a merge commit
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Create a commit on main after branching to create divergent history
    main_file = repo_path / "main_file.txt"
    main_file.write_text("content on main")
    env.run_command(["git", "add", "main_file.txt"], cwd=repo_path)
    env.run_command(["git", "commit", "-m", "commit on main"], cwd=repo_path)

    # Create a commit on feature branch
    worktree_path = get_worktree_path(repo_path, branch_name)
    commit_msg = "feat: add new file"
    create_commit(env, worktree_path, commit_msg)

    commit_hash = env.run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=worktree_path
    ).stdout.strip()

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name)

    assert not worktree_path.exists(), "Worktree directory should be removed"
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    assert window_name not in list_windows_result.stdout, "Tmux window should be closed"
    branch_list_result = env.run_command(["git", "branch", "--list", branch_name])
    assert branch_name not in branch_list_result.stdout, (
        "Local branch should be deleted"
    )

    log_result = env.run_command(["git", "log", "--oneline", "main"])
    assert commit_hash in log_result.stdout, "Feature commit should be on main branch"
    assert "Merge branch" in log_result.stdout, "A merge commit should exist on main"


def test_merge_from_within_worktree_succeeds(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies `workmux merge` with no branch arg works from inside the worktree window."""
    env = isolated_tmux_server
    branch_name = "feature-in-window"
    window_name = get_window_name(branch_name)
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    create_commit(env, worktree_path, "feat: a simple change")

    run_workmux_merge(
        env,
        workmux_exe_path,
        repo_path,
        branch_name=None,
        from_window=window_name,
    )

    assert not worktree_path.exists()
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    assert window_name not in list_windows_result.stdout
    branch_list_result = env.run_command(["git", "branch", "--list", branch_name])
    assert branch_name not in branch_list_result.stdout


def test_merge_rebase_strategy_succeeds(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies --rebase merge results in a linear history."""
    env = isolated_tmux_server
    branch_name = "feature-to-rebase"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    # Create a commit on main after branching to create divergent history
    main_file = repo_path / "main_update.txt"
    main_file.write_text("update on main")
    env.run_command(["git", "add", "main_update.txt"], cwd=repo_path)
    main_commit_msg = "docs: update readme on main"
    env.run_command(["git", "commit", "-m", main_commit_msg], cwd=repo_path)

    # Create a commit on the feature branch
    worktree_path = get_worktree_path(repo_path, branch_name)
    feature_commit_msg = "feat: rebased feature"
    create_commit(env, worktree_path, feature_commit_msg)

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name, rebase=True)

    assert not worktree_path.exists()

    log_result = env.run_command(["git", "log", "--oneline", "main"])
    # Note: After rebase, the commit hash changes, so we check for the message
    assert feature_commit_msg in log_result.stdout, (
        "Feature commit should be in main history"
    )
    assert "Merge branch" not in log_result.stdout, (
        "No merge commit should exist for rebase"
    )

    # Verify linear history: the feature commit should come after the main commit
    log_lines = log_result.stdout.strip().split("\n")
    feature_commit_index = next(
        i for i, line in enumerate(log_lines) if feature_commit_msg in line
    )
    main_commit_index = next(
        i for i, line in enumerate(log_lines) if main_commit_msg in line
    )
    assert feature_commit_index < main_commit_index, (
        "Feature commit should be rebased on top of main's new commit"
    )


def test_merge_squash_strategy_succeeds(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies --squash merge combines multiple commits into one."""
    env = isolated_tmux_server
    branch_name = "feature-to-squash"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    create_commit(env, worktree_path, "feat: first commit")
    first_commit_hash = env.run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=worktree_path
    ).stdout.strip()
    create_commit(env, worktree_path, "feat: second commit")
    second_commit_hash = env.run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=worktree_path
    ).stdout.strip()

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name, squash=True)

    assert not worktree_path.exists()

    log_result = env.run_command(["git", "log", "--oneline", "main"])
    assert first_commit_hash not in log_result.stdout, (
        "Original commits should not be in main history"
    )
    assert second_commit_hash not in log_result.stdout, (
        "Original commits should not be in main history"
    )
    assert "Merge branch" not in log_result.stdout, "No merge commit for squash"


def test_merge_fails_on_unstaged_changes(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merge fails if worktree has unstaged changes."""
    env = isolated_tmux_server
    branch_name = "feature-with-unstaged"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    # Create a commit first, then modify the file to create unstaged changes
    create_commit(env, worktree_path, "feat: initial work")
    # Modify an existing tracked file to create unstaged changes
    (worktree_path / "file_for_feat_initial_work.txt").write_text("modified content")

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name, expect_fail=True)

    assert worktree_path.exists(), "Worktree should not be removed when command fails"


def test_merge_succeeds_with_ignore_uncommitted_flag(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies --ignore-uncommitted allows merge despite unstaged changes."""
    env = isolated_tmux_server
    branch_name = "feature-ignore-uncommitted"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    create_commit(env, worktree_path, "feat: committed work")
    create_dirty_file(worktree_path)

    run_workmux_merge(
        env, workmux_exe_path, repo_path, branch_name, ignore_uncommitted=True
    )

    assert not worktree_path.exists(), "Worktree should be removed despite dirty files"


def test_merge_commits_staged_changes_before_merge(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merge automatically commits staged changes."""
    env = isolated_tmux_server
    branch_name = "feature-with-staged"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    staged_file = worktree_path / "staged_file.txt"
    staged_file.write_text("staged content")
    env.run_command(["git", "add", "staged_file.txt"], cwd=worktree_path)

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name)

    assert not worktree_path.exists()
    show_result = env.run_command(["git", "show", "main:staged_file.txt"])
    assert "staged content" in show_result.stdout, "Staged file should be in main"


def test_merge_fails_if_main_worktree_has_uncommitted_changes(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merge fails if main worktree has uncommitted changes."""
    env = isolated_tmux_server
    branch_name = "feature-clean"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    create_commit(env, worktree_path, "feat: work done")

    create_dirty_file(repo_path, "dirty_in_main.txt")

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name, expect_fail=True)

    assert worktree_path.exists(), "Worktree should remain when merge fails"


def test_merge_with_keep_flag_skips_cleanup(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies --keep flag merges without cleaning up worktree, window, or branch."""
    env = isolated_tmux_server
    branch_name = "feature-to-keep"
    window_name = get_window_name(branch_name)
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    commit_msg = "feat: add feature"
    create_commit(env, worktree_path, commit_msg)

    commit_hash = env.run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=worktree_path
    ).stdout.strip()

    run_workmux_merge(env, workmux_exe_path, repo_path, branch_name, keep=True)

    # Verify the merge happened
    log_result = env.run_command(["git", "log", "--oneline", "main"])
    assert commit_hash in log_result.stdout, "Feature commit should be on main branch"

    # Verify cleanup did NOT happen
    assert worktree_path.exists(), "Worktree should still exist with --keep"
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    assert window_name in list_windows_result.stdout, "Tmux window should still exist"
    branch_list_result = env.run_command(["git", "branch", "--list", branch_name])
    assert branch_name in branch_list_result.stdout, "Local branch should still exist"


def test_merge_with_custom_target_branch(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merging into a custom target branch (not main)."""
    env = isolated_tmux_server
    target_branch = "feature-target"
    source_branch = "feature-source"
    target_window = get_window_name(target_branch)
    source_window = get_window_name(source_branch)
    write_workmux_config(repo_path, env=env)

    # Create the target worktree first (feature-target branched from main)
    run_workmux_add(env, workmux_exe_path, repo_path, target_branch)
    target_worktree = get_worktree_path(repo_path, target_branch)

    # Create a commit on the target branch
    target_commit_msg = "feat: target branch work"
    create_commit(env, target_worktree, target_commit_msg)

    # Create the source worktree branched from target
    run_workmux_add(env, workmux_exe_path, repo_path, source_branch, base=target_branch)
    source_worktree = get_worktree_path(repo_path, source_branch)

    # Create a commit on the source branch
    source_commit_msg = "feat: source branch work"
    create_commit(env, source_worktree, source_commit_msg)
    source_commit_hash = env.run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=source_worktree
    ).stdout.strip()

    # Merge source into target (not main)
    run_workmux_merge(
        env, workmux_exe_path, repo_path, source_branch, target=target_branch
    )

    # Source worktree should be cleaned up
    assert not source_worktree.exists(), "Source worktree should be removed"

    # Source window should be closed
    list_windows_result = env.tmux(["list-windows", "-F", "#{window_name}"])
    assert source_window not in list_windows_result.stdout, (
        "Source tmux window should be closed"
    )

    # Source branch should be deleted
    branch_list_result = env.run_command(["git", "branch", "--list", source_branch])
    assert source_branch not in branch_list_result.stdout, (
        "Source branch should be deleted"
    )

    # Target worktree should still exist
    assert target_worktree.exists(), "Target worktree should still exist"

    # Target window should still exist
    assert target_window in list_windows_result.stdout, (
        "Target tmux window should still exist"
    )

    # Verify the merge happened on the target branch, not main
    target_log = env.run_command(["git", "log", "--oneline", target_branch])
    assert source_commit_hash in target_log.stdout, (
        "Source commit should be on target branch"
    )

    main_log = env.run_command(["git", "log", "--oneline", "main"])
    assert source_commit_hash not in main_log.stdout, (
        "Source commit should NOT be on main branch"
    )


def test_merge_target_self_merge_fails(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merge fails when trying to merge a branch into itself."""
    env = isolated_tmux_server
    branch_name = "feature-self-merge"
    write_workmux_config(repo_path, env=env)
    run_workmux_add(env, workmux_exe_path, repo_path, branch_name)

    worktree_path = get_worktree_path(repo_path, branch_name)
    create_commit(env, worktree_path, "feat: some work")

    # Try to merge branch into itself
    run_workmux_merge(
        env,
        workmux_exe_path,
        repo_path,
        branch_name,
        target=branch_name,
        expect_fail=True,
    )

    # Worktree should still exist
    assert worktree_path.exists(), "Worktree should remain when merge fails"


def test_merge_target_without_worktree_fails(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies merge fails when target branch has no active worktree."""
    env = isolated_tmux_server
    source_branch = "feature-source"
    target_branch = "nonexistent-worktree"
    write_workmux_config(repo_path, env=env)

    # Create source worktree
    run_workmux_add(env, workmux_exe_path, repo_path, source_branch)
    source_worktree = get_worktree_path(repo_path, source_branch)
    create_commit(env, source_worktree, "feat: source work")

    # Create target branch but WITHOUT a worktree
    env.run_command(["git", "branch", target_branch], cwd=repo_path)

    # Try to merge into target without worktree - should fail
    run_workmux_merge(
        env,
        workmux_exe_path,
        repo_path,
        source_branch,
        target=target_branch,
        expect_fail=True,
    )

    # Source worktree should still exist
    assert source_worktree.exists(), "Source worktree should remain when merge fails"


def test_merge_target_with_rebase_strategy(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies --rebase merge into custom target results in linear history."""
    env = isolated_tmux_server
    target_branch = "feature-target-rebase"
    source_branch = "feature-source-rebase"
    write_workmux_config(repo_path, env=env)

    # Create target worktree
    run_workmux_add(env, workmux_exe_path, repo_path, target_branch)
    target_worktree = get_worktree_path(repo_path, target_branch)

    # Create source worktree from target
    run_workmux_add(env, workmux_exe_path, repo_path, source_branch, base=target_branch)
    source_worktree = get_worktree_path(repo_path, source_branch)

    # Create a commit on target after source branched to create divergent history
    target_commit_msg = "feat: target update after branch"
    create_commit(env, target_worktree, target_commit_msg)

    # Create a commit on source
    source_commit_msg = "feat: source rebased feature"
    create_commit(env, source_worktree, source_commit_msg)

    # Merge with rebase
    run_workmux_merge(
        env,
        workmux_exe_path,
        repo_path,
        source_branch,
        target=target_branch,
        rebase=True,
    )

    assert not source_worktree.exists()

    # Verify linear history (no merge commits)
    log_result = env.run_command(["git", "log", "--oneline", target_branch])
    assert source_commit_msg in log_result.stdout, (
        "Source commit should be in target history"
    )
    assert "Merge branch" not in log_result.stdout, "No merge commit for rebase"


def test_merge_target_navigates_to_target_window(
    isolated_tmux_server: TmuxEnvironment, workmux_exe_path: Path, repo_path: Path
):
    """Verifies after merge, tmux navigates to target branch window (not main)."""
    env = isolated_tmux_server
    target_branch = "feature-target-nav"
    source_branch = "feature-source-nav"
    target_window = get_window_name(target_branch)
    write_workmux_config(repo_path, env=env)

    # Create target worktree
    run_workmux_add(env, workmux_exe_path, repo_path, target_branch)
    target_worktree = get_worktree_path(repo_path, target_branch)

    # Create source worktree from target
    run_workmux_add(env, workmux_exe_path, repo_path, source_branch, base=target_branch)
    source_worktree = get_worktree_path(repo_path, source_branch)
    create_commit(env, source_worktree, "feat: source work")

    # Merge source into target
    run_workmux_merge(
        env, workmux_exe_path, repo_path, source_branch, target=target_branch
    )

    # Wait a bit for tmux navigation to complete
    import time

    time.sleep(0.5)

    # Check which window is currently active
    current_window = env.tmux(
        ["display-message", "-p", "#{window_name}"]
    ).stdout.strip()

    assert current_window == target_window, (
        f"Should navigate to target window '{target_window}', but current is '{current_window}'"
    )
