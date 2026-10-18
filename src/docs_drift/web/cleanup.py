"""Remove only a server-owned workspace, including read-only Windows Git packfiles."""

import os
import shutil
from pathlib import Path
from typing import Any


def remove_workspace(root: Path, workspace: Path) -> None:
    root = root.resolve()
    if workspace.is_symlink() or workspace.parent.resolve() != root:
        raise ValueError("Refusing to remove a path outside the server workspace root.")

    def retry(function: Any, path: str, exception: Any) -> None:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(workspace.resolve()):
            raise OSError("Refusing to change permissions outside the workspace.")
        os.chmod(candidate, 0o700)
        function(path)

    if workspace.exists():
        # onerror works on Python 3.11 as well as newer supported versions.
        shutil.rmtree(workspace, onerror=retry)
