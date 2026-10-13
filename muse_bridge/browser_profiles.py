"""Isolated browser profiles for multi-account sessions.

Each account gets its own browser user-data directory. Launching the
browser with --user-data-dir=<profile> gives that account a completely
separate cookie jar / local storage — the supported, ordinary way to keep
two logins from seeing each other. The bridge never copies cookies,
tokens, or passwords between profiles.

The user signs in through the site's normal login flow inside the launched
window. The bridge cannot and does not detect the login itself; the user
marks the account signed in afterwards (honest by design).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path


MUSE_URL = "https://muse.ai"


def find_browser() -> str | None:
    """Locate Chrome or Edge. Returns the executable path or None."""
    candidates: list[str] = []
    if platform.system() == "Windows":
        pf = Path("C:/Program Files")
        pfx86 = Path("C:/Program Files (x86)")
        candidates = [
            str(pf / "Google/Chrome/Application/chrome.exe"),
            str(pfx86 / "Google/Chrome/Application/chrome.exe"),
            str(pf / "Microsoft/Edge/Application/msedge.exe"),
            str(pfx86 / "Microsoft/Edge/Application/msedge.exe"),
        ]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser",
                     "microsoft-edge"):
            p = shutil.which(name)
            if p:
                candidates.append(p)
    for c in candidates:
        if c and Path(c).exists():
            return c
    # Fall back to PATH lookup on Windows too.
    for name in ("chrome.exe", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def launch_profile(profile_dir: str, url: str = MUSE_URL) -> tuple[bool, str]:
    """Open the browser with an isolated profile at the given URL.

    Returns (ok, detail). The browser is left running for the user.
    """
    exe = find_browser()
    if not exe:
        return False, "No Chrome/Edge browser found on this machine."
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [exe, f"--user-data-dir={profile_dir}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        return True, f"Opened {url} in isolated profile."
    except Exception as e:
        return False, f"Could not launch browser: {e}"
