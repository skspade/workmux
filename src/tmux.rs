use anyhow::{Context, Result, anyhow};
use std::borrow::Cow;
use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

use crate::cmd::Cmd;
use crate::config::{PaneConfig, SplitDirection};

/// Helper function to add prefix to window name
pub fn prefixed(prefix: &str, window_name: &str) -> String {
    format!("{}{}", prefix, window_name)
}

/// Get all tmux window names in a single call
pub fn get_all_window_names() -> Result<HashSet<String>> {
    // tmux list-windows may exit with error if no windows exist
    let windows = Cmd::new("tmux")
        .args(&["list-windows", "-F", "#{window_name}"])
        .run_and_capture_stdout()
        .unwrap_or_default(); // Return empty string if command fails

    Ok(windows.lines().map(String::from).collect())
}

/// Check if tmux server is running
pub fn is_running() -> Result<bool> {
    Cmd::new("tmux").arg("has-session").run_as_check()
}

/// Check if a tmux window with the given name exists
pub fn window_exists(prefix: &str, window_name: &str) -> Result<bool> {
    let prefixed_name = prefixed(prefix, window_name);
    let windows = Cmd::new("tmux")
        .args(&["list-windows", "-F", "#{window_name}"])
        .run_and_capture_stdout();

    match windows {
        Ok(output) => Ok(output.lines().any(|line| line == prefixed_name)),
        Err(_) => Ok(false), // If command fails, window doesn't exist
    }
}

/// Return the tmux window name for the current pane, if any
pub fn current_window_name() -> Result<Option<String>> {
    match Cmd::new("tmux")
        .args(&["display-message", "-p", "#{window_name}"])
        .run_and_capture_stdout()
    {
        Ok(name) => Ok(Some(name.trim().to_string())),
        Err(_) => Ok(None),
    }
}

/// Create a new tmux window with the given name and working directory
pub fn create_window(prefix: &str, window_name: &str, working_dir: &Path) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let working_dir_str = working_dir
        .to_str()
        .ok_or_else(|| anyhow!("Working directory path contains non-UTF8 characters"))?;

    Cmd::new("tmux")
        .args(&["new-window", "-n", &prefixed_name, "-c", working_dir_str])
        .run()
        .context("Failed to create tmux window")?;

    Ok(())
}

/// Select a specific pane
pub fn select_pane(prefix: &str, window_name: &str, pane_index: usize) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let target = format!("={}.{}", prefixed_name, pane_index);

    Cmd::new("tmux")
        .args(&["select-pane", "-t", &target])
        .run()
        .context("Failed to select pane")?;

    Ok(())
}

/// Select a specific window
pub fn select_window(prefix: &str, window_name: &str) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let target = format!("={}", prefixed_name);

    Cmd::new("tmux")
        .args(&["select-window", "-t", &target])
        .run()
        .context("Failed to select window")?;

    Ok(())
}

/// Kill a tmux window
pub fn kill_window(prefix: &str, window_name: &str) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let target = format!("={}", prefixed_name);

    Cmd::new("tmux")
        .args(&["kill-window", "-t", &target])
        .run()
        .context("Failed to kill tmux window")?;

    Ok(())
}

/// Schedule a tmux window to be killed after a short delay. This is useful when
/// the current command is running inside the window that needs to close.
pub fn schedule_window_close(prefix: &str, window_name: &str, delay: Duration) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let delay_secs = format!("{:.3}", delay.as_secs_f64());
    let script = format!(
        "sleep {delay}; tmux kill-window -t ={window} >/dev/null 2>&1",
        delay = delay_secs,
        window = prefixed_name
    );

    Cmd::new("tmux")
        .args(&["run-shell", &script])
        .run()
        .context("Failed to schedule tmux window close")?;

    Ok(())
}

/// Builds a shell command string for tmux that executes an optional user command
/// and then leaves an interactive shell open.
///
/// The escaping strategy uses POSIX-style quote escaping ('\'\'). This works
/// correctly with bash, zsh, fish, and other common shells.
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
    // nvm, and rbenv are loaded before the user command is executed. These
    // hooks are normally only triggered before an interactive prompt.
    let pre_command_hook = match shell_name {
        "zsh" => "for f in \"${precmd_functions[@]}\"; do \"$f\"; done",
        "bash" => "eval \"${PROMPT_COMMAND:-}\"",
        "fish" => "functions -q fish_prompt; and emit fish_prompt",
        _ => "true", // No-op for other shells
    };

    // To run `user_command` and then `exec shell` inside a new shell instance,
    // we use the form: `$SHELL -ic '<hooks>; <user_command>; exec $SHELL -l'`.
    // We must escape single quotes within the user command using POSIX-style escaping.
    let escaped_command = command.replace('\'', r#"'\''"#);

    let inner_command = format!(
        "{pre_hook}; {user_cmd}; exec {shell} -l",
        pre_hook = pre_command_hook,
        user_cmd = escaped_command,
        shell = shell_path,
    );

    // The initial shell is interactive (-i) to ensure rc files (~/.bashrc,
    // ~/.zshrc) are sourced, which is where shell hooks are configured. It is
    // NOT a login shell (-l), as this can prevent rc files from sourcing in
    // bash. The final `exec $SHELL -l` ensures the user is left in a login
    // shell, matching tmux's default behavior.
    let full_command = format!(
        "{shell} -ic '{inner_command}'",
        shell = shell_path,
        inner_command = inner_command,
    );

    Ok(Some(full_command))
}

