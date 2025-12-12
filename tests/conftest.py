import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

from dataclasses import dataclass

import pytest
import yaml


class ZellijEnvironment:
    """
    A helper class to manage the state of an isolated test environment.
    It simulates a zellij session by setting environment variables and
    creating a fake zellij command that tracks state.
    """

    def __init__(self, tmp_path: Path):
        # The base directory for all temporary test files
        self.tmp_path = tmp_path

        # Create a dedicated home directory for the test to prevent
        # loading the user's real shell configuration (.zshrc, .bash_history, etc.)
        self.home_path = self.tmp_path / "test_home"
        self.home_path.mkdir()

        # State file for tracking zellij tabs
        self.state_file = self.tmp_path / "zellij_state.json"
        self._init_state()

        # Create a copy of the current environment variables
        self.env = os.environ.copy()

        # Simulate being inside a zellij session
        self.env["ZELLIJ"] = "1"
        self.env["ZELLIJ_TAB_NAME"] = "test"

        # Isolate the shell environment completely to prevent history pollution
        # and other side effects from user's shell configuration
        self.env["HOME"] = str(self.home_path)

        # Create bin directory and fake zellij command
        self._create_fake_zellij()

        # Create a fake git editor for non-interactive commits
        # Git needs the commit message file to be modified, so we ensure it has content
        fake_editor_script = self.home_path / "fake_git_editor.sh"
        fake_editor_script.write_text(
            "#!/bin/sh\n"
            "# If the file is empty or only has comments, add a default message\n"
            'if ! grep -q "^[^#]" "$1" 2>/dev/null; then\n'
            '  echo "Test commit" > "$1"\n'
            "fi\n"
        )
        fake_editor_script.chmod(0o755)
        self.env["GIT_EDITOR"] = str(fake_editor_script)

    def _init_state(self):
        """Initialize the zellij state file with default session."""
        state = {"tabs": ["test"], "current_tab": "test"}
        self.state_file.write_text(json.dumps(state))

    def _create_fake_zellij(self):
        """Create a fake zellij command that tracks state in files."""
        bin_dir = self.home_path / "bin"
        bin_dir.mkdir(exist_ok=True)

        fake_zellij = bin_dir / "zellij"
        state_file_path = str(self.state_file)

        # Create a shell script that simulates zellij behavior
        script = f"""#!/bin/sh
STATE_FILE="{state_file_path}"

# Helper to read JSON state
read_state() {{
    cat "$STATE_FILE"
}}

# Helper to write JSON state
write_state() {{
    echo "$1" > "$STATE_FILE"
}}

# Handle different zellij commands
if [ "$1" = "action" ]; then
    case "$2" in
        "query-tab-names")
            # Return list of tab names, one per line
            python3 -c "import json; state = json.load(open('$STATE_FILE')); print('\\n'.join(state.get('tabs', [])))"
            exit 0
            ;;
        "new-tab")
            # Parse --name argument
            shift 2
            NAME=""
            CWD=""
            while [ $# -gt 0 ]; do
                case "$1" in
                    --name) NAME="$2"; shift 2 ;;
                    --cwd) CWD="$2"; shift 2 ;;
                    *) shift ;;
                esac
            done
            if [ -n "$NAME" ]; then
                python3 -c "
import json
state = json.load(open('$STATE_FILE'))
if '$NAME' not in state.get('tabs', []):
    state.setdefault('tabs', []).append('$NAME')
state['current_tab'] = '$NAME'
json.dump(state, open('$STATE_FILE', 'w'))
"
            fi
            exit 0
            ;;
        "go-to-tab-name")
            NAME="$3"
            python3 -c "
import json
state = json.load(open('$STATE_FILE'))
if '$NAME' in state.get('tabs', []):
    state['current_tab'] = '$NAME'
    json.dump(state, open('$STATE_FILE', 'w'))
    exit(0)
else:
    exit(1)
" || exit 1
            exit 0
            ;;
        "close-tab")
            # Close current tab
            python3 -c "
import json
state = json.load(open('$STATE_FILE'))
current = state.get('current_tab', '')
tabs = state.get('tabs', [])
if current in tabs:
    tabs.remove(current)
    state['tabs'] = tabs
    state['current_tab'] = tabs[0] if tabs else ''
json.dump(state, open('$STATE_FILE', 'w'))
"
            exit 0
            ;;
        "new-pane")
            # Simulates creating a new pane and running a command
            # In our simplified model, we just ignore this
            exit 0
            ;;
        *)
            echo "Unknown zellij action: $2" >&2
            exit 1
            ;;
    esac
fi

echo "Unknown zellij command: $@" >&2
exit 1
"""
        fake_zellij.write_text(script)
        fake_zellij.chmod(0o755)

        # Add bin directory to PATH
        self.env["PATH"] = f"{bin_dir}:{self.env.get('PATH', '')}"

    def get_tabs(self) -> List[str]:
        """Get the current list of zellij tabs."""
        state = json.loads(self.state_file.read_text())
        return state.get("tabs", [])

    def get_current_tab(self) -> str:
        """Get the current zellij tab name."""
        state = json.loads(self.state_file.read_text())
        return state.get("current_tab", "")

    def set_current_tab(self, tab_name: str):
        """Set the current tab (simulates being in that tab)."""
        self.env["ZELLIJ_TAB_NAME"] = tab_name
        state = json.loads(self.state_file.read_text())
        state["current_tab"] = tab_name
        self.state_file.write_text(json.dumps(state))

    def close_tab(self, tab_name: str):
        """Close a specific tab by name (simulates closing a zellij tab)."""
        state = json.loads(self.state_file.read_text())
        tabs = state.get("tabs", [])
        if tab_name in tabs:
            tabs.remove(tab_name)
            state["tabs"] = tabs
            # If closing the current tab, switch to another
            if state.get("current_tab") == tab_name:
                state["current_tab"] = tabs[0] if tabs else ""
            self.state_file.write_text(json.dumps(state))

    def tab_exists(self, tab_name: str) -> bool:
        """Check if a tab with the given name exists."""
        return tab_name in self.get_tabs()

    def run_command(
        self, cmd: list[str], check: bool = True, cwd: Optional[Path] = None
    ):
        """Runs a generic command within the isolated environment."""
        working_dir = cwd if cwd is not None else self.tmp_path
        return subprocess.run(
            cmd,
            cwd=working_dir,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )


