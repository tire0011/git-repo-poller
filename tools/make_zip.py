"""Build the release ZIP: dist/git-repo-poller-<version>.zip (standard library only).

The ZIP is a source distribution: `pip install git-repo-poller-<version>.zip`
works, and unzipping it gives a runnable tree (`python -m git_repo_poller` or
`poller.ps1`). Built from a Git checkout or from an unzipped ZIP alike.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_repo_poller import __version__  # noqa: E402

FILES = ("pyproject.toml", "README.md", "LICENSE", "poller.ps1")
DIRS = ("git_repo_poller", "examples")


def zip_name() -> str:
    return f"git-repo-poller-{__version__}.zip"


def build(out_dir: Path = ROOT / "dist") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / zip_name()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in FILES:
            zf.write(ROOT / rel, rel)
        for name in DIRS:
            for f in sorted((ROOT / name).rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    zf.write(f, f.relative_to(ROOT).as_posix())
    return out


if __name__ == "__main__":
    print(build())
