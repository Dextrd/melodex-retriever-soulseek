"""Finding requested music on Soulseek through the user's own slskd, and downloading it.

What Soulseek finds is other people's shared files, in their folders. An album is a folder with a file for every
track asked for; a song is one file. Peers come and go and keep queues, so a download that waits too long in a queue,
stalls, or is refused fails, and the core moves on to the next candidate, usually another peer's copy.

slskd puts finished downloads in its downloads folder, in a folder named after the peer's, and this plugin needs that
folder mounted too, to move them into staging.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .contract import Candidate, Job, Kind, Target, env, probe
from .matching import duration_score, normalise, similarity, track_score

FORMATS = ["best", "lossless"]
LOSSLESS = {"flac", "wav", "aiff", "aif", "ape", "wv"}
LOSSY = {"mp3", "m4a", "ogg", "opus", "aac", "wma"}

# How often a search and a download are looked at again; tests make it quicker.
POLL_SECONDS = float(env("POLL_SECONDS", "2"))

# A leading track number, as in "01 - Title", "1-03 Title" or "A2. Title".
_NUMBER = re.compile(r"(?i)^\s*(?:[a-d]?\d{1,2}[-.]?)?\d{0,3}\s*[-._)]*\s+")


def client(settings: dict[str, Any]) -> httpx.Client:
    """The HTTP client every call goes through; tests swap in a fake one."""
    url = str(settings.get("url") or "").rstrip("/")
    return httpx.Client(base_url=f"{url}/api/v0/", headers={"X-API-Key": str(settings.get("apiKey") or "")}, timeout=30)


def downloads(settings: dict[str, Any]) -> Path:
    return Path(str(settings.get("downloadsFolder") or "/downloads"))


def application(http: httpx.Client) -> dict[str, Any]:
    response = http.get("application")
    if response.status_code == 401:
        raise PermissionError("slskd refused the API key.")
    response.raise_for_status()
    return response.json()


def connected(app: dict[str, Any]) -> bool:
    server = app.get("server") or {}
    return bool(server.get("isLoggedIn") or server.get("isConnected") or "LoggedIn" in str(server.get("state", "")))


def shared_files(app: dict[str, Any]) -> int | None:
    shares = app.get("shares")
    return shares.get("files") if isinstance(shares, dict) and isinstance(shares.get("files"), int) else None


def _parts(filename: str) -> list[str]:
    """A peer's path, which is usually Windows-style, as its folders and file name."""
    return [p for p in re.split(r"[\\/]", filename) if p]


def _extension(file: dict[str, Any]) -> str:
    ext = str(file.get("extension") or "").lstrip(".").lower()
    return ext or Path(_parts(file["filename"])[-1]).suffix.lstrip(".").lower()


def _title(filename: str) -> str:
    """The song a file is of, as its name says: without its folder, extension and track number."""
    stem = Path(_parts(filename)[-1]).stem
    return _NUMBER.sub("", stem, count=1) or stem


def _query(*words: str | None) -> str:
    # Soulseek matches every word, and a word starting with "-" leaves things out, so only plain words go.
    return normalise(" ".join(w for w in words if w))


# ---- Search ----

def responses(http: httpx.Client, text: str, seconds: float) -> list[dict[str, Any]]:
    """Every peer's answer to a search, once slskd has finished it."""
    search_id = str(uuid.uuid4())
    response = http.post("searches", json={"id": search_id, "searchText": text, "searchTimeout": int(seconds * 1000),
                                           "responseLimit": 200, "fileLimit": 20000, "filterResponses": True})
    response.raise_for_status()
    try:
        deadline = time.monotonic() + seconds + 15
        while time.monotonic() < deadline:
            state = http.get(f"searches/{search_id}").json()
            if state.get("isComplete") or "Completed" in str(state.get("state", "")):
                break
            time.sleep(POLL_SECONDS)
        found = http.get(f"searches/{search_id}/responses")
        found.raise_for_status()
        return found.json()
    finally:
        try:
            http.delete(f"searches/{search_id}")
        except httpx.HTTPError:
            pass


def _files(response: dict[str, Any], format: str) -> list[dict[str, Any]]:
    """A peer's audio files that can be downloaded, in the formats asked for."""
    wanted = LOSSLESS if format == "lossless" else LOSSLESS | LOSSY
    return [f for f in response.get("files") or [] if not f.get("isLocked") and _extension(f) in wanted]


def _availability(response: dict[str, Any]) -> float:
    """How soon a peer's likely to send it, from 0 to 1: a free slot, a short queue, a fast upload."""
    queue = min(int(response.get("queueLength") or 0), 50)
    speed = min(float(response.get("uploadSpeed") or 0) / 2_000_000, 1.0)
    return 0.5 * bool(response.get("hasFreeUploadSlot")) + 0.3 * (1 - queue / 50) + 0.2 * speed


