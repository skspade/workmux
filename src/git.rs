use anyhow::{anyhow, Context, Result};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::cmd::Cmd;

/// Custom error type for worktree not found
#[derive(Debug, thiserror::Error)]
#[error("Worktree not found for branch: {0}")]
pub struct WorktreeNotFound(String);

/// Check if we're in a git repository
pub fn is_git_repo() -> Result<bool> {
    Cmd::new("git")
        .args(&["rev-parse", "--git-dir"])
        .run_as_check()
}

/// Get the root directory of the git repository
pub fn get_repo_root() -> Result<PathBuf> {
    let path = Cmd::new("git")
        .args(&["rev-parse", "--show-toplevel"])
        .run_and_capture_stdout()?;
    Ok(PathBuf::from(path))
}

/// Get the main worktree root directory (not a linked worktree)
pub fn get_main_worktree_root() -> Result<PathBuf> {
    // Get all worktrees
    let list_str = Cmd::new("git")
        .args(&["worktree", "list", "--porcelain"])
        .run_and_capture_stdout()
        .context("Failed to list worktrees while locating main worktree")?;

    let worktrees = parse_worktree_list_porcelain(&list_str)?;

    // The first worktree in the list is always the main worktree
    if let Some((path, _)) = worktrees.first() {
        Ok(path.clone())
    } else {
        Err(anyhow!("No main worktree found"))
    }
}

/// Get the default branch (main or master)
pub fn get_default_branch() -> Result<String> {
    // Try to get the default branch from the remote
    if let Ok(ref_name) = Cmd::new("git")
        .args(&["symbolic-ref", "refs/remotes/origin/HEAD"])
        .run_and_capture_stdout()
    {
        if let Some(branch) = ref_name.strip_prefix("refs/remotes/origin/") {
            return Ok(branch.to_string());
        }
    }

    // Fallback: check if main or master exists locally
    if branch_exists("main")? {
        return Ok("main".to_string());
    }

    if branch_exists("master")? {
        return Ok("master".to_string());
    }

    // No default branch could be determined - require explicit configuration
    Err(anyhow!(
        "Could not determine the default branch (e.g., 'main' or 'master'). \
        Please specify it in .workmux.yaml using the 'main_branch' key."
    ))
}

/// Check if a branch exists (can be local or remote tracking branch)
pub fn branch_exists(branch_name: &str) -> Result<bool> {
    Cmd::new("git")
        .args(&["rev-parse", "--verify", "--quiet", branch_name])
        .run_as_check()
}

/// Check if a worktree already exists for a branch
pub fn worktree_exists(branch_name: &str) -> Result<bool> {
    match get_worktree_path(branch_name) {
        Ok(_) => Ok(true),
        Err(e) => {
            // Check if this is a WorktreeNotFound error
            if e.is::<WorktreeNotFound>() {
                Ok(false)
            } else {
                Err(e)
            }
        }
    }
}

/// Create a new git worktree
pub fn create_worktree(worktree_path: &Path, branch_name: &str, create_branch: bool) -> Result<()> {
    let path_str = worktree_path
        .to_str()
        .ok_or_else(|| anyhow!("Invalid worktree path"))?;

    let mut cmd = Cmd::new("git").arg("worktree").arg("add");

    if create_branch {
        cmd = cmd.arg("-b").arg(branch_name).arg(path_str);
    } else {
        cmd = cmd.arg(path_str).arg(branch_name);
    }

    cmd.run().context("Failed to create worktree")?;
    Ok(())
}

/// Remove a git worktree
pub fn remove_worktree(branch_name: &str, force: bool) -> Result<()> {
    // Run from main worktree root to avoid issues when removing from within a worktree
    let main_worktree_root = get_main_worktree_root()?;
    let worktree_path = get_worktree_path(branch_name)?;

    let path_str = worktree_path.to_str().ok_or_else(|| {
        anyhow!(
            "Worktree path is not valid UTF-8: {}",
            worktree_path.display()
        )
    })?;

    let mut cmd = Cmd::new("git")
        .workdir(&main_worktree_root)
        .arg("worktree")
        .arg("remove");
    if force {
        cmd = cmd.arg("--force");
    }
    cmd.arg(path_str)
        .run()
        .context("Failed to remove worktree")?;

    Ok(())
}

/// Prune stale worktree metadata
pub fn prune_worktrees() -> Result<()> {
    Cmd::new("git")
        .args(&["worktree", "prune"])
        .run()
        .context("Failed to prune worktrees")?;
    Ok(())
}

/// Parse the output of `git worktree list --porcelain`
fn parse_worktree_list_porcelain(output: &str) -> Result<Vec<(PathBuf, String)>> {
    let mut worktrees = Vec::new();
    for block in output.trim().split("\n\n") {
        let mut path: Option<PathBuf> = None;
        let mut branch: Option<String> = None;

        for line in block.lines() {
            if let Some(p) = line.strip_prefix("worktree ") {
                path = Some(PathBuf::from(p));
            } else if let Some(b) = line.strip_prefix("branch refs/heads/") {
                branch = Some(b.to_string());
            } else if line.trim() == "detached" {
                branch = Some("(detached)".to_string());
            }
        }

        if let (Some(p), Some(b)) = (path, branch) {
            worktrees.push((p, b));
        }
    }
    Ok(worktrees)
}

/// Get the path to a worktree for a given branch
pub fn get_worktree_path(branch_name: &str) -> Result<PathBuf> {
    let list_str = Cmd::new("git")
        .args(&["worktree", "list", "--porcelain"])
        .run_and_capture_stdout()
        .context("Failed to list worktrees while locating worktree path")?;

    let worktrees = parse_worktree_list_porcelain(&list_str)?;

    for (path, branch) in worktrees {
        if branch == branch_name {
            return Ok(path);
        }
    }

    Err(WorktreeNotFound(branch_name.to_string()).into())
}

