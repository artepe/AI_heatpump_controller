"""Build release-assets/mango_cop_ai_<version>.zip for manual installation.

The zip unpacks into the Home Assistant config folder as custom_components/mango_cop_ai/, with the
Finnish guide and both dashboard cards inside the integration folder.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "mango_cop_ai"
EXTRAS = {ROOT / "README_FI.md": "README_FI.md", ROOT / "examples" / "kortti.yaml": "kortti.yaml",
          ROOT / "examples" / "kortti_tekninen.yaml": "kortti_tekninen.yaml"}


def main() -> None:
    version = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))["version"]
    out_dir = ROOT / "release-assets"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"mango_cop_ai_{version}.zip"
    files = sorted(p for p in SOURCE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, Path("custom_components") / "mango_cop_ai" / path.relative_to(SOURCE))
        for path, name in EXTRAS.items():
            zf.write(path, Path("custom_components") / "mango_cop_ai" / name)
    print(f"{target.relative_to(ROOT)}: {len(files) + len(EXTRAS)} files")


if __name__ == "__main__":
    main()