def search(target: Target, limit: int, settings: dict[str, Any]) -> list[Candidate]:
    format = settings.get("format", "best")
    min_score = float(settings.get("minScore", 0.6))
    seconds = float(settings.get("searchSeconds", 15))
    text = _query(target.artist, target.title)
    with client(settings) as http:
        found = responses(http, text, seconds)

    ranked: list[tuple[float, float, Candidate]] = []
    for response in found:
        files = _files(response, format)
        if not files:
            continue
        availability = _availability(response)
        if target.kind == Kind.album:
            folders: dict[str, list[dict[str, Any]]] = {}
            for f in files:
                folders.setdefault("\\".join(_parts(f["filename"])[:-1]), []).append(f)
            for folder, listed in folders.items():
                if (candidate := _album(target, response["username"], folder, listed)) is not None:
                    ranked.append((candidate.score or 0, availability, candidate))
        else:
            ranked += [(c.score or 0, availability, c) for c in _tracks(target, response["username"], files)]

    ranked = [r for r in ranked if r[0] >= min_score]
    # Lossless first among copies that match as well, then those likeliest to come soon.
    ranked.sort(key=lambda r: (round(r[0], 1), r[2].format in LOSSLESS, r[1], r[0]), reverse=True)
    return [c for _, _, c in ranked[:limit]]


def _candidate(username: str, kind: Kind, title: str, chosen: list[tuple[int | None, int | None, dict[str, Any]]],
               score: float, album: str | None = None) -> Candidate:
    files = [f for _, _, f in chosen]
    lengths = [f.get("length") for f in files]
    rates = [f["bitRate"] for f in files if f.get("bitRate")]
    format = _extension(files[0])
    return Candidate(
        candidate_id=json.dumps({"u": username, "f": [[d, p, f["filename"], int(f.get("size") or 0)] for d, p, f in chosen]}),
        kind=kind,
        title=title,
        album=album,
        duration_seconds=round(sum(lengths), 1) if all(lengths) else None,
        source=f"Soulseek: {username}",
        format=format,
        bitrate_kbps=round(sum(rates) / len(rates)) if rates else None,
        bit_depth=files[0].get("bitDepth") if format in LOSSLESS else None,
        sample_rate_hz=files[0].get("sampleRate") if format in LOSSLESS else None,
        score=round(score, 3),
    )


def _tracks(target: Target, username: str, files: list[dict[str, Any]]) -> list[Candidate]:
    scored = []
    for f in files:
        folders = _parts(f["filename"])[:-1]
        scored.append((track_score(target.title, target.artist, target.duration_seconds, _title(f["filename"]), folders[-3:],
                                   f.get("length")), f))
    scored.sort(key=lambda x: x[0], reverse=True)
    # A peer's two best: often the same song in two formats or two of their folders.
    return [_candidate(username, Kind.track, _title(f["filename"]), [(None, None, f)], score,
                       album=(_parts(f["filename"])[-2:-1] or [None])[0]) for score, f in scored[:2]]


def _album(target: Target, username: str, folder: str, listed: list[dict[str, Any]]) -> Candidate | None:
    """A peer's folder as the requested album, offered only when it has a file for every requested track."""
    names = _parts(folder)
    wanted = target.tracks or []
    used: set[str] = set()
    chosen = []
    matches = []
    for want in wanted:
        best = max(((0.6 * similarity(want.title, _title(f["filename"])) + 0.4 * duration_score(want.duration_seconds, f.get("length")),
                     _extension(f) in LOSSLESS, f) for f in listed if f["filename"] not in used),
                   key=lambda x: (x[0], x[1]), default=None)
        if best is None or best[0] < 0.5:
            return None
        used.add(best[2]["filename"])
        chosen.append((want.disc, want.position, best[2]))
        matches.append(best[0])
    # The album's title is usually the folder's name, sometimes with the artist and year, or its parent's, when the
    # folder is "CD1" or "FLAC".
    title_match = max([similarity(target.title, n) for n in names[-2:]] + [0.0])
    if not wanted:
        # With no tracklist to check, only the title says it's the album.
        if title_match < 0.85:
            return None
        listed = sorted(listed, key=lambda f: f["filename"])
        chosen = [(1, i + 1, f) for i, f in enumerate(listed)]
        matches = [0.7]
    artist_match = max([similarity(target.artist, n) for n in names[-3:]] + [0.0])
    if normalise(target.artist) and normalise(target.artist) in normalise(" ".join(names[-3:])):
        artist_match = max(artist_match, 0.9)
    score = 0.25 * title_match + 0.15 * artist_match + 0.6 * sum(matches) / len(matches)
    return _candidate(username, Kind.album, names[-1] if names else target.title, chosen, score)


