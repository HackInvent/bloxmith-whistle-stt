"""Download and verify Whistle assets into an explicitly chosen block storage directory.

This optional offline-preparation tool never changes a graph or application config.
Runtime inference uses the same pinned asset loader and downloads only on demand.
"""

import argparse
from pathlib import Path
import tempfile
from worker import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", required=True, type=Path)
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()
    if not 10 <= args.timeout_sec <= 600:
        parser.error("Timeout must be between 10 and 600 seconds")
    storage = args.storage.absolute()
    if storage.is_symlink():
        parser.error("Storage must not be a symbolic link")
    storage.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whistle-setup-", dir=storage) as temporary:
        weights, library = prepare(storage, Path(temporary), {"download_timeout_sec": args.timeout_sec, "download_missing": True})
        print("[ok] Pinned Whistle weights and engine verified in " + str(weights.parent))


if __name__ == "__main__":
    main()