# Alias for backward compatibility
TmuxEnvironment = ZellijEnvironment


@pytest.fixture
def isolated_zellij_env(tmp_path: Path) -> Generator[ZellijEnvironment, None, None]:
    """
    A pytest fixture that provides a fully isolated zellij environment for a single test.

    It performs the following steps:
    1. Creates a ZellijEnvironment instance.
    2. Yields the environment manager to the test function.
    3. After the test runs, cleanup is automatic via tmp_path.
    """
    test_env = ZellijEnvironment(tmp_path)
    yield test_env


# Alias for backward compatibility with existing tests
@pytest.fixture
def isolated_tmux_server(tmp_path: Path) -> Generator[ZellijEnvironment, None, None]:
    """Alias for isolated_zellij_env for backward compatibility."""
    test_env = ZellijEnvironment(tmp_path)
    yield test_env


def setup_git_repo(path: Path, env_vars: Optional[dict] = None):
    """Initializes a git repository in the given path with an initial commit."""
    subprocess.run(
        ["git", "init"], cwd=path, check=True, capture_output=True, env=env_vars
    )
    # Configure git user for commits
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env_vars,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env_vars,
    )
    # Ignore test_home directory, test output files, and zellij state to prevent uncommitted changes
    gitignore_path = path / ".gitignore"
    gitignore_path.write_text(
        "test_home/\nworkmux_*.txt\nzellij_state.json\n"  # Test helper output files
    )
    subprocess.run(
        ["git", "add", ".gitignore"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env_vars,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env_vars,
    )


@pytest.fixture
def repo_path(isolated_tmux_server: "ZellijEnvironment") -> Path:
    """Initializes a git repo in the test env and returns its path."""
    path = isolated_tmux_server.tmp_path
    setup_git_repo(path, isolated_tmux_server.env)
    return path


@pytest.fixture
def remote_repo_path(isolated_tmux_server: "ZellijEnvironment") -> Path:
    """Creates a bare git repo to act as a remote."""
    parent = isolated_tmux_server.tmp_path.parent
    remote_path = Path(tempfile.mkdtemp(prefix="remote_repo_", dir=parent))
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=remote_path,
        check=True,
        capture_output=True,
    )
    return remote_path