# ---- Fetch ----

def _reason(state: str) -> str:
    for word, reason in (("Rejected", "refused it"), ("TimedOut", "timed out"), ("Cancelled", "cancelled it"),
                         ("Errored", "the download failed"), ("Failed", "the download failed")):
        if word in state:
            return reason
    return "the download failed"


def transfers(http: httpx.Client, username: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    """slskd's downloads from the peer of the files asked for, by file name; the latest of each."""
    response = http.get(f"transfers/downloads/{quote(username)}")
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    found: dict[str, dict[str, Any]] = {}
    for directory in response.json().get("directories") or []:
        for transfer in directory.get("files") or []:
            if transfer.get("filename") in wanted:
                found[transfer["filename"]] = transfer
    return found


def _cancel(http: httpx.Client, username: str, found: dict[str, dict[str, Any]]) -> None:
    for transfer in found.values():
        try:
            http.delete(f"transfers/downloads/{quote(username)}/{quote(str(transfer['id']))}", params={"remove": "true"})
        except httpx.HTTPError:
            pass


def fetch(job: Job, candidate_id: str, directory: Path, settings: dict[str, Any]) -> list[dict[str, Any]]:
    chosen = json.loads(candidate_id)
    username, files = chosen["u"], chosen["f"]
    wanted = {name for _, _, name, _ in files}
    total = sum(size for _, _, _, size in files) or None
    queue_limit = float(settings.get("queueMinutes", 20)) * 60
    stall_limit = float(settings.get("stallMinutes", 5)) * 60

    with client(settings) as http:
        response = http.post(f"transfers/downloads/{quote(username)}", json=[{"filename": n, "size": s} for _, _, n, s in files])
        if response.status_code >= 400:
            raise RuntimeError(f"slskd wouldn't ask {username} for it: {response.text.strip() or response.status_code}")
        started = last_change = time.monotonic()
        last_bytes = -1
        found: dict[str, dict[str, Any]] = {}
        try:
            while True:
                job.check()
                found = transfers(http, username, wanted)
                states = {name: str(t.get("state", "")) for name, t in found.items()}
                if failed := next((s for s in states.values() if "Completed" in s and "Succeeded" not in s), None):
                    raise RuntimeError(f"{username} {_reason(failed)}. Another peer may have it.")
                if len(states) == len(wanted) and all("Succeeded" in s for s in states.values()):
                    break
                done = sum(int(t.get("bytesTransferred") or 0) for t in found.values())
                if total:
                    job.progress = min(0.99, done / total)
                now = time.monotonic()
                if done != last_bytes:
                    last_bytes, last_change = done, now
                waiting = all("Queued" in s or "Requested" in s or not s for s in states.values() if "Succeeded" not in s)
                if waiting and done == 0 and now - started > queue_limit:
                    raise RuntimeError(f"Still waiting in {username}'s queue after {queue_limit / 60:g} minutes. Another peer may have it sooner.")
                if not waiting and now - last_change > stall_limit:
                    raise RuntimeError(f"The download from {username} stalled for {stall_limit / 60:g} minutes.")
                time.sleep(POLL_SECONDS)
        except BaseException:
            _cancel(http, username, found)
            raise

        written = []
        for disc, position, name, size in files:
            source = _downloaded(settings, name, size)
            target = directory / _safe(_parts(name)[-1])
            shutil.move(source, target)
            _tidy(source.parent, downloads(settings))
            written.append(probe(target, directory, disc, position))
        # Done with: off slskd's list of downloads.
        _cancel(http, username, found)
    return written


def _downloaded(settings: dict[str, Any], name: str, size: int) -> Path:
    """Where slskd put a finished download: in a folder named after the peer's, or anywhere in its downloads folder."""
    base = downloads(settings)
    parts = _parts(name)
    for path in ([base / parts[-2] / parts[-1]] if len(parts) > 1 else []) + [base / parts[-1]]:
        if path.is_file():
            return path
    for path in base.rglob(parts[-1]) if base.is_dir() else []:
        if path.is_file() and (not size or path.stat().st_size == size):
            return path
    raise RuntimeError(f"slskd finished {parts[-1]}, but it isn't in {base}. Mount slskd's downloads folder there.")


def _tidy(folder: Path, base: Path) -> None:
    """Removes a download's folder once it's empty, short of the downloads folder itself."""
    try:
        while folder != base and base in folder.parents and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    except OSError:
        pass


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "track"
