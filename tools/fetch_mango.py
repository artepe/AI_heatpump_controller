"""Fetch Mango Tuya Local for the end-to-end tests.

The tests write through the real Mango Tuya Local integration over its simulated Tuya transport,
so the integration must sit next to this one in custom_components/. It is not part of this
repository (HACS installs one integration per repository); this script copies it from
artepe/chinaheatpump-tuya-local-home-assistant at a pinned commit. The copy is git-ignored.

    python tools/fetch_mango.py            # pinned commit
    python tools/fetch_mango.py --ref main # another branch, tag or commit
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = "https://github.com/artepe/chinaheatpump-tuya-local-home-assistant"
PINNED = "4a357303aaaa3a33e0a6e97e7ec64384950956af"  # Mango Tuya Local v0.2.1
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "custom_components" / "mango_tuya_local"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default=PINNED, help="branch, tag or commit (default: pinned v0.2.1)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        subprocess.run(["git", "-C", tmp, "fetch", "-q", "--depth", "1", REPO, args.ref], check=True)
        subprocess.run(["git", "-C", tmp, "checkout", "-q", "FETCH_HEAD"], check=True)
        source = Path(tmp) / "custom_components" / "mango_tuya_local"
        if TARGET.is_symlink() or TARGET.is_file():
            TARGET.unlink()
        elif TARGET.exists():
            shutil.rmtree(TARGET)
        shutil.copytree(source, TARGET, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"Mango Tuya Local {args.ref[:12]} -> {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