/// Split a pane with optional command
pub fn split_pane_with_command(
    prefix: &str,
    window_name: &str,
    pane_index: usize,
    direction: &SplitDirection,
    working_dir: &Path,
    command: Option<&str>,
) -> Result<()> {
    let split_arg = match direction {
        SplitDirection::Horizontal => "-h",
        SplitDirection::Vertical => "-v",
    };

    let prefixed_name = prefixed(prefix, window_name);
    let target = format!("={}.{}", prefixed_name, pane_index);
    let working_dir_str = working_dir
        .to_str()
        .ok_or_else(|| anyhow!("Working directory path contains non-UTF8 characters"))?;

    let cmd = Cmd::new("tmux").args(&[
        "split-window",
        split_arg,
        "-t",
        &target,
        "-c",
        working_dir_str,
    ]);

    let cmd = if let Some(cmd_str) = command {
        cmd.arg(cmd_str)
    } else {
        cmd
    };

    cmd.run().context("Failed to split pane")?;
    Ok(())
}

/// Respawn a pane with a new command
pub fn respawn_pane(
    prefix: &str,
    window_name: &str,
    pane_index: usize,
    working_dir: &Path,
    command: &str,
) -> Result<()> {
    let prefixed_name = prefixed(prefix, window_name);
    let target = format!("={}.{}", prefixed_name, pane_index);
    let working_dir_str = working_dir
        .to_str()
        .ok_or_else(|| anyhow!("Working directory path contains non-UTF8 characters"))?;

    Cmd::new("tmux")
        .args(&[
            "respawn-pane",
            "-t",
            &target,
            "-c",
            working_dir_str,
            "-k",
            command,
        ])
        .run()
        .context("Failed to respawn pane")?;

    Ok(())
}

/// Result of setting up panes
pub struct PaneSetupResult {
    /// The index of the pane that should receive focus.
    pub focus_pane_index: usize,
}

/// Setup panes in a window according to configuration
pub fn setup_panes(
    prefix: &str,
    window_name: &str,
    panes: &[PaneConfig],
    working_dir: &Path,
    prompt_file_path: Option<&Path>,
) -> Result<PaneSetupResult> {
    if panes.is_empty() {
        return Ok(PaneSetupResult {
            focus_pane_index: 0,
        });
    }

    let mut focus_pane_index: Option<usize> = None;

    // Handle the first pane (index 0), which already exists from window creation
    if let Some(pane_config) = panes.first() {
        let adjusted_command = pane_config
            .command
            .as_deref()
            .map(|cmd| adjust_command(cmd, prompt_file_path, working_dir));
        if let Some(cmd_str) = adjusted_command.as_ref().map(|c| c.as_ref())
            && let Some(startup_cmd) = build_startup_command(Some(cmd_str))?
        {
            respawn_pane(prefix, window_name, 0, working_dir, &startup_cmd)?;
        }
        if pane_config.focus {
            focus_pane_index = Some(0);
        }
    }

    let mut actual_pane_count = 1;

    // Create additional panes by splitting
    for (_i, pane_config) in panes.iter().enumerate().skip(1) {
        if let Some(ref direction) = pane_config.split {
            // Determine which pane to split
            let target_pane_to_split = pane_config.target.unwrap_or(actual_pane_count - 1);

            let adjusted_command = pane_config
                .command
                .as_deref()
                .map(|cmd| adjust_command(cmd, prompt_file_path, working_dir));
            let startup_cmd = build_startup_command(adjusted_command.as_ref().map(|c| c.as_ref()))?;

            split_pane_with_command(
                prefix,
                window_name,
                target_pane_to_split,
                direction,
                working_dir,
                startup_cmd.as_deref(),
            )?;

            let new_pane_index = actual_pane_count;

            if pane_config.focus {
                focus_pane_index = Some(new_pane_index);
            }
            actual_pane_count += 1;
        }
    }

    Ok(PaneSetupResult {
        focus_pane_index: focus_pane_index.unwrap_or(0),
    })
}

