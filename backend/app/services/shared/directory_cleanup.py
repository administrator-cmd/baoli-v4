import os
import shutil
from pathlib import Path


def cleanup_path(path: Path) -> Path:
    """Use an extended Windows path for I/O on an already resolved cleanup root."""
    name = os.fspath(path)
    if os.name != "nt" or name.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name)


def remove_directory_tree(path: Path) -> None:
    shutil.rmtree(cleanup_path(path))
