from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    """Resolved paths shared by the GUI, CLI and daemon."""

    data_dir: Path
    runtime_dir: Path


def _default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "M7AccountManager"
    state_home = os.environ.get("XDG_STATE_HOME")
    return Path(state_home) / "m7manager" if state_home else Path.home() / ".local" / "state" / "m7manager"


def _default_runtime_dir(data_dir: Path) -> Path:
    if os.name == "nt":
        return data_dir / "run"
    runtime_home = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime_home) / "m7manager" if runtime_home else Path.home() / ".cache" / "m7manager" / "run"


def resolve_paths(data_dir: Path | str | None = None, runtime_dir: Path | str | None = None) -> AppPaths:
    """Resolve CLI > environment > platform defaults without creating directories."""

    data = Path(data_dir or os.environ.get("M7_DATA_DIR") or _default_data_dir()).expanduser().resolve()
    runtime = Path(runtime_dir or os.environ.get("M7_RUNTIME_DIR") or _default_runtime_dir(data)).expanduser().resolve()
    return AppPaths(data, runtime)


def is_server_platform() -> bool:
    return sys.platform != "win32"
