use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

use crate::git;

/// Configuration for file operations during worktree creation
#[derive(Debug, Deserialize, Serialize, Default, Clone)]
pub struct FileConfig {
    /// Glob patterns for files to copy from the repo root to the new worktree
    #[serde(default)]
    pub copy: Option<Vec<String>>,

    /// Glob patterns for files to symlink from the repo root into the new worktree
    #[serde(default)]
    pub symlink: Option<Vec<String>>,
}

/// Configuration for the workmux tool, read from .workmux.yaml
#[derive(Debug, Deserialize, Serialize, Default, Clone)]
pub struct Config {
    /// The primary branch to merge into (optional, auto-detected if not set)
    #[serde(default)]
    pub main_branch: Option<String>,

    /// Directory where worktrees should be created (optional, defaults to <project>__worktrees pattern)
    /// Can be relative to repo root or absolute path
    #[serde(default)]
    pub worktree_dir: Option<String>,

    /// Prefix for tmux window names (optional, defaults to "wm-")
    #[serde(default)]
    pub window_prefix: Option<String>,

    /// Tmux pane configuration
    #[serde(default)]
    pub panes: Option<Vec<PaneConfig>>,

    /// Commands to run after creating the worktree
    #[serde(default)]
    pub post_create: Option<Vec<String>>,

    /// File operations to perform after creating the worktree
    #[serde(default)]
    pub files: FileConfig,
}

/// Configuration for a single tmux pane
#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct PaneConfig {
    /// Command to run in this pane
    pub command: String,

    /// Whether this pane should receive focus after creation
    #[serde(default)]
    pub focus: bool,

    /// Split direction from the previous pane (horizontal or vertical)
    #[serde(default)]
    pub split: Option<SplitDirection>,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(rename_all = "lowercase")]
pub enum SplitDirection {
    Horizontal,
    Vertical,
}

impl Config {
    /// Load and merge global and project configurations.
    pub fn load() -> anyhow::Result<Self> {
        let global_config = Self::load_global()?.unwrap_or_default();
        let project_config = Self::load_project()?.unwrap_or_default();

        let mut config = global_config.merge(project_config);

        // After merging, apply sensible defaults for any values that are not configured.
        let needs_defaults = config.panes.is_none() || config.post_create.is_none();

        if needs_defaults {
            if let Ok(repo_root) = git::get_repo_root() {
                // Apply defaults that require inspecting the repository.

                // Default panes based on project type
                if config.panes.is_none() {
                    if repo_root.join("CLAUDE.md").exists() {
                        config.panes = Some(Self::claude_default_panes());
                    } else {
                        config.panes = Some(Self::default_panes());
                    }
                }

                // Default post_create hooks based on package manager
                if config.post_create.is_none() && repo_root.join("pnpm-lock.yaml").exists() {
                    config.post_create = Some(vec!["pnpm install".to_string()]);
                }
            } else {
                // Apply fallback defaults for when not in a git repo (e.g., `workmux init`).
                if config.panes.is_none() {
                    config.panes = Some(Self::default_panes());
                }
            }
        }

        Ok(config)
    }

    /// Load configuration from a specific path.
    fn load_from_path(path: &Path) -> anyhow::Result<Option<Self>> {
        if !path.exists() {
            return Ok(None);
        }
        let contents = fs::read_to_string(path)?;
        let config: Config = serde_yaml::from_str(&contents)
            .map_err(|e| anyhow::anyhow!("Failed to parse config at {}: {}", path.display(), e))?;
        Ok(Some(config))
    }

    /// Load the global configuration file from the XDG config directory.
    fn load_global() -> anyhow::Result<Option<Self>> {
        // Check ~/.config/workmux (XDG convention, works cross-platform)
        if let Some(home_dir) = home::home_dir() {
            let xdg_config_path = home_dir.join(".config/workmux/config.yaml");
            if xdg_config_path.exists() {
                return Self::load_from_path(&xdg_config_path);
            }
            let xdg_config_path_yml = home_dir.join(".config/workmux/config.yml");
            if xdg_config_path_yml.exists() {
                return Self::load_from_path(&xdg_config_path_yml);
            }
        }
        Ok(None)
    }

