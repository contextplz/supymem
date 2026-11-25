"""Background-first browser backend for the computer-use engine (CDP, DOM-indexed).

DOM indexing follows browser-use's approach (MIT, github.com/browser-use/browser-use):
visible interactive elements get a numeric index the planner refers to.
"""
from .backend import ACTIONS, BrowserBackend, Observation, SafetyRefusal, is_browser_step
from .chrome import (ChromeSession, find_profile_dir, persistent_profile_dir, prepare_profile_copy,
                     release_stale_singleton)

__all__ = ["ACTIONS", "BrowserBackend", "ChromeSession", "Observation", "SafetyRefusal",
           "find_profile_dir", "is_browser_step", "persistent_profile_dir", "prepare_profile_copy",
           "release_stale_singleton"]
