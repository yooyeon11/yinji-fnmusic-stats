"""Standard-library connector for the public fnMusic web API (Python 3.10+).

Credentials are never logged or persisted here. Only login performs a write;
me/history/track/cover only read. proxy is a raw transport for the embedding
server; that server must enforce its own authorization and write policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from datetime import date
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

API_BASE = "/music/api/v1"
# Public frontend signature constants, NOT account credentials.
_SIGN_PREFIX = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_SIGN_API_KEY = bytes(value ^ 185 for value in (
    143, 253, 140, 143, 137, 139, 253, 141, 148, 248, 138, 141,
    139, 148, 141, 142, 128, 128, 148, 248, 137, 255, 137, 148,
    251, 251, 142, 128, 140, 252, 142, 136, 143, 142, 253, 137,
)).decode("utf-8")
_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
})


class MusicError(RuntimeError):
    """Sanitized upstream failure; response bodies and secrets are not retained."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.status_code = status
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Return redirects verbatim; never send a token to a different origin.
        return None


def _validate_path(path: str) -> str:
    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("Only absolute /music paths are allowed")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Invalid control character in path")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("Only absolute /music paths are allowed")
    decoded = parsed.path
    # Reject encoded traversal, including nested encodings and backslashes.
    for _ in range(10):
        if "\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise ValueError("Invalid /music path")
        if not (decoded == "/music" or decoded.startswith("/music/")):
            raise ValueError("Only /music paths are allowed")
        if any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("Path traversal is not allowed")
        next_decoded = unquote(decoded, errors="strict")
        if next_decoded == decoded:
            return path
        decoded = next_decoded
    raise ValueError("Too many nested path encodings")


def _clean_headers(headers: Mapping[str, str] | None, *, request: bool) -> dict[str, str]:
    original = {str(key): str(value) for key, value in (headers or {}).items()}
    connection = ",".join(value for key, value in original.items() if key.lower() == "connection")
    excluded = set(_HOP_HEADERS) | {name.strip().lower() for name in connection.split(",")}
    if request:
        # Local session cookies and local Host must never leave this gateway.
        excluded |= {"host", "content-length", "cookie"}
    return {key: value for key, value in original.items() if key.lower() not in excluded}


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def _authx(method: str, path: str, body: bytes | None) -> str:
    parsed = urlsplit(path)
    if method == "GET":
        # JS URLSearchParams: last value wins; sorted keys; '+' becomes '%20';
        # then decodeURIComponent before hashing, keeping literal '+' intact.
        params = {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                  if value not in {"undefined", "null"}}
        serialized = urlencode(sorted(params.items())).replace("+", "%20")
        data_hash = _md5(unquote(serialized, errors="strict"))
    else:
        data_hash = _md5(body.decode("utf-8") if body is not None else "")
    nonce = str(secrets.randbelow(900000) + 100000)
    timestamp = str(int(time.time() * 1000))
    signature = _md5("_".join((
        _SIGN_PREFIX, parsed.path, nonce, timestamp, data_hash, _SIGN_API_KEY,
    )))
    return f"nonce={nonce}&timestamp={timestamp}&sign={signature}"


