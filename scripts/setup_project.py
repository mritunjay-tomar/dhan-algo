#!/usr/bin/env python3
"""One-time local environment setup. No credentials or broker calls."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true", help="Only create the environment template")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info < (3, 12):
        parser.error("Python 3.12+ is required by pandas-ta.")
    if not (root / ".env").exists():
        shutil.copyfile(root / ".env.example", root / ".env")
    if not args.skip_install:
        venv.create(root / ".venv", with_pip=True)
        python = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements-dev.txt")], check=True)
    print("Setup complete. Fill .env, export its values, then run main.py with .venv's Python.")


if __name__ == "__main__":
    main()
