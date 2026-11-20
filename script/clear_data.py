"""Preview or clear the repository's data directory after stopping all writers."""

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from app.services.shared.directory_cleanup import remove_directory_tree  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--yes", action="store_true", help="permanently delete the listed contents")
    parser.add_argument("--offline", action="store_true", help="confirm the backend and workers are stopped")
    args = parser.parse_args()
    if args.yes and not args.offline:
        parser.error("Stop the backend and workers first, then use --yes --offline.")

    root = PROJECT_DIR / "data"
    if root.resolve(strict=True) != root:
        parser.error(f"Refusing to clear a redirected data directory: {root}")

    entries = sorted(root.iterdir())
    print(f"{root} ({len(entries)} top-level entries)")
    for entry in entries:
        print(f"  {entry.name}")
    if not args.yes:
        print("Preview only. Nothing deleted.")
        return

    for entry in entries:
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_junction():
            entry.rmdir()
        elif entry.is_dir():
            remove_directory_tree(entry)
        else:
            entry.unlink()
    print(f"Cleared {root}")


if __name__ == "__main__":
    main()