class MusicClient:
    def __init__(self, server: str, token: str = "") -> None:
        if not isinstance(server, str) or any(ord(char) < 32 for char in server):
            raise ValueError("Invalid music server URL")
        parsed = urlsplit(server.strip())
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") not in {"", "/music"}):
            raise ValueError("Server must be an HTTP(S) origin or its /music/ entry")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Invalid music server port") from None
        self.server = f"{parsed.scheme}://{parsed.netloc}"
        self.token = token
        self.timeout = 30
        self._device_id = secrets.token_hex(16)
        # LAN authorization must not pass through environment external proxies.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def proxy(self, method: str, path: str, body: bytes | str | None = None,
              headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        """Raw same-origin /music transport, including HTTP errors and redirects.

        Headers are a dictionary; duplicate Set-Cookie is consequently not
        represented. The embedding server should suppress remote Set-Cookie and
        provide its own JS-readable non-secret music-token sentinel cookie.
        This method does not generate authx or change response encoding/body.
        """
        _validate_path(path)
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise ValueError("Unsupported HTTP method")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is not None and not isinstance(body, bytes):
            raise TypeError("body must be bytes, str, or None")
        outgoing = _clean_headers(headers, request=True)
        if self.token:
            outgoing["Cookie"] = "music-token=" + quote(self.token, safe="")
        request = Request(self.server + path, data=body, headers=outgoing, method=method)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except (URLError, OSError, TimeoutError):
            raise MusicError("Music server connection failed") from None
        with response:
            status = response.status
            response_headers = _clean_headers(dict(response.headers.items()), request=False)
            try:
                content = response.read()
            except (OSError, ValueError):
                raise MusicError("Music server response could not be read", status=status) from None
        return status, response_headers, content

    def _json(self, method: str, endpoint: str, *, params: Mapping | None = None,
              body: dict | None = None):
        path = API_BASE + endpoint
        if params:
            path += "?" + urlencode(params)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Accept-Language": "zh-CN",
                   "authx": _authx(method, path, raw)}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        status, _, content = self.proxy(method, path, raw, headers)
        if status < 200 or status >= 300:
            raise MusicError(f"Music API HTTP {status}", status=status)
        try:
            envelope = json.loads(content)
        except (ValueError, UnicodeError):
            raise MusicError("Invalid music API JSON", status=status) from None
        if not isinstance(envelope, dict):
            raise MusicError("Invalid music API envelope", status=status)
        code = envelope.get("code")
        if isinstance(code, str) and re.fullmatch(r"[+-]?\d+", code.strip()):
            code = int(code)
        if not isinstance(code, int) or isinstance(code, bool):
            raise MusicError("Invalid music API result code", status=status)
        if code != 0:
            # Backend messages can echo request credentials: never retain them.
            raise MusicError(f"Music API rejected request (code {code})", status=status, code=code)
        return envelope.get("data")

    def login(self, username: str, password: str) -> dict:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("username and password must be strings")
        if not username.strip() or not password:
            raise ValueError("Username and password are required")
        self.token = ""
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        data = self._json("POST", "/user/password-login", body={
            "username": username.strip(), "password": digest, "deviceId": self._device_id,
        })
        if (not isinstance(data, dict) or not isinstance(data.get("userToken"), str)
                or not data["userToken"] or not isinstance(data.get("user"), dict)):
            raise MusicError("Incomplete music login response")
        self.token = data["userToken"]
        return {"token": self.token, "user": data["user"]}

    def me(self) -> dict:
        data = self._json("GET", "/user/me")
        if not isinstance(data, dict):
            raise MusicError("Invalid music user response")
        return data

    def history(self) -> list:
        """Read all exposed pages, preserving raw items, order and duplicates.

        Raw updatedAt/createdAt are never interpreted as playback timestamps.
        Pagination is not a historical archive or a snapshot under concurrent
        listening. Exposed server retention limits cannot be recovered here.
        """
        items: list = []
        previous: list | None = None
        for page in range(1, 10001):
            data = self._json("GET", "/play-history/list", params={"page": page, "size": 100})
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise MusicError("Invalid music history response")
            batch = data["list"]
            if not batch:
                return items
            if previous == batch:
                raise MusicError("Music history pagination did not advance")
            items.extend(batch)
            total = data.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                if len(items) >= total:
                    return items
            elif len(batch) < 100:
                return items
            previous = batch
        raise MusicError("Music history exceeded pagination limit")

    def recent(self, size: int = 100) -> list:
        """Newest page of the play history, raw and unreordered.

        Playback detection polls this single page; the endpoint exposes no
        playback timestamp, so callers must not infer one from any field here.
        """
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100:
            raise ValueError("size must be between 1 and 100")
        data = self._json("GET", "/play-history/list", params={"page": 1, "size": size})
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise MusicError("Invalid music history response")
        return data["list"]

    def track(self, guid: str) -> dict:
        if not isinstance(guid, str) or not guid:
            raise ValueError("Track GUID is required")
        data = self._json("GET", "/track/metadata", params={"guid": guid})
        if not isinstance(data, dict):
            raise MusicError("Invalid music track response")
        return data

    def cover(self, cover_id: str) -> tuple[bytes, str]:
        if cover_id is None or not str(cover_id):
            raise ValueError("Cover ID is required")
        path = API_BASE + "/static/cover?" + urlencode({"coverId": str(cover_id)})
        status, headers, content = self.proxy("GET", path)
        if status < 200 or status >= 300:
            raise MusicError(f"Music cover HTTP {status}", status=status)
        content_type = next((value for key, value in headers.items()
                             if key.lower() == "content-type"), "application/octet-stream")
        return content, content_type


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if not isinstance(value, list):
        return []
    return [name for item in value
            if (name := _text(item.get("name") if isinstance(item, dict) else item))]


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _release_year(track: dict, album: dict) -> int | None:
    # year=0/null means unknown; prefer explicit years before album date.
    for value in (track.get("year"), album.get("originalReleaseYear")):
        year = _number(value)
        if year is not None and year > 0 and year.is_integer():
            return int(year)
    release_date = album.get("releaseDate")
    if isinstance(release_date, str):
        release_date = release_date.strip()
        if re.fullmatch(r"\d{4}", release_date):
            year = int(release_date)
            return year if year > 0 else None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
            try:
                return date.fromisoformat(release_date).year
            except ValueError:
                pass
    return None