/// List all worktrees with their branches
pub fn list_worktrees() -> Result<Vec<(PathBuf, String)>> {
    let list = Cmd::new("git")
        .args(&["worktree", "list", "--porcelain"])
        .run_and_capture_stdout()
        .context("Failed to list worktrees")?;
    parse_worktree_list_porcelain(&list)
}

/// Check if the worktree has uncommitted changes
pub fn has_uncommitted_changes(worktree_path: &Path) -> Result<bool> {
    let output = Cmd::new("git")
        .workdir(worktree_path)
        .args(&["status", "--porcelain"])
        .run_and_capture_stdout()?;

    Ok(!output.is_empty())
}

/// Check if the worktree has staged changes
pub fn has_staged_changes(worktree_path: &Path) -> Result<bool> {
    // Exit code 0 = no changes, 1 = has changes
    // So we invert the result of run_as_check
    let no_changes = Cmd::new("git")
        .workdir(worktree_path)
        .args(&["diff", "--cached", "--quiet"])
        .run_as_check()?;
    Ok(!no_changes)
}

/// Check if the worktree has unstaged changes
pub fn has_unstaged_changes(worktree_path: &Path) -> Result<bool> {
    // Exit code 0 = no changes, 1 = has changes
    // So we invert the result of run_as_check
    let no_changes = Cmd::new("git")
        .workdir(worktree_path)
        .args(&["diff", "--quiet"])
        .run_as_check()?;
    Ok(!no_changes)
}

/// Commit staged changes in a worktree using the user's editor
pub fn commit_with_editor(worktree_path: &Path) -> Result<()> {
    let status = Command::new("git")
        .current_dir(worktree_path)
        .arg("commit")
        .status()
        .context("Failed to run git commit")?;

    if !status.success() {
        return Err(anyhow!("Commit was aborted or failed"));
    }

    Ok(())
}

/// Get the base branch for merge checks, preferring remote tracking branch
pub fn get_merge_base(main_branch: &str) -> Result<String> {
    let remote_main = format!("origin/{}", main_branch);
    if branch_exists(&remote_main)? {
        Ok(remote_main)
    } else {
        Ok(main_branch.to_string())
    }
}

/// Get a set of all branches not merged into the base branch
pub fn get_unmerged_branches(base_branch: &str) -> Result<HashSet<String>> {
    // Special handling for potential errors since base branch might not exist
    let no_merged_arg = format!("--no-merged={}", base_branch);
    let result = Cmd::new("git")
        .args(&[
            "for-each-ref",
            "--format=%(refname:short)",
            &no_merged_arg,
            "refs/heads/",
        ])
        .run_and_capture_stdout();

    match result {
        Ok(stdout) => {
            let branches: HashSet<String> = stdout.lines().map(String::from).collect();
            Ok(branches)
        }
        Err(e) => {
            // Non-fatal error if base branch doesn't exist; return empty set.
            let err_msg = e.to_string();
            if err_msg.contains("malformed object name") || err_msg.contains("unknown commit") {
                Ok(HashSet::new())
            } else {
                Err(e)
            }
        }
    }
}

/// Merge a branch into the current branch in a specific worktree
pub fn merge_in_worktree(worktree_path: &Path, branch_name: &str) -> Result<()> {
    Cmd::new("git")
        .workdir(worktree_path)
        .args(&["merge", branch_name])
        .run()
        .context("Failed to merge")?;
    Ok(())
}

/// Rebase the current branch in a worktree onto a base branch
pub fn rebase_branch_onto_base(worktree_path: &Path, base_branch: &str) -> Result<()> {
    Cmd::new("git")
        .workdir(worktree_path)
        .args(&["rebase", base_branch])
        .run()
        .with_context(|| format!("Failed to rebase onto '{}'", base_branch))?;
    Ok(())
}

/// Perform a squash merge in a specific worktree (does not commit)
pub fn merge_squash_in_worktree(worktree_path: &Path, branch_name: &str) -> Result<()> {
    Cmd::new("git")
        .workdir(worktree_path)
        .args(&["merge", "--squash", branch_name])
        .run()
        .context("Failed to perform squash merge")?;
    Ok(())
}

/// Switch to a different branch in a specific worktree
pub fn switch_branch_in_worktree(worktree_path: &Path, branch_name: &str) -> Result<()> {
    Cmd::new("git")
        .workdir(worktree_path)
        .args(&["switch", branch_name])
        .run()
        .with_context(|| {
            format!(
                "Failed to switch to branch '{}' in worktree '{}'",
                branch_name,
                worktree_path.display()
            )
        })?;
    Ok(())
}

/// Get the current branch name
pub fn get_current_branch() -> Result<String> {
    Cmd::new("git")
        .args(&["branch", "--show-current"])
        .run_and_capture_stdout()
}

/// Delete a local branch
pub fn delete_branch(branch_name: &str, force: bool) -> Result<()> {
    // Run from main worktree root to avoid issues when deleting from within a worktree
    // or after a worktree has been removed
    let main_worktree_root = get_main_worktree_root()?;

    let mut cmd = Cmd::new("git").workdir(&main_worktree_root).arg("branch");

    if force {
        cmd = cmd.arg("-D");
    } else {
        cmd = cmd.arg("-d");
    }

    cmd.arg(branch_name)
        .run()
        .context("Failed to delete branch")?;
    Ok(())
}

/// Delete a remote branch
pub fn delete_remote_branch(branch_name: &str) -> Result<()> {
    Cmd::new("git")
        .args(&["push", "origin", "--delete", branch_name])
        .run()
        .with_context(|| format!("Failed to delete remote branch '{}'", branch_name))?;
    Ok(())
}
