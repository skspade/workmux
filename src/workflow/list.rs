use anyhow::{Result, anyhow};

use crate::{config, git, zellij};

use super::types::WorktreeInfo;

/// List all worktrees with their status
pub fn list(config: &config::Config) -> Result<Vec<WorktreeInfo>> {
    if !git::is_git_repo()? {
        return Err(anyhow!("Not in a git repository"));
    }

    let worktrees_data = git::list_worktrees()?;

    if worktrees_data.is_empty() {
        return Ok(Vec::new());
    }

    // Check zellij status and get all tabs once to avoid repeated process calls
    let zellij_tabs: std::collections::HashSet<String> = if zellij::is_running().unwrap_or(false) {
        zellij::get_all_tab_names().unwrap_or_default()
    } else {
        std::collections::HashSet::new()
    };

    // Get the main branch for unmerged checks
    let main_branch = git::get_default_branch().ok();

    // Get all unmerged branches in one go for efficiency
    // Prefer checking against remote tracking branch for more accurate results
    let unmerged_branches = main_branch
        .as_deref()
        .and_then(|main| git::get_merge_base(main).ok())
        .and_then(|base| git::get_unmerged_branches(&base).ok())
        .unwrap_or_default(); // Use an empty set on failure

    let prefix = config.window_prefix();
    let worktrees: Vec<WorktreeInfo> = worktrees_data
        .into_iter()
        .map(|(path, branch)| {
            let prefixed_branch_name = zellij::prefixed(prefix, &branch);
            let has_tmux = zellij_tabs.contains(&prefixed_branch_name); // TODO: rename field to has_zellij

            // Check for unmerged commits, but only if this isn't the main branch
            let has_unmerged = if let Some(ref main) = main_branch {
                if branch == *main || branch == "(detached)" {
                    false
                } else {
                    unmerged_branches.contains(&branch)
                }
            } else {
                false
            };

            WorktreeInfo {
                branch,
                path,
                has_tmux,
                has_unmerged,
            }
        })
        .collect();

    Ok(worktrees)
}
