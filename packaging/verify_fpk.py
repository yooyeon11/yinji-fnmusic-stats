"""Verify the built FPK: structure, icons, and payload freshness.

Run after packaging/build.py:
    python packaging/verify_fpk.py
Exits 0 only when every required member exists and every payload file matches
the current source tree byte for byte.
"""
import hashlib
import io
import struct
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
FPK = PACKAGING / "fnmusic-journal.fpk"

OUTER_REQUIRED = ["manifest", "ICON.PNG", "ICON_256.PNG", "app.tgz"] + [
    f"cmd/{name}" for name in ("main", "install_init", "install_callback", "upgrade_init",
                               "upgrade_callback", "uninstall_init", "uninstall_callback",
                               "config_init", "config_callback")
] + ["config/privilege", "config/resource"]
INNER_REQUIRED = ["ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png",
                  "server/README.md", "server/app/server.py", "server/app/storage.py",
                  "server/app/connector.py", "server/app/poller.py",
                  "server/app/report_extras.py", "server/app/timezones.py",
                  "server/web/index.html", "server/web/app.css", "server/web/app.js",
                  "server/web/theme.js", "server/web/collector.js", "server/web/assets/icon.svg"]
FRESHNESS = [
    ("app/server.py", "server/app/server.py"),
    ("app/storage.py", "server/app/storage.py"),
    ("app/connector.py", "server/app/connector.py"),
    ("app/poller.py", "server/app/poller.py"),
    ("app/report_extras.py", "server/app/report_extras.py"),
    ("app/timezones.py", "server/app/timezones.py"),
    ("web/index.html", "server/web/index.html"),
    ("web/app.css", "server/web/app.css"),
    ("web/app.js", "server/web/app.js"),
    ("web/theme.js", "server/web/theme.js"),
    ("web/collector.js", "server/web/collector.js"),
    ("README.md", "server/README.md"),
]


def png_size(data):
    return struct.unpack(">II", data[16:24])


def main():
    if not FPK.exists():
        sys.exit("missing package: " + str(FPK))
    problems = []
    with tarfile.open(FPK) as archive:
        names = set(archive.getnames())
        problems += [f"missing outer member: {name}" for name in OUTER_REQUIRED if name not in names]
        for name, expected in (("ICON.PNG", (64, 64)), ("ICON_256.PNG", (256, 256))):
            if name in names:
                size = png_size(archive.extractfile(name).read())
                if size != expected:
                    problems.append(f"{name} is {size}, expected {expected}")
        nested = archive.extractfile("app.tgz").read() if "app.tgz" in names else b""
    if not nested:
        sys.exit("\n".join(problems + ["app.tgz missing or empty"]))
    with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as inner:
        inner_names = set(inner.getnames())
        problems += [f"missing payload member: {name}" for name in INNER_REQUIRED if name not in inner_names]
        for source, packed in FRESHNESS:
            if packed not in inner_names:
                continue
            left = hashlib.sha256((ROOT / source).read_bytes()).hexdigest()
            right = hashlib.sha256(inner.extractfile(packed).read()).hexdigest()
            if left != right:
                problems.append(f"payload differs from source: {source}")
    if problems:
        print("FPK verification failed:")
        for item in problems:
            print(" -", item)
        raise SystemExit(1)
    print(f"FPK verified: {FPK.name} ({FPK.stat().st_size} bytes), structure and payload match the source tree.")


if __name__ == "__main__":
    main()