def poll_until(
    condition: Callable[[], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> bool:
    """
    Poll until a condition is met or timeout is reached.

    Args:
        condition: A callable that returns True when the condition is met
        timeout: Maximum time to wait in seconds
        poll_interval: Time to wait between checks in seconds

    Returns:
        True if condition was met, False if timeout was reached
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition():
            return True
        time.sleep(poll_interval)
    return False


@dataclass
class WorkmuxCommandResult:
    """Represents the result of running a workmux command."""

    exit_code: int
    stdout: str
    stderr: str


@pytest.fixture(scope="session")
def workmux_exe_path() -> Path:
    """
    Returns the path to the local workmux build for testing.
    """
    local_path = Path(__file__).parent.parent / "target/debug/workmux"
    if not local_path.exists():
        pytest.fail("Could not find workmux executable. Run 'cargo build' first.")
    return local_path


def write_workmux_config(
    repo_path: Path,
    panes: Optional[List[Dict[str, Any]]] = None,
    post_create: Optional[List[str]] = None,
    files: Optional[Dict[str, List[str]]] = None,
    env: Optional[ZellijEnvironment] = None,
    window_prefix: Optional[str] = None,
    agent: Optional[str] = None,
):
    """Creates a .workmux.yaml file from structured data and optionally commits it."""
    config: Dict[str, Any] = {}
    if panes is not None:
        config["panes"] = panes
    if post_create:
        config["post_create"] = post_create
    if files:
        config["files"] = files
    if window_prefix:
        config["window_prefix"] = window_prefix
    if agent:
        config["agent"] = agent
    (repo_path / ".workmux.yaml").write_text(yaml.dump(config))

    # If env is provided, commit the config file to avoid uncommitted changes in merge tests
    if env:
        subprocess.run(
            ["git", "add", ".workmux.yaml"], cwd=repo_path, check=True, env=env.env
        )
        subprocess.run(
            ["git", "commit", "-m", "Add workmux config"],
            cwd=repo_path,
            check=True,
            env=env.env,
        )


def write_global_workmux_config(
    env: ZellijEnvironment,
    panes: Optional[List[Dict[str, Any]]] = None,
    post_create: Optional[List[str]] = None,
    files: Optional[Dict[str, List[str]]] = None,
    window_prefix: Optional[str] = None,
) -> Path:
    """Creates the global ~/.config/workmux/config.yaml file within the isolated HOME."""
    config: Dict[str, Any] = {}
    if panes is not None:
        config["panes"] = panes
    if post_create is not None:
        config["post_create"] = post_create
    if files is not None:
        config["files"] = files
    if window_prefix is not None:
        config["window_prefix"] = window_prefix

    config_dir = env.home_path / ".config" / "workmux"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.dump(config))
    return config_path


def get_worktree_path(repo_path: Path, branch_name: str) -> Path:
    """Returns the expected path for a worktree directory."""
    return repo_path.parent / f"{repo_path.name}__worktrees" / branch_name


def get_tab_name(branch_name: str) -> str:
    """Returns the expected zellij tab name for a worktree."""
    return f"wm-{branch_name}"


# Alias for backward compatibility
def get_window_name(branch_name: str) -> str:
    """Returns the expected zellij tab name for a worktree (alias for get_tab_name)."""
    return get_tab_name(branch_name)


def run_workmux_command(
    env: ZellijEnvironment,
    workmux_exe_path: Path,
    repo_path: Path,
    command: str,
    pre_run_cmds: Optional[List[List[str]]] = None,
    expect_fail: bool = False,
    working_dir: Optional[Path] = None,
) -> WorkmuxCommandResult:
    """
    Helper to run a workmux command within the isolated environment.

    Allows tests to optionally expect failure while still capturing stdout/stderr.

    Args:
        env: The isolated zellij environment
        workmux_exe_path: Path to the workmux executable
        repo_path: Path to the git repository
        command: The workmux command to run (e.g., "add feature-branch")
        pre_run_cmds: Optional list of commands to run before the workmux command
        expect_fail: Whether the command is expected to fail (non-zero exit)
        working_dir: Optional directory to run the command from (defaults to repo_path)
    """
    if pre_run_cmds:
        for cmd_args in pre_run_cmds:
            env.run_command(cmd_args)

    workdir = working_dir if working_dir is not None else repo_path

    # Run workmux command directly
    full_cmd = [str(workmux_exe_path)] + command.split()
    result = subprocess.run(
        full_cmd,
        cwd=workdir,
        env=env.env,
        capture_output=True,
        text=True,
    )

    cmd_result = WorkmuxCommandResult(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )

    if expect_fail:
        if cmd_result.exit_code == 0:
            raise AssertionError(
                f"workmux {command} was expected to fail but succeeded.\nStdout:\n{cmd_result.stdout}"
            )
    else:
        if cmd_result.exit_code != 0:
            raise AssertionError(
                f"workmux {command} failed with exit code {cmd_result.exit_code}\n{cmd_result.stderr}"
            )

    return cmd_result


def run_workmux_add(
    env: ZellijEnvironment,
    workmux_exe_path: Path,
    repo_path: Path,
    branch_name: str,
    pre_run_cmds: Optional[List[List[str]]] = None,
    *,
    base: Optional[str] = None,
    background: bool = False,
) -> None:
    """
    Helper to run `workmux add` command within the isolated environment.

    Asserts that the command completes successfully.

    Args:
        env: The isolated zellij environment
        workmux_exe_path: Path to the workmux executable
        repo_path: Path to the git repository
        branch_name: Name of the branch/worktree to create
        pre_run_cmds: Optional list of commands to run before workmux add
        base: Optional base branch for the new worktree (passed as `--base`)
        background: If True, pass `--background` so the tab is created without focus
    """
    args = ["add", branch_name]
    if base:
        args.extend(["--base", base])
    if background:
        args.append("--background")

    command = " ".join(args)

    run_workmux_command(
        env,
        workmux_exe_path,
        repo_path,
        command,
        pre_run_cmds=pre_run_cmds,
    )


def run_workmux_open(
    env: ZellijEnvironment,
    workmux_exe_path: Path,
    repo_path: Path,
    branch_name: str,
    *,
    run_hooks: bool = False,
    force_files: bool = False,
    pre_run_cmds: Optional[List[List[str]]] = None,
    expect_fail: bool = False,
) -> WorkmuxCommandResult:
    """
    Helper to run `workmux open` command within the isolated environment.

    Returns the command result so tests can assert on stdout/stderr.
    """
    flags: List[str] = []
    if run_hooks:
        flags.append("--run-hooks")
    if force_files:
        flags.append("--force-files")

    flag_str = f" {' '.join(flags)}" if flags else ""
    return run_workmux_command(
        env,
        workmux_exe_path,
        repo_path,
        f"open {branch_name}{flag_str}",
        pre_run_cmds=pre_run_cmds,
        expect_fail=expect_fail,
    )


def create_commit(env: ZellijEnvironment, path: Path, message: str):
    """Creates and commits a file within the test env at a specific path."""
    (path / f"file_for_{message.replace(' ', '_').replace(':', '')}.txt").write_text(
        f"content for {message}"
    )
    env.run_command(["git", "add", "."], cwd=path)
    env.run_command(["git", "commit", "-m", message], cwd=path)


def create_dirty_file(path: Path, filename: str = "dirty.txt"):
    """Creates an uncommitted file."""
    (path / filename).write_text("uncommitted changes")


def run_workmux_remove(
    env: ZellijEnvironment,
    workmux_exe_path: Path,
    repo_path: Path,
    branch_name: Optional[str] = None,
    force: bool = False,
    keep_branch: bool = False,
    user_input: Optional[str] = None,
    expect_fail: bool = False,
    from_tab: Optional[str] = None,
) -> None:
    """
    Helper to run `workmux remove` command within the isolated environment.

    Asserts that the command completes successfully unless expect_fail is True.

    Args:
        env: The isolated zellij environment
        workmux_exe_path: Path to the workmux executable
        repo_path: Path to the git repository
        branch_name: Optional name of the branch/worktree to remove (omit to auto-detect from current branch)
        force: Whether to use -f flag to skip confirmation
        keep_branch: Whether to use --keep-branch flag to keep the local branch
        user_input: Optional string to pipe to stdin (e.g., 'y' for confirmation)
        expect_fail: If True, asserts the command fails (non-zero exit code)
        from_tab: Optional tab name to run the command from (useful for testing remove from within worktree tab)
    """
    args = ["remove"]
    if force:
        args.append("-f")
    if keep_branch:
        args.append("--keep-branch")
    if branch_name:
        args.append(branch_name)

    command = " ".join(args)

    # Determine working directory
    if from_tab:
        from_branch = from_tab.replace("wm-", "")
        workdir = get_worktree_path(repo_path, from_branch)
        # Set the current tab to simulate being in that tab
        env.set_current_tab(from_tab)
    else:
        workdir = repo_path

    # Run the command
    full_cmd = [str(workmux_exe_path)] + args
    if user_input:
        result = subprocess.run(
            full_cmd,
            cwd=workdir,
            env=env.env,
            capture_output=True,
            text=True,
            input=user_input,
        )
    else:
        result = subprocess.run(
            full_cmd,
            cwd=workdir,
            env=env.env,
            capture_output=True,
            text=True,
        )

    if expect_fail:
        if result.returncode == 0:
            raise AssertionError(
                f"workmux remove was expected to fail but succeeded.\nStderr:\n{result.stderr}"
            )
    else:
        if result.returncode != 0:
            raise AssertionError(
                f"workmux remove failed with exit code {result.returncode}\nStderr:\n{result.stderr}"
            )

    # When running from within a tab, workmux schedules an async tab close via nohup.
    # We need to wait for it to complete and manually close the tab in our test state
    # since the nohup process runs asynchronously.
    if from_tab and not expect_fail:
        # Wait for the async close (workmux uses a 1-second delay)
        time.sleep(1.5)
        # Manually close the tab in test state (the async nohup may not update our state)
        env.close_tab(from_tab)


def run_workmux_merge(
    env: ZellijEnvironment,
    workmux_exe_path: Path,
    repo_path: Path,
    branch_name: Optional[str] = None,
    ignore_uncommitted: bool = False,
    delete_remote: bool = False,
    rebase: bool = False,
    squash: bool = False,
    keep: bool = False,
    expect_fail: bool = False,
    from_tab: Optional[str] = None,
) -> None:
    """
    Helper to run `workmux merge` command within the isolated environment.

    Asserts that the command completes successfully unless expect_fail is True.

    Args:
        env: The isolated zellij environment
        workmux_exe_path: Path to the workmux executable
        repo_path: Path to the git repository
        branch_name: Optional name of the branch to merge (omit to auto-detect from current branch)
        ignore_uncommitted: Whether to use --ignore-uncommitted flag
        delete_remote: Whether to use --delete-remote flag
        rebase: Whether to use --rebase flag
        squash: Whether to use --squash flag
        keep: Whether to use --keep flag
        expect_fail: If True, asserts the command fails (non-zero exit code)
        from_tab: Optional tab name to run the command from
    """
    args = ["merge"]
    if ignore_uncommitted:
        args.append("--ignore-uncommitted")
    if delete_remote:
        args.append("--delete-remote")
    if rebase:
        args.append("--rebase")
    if squash:
        args.append("--squash")
    if keep:
        args.append("--keep")
    if branch_name:
        args.append(branch_name)

    # Determine working directory
    if from_tab:
        from_branch = from_tab.replace("wm-", "")
        workdir = get_worktree_path(repo_path, from_branch)
        # Set the current tab to simulate being in that tab
        env.set_current_tab(from_tab)
    else:
        workdir = repo_path

    # Run the command
    full_cmd = [str(workmux_exe_path)] + args
    result = subprocess.run(
        full_cmd,
        cwd=workdir,
        env=env.env,
        capture_output=True,
        text=True,
    )

    if expect_fail:
        if result.returncode == 0:
            raise AssertionError(
                f"workmux merge was expected to fail but succeeded.\nStderr:\n{result.stderr}"
            )
    else:
        if result.returncode != 0:
            raise AssertionError(
                f"workmux merge failed with exit code {result.returncode}\nStderr:\n{result.stderr}"
            )

    # When running from within a tab, workmux schedules an async tab close via nohup.
    # We need to wait for it to complete and manually close the tab in our test state
    # since the nohup process runs asynchronously.
    if from_tab and not expect_fail and not keep:
        # Wait for the async close (workmux uses a 1-second delay)
        time.sleep(1.5)
        # Manually close the tab in test state (the async nohup may not update our state)
        env.close_tab(from_tab)


def install_fake_gh_cli(
    env: ZellijEnvironment,
    pr_number: int,
    json_response: Optional[Dict[str, Any]] = None,
    stderr: str = "",
    exit_code: int = 0,
):
    """
    Creates a fake 'gh' command that responds to 'pr view <number> --json' with controlled output.

    Args:
        env: The isolated zellij environment
        pr_number: The PR number to respond to
        json_response: Dict containing the PR data to return as JSON (or None to return error)
        stderr: Error message to output to stderr
        exit_code: Exit code for the fake gh command (0 for success, non-zero for error)
    """
    # Create a bin directory in the test home
    bin_dir = env.home_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    # Create the fake gh script
    gh_script = bin_dir / "gh"

    # Build the script content
    json_output = json.dumps(json_response) if json_response else ""

    # Escape single quotes in JSON for shell script
    json_output_escaped = json_output.replace("'", "'\\''")

    script_content = f"""#!/bin/sh
# Fake gh CLI for testing

# Check if this is a 'pr view' command for our PR number
# The command will be: gh pr view {pr_number} --json fields...
if [ "$1" = "pr" ] && [ "$2" = "view" ] && [ "$3" = "{pr_number}" ]; then
    if [ {exit_code} -ne 0 ]; then
        echo "{stderr}" >&2
        exit {exit_code}
    fi
    echo '{json_output_escaped}'
    exit 0
fi

# For any other command, fail
echo "gh: command not implemented in fake" >&2
exit 1
"""

    gh_script.write_text(script_content)
    gh_script.chmod(0o755)

    # The bin directory is already in PATH from _create_fake_zellij
