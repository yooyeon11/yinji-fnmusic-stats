"""Build the fnOS FPK package with the official fnpack tool (Windows-friendly).

Copies the application payload into packaging/fnmusic-journal/app/server,
generates the icons, then runs fnpack build. Standard library only.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
PACKAGE = PACKAGING / "fnmusic-journal"
FNPACK = PACKAGING / "tools" / "fnpack-1.2.3-windows-amd64.exe"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def build_payload():
    target = PACKAGE / "app" / "server"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copytree(ROOT / "app", target / "app", ignore=IGNORE)
    shutil.copytree(ROOT / "web", target / "web", ignore=IGNORE)
    shutil.copy2(ROOT / "README.md", target / "README.md")
    print("payload:", target)


def main():
    if not FNPACK.exists():
        sys.exit("fnpack executable missing: " + str(FNPACK))
    build_payload()
    subprocess.run([sys.executable, str(PACKAGING / "tools" / "make_icons.py")], check=True)
    result = subprocess.run([str(FNPACK), "build", "-d", str(PACKAGE)], cwd=str(PACKAGING))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
