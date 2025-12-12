use anyhow::{Context, Result};
use std::path::Path;
use std::{thread, time::Duration};

use crate::{cmd, git, zellij};
use tracing::{debug, info, warn};

use super::context::WorkflowContext;
use super::types::CleanupResult;

const WINDOW_CLOSE_DELAY_MS: u64 = 300;

/// Centralized function to clean up zellij and git resources
pub fn cleanup(
    context: &WorkflowContext,
    branch_name: &str,
    worktree_path: &Path,
    force: bool,
    delete_remote: bool,
    keep_branch: bool,
) -> Result<CleanupResult> {
    info!(
        branch = branch_name,
        path = %worktree_path.display(),
        force,
        delete_remote,
        keep_branch,
        "cleanup:start"
    );
    // Change the CWD to main worktree before any destructive operations.
    // This prevents "Unable to read current working directory" errors when the command
    // is run from within the worktree being deleted.
    context.chdir_to_main_worktree()?;

    let zellij_running = zellij::is_running().unwrap_or(false);
    let running_inside_target_tab = if zellij_running {
        match zellij::current_tab_name() {
            Ok(Some(current_name)) => current_name == zellij::prefixed(&context.prefix, branch_name),
            _ => false,
        }
    } else {
        false
    };

    let mut result = CleanupResult {
        tmux_window_killed: false, // TODO: rename to zellij_tab_closed in types.rs
        worktree_removed: false,
        local_branch_deleted: false,
        remote_branch_deleted: false,
        remote_delete_error: None,
        ran_inside_target_window: running_inside_target_tab,
    };

    // Helper closure to perform the actual filesystem and git cleanup.
    // This avoids code duplication while enforcing the correct operational order.
    let perform_fs_git_cleanup = |result: &mut CleanupResult| -> Result<()> {
        // Run pre-delete hooks before removing the worktree directory
        if let Some(pre_delete_hooks) = &context.config.pre_delete {
            info!(
                branch = branch_name,
                count = pre_delete_hooks.len(),
                "cleanup:running pre-delete hooks"
            );
            for command in pre_delete_hooks {
                // Run the hook with the worktree path as the working directory.
                // This allows for relative paths like `node_modules` in the command.
                cmd::shell_command(command, worktree_path)
                    .with_context(|| format!("Failed to run pre-delete command: '{}'", command))?;
            }
        }

        // 1. Forcefully remove the worktree directory from the filesystem.
        if worktree_path.exists() {
            std::fs::remove_dir_all(worktree_path).with_context(|| {
                format!(
                    "Failed to remove worktree directory at {}. \
                Please close any terminals or editors using this directory and try again.",
                    worktree_path.display()
                )
            })?;
            result.worktree_removed = true;
            info!(branch = branch_name, path = %worktree_path.display(), "cleanup:worktree directory removed");
        }

        // Clean up the prompt file if it exists
        let prompt_filename = format!("workmux-prompt-{}.md", branch_name);
        let prompt_file = std::env::temp_dir().join(prompt_filename);
        if prompt_file.exists() {
            if let Err(e) = std::fs::remove_file(&prompt_file) {
                warn!(path = %prompt_file.display(), error = %e, "cleanup:failed to remove prompt file");
            } else {
                debug!(path = %prompt_file.display(), "cleanup:prompt file removed");
            }
        }

        // 2. Prune worktrees to clean up git's metadata.
        git::prune_worktrees().context("Failed to prune worktrees")?;
        debug!("cleanup:git worktrees pruned");

        // 3. Delete the local branch (unless keeping it).
        if !keep_branch {
            git::delete_branch(branch_name, force).context("Failed to delete local branch")?;
            result.local_branch_deleted = true;
            info!(branch = branch_name, "cleanup:local branch deleted");
        }

        // 4. Delete the remote branch if requested (redundant check due to CLI conflict, but safe).
        if delete_remote && !keep_branch {
            match git::delete_remote_branch(branch_name) {
                Ok(_) => {
                    result.remote_branch_deleted = true;
                    info!(branch = branch_name, "cleanup:remote branch deleted");
                }
                Err(e) => {
                    warn!(branch = branch_name, error = %e, "cleanup:failed to delete remote branch");
                    result.remote_delete_error = Some(e.to_string());
                }
            }
        }
        Ok(())
    };

    if running_inside_target_tab {
        info!(
            branch = branch_name,
            "cleanup:deferring zellij tab close because command is running inside the tab"
        );
        // Perform all filesystem and git cleanup *before* returning. The caller
        // will then schedule the asynchronous tab close.
        perform_fs_git_cleanup(&mut result)?;
    } else {
        // Not running inside the target tab, so we close the tab first
        // to release any shell locks on the directory.
        if zellij_running && zellij::tab_exists(&context.prefix, branch_name).unwrap_or(false) {
            zellij::close_tab(&context.prefix, branch_name)
                .context("Failed to close zellij tab")?;
            result.tmux_window_killed = true; // TODO: rename field
            info!(branch = branch_name, "cleanup:zellij tab closed");

            // Poll to confirm the tab is gone before proceeding. This prevents a race
            // condition where we try to delete the directory before the shell inside
            // the zellij tab has terminated.
            const MAX_RETRIES: u32 = 20;
            const RETRY_DELAY: Duration = Duration::from_millis(50);
            let mut tab_is_gone = false;
            for _ in 0..MAX_RETRIES {
                if !zellij::tab_exists(&context.prefix, branch_name)? {
                    tab_is_gone = true;
                    break;
                }
                thread::sleep(RETRY_DELAY);
            }

            if !tab_is_gone {
                warn!(
                    branch = branch_name,
                    "cleanup:zellij tab did not close within retry budget"
                );
                eprintln!(
                    "Warning: zellij tab for '{}' did not close in the allotted time. \
                    Filesystem cleanup may fail.",
                    branch_name
                );
            }
        }
        // Now that the tab is gone, it's safe to clean up the filesystem and git state.
        perform_fs_git_cleanup(&mut result)?;
    }

    Ok(result)
}

