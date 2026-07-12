from __future__ import annotations

import os
import shutil
import sys


def self_invocation() -> tuple[str, str]:
    """Return ``(interpreter, script)`` used to re-run codelight.

    This is the single source of truth for "how codelight invokes itself" —
    consumed by the systemd unit's ``ExecStart`` and by the command embedded in
    every installed agent hook. Centralising it keeps those two in sync and
    gives packaging one place to change:

    - The script path is derived from *this package's* location, so it is
      correct regardless of the working directory or how the entry script was
      launched (direct path, symlink, wrapper, ``python -m``).
    - The interpreter is the Python now running the daemon, so hooks and the
      service use the same one that installed them.

    A future pip/pipx console entry point (``codelight`` on PATH) would extend
    this to return that instead — tracked in PLAN.md under
    "Packaging & distribution".
    """
    interpreter = sys.executable or shutil.which("python3") or "python3"
    # invocation.py lives at <companion>/codelight_core/invocation.py, so the
    # entry script is two directories up.
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "codelight.py",
    )
    return interpreter, script
