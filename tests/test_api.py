import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, slskd

KEY = "test-key"


def flac_bytes(tmp_path: Path, seconds: float) -> bytes:
    path = tmp_path / "made.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds),
                    "-y", str(path)], check=True)
    return path.read_bytes()


def peer(username, folder, files, free=True, queue=0, speed=1_000_000, ext="flac", locked=False):
    """A peer's answer to a search: one folder of files, named the way people name them."""
    return {"username": username, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed,
            "files": [{"filename": f"{folder}\\{name}.{ext}", "size": 3_000_000, "extension": ext, "length": length,
                       "bitRate": 900 if ext == "flac" else 320, "isLocked": locked,
                       **({"bitDepth": 16, "sampleRate": 44100} if ext == "flac" else {})}
                      for name, length in files]}


DISCOVERY = [("01 - One More Time", 320), ("02 - Aerodynamic", 212)]


class FakeSlskd:
    """slskd's API: searches answered with what a test gives, and downloads that land in its downloads folder at once,
    or wait, or are refused, as a test says."""

    def __init__(self, found, folder: Path | None = None, body=b"", outcome="Completed, Succeeded"):
        self.found, self.folder, self.body, self.outcome = found, folder, body, outcome
        self.searches, self.queued, self.removed, self.deleted_searches = [], [], [], []
        self.transfers: dict[str, list[dict]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-API-Key") != KEY:
            return httpx.Response(401)
        path = request.url.path.removeprefix("/api/v0/")
        if path == "application":
            return httpx.Response(200, json={"server": {"state": "Connected, LoggedIn", "isConnected": True, "isLoggedIn": True},
                                             "shares": {"directories": 0, "files": 0}})
        if path == "searches" and request.method == "POST":
            self.searches.append(json.loads(request.content))
            return httpx.Response(200, json={"id": self.searches[-1]["id"], "state": "InProgress", "isComplete": False})
        if path.startswith("searches/"):
            parts = path.split("/")
            if request.method == "DELETE":
                self.deleted_searches.append(parts[1])
                return httpx.Response(204)
            if len(parts) == 3:
                return httpx.Response(200, json=self.found)
            return httpx.Response(200, json={"id": parts[1], "state": "Completed, TimedOut", "isComplete": True})
        if path.startswith("transfers/downloads/"):
            parts = path.removeprefix("transfers/downloads/").split("/")
            user = parts[0]
            if request.method == "POST":
                wanted = json.loads(request.content)
                self.queued.append((user, [w["filename"] for w in wanted]))
                for w in wanted:
                    done = self.outcome == "Completed, Succeeded"
                    self.transfers.setdefault(user, []).append({
                        "id": f"t{len(self.transfers.get(user, []))}", "username": user, "filename": w["filename"],
                        "size": w["size"], "state": self.outcome, "bytesTransferred": w["size"] if done else 0})
                    if done and self.folder is not None:
                        names = w["filename"].split("\\")
                        (self.folder / names[-2]).mkdir(parents=True, exist_ok=True)
                        (self.folder / names[-2] / names[-1]).write_bytes(self.body)
                return httpx.Response(201)
            if request.method == "DELETE":
                self.removed.append(parts[1])
                return httpx.Response(204)
            if user not in self.transfers:
                return httpx.Response(404)
            return httpx.Response(200, json={"username": user, "directories": [{"directory": "x", "files": self.transfers[user]}]})
        return httpx.Response(404)


@pytest.fixture
def client(monkeypatch, tmp_path):
    (tmp_path / "staging").mkdir()
    (tmp_path / "downloads").mkdir()
    monkeypatch.setattr(main, "STAGING", str(tmp_path / "staging"))
    monkeypatch.setattr(slskd, "POLL_SECONDS", 0.01)
    return TestClient(main.app)


def settings(tmp_path: Path, **more):
    return {"url": "http://slskd:5030", "apiKey": KEY, "downloadsFolder": str(tmp_path / "downloads"), "searchSeconds": 5, **more}


def use(monkeypatch, fake: FakeSlskd) -> FakeSlskd:
    original = slskd.client
    def fake_client(s):
        real = original(s)
        return httpx.Client(base_url=real.base_url, headers=real.headers, transport=httpx.MockTransport(fake))
    monkeypatch.setattr(slskd, "client", fake_client)
    return fake


def wait(client, job_id):
    for _ in range(500):
        status = client.get(f"/v1/fetch/{job_id}").json()
        if status["state"] not in ("queued", "running"):
            return status
        time.sleep(0.02)
    raise TimeoutError(status)


TRACK = {"kind": "track", "title": "Aerodynamic", "artist": "Daft Punk", "durationSeconds": 212}
ALBUM = {"kind": "album", "title": "Discovery", "artist": "Daft Punk",
         "tracks": [{"disc": 1, "position": 1, "title": "One More Time", "durationSeconds": 320},
                    {"disc": 1, "position": 2, "title": "Aerodynamic", "durationSeconds": 212}]}


def test_manifest_declares_the_contract(client):
    manifest = client.get("/v1/manifest").json()
    assert manifest["type"] == "retrieval"
    assert manifest["contractVersion"] == "1.0"
    assert set(manifest["capabilities"]) == {"fetch-track", "fetch-album", "cancel"}
    assert manifest["settingsSchema"]["properties"]["apiKey"]["writeOnly"] is True


def test_it_can_be_added_before_its_settings_are_filled_in(client):
    schema = client.get("/v1/manifest").json()["settingsSchema"]
    assert "required" not in schema
    defaults = {name: p["default"] for name, p in schema["properties"].items() if "default" in p}
    health = client.post("/v1/health", json={"settings": defaults}).json()
    assert health["status"] == "unhealthy" and "API key" in health["message"]


@pytest.mark.parametrize("name, title", [("01 - One More Time.flac", "One More Time"), ("1-03 Digital Love.mp3", "Digital Love"),
                                          ("A2. Aerodynamic.flac", "Aerodynamic"), ("Voyager.flac", "Voyager")])
def test_a_files_title_is_its_name_without_its_number(name, title):
    assert slskd._title(f"Music\\Daft Punk\\Discovery\\{name}") == title


def test_a_whole_folder_is_the_album_lossless_and_free_first(client, monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeSlskd([
        peer("slow", "Music\\Daft Punk\\Discovery (2001) [FLAC]", DISCOVERY, free=False, queue=40),
        peer("lossy", "share\\Daft Punk - Discovery", DISCOVERY, ext="mp3"),
        peer("fast", "Music\\Daft Punk\\Discovery (2001) [FLAC]", DISCOVERY),
        peer("partial", "Music\\Daft Punk\\Discovery", DISCOVERY[:1]),
    ]))
    candidates = client.post("/v1/search", json={"settings": settings(tmp_path), "target": ALBUM}).json()["candidates"]
    assert [json.loads(c["candidateId"])["u"] for c in candidates] == ["fast", "slow", "lossy"]
    best = candidates[0]
    assert (best["format"], best["bitDepth"], best["sampleRateHz"], best["source"]) == ("flac", 16, 44100, "Soulseek: fast")
    assert [(d, p) for d, p, _, _ in json.loads(best["candidateId"])["f"]] == [(1, 1), (1, 2)]
    assert fake.searches[0]["searchText"] == "daft punk discovery"
    assert fake.deleted_searches == [fake.searches[0]["id"]]


def test_lossless_only_leaves_out_lossy_and_locked_files(client, monkeypatch, tmp_path):
    use(monkeypatch, FakeSlskd([peer("lossy", "share\\Daft Punk - Discovery", DISCOVERY, ext="mp3"),
                                peer("locked", "Music\\Daft Punk\\Discovery", DISCOVERY, locked=True)]))
    found = client.post("/v1/search", json={"settings": settings(tmp_path, format="lossless"), "target": ALBUM}).json()["candidates"]
    assert found == []


def test_a_song_is_found_as_a_file(client, monkeypatch, tmp_path):
    use(monkeypatch, FakeSlskd([peer("someone", "Music\\Daft Punk\\Discovery", DISCOVERY)]))
    best = client.post("/v1/search", json={"settings": settings(tmp_path), "target": TRACK}).json()["candidates"][0]
    assert (best["title"], best["album"], best["kind"]) == ("Aerodynamic", "Discovery", "track")
    assert json.loads(best["candidateId"])["f"] == [[None, None, "Music\\Daft Punk\\Discovery\\02 - Aerodynamic.flac", 3_000_000]]


def test_health_says_whats_wrong(client, monkeypatch, tmp_path):
    use(monkeypatch, FakeSlskd([]))
    health = client.post("/v1/health", json={"settings": settings(tmp_path)}).json()
    assert health["status"] == "healthy" and "sharing nothing" in health["message"]
    assert client.post("/v1/health", json={"settings": settings(tmp_path, apiKey="wrong")}).json()["status"] == "unhealthy"
    missing = client.post("/v1/health", json={"settings": settings(tmp_path, downloadsFolder=str(tmp_path / "nowhere"))}).json()
    assert missing["status"] == "degraded" and "mount" in missing["message"]
    assert client.post("/v1/health", json={"settings": {"apiKey": KEY}}).json()["status"] == "unhealthy"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make test files")
def test_an_album_is_downloaded_and_moved_to_staging(client, monkeypatch, tmp_path):
    body = flac_bytes(tmp_path, 2)
    fake = use(monkeypatch, FakeSlskd([peer("fast", "Music\\Daft Punk\\Discovery", DISCOVERY)], tmp_path / "downloads", body))
    s = settings(tmp_path)
    candidate = client.post("/v1/search", json={"settings": s, "target": ALBUM}).json()["candidates"][0]
    started = client.post("/v1/fetch", json={"settings": s, "jobId": "soul-1", "target": ALBUM, "candidate": candidate,
                                             "stagingDirectory": "soul-1"})
    assert started.status_code == 202
    status = wait(client, "soul-1")

    assert status["state"] == "completed", status
    assert sorted((f["path"], f["position"], f["codec"]) for f in status["files"]) == [
        ("01 - One More Time.flac", 1, "flac"), ("02 - Aerodynamic.flac", 2, "flac")]
    assert (tmp_path / "staging" / "soul-1" / "02 - Aerodynamic.flac").read_bytes() == body
    # Moved, not copied, and slskd's empty folder and list of downloads tidied.
    assert list((tmp_path / "downloads").iterdir()) == []
    assert fake.queued == [("fast", ["Music\\Daft Punk\\Discovery\\01 - One More Time.flac", "Music\\Daft Punk\\Discovery\\02 - Aerodynamic.flac"])]
    assert sorted(fake.removed) == ["t0", "t1"]


def test_a_refused_download_fails_so_another_peer_is_tried(client, monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeSlskd([peer("grumpy", "Music\\Daft Punk\\Discovery", DISCOVERY)], outcome="Completed, Rejected"))
    s = settings(tmp_path)
    candidate = client.post("/v1/search", json={"settings": s, "target": TRACK}).json()["candidates"][0]
    client.post("/v1/fetch", json={"settings": s, "jobId": "soul-2", "target": TRACK, "candidate": candidate, "stagingDirectory": "soul-2"})
    status = wait(client, "soul-2")
    assert status["state"] == "failed"
    assert "grumpy refused it" in status["error"]
    assert fake.removed == ["t0"]


def test_a_long_queue_gives_up_and_cancels(client, monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeSlskd([peer("busy", "Music\\Daft Punk\\Discovery", DISCOVERY)], outcome="Queued, Remotely"))
    s = settings(tmp_path, queueMinutes=0.001)
    candidate = client.post("/v1/search", json={"settings": s, "target": TRACK}).json()["candidates"][0]
    client.post("/v1/fetch", json={"settings": s, "jobId": "soul-3", "target": TRACK, "candidate": candidate, "stagingDirectory": "soul-3"})
    status = wait(client, "soul-3")
    assert status["state"] == "failed"
    assert "busy's queue" in status["error"]
    assert fake.removed == ["t0"]


def test_a_finished_download_slskd_put_elsewhere_is_reported(client, monkeypatch, tmp_path):
    use(monkeypatch, FakeSlskd([peer("someone", "Music\\Daft Punk\\Discovery", DISCOVERY)]))
    s = settings(tmp_path)
    candidate = client.post("/v1/search", json={"settings": s, "target": TRACK}).json()["candidates"][0]
    client.post("/v1/fetch", json={"settings": s, "jobId": "soul-4", "target": TRACK, "candidate": candidate, "stagingDirectory": "soul-4"})
    status = wait(client, "soul-4")
    assert status["state"] == "failed"
    assert "Mount slskd's downloads folder" in status["error"]
