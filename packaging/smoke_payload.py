"""Smoke-test the packaged payload: extract app.tgz and actually start it."""
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPK = ROOT / "packaging" / "fnmusic-journal.fpk"
PORT = 5699

with tempfile.TemporaryDirectory(prefix="fpk-payload-") as scratch:
    with tarfile.open(FPK) as archive:
        nested = archive.extractfile("app.tgz").read()
    with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as inner:
        inner.extractall(scratch)
    server = Path(scratch) / "server"
    data = Path(scratch) / "var"
    data.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-m", "app.server", "--host", "127.0.0.1", "--port", str(PORT),
         "--data-dir", str(data), "--music-server", "http://127.0.0.1:1", "--poll-seconds", "5"],
        cwd=str(server), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 20
        payload = None
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as response:
                    payload = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.4)
        if payload is None:
            output = process.stdout.read() if process.poll() is not None else "(still starting)"
            raise SystemExit(f"packaged server did not answer health check\n{output}")
        print("health:", payload)
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        print("index served:", "音迹" in page, "bytes:", len(page))
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        print("app.js served:", len(script), "bytes; poller label present:", "轮询探测" in script)
        print("archive created:", (data / "journal.sqlite3").exists())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
print("packaged payload smoke test passed")
