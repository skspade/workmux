"""Tests for pane-base-index configuration compatibility.

NOTE: These tests were specific to tmux pane indexing behavior.
Since workmux now uses zellij which doesn't have multi-pane support,
these tests are skipped.
"""

import pytest


pytestmark = pytest.mark.skip(reason="Zellij does not support multi-pane layouts like tmux")