fn adjust_command<'a>(
    command: &'a str,
    prompt_file_path: Option<&Path>,
    working_dir: &Path,
) -> Cow<'a, str> {
    if let Some(prompt_path) = prompt_file_path
        && let Some(rewritten) = rewrite_agent_command(command, prompt_path, working_dir)
    {
        return Cow::Owned(rewritten);
    }
    Cow::Borrowed(command)
}

fn rewrite_agent_command(command: &str, prompt_file: &Path, working_dir: &Path) -> Option<String> {
    let trimmed = command.trim();
    if trimmed.is_empty() {
        return None;
    }

    let (first_token, rest) = split_first_token(trimmed)?;

    // Extract the agent name from the command (handles paths like /usr/local/bin/claude)
    let agent_name = std::path::Path::new(first_token)
        .file_name()
        .and_then(|s| s.to_str());

    let relative = prompt_file.strip_prefix(working_dir).unwrap_or(prompt_file);
    let prompt_path = relative.to_string_lossy();
    let rest = rest.trim_start();

    let rewritten = match agent_name {
        Some("claude") | Some("codex") => {
            // Use command substitution to pass prompt content directly
            let mut cmd = format!("{} \"$(cat {})\"", first_token, prompt_path);
            if !rest.is_empty() {
                cmd.push(' ');
                cmd.push_str(rest);
            }
            cmd
        }
        Some("gemini") => {
            // gemini needs -i flag for interactive mode after prompt
            let mut cmd = format!("{} -i \"$(cat {})\"", first_token, prompt_path);
            if !rest.is_empty() {
                cmd.push(' ');
                cmd.push_str(rest);
            }
            cmd
        }
        _ => return None,
    };

    Some(rewritten)
}

fn split_first_token(command: &str) -> Option<(&str, &str)> {
    let trimmed = command.trim_start();
    if trimmed.is_empty() {
        return None;
    }
    // `split_once` finds the first whitespace and splits there
    // If no whitespace found, returns None, so we treat whole string as first token
    Some(
        trimmed
            .split_once(char::is_whitespace)
            .unwrap_or((trimmed, "")),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_rewrite_claude_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("claude", &prompt_file, &working_dir);
        assert_eq!(result, Some("claude \"$(cat PROMPT.md)\"".to_string()));
    }

    #[test]
    fn test_rewrite_codex_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("codex", &prompt_file, &working_dir);
        assert_eq!(result, Some("codex \"$(cat PROMPT.md)\"".to_string()));
    }

    #[test]
    fn test_rewrite_gemini_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("gemini", &prompt_file, &working_dir);
        assert_eq!(result, Some("gemini -i \"$(cat PROMPT.md)\"".to_string()));
    }

    #[test]
    fn test_rewrite_command_with_path() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("/usr/local/bin/claude", &prompt_file, &working_dir);
        assert_eq!(
            result,
            Some("/usr/local/bin/claude \"$(cat PROMPT.md)\"".to_string())
        );
    }

    #[test]
    fn test_rewrite_command_with_args() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("claude --verbose", &prompt_file, &working_dir);
        assert_eq!(
            result,
            Some("claude \"$(cat PROMPT.md)\" --verbose".to_string())
        );
    }

    #[test]
    fn test_rewrite_unknown_agent() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("unknown-agent", &prompt_file, &working_dir);
        assert_eq!(result, None);
    }

    #[test]
    fn test_rewrite_empty_command() {
        let prompt_file = PathBuf::from("/tmp/worktree/PROMPT.md");
        let working_dir = PathBuf::from("/tmp/worktree");

        let result = rewrite_agent_command("", &prompt_file, &working_dir);
        assert_eq!(result, None);
    }

    #[test]
    fn test_split_first_token_single_word() {
        assert_eq!(split_first_token("claude"), Some(("claude", "")));
    }

    #[test]
    fn test_split_first_token_with_args() {
        assert_eq!(
            split_first_token("claude --verbose"),
            Some(("claude", "--verbose"))
        );
    }

    #[test]
    fn test_split_first_token_multiple_spaces() {
        assert_eq!(
            split_first_token("claude   --verbose"),
            Some(("claude", "  --verbose"))
        );
    }

    #[test]
    fn test_split_first_token_leading_whitespace() {
        assert_eq!(
            split_first_token("  claude --verbose"),
            Some(("claude", "--verbose"))
        );
    }

    #[test]
    fn test_split_first_token_empty_string() {
        assert_eq!(split_first_token(""), None);
    }

    #[test]
    fn test_split_first_token_only_whitespace() {
        assert_eq!(split_first_token("   "), None);
    }
}
