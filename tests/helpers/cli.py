"""Run the areaDef CLI in a subprocess and capture its result."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: Wall-clock ceiling for a single CLI invocation (global constraint).
CLI_TIMEOUT_S = 90


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI invocation."""

    code: int
    stdout: str
    stderr: str

    def ok(self) -> bool:
        return self.code == 0

    def json(self) -> Any:
        """Parse stdout as JSON (the CLI prints JSON when ``-o`` is omitted)."""
        return json.loads(self.stdout)

    def __str__(self) -> str:  # shows up verbatim in assertion failures
        return (f"exit={self.code}\n--- stdout ---\n{self.stdout}"
                f"\n--- stderr ---\n{self.stderr}")


class CliRunner:
    """Invokes ``python areaDef.py ...`` with a headless Qt platform."""

    def __init__(self, script: os.PathLike | str, cwd: Optional[os.PathLike | str] = None) -> None:
        self.script = Path(script)
        self.cwd = Path(cwd) if cwd is not None else None

    def run(self, *args: Any, timeout: float = CLI_TIMEOUT_S) -> CliResult:
        proc = subprocess.run(
            [sys.executable, str(self.script), *[str(a) for a in args]],
            capture_output=True,
            text=True,
            # Decode explicitly rather than by the ambient locale: the CLI
            # writes UTF-8 (shape names, paths), and a locale that says
            # otherwise -- a bare LANG=C on a CI image, cp1252 on Windows --
            # would turn a passing assertion into a UnicodeDecodeError from the
            # helper. errors="replace" keeps a bad byte visible in the captured
            # output instead of losing the whole result to an exception.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(self.cwd) if self.cwd else None,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        return CliResult(proc.returncode, proc.stdout, proc.stderr)
