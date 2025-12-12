use anyhow::{Context, Result, anyhow};
use std::borrow::Cow;
use std::collections::HashSet;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

use crate::cmd::Cmd;
use crate::config::PaneConfig;

/// Helper function to add prefix to tab name
pub fn prefixed(prefix: &str, tab_name: &str) -> String {
    format!("{}{}", prefix, tab_name)
}

/// Get all zellij tab names in the current session
pub fn get_all_tab_names() -> Result<HashSet<String>> {
    // zellij action query-tab-names returns tab names, one per line
    let tabs = Cmd::new("zellij")
        .args(&["action", "query-tab-names"])
        .run_and_capture_stdout()
        .unwrap_or_default();

    Ok(tabs.lines().map(String::from).collect())
}

/// Check if zellij is running (inside a zellij session)
pub fn is_running() -> Result<bool> {
    // Check if ZELLIJ environment variable is set, which indicates we're in a session
    Ok(std::env::var("ZELLIJ").is_ok())
}

/// Check if a zellij tab with the given name exists
pub fn tab_exists(prefix: &str, tab_name: &str) -> Result<bool> {
    let prefixed_name = prefixed(prefix, tab_name);
    let tabs = get_all_tab_names()?;
    Ok(tabs.contains(&prefixed_name))
}

/// Return the zellij tab name for the current tab, if any
pub fn current_tab_name() -> Result<Option<String>> {
    // ZELLIJ_TAB_NAME environment variable contains the current tab name
    match std::env::var("ZELLIJ_TAB_NAME") {
        Ok(name) if !name.is_empty() => Ok(Some(name)),
        _ => Ok(None),
    }
}

/// Create a new zellij tab with the given name and working directory.
///
/// When `detached` is true, the tab is created but focus returns to the original tab.
pub fn create_tab(
    prefix: &str,
    tab_name: &str,
    working_dir: &Path,
    detached: bool,
) -> Result<()> {
    let prefixed_name = prefixed(prefix, tab_name);
    let working_dir_str = working_dir
        .to_str()
        .ok_or_else(|| anyhow!("Working directory path contains non-UTF8 characters"))?;

    // Remember current tab if we need to return to it
    let original_tab = if detached {
        current_tab_name().ok().flatten()
    } else {
        None
    };

    // Create the new tab (zellij automatically focuses it)
    Cmd::new("zellij")
        .args(&[
            "action",
            "new-tab",
            "--name",
            &prefixed_name,
            "--cwd",
            working_dir_str,
        ])
        .run()
        .context("Failed to create zellij tab")?;

    // If detached mode, switch back to the original tab
    if let Some(orig_tab) = original_tab {
        Cmd::new("zellij")
            .args(&["action", "go-to-tab-name", &orig_tab])
            .run()
            .context("Failed to return to original tab")?;
    }

    Ok(())
}

/// Select a specific tab by name
pub fn select_tab(prefix: &str, tab_name: &str) -> Result<()> {
    let prefixed_name = prefixed(prefix, tab_name);

    Cmd::new("zellij")
        .args(&["action", "go-to-tab-name", &prefixed_name])
        .run()
        .context("Failed to select tab")?;

    Ok(())
}

/// Close a zellij tab by navigating to it and closing it
pub fn close_tab(prefix: &str, tab_name: &str) -> Result<()> {
    let prefixed_name = prefixed(prefix, tab_name);

    // First navigate to the tab
    Cmd::new("zellij")
        .args(&["action", "go-to-tab-name", &prefixed_name])
        .run()
        .context("Failed to navigate to tab for closing")?;

    // Then close the current tab
    Cmd::new("zellij")
        .args(&["action", "close-tab"])
        .run()
        .context("Failed to close zellij tab")?;

    Ok(())
}

/// Schedule a zellij tab to be closed after a short delay. This is useful when
/// the current command is running inside the tab that needs to close.
pub fn schedule_tab_close(prefix: &str, tab_name: &str, delay: Duration) -> Result<()> {
    let prefixed_name = prefixed(prefix, tab_name);
    let delay_secs = format!("{:.3}", delay.as_secs_f64());

    // Use nohup with shell to run asynchronously since zellij has no run-shell equivalent
    let script = format!(
        r#"sleep {delay}; zellij action go-to-tab-name "{tab}" 2>/dev/null && zellij action close-tab 2>/dev/null"#,
        delay = delay_secs,
        tab = prefixed_name
    );

    Command::new("sh")
        .args(["-c", &format!("nohup sh -c '{}' >/dev/null 2>&1 &", script)])
        .spawn()
        .context("Failed to schedule tab close")?;

    Ok(())
}

