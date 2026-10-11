"""Inspect only JavaScript assets explicitly referenced by the public entry page.

No credentials are needed or collected. API discovery performs GET requests only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import re
from urllib.request import ProxyHandler, build_opener

DEFAULT_SERVER = "http://192.168.1.100:5666"
ENTRY = "/music/"
ASSETS = "/music/static/assets/"
JS_NAME = re.compile(r"[a-f0-9]{32}-[\w-]+\.js")
TERMS = (
    "/login", "password:", "encrypt", "publicKey", "RSA", "music-token",
    "play-history", "/metadata", "/event/report", "TrackPlay", "occurredAt",
    "coverId", "/user/me", "/auth", "baseURL", "responseType",
)


def fetch(server: str, path: str) -> str:
    # Do not let an environment-configured external proxy see LAN requests.
    opener = build_opener(ProxyHandler({}))
    with opener.open(server.rstrip("/") + path, timeout=30) as response:
        return response.read().decode("utf-8")


def inspect(server: str, asset: str, terms: tuple[str, ...]) -> tuple[str, set[str], str]:
    text = fetch(server, ASSETS + asset)
    lines = []
    for term in terms:
        matches = list(re.finditer(re.escape(term), text))
        if matches:
            lines.append(f"TERM {term} ({len(matches)} matches)")
            for match in matches[:6]:
                lines.append(text[max(0, match.start() - 220):match.end() + 420])
    return asset, set(JS_NAME.findall(text)), "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--asset", help="Inspect one explicitly discovered asset filename")
    parser.add_argument("--terms", help="Comma-separated search terms")
    args = parser.parse_args()
    terms = tuple(args.terms.split(",")) if args.terms else TERMS
    if args.asset:
        name, _, excerpts = inspect(args.server, args.asset, terms)
        print(name, excerpts, sep="\n")
        return
    html = fetch(args.server, ENTRY)
    pending = set(JS_NAME.findall(html))
    seen: set[str] = set()
    # Dependencies are discovered from already-read assets; no directory scanning.
    with ThreadPoolExecutor(max_workers=6) as pool:
        while pending:
            current = sorted(pending - seen)
            if not current:
                break
            pending = set()
            for name, dependencies, excerpts in pool.map(
                lambda name: inspect(args.server, name, terms), current
            ):
                seen.add(name)
                pending.update(dependencies - seen)
                if excerpts:
                    print("\nASSET", name, "\n" + excerpts)
    print("Inspected referenced assets:", len(seen))


if __name__ == "__main__":
    main()