    /// Load the project-specific configuration file from the current directory.
    fn load_project() -> anyhow::Result<Option<Self>> {
        let config_path_yaml = Path::new(".workmux.yaml");
        if config_path_yaml.exists() {
            return Self::load_from_path(config_path_yaml);
        }
        let config_path_yml = Path::new(".workmux.yml");
        if config_path_yml.exists() {
            return Self::load_from_path(config_path_yml);
        }
        Ok(None)
    }

    /// Merge a project config into a global config.
    /// Project config takes precedence. For lists, "<global>" placeholder expands to global items.
    fn merge(self, project: Self) -> Self {
        // Helper to merge vectors with "<global>" placeholder expansion
        fn merge_vec_with_placeholder(
            global: Option<Vec<String>>,
            project: Option<Vec<String>>,
        ) -> Option<Vec<String>> {
            match (global, project) {
                (Some(global_items), Some(project_items)) => {
                    // Check if project items contain the "<global>" placeholder
                    let has_placeholder = project_items.iter().any(|s| s == "<global>");
                    if has_placeholder {
                        // Replace "<global>" with global items
                        let mut result = Vec::new();
                        for item in project_items {
                            if item == "<global>" {
                                result.extend(global_items.clone());
                            } else {
                                result.push(item);
                            }
                        }
                        Some(result)
                    } else {
                        // No placeholder, project completely replaces global
                        Some(project_items)
                    }
                }
                (global, project) => project.or(global),
            }
        }

        Self {
            // Scalar values: project wins
            main_branch: project.main_branch.or(self.main_branch),
            worktree_dir: project.worktree_dir.or(self.worktree_dir),
            window_prefix: project.window_prefix.or(self.window_prefix),

            // Panes: project replaces global (no placeholder support)
            panes: project.panes.or(self.panes),

            // List values with placeholder support
            post_create: merge_vec_with_placeholder(self.post_create, project.post_create),

            // File config with placeholder support
            files: FileConfig {
                copy: merge_vec_with_placeholder(self.files.copy, project.files.copy),
                symlink: merge_vec_with_placeholder(self.files.symlink, project.files.symlink),
            },
        }
    }

    /// Get default panes.
    fn default_panes() -> Vec<PaneConfig> {
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "bash".to_string());
        vec![
            PaneConfig {
                command: shell,
                focus: true,
                split: None,
            },
            PaneConfig {
                command: "clear".to_string(),
                focus: false,
                split: Some(SplitDirection::Horizontal),
            },
        ]
    }

    /// Get default panes for a Claude project.
    fn claude_default_panes() -> Vec<PaneConfig> {
        vec![
            PaneConfig {
                command: "claude".to_string(),
                focus: true,
                split: None,
            },
            PaneConfig {
                command: "clear".to_string(),
                focus: false,
                split: Some(SplitDirection::Horizontal),
            },
        ]
    }

    /// Get the window prefix to use, defaulting to "wm-" if not configured
    pub fn window_prefix(&self) -> &str {
        self.window_prefix.as_deref().unwrap_or("wm-")
    }

    /// Create an example .workmux.yaml configuration file
    pub fn init() -> anyhow::Result<()> {
        use std::path::PathBuf;

        let config_path = PathBuf::from(".workmux.yaml");

        if config_path.exists() {
            return Err(anyhow::anyhow!(
                ".workmux.yaml already exists. Remove it first if you want to regenerate it."
            ));
        }

        let example_config = r#"# workmux project configuration
# Overrides global settings in ~/.config/workmux/config.yaml

# The primary branch to merge into (auto-detected if not set)
# main_branch: main

# Custom worktree directory for this project
# worktree_dir: .worktrees

# Custom tmux window prefix for this project
# window_prefix: proj-

# Setup commands run after worktree creation
# Use "<global>" to inherit global hooks (e.g., mise install)
post_create:
  # - "<global>"     # Uncomment to run global hooks first
  - pnpm install

# Tmux pane layout for this project
# If not set, uses global panes from ~/.config/workmux/config.yaml
panes:
  - command: nvim .
    focus: true
  - command: pnpm run dev
    split: horizontal

# File operations
files:
  # Copy files that may differ per worktree
  copy:
    - .env.example

  # Symlink shared resources
  # Use "<global>" to inherit global patterns
  symlink:
    # - "<global>"   # Uncomment to include global symlinks
    - node_modules
"#;

        fs::write(&config_path, example_config)?;

        println!("✓ Created .workmux.yaml");
        println!("\nThis file provides project-specific overrides.");
        println!("For global settings, edit ~/.config/workmux/config.yaml");

        Ok(())
    }
}