/// Builds a shell command string that executes an optional user command
/// and then leaves an interactive shell open.
pub fn build_startup_command(command: Option<&str>) -> Result<Option<String>> {
    let command = match command {
        Some(c) => c,
        None => return Ok(None),
    };

    let shell_path = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
    let shell_name = std::path::Path::new(&shell_path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");

    // Manually trigger shell pre-prompt hooks to ensure tools like direnv,
    // nvm, and rbenv are loaded before the user command is executed.
    let pre_command_hook = match shell_name {
        "zsh" => {
            "if (( ${#precmd_functions[@]} )); then for f in \"${precmd_functions[@]}\"; do \"$f\"; done; fi"
        }
        "bash" => "eval \"${PROMPT_COMMAND:-}\"",
        "fish" => "functions -q fish_prompt; and emit fish_prompt",
        _ => "true",
    };

    let escaped_command = command.replace('\'', r#"'\''"#);

    let inner_command = format!(
        "{pre_hook}; {user_cmd}; exec {shell} -l",
        pre_hook = pre_command_hook,
        user_cmd = escaped_command,
        shell = shell_path,
    );

    let full_command = format!(
        "{shell} -ic '{inner_command}'",
        shell = shell_path,
        inner_command = inner_command,
    );

    Ok(Some(full_command))
}

/// Run a command in the current tab by creating a new pane and running it
pub fn run_command_in_tab(working_dir: &Path, command: &str) -> Result<()> {
    let working_dir_str = working_dir
        .to_str()
        .ok_or_else(|| anyhow!("Working directory path contains non-UTF8 characters"))?;

    // Use zellij action new-pane with -- to run a command
    // Since we're in a single-pane model, we run in the existing pane context
    Cmd::new("zellij")
        .args(&["action", "new-pane", "--cwd", working_dir_str, "--", "sh", "-c", command])
        .run()
        .context("Failed to run command in tab")?;

    Ok(())
}

/// Result of setting up the tab
pub struct TabSetupResult {
    // Placeholder for future use
    _private: (),
}

pub struct TabSetupOptions<'a> {
    pub run_commands: bool,
    pub prompt_file_path: Option<&'a Path>,
}

/// Setup a single pane in a tab according to configuration (simplified from tmux multi-pane)
pub fn setup_tab(
    panes: &[PaneConfig],
    working_dir: &Path,
    options: TabSetupOptions<'_>,
    config: &crate::config::Config,
    task_agent: Option<&str>,
) -> Result<TabSetupResult> {
    if panes.is_empty() || !options.run_commands {
        return Ok(TabSetupResult { _private: () });
    }

    let effective_agent = task_agent.or(config.agent.as_deref());

    // Use only the first pane configuration (simplified single-pane model)
    if let Some(pane_config) = panes.first() {
        let command_to_run = if pane_config.command.as_deref() == Some("<agent>") {
            effective_agent.map(|agent_cmd| agent_cmd.to_string())
        } else {
            pane_config.command.clone()
        };

        if let Some(ref cmd) = command_to_run {
            let adjusted_command = adjust_command(
                cmd,
                options.prompt_file_path,
                working_dir,
                effective_agent,
            );

            if let Some(startup_cmd) = build_startup_command(Some(&adjusted_command))? {
                run_command_in_tab(working_dir, &startup_cmd)?;
            }
        }
    }

    // Warn if multi-pane config detected
    if panes.len() > 1 {
        tracing::warn!(
            "Multi-pane configuration detected but zellij only supports single-pane mode. Only the first pane will be used."
        );
    }

    Ok(TabSetupResult { _private: () })
}

fn adjust_command<'a>(
    command: &'a str,
    prompt_file_path: Option<&Path>,
    working_dir: &Path,
    effective_agent: Option<&str>,
) -> Cow<'a, str> {
    if let Some(prompt_path) = prompt_file_path
        && let Some(rewritten) =
            rewrite_agent_command(command, prompt_path, working_dir, effective_agent)
    {
        return Cow::Owned(rewritten);
    }
    Cow::Borrowed(command)
}

/// Rewrites an agent command to inject a prompt file's contents.
fn rewrite_agent_command(
    command: &str,
    prompt_file: &Path,
    working_dir: &Path,
    effective_agent: Option<&str>,
) -> Option<String> {
    let agent_command = effective_agent?;
    let trimmed_command = command.trim();
    if trimmed_command.is_empty() {
        return None;
    }

    let (pane_token, pane_rest) = crate::config::split_first_token(trimmed_command)?;
    let (config_token, _) = crate::config::split_first_token(agent_command)?;

    let resolved_pane_path = crate::config::resolve_executable_path(pane_token)
        .unwrap_or_else(|| pane_token.to_string());
    let resolved_config_path = crate::config::resolve_executable_path(config_token)
        .unwrap_or_else(|| config_token.to_string());

    let pane_stem = Path::new(&resolved_pane_path).file_stem();
    let config_stem = Path::new(&resolved_config_path).file_stem();

    if pane_stem != config_stem {
        return None;
    }

    let relative = prompt_file.strip_prefix(working_dir).unwrap_or(prompt_file);
    let prompt_path = relative.to_string_lossy();
    let rest = pane_rest.trim_start();

    let mut cmd = pane_token.to_string();

    if !rest.is_empty() {
        cmd.push(' ');
        cmd.push_str(rest);
    }

    let is_gemini = pane_stem.and_then(|s| s.to_str()) == Some("gemini");
    if is_gemini {
        cmd.push_str(&format!(" -i \"$(cat {})\"", prompt_path));
    } else {
        cmd.push_str(&format!(" -- \"$(cat {})\"", prompt_path));
    }

    Some(cmd)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_prefixed() {
        assert_eq!(prefixed("wm-", "feature"), "wm-feature");
        assert_eq!(prefixed("", "feature"), "feature");
    }

    #[test]
    fn test_rewrite_claude_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("claude", &prompt_file, &working_dir, Some("claude"));
        assert_eq!(result, Some("claude -- \"$(cat PROMPT.md)\"".to_string()));
    }

    #[test]
    fn test_rewrite_gemini_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("gemini", &prompt_file, &working_dir, Some("gemini"));
        assert_eq!(result, Some("gemini -i \"$(cat PROMPT.md)\"".to_string()));
    }

    #[test]
    fn test_rewrite_mismatched_agent() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("claude", &prompt_file, &working_dir, Some("gemini"));
        assert_eq!(result, None);
    }

    #[test]
    fn test_rewrite_empty_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("", &prompt_file, &working_dir, Some("claude"));
        assert_eq!(result, None);
    }
}