def parse_track(item: dict) -> dict:
    """Normalize a raw history track or raw metadata {track,audioSpec}.

    Duration is already milliseconds upstream. Unknown duration is 0 and
    unknown release year is None. No playback-time field is synthesized.
    """
    if not isinstance(item, dict):
        raise TypeError("Track item must be a dictionary")
    track = item.get("track") if isinstance(item.get("track"), dict) else item
    spec = item.get("audioSpec")
    if not isinstance(spec, dict):
        spec = track.get("audioSpec") if isinstance(track.get("audioSpec"), dict) else {}
    album_value = track.get("album")
    album = album_value if isinstance(album_value, dict) else {}
    duration = _number(spec.get("duration"))
    if duration is None:
        duration = _number(track.get("duration"))
    year = _release_year(track, album)
    cover_id = track.get("coverId")
    if cover_id in (None, ""):
        cover_id = album.get("coverId")
    return {
        "guid": _text(track.get("guid")),
        "title": _text(track.get("title")),
        "artist": " / ".join(_names(track.get("artists", track.get("artist")))),
        "album": _text(album.get("name")) if isinstance(album_value, dict) else _text(album_value),
        "duration_ms": max(0, int(duration)) if duration is not None else 0,
        "genre": ";".join(_names(track.get("genres", track.get("genre")))),
        "cover_id": str(cover_id) if cover_id is not None else "",
        "release_year": year,
    }


def parse_artists(item: dict) -> list[dict]:
    """Deduplicated {name, cover_id} pairs from a raw history or metadata item.

    fnMusic stores one artist image per artist, served by the same cover
    endpoint as album art (coverId prefixed artist_...). Artists without a
    usable name are dropped; a missing coverId yields an empty string so the
    caller can fall back to album art.
    """
    if not isinstance(item, dict):
        return []
    track = item.get("track") if isinstance(item.get("track"), dict) else item
    raw = track.get("artists")
    if not isinstance(raw, list):
        single = track.get("artist")
        raw = [single] if isinstance(single, (dict, str)) else []
    out, seen = [], set()
    for entry in raw:
        if isinstance(entry, dict):
            name, cover = entry.get("name"), entry.get("coverId")
        elif isinstance(entry, str):
            name, cover = entry, None
        else:
            continue
        name = name.strip() if isinstance(name, str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "cover_id": str(cover) if cover is not None else ""})
    return out