/// Navigate to the main branch tab and close the target tab.
/// Handles both cases: running inside the target tab (async) and outside (sync).
pub fn navigate_to_main_and_close(
    prefix: &str,
    main_branch: &str,
    target_branch: &str,
    cleanup_result: &CleanupResult,
) -> Result<()> {
    // Check if main branch tab exists
    if !zellij::is_running()? || !zellij::tab_exists(prefix, main_branch)? {
        // If main tab doesn't exist, still need to close target tab if running inside it
        if cleanup_result.ran_inside_target_window {
            let delay = Duration::from_millis(WINDOW_CLOSE_DELAY_MS);
            match zellij::schedule_tab_close(prefix, target_branch, delay) {
                Ok(_) => info!(
                    branch = target_branch,
                    "cleanup:zellij tab close scheduled"
                ),
                Err(e) => warn!(
                    branch = target_branch,
                    error = %e,
                    "cleanup:failed to schedule zellij tab close",
                ),
            }
        }
        return Ok(());
    }

    if cleanup_result.ran_inside_target_window {
        // Running inside target tab: schedule both navigation and close together
        let delay = Duration::from_millis(WINDOW_CLOSE_DELAY_MS);
        let delay_secs = format!("{:.3}", delay.as_secs_f64());
        let main_prefixed = zellij::prefixed(prefix, main_branch);
        let target_prefixed = zellij::prefixed(prefix, target_branch);

        // Use nohup for async execution since zellij has no run-shell equivalent
        let script = format!(
            r#"sleep {delay}; zellij action go-to-tab-name "{main}" 2>/dev/null; zellij action go-to-tab-name "{target}" 2>/dev/null && zellij action close-tab 2>/dev/null"#,
            delay = delay_secs,
            main = main_prefixed,
            target = target_prefixed,
        );

        match std::process::Command::new("sh")
            .args(["-c", &format!("nohup sh -c '{}' >/dev/null 2>&1 &", script)])
            .spawn()
        {
            Ok(_) => info!(
                branch = target_branch,
                main = main_branch,
                "cleanup:scheduled navigation to main and tab close"
            ),
            Err(e) => warn!(
                branch = target_branch,
                error = %e,
                "cleanup:failed to schedule navigation and tab close",
            ),
        }
    } else {
        // Running outside target tab: synchronously navigate to main and close target
        zellij::select_tab(prefix, main_branch)?;
        info!(
            branch = target_branch,
            main = main_branch,
            "cleanup:navigated to main branch tab"
        );

        // Close the target tab now that we've navigated away
        match zellij::close_tab(prefix, target_branch) {
            Ok(_) => info!(
                branch = target_branch,
                "cleanup:closed target branch tab"
            ),
            Err(e) => warn!(
                branch = target_branch,
                error = %e,
                "cleanup:failed to close target branch tab",
            ),
        }
    }

    Ok(())
}
