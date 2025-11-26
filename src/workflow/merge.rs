use anyhow::{Context, Result, anyhow};

use crate::git;
use tracing::{debug, info};

use super::cleanup;
use super::context::WorkflowContext;
use super::types::MergeResult;

/// Merge a branch into a target branch and clean up
#[allow(clippy::too_many_arguments)]
pub fn merge(
    branch_name: &str,
    ignore_uncommitted: bool,
    delete_remote: bool,
    rebase: bool,
    squash: bool,
    keep: bool,
    target_branch: &str,
    context: &WorkflowContext,
) -> Result<MergeResult> {
    info!(
        branch = branch_name,
        target = target_branch,
        ignore_uncommitted,
        delete_remote,
        rebase,
        squash,
        keep,
        "merge:start"
    );

    // Change CWD to main worktree to prevent errors if the command is run from within
    // the worktree that is about to be deleted.
    context.chdir_to_main_worktree()?;

    let branch_to_merge = branch_name;

    // Get worktree path for the branch to be merged
    let worktree_path = git::get_worktree_path(branch_to_merge)
        .with_context(|| format!("No worktree found for branch '{}'", branch_to_merge))?;
    debug!(
        branch = branch_to_merge,
        path = %worktree_path.display(),
        "merge:worktree resolved"
    );

    // Resolve target worktree path - use main worktree if target is main, otherwise look up the target's worktree
    let target_worktree = if target_branch == context.main_branch {
        context.main_worktree_root.clone()
    } else {
        git::get_worktree_path(target_branch)
            .with_context(|| format!("No worktree found for target branch '{}'. The target branch must have an active worktree.", target_branch))?
    };
    debug!(
        target = target_branch,
        path = %target_worktree.display(),
        "merge:target worktree resolved"
    );

    // Handle changes in the source worktree
    if git::has_unstaged_changes(&worktree_path)? && !ignore_uncommitted {
        return Err(anyhow!(
            "Worktree for '{}' has unstaged changes. Please stage or stash them, or use --ignore-uncommitted.",
            branch_to_merge
        ));
    }

    let had_staged_changes = git::has_staged_changes(&worktree_path)?;
    if had_staged_changes && !ignore_uncommitted {
        // Commit using git's editor (respects $EDITOR or git config)
        info!(path = %worktree_path.display(), "merge:committing staged changes");
        git::commit_with_editor(&worktree_path).context("Failed to commit staged changes")?;
    }

    if branch_to_merge == target_branch {
        return Err(anyhow!("Cannot merge a branch into itself."));
    }
    debug!(
        branch = branch_to_merge,
        target = target_branch,
        "merge:target branch resolved"
    );

    // Safety check: Abort if the target worktree has uncommitted changes
    if git::has_uncommitted_changes(&target_worktree)? {
        return Err(anyhow!(
            "Target worktree '{}' has uncommitted changes. Please commit or stash them before merging.",
            target_branch
        ));
    }

    // Explicitly switch to the target branch to ensure correct merge target
    git::switch_branch_in_worktree(&target_worktree, target_branch)?;

    // Helper closure to generate the error message for merge conflicts
    let conflict_err = |branch: &str| -> anyhow::Error {
        anyhow!(
            "Merge failed due to conflicts. Target worktree kept clean.\n\n\
            To resolve, update your branch in worktree at {}:\n\
              git rebase {}  (recommended)\n\
            Or:\n\
              git merge {}\n\n\
            After resolving conflicts, retry: workmux merge {}{}",
            worktree_path.display(),
            target_branch,
            target_branch,
            branch,
            if target_branch != context.main_branch {
                format!(" --target {}", target_branch)
            } else {
                String::new()
            }
        )
    };

    if rebase {
        // Rebase the feature branch on top of target inside its own worktree.
        // This is where conflicts will be detected.
        println!(
            "Rebasing '{}' onto '{}'...",
            &branch_to_merge, target_branch
        );
        info!(
            branch = branch_to_merge,
            base = target_branch,
            "merge:rebase start"
        );
        git::rebase_branch_onto_base(&worktree_path, target_branch).with_context(|| {
            format!(
                "Rebase failed, likely due to conflicts.\n\n\
                Please resolve them manually inside the worktree at '{}'.\n\
                Then, run 'git rebase --continue' to proceed or 'git rebase --abort' to cancel.",
                worktree_path.display()
            )
        })?;

        // After a successful rebase, merge into target. This will be a fast-forward.
        git::merge_in_worktree(&target_worktree, branch_to_merge)
            .context("Failed to merge rebased branch. This should have been a fast-forward.")?;
        info!(branch = branch_to_merge, "merge:fast-forward complete");
    } else if squash {
        // Perform the squash merge. This stages all changes from the feature branch but does not commit.
        if let Err(e) = git::merge_squash_in_worktree(&target_worktree, branch_to_merge) {
            info!(branch = branch_to_merge, error = %e, "merge:squash merge failed, resetting target worktree");
            // Best effort to reset; ignore failure as the user message is the priority.
            let _ = git::reset_hard(&target_worktree);
            return Err(conflict_err(branch_to_merge));
        }

        // Prompt the user to provide a commit message for the squashed changes.
        println!("Staged squashed changes. Please provide a commit message in your editor.");
        git::commit_with_editor(&target_worktree)
            .context("Failed to commit squashed changes. You may need to commit them manually.")?;
        info!(branch = branch_to_merge, "merge:squash merge committed");
    } else {
        // Default merge commit workflow
        if let Err(e) = git::merge_in_worktree(&target_worktree, branch_to_merge) {
            info!(branch = branch_to_merge, error = %e, "merge:standard merge failed, aborting merge in target worktree");
            // Best effort to abort; ignore failure as the user message is the priority.
            let _ = git::abort_merge_in_worktree(&target_worktree);
            return Err(conflict_err(branch_to_merge));
        }
        info!(branch = branch_to_merge, "merge:standard merge complete");
    }

    // Skip cleanup if --keep flag is used
    if keep {
        info!(branch = branch_to_merge, "merge:skipping cleanup (--keep)");
        return Ok(MergeResult {
            branch_merged: branch_to_merge.to_string(),
            merge_target: target_branch.to_string(),
            had_staged_changes,
        });
    }

    // Always force cleanup after a successful merge
    info!(
        branch = branch_to_merge,
        delete_remote, "merge:cleanup start"
    );
    let cleanup_result = cleanup::cleanup(
        context,
        branch_to_merge,
        &worktree_path,
        true,
        delete_remote,
        false, // keep_branch: always delete when merging
    )?;

    // Navigate to the target branch window and close the source window
    cleanup::navigate_to_main_and_close(
        &context.prefix,
        target_branch,
        branch_to_merge,
        &cleanup_result,
    )?;

    Ok(MergeResult {
        branch_merged: branch_to_merge.to_string(),
        merge_target: target_branch.to_string(),
        had_staged_changes,
    })
}
