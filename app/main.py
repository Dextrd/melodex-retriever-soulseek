"""The Soulseek retrieval plugin's HTTP API (Melodex retrieval contract v1.0), through the user's own slskd."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from . import slskd
from .contract import (CONTRACT_VERSION, FetchRequest, HealthRequest, Jobs, SearchRequest, dump, env, error,
                       staging_directory)

VERSION = "0.1.0"
STAGING = env("STAGING_ROOT", "/staging")

# Nothing is marked required: Melodex adds a plugin before its settings can be filled in, and health says what's
# missing until they are.
SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string", "default": "http://slskd:5030", "title": "slskd's address",
            "description": "Where this plugin reaches your slskd, such as http://slskd:5030.",
        },
        "apiKey": {
            "type": "string", "title": "API key", "writeOnly": True,
            "description": "One of slskd's API keys, from the web.authentication.api_keys part of its settings.",
        },
        "downloadsFolder": {
            "type": "string", "default": "/downloads", "title": "slskd's downloads folder",
            "description": "Where slskd's downloads folder is mounted in this plugin, so finished downloads can be moved "
                           "into Melodex's staging. Mount the same folder slskd downloads to.",
        },
        "format": {
            "type": "string", "enum": slskd.FORMATS, "default": "best", "title": "Format",
            "description": "best prefers lossless copies and takes lossy ones where there's nothing better. lossless "
                           "only offers FLAC, WAV, AIFF, APE and WavPack.",
        },
        "searchSeconds": {
            "type": "integer", "minimum": 5, "maximum": 120, "default": 15, "title": "Search for (seconds)",
            "description": "How long a search waits for peers to answer. Longer finds more, more slowly.",
        },
        "queueMinutes": {
            "type": "integer", "minimum": 1, "maximum": 25, "default": 20, "title": "Wait in a queue for (minutes)",
            "description": "How long to wait in a peer's queue before trying another peer's copy.",
        },
        "stallMinutes": {
            "type": "integer", "minimum": 1, "maximum": 25, "default": 5, "title": "Give up on a stalled download after (minutes)",
            "description": "How long a download may go without receiving anything before trying another peer's copy.",
        },
        "minScore": {
            "type": "number", "minimum": 0, "maximum": 1, "default": 0.6, "title": "Lowest match score",
            "description": "Files and folders that match the request less well than this aren't offered.",
        },
    },
}

MANIFEST: dict[str, Any] = {
    "id": "melodex.retrieval.soulseek",
    "name": "Soulseek",
    "version": VERSION,
    "type": "retrieval",
    "contractVersion": CONTRACT_VERSION,
    "description": "Finds songs and albums in what Soulseek's users share, and downloads them through your own slskd, in FLAC where there is one.",
    "homepage": "https://github.com/slskd/slskd",
    "capabilities": ["fetch-track", "fetch-album", "cancel"],
    "settingsSchema": SETTINGS_SCHEMA,
    "access": {"network": ["your slskd"], "files": ["staging (write)", "slskd's downloads (read, move)"]},
    "maxRequestsPerSecond": 2,
    "retrieval": {"formats": sorted(slskd.LOSSLESS | slskd.LOSSY), "lossless": True},
}

app = FastAPI(title="Melodex Soulseek retriever", version=VERSION)
jobs = Jobs(int(env("PARALLEL_FETCHES", "4")))


@app.exception_handler(HTTPException)
async def contract_error(_: Request, e: HTTPException) -> JSONResponse:
    body = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
    return JSONResponse(body, status_code=e.status_code)


def check_settings(settings: dict[str, Any]) -> None:
    if not str(settings.get("url") or "").startswith(("http://", "https://")):
        raise error(400, "Give slskd's address, starting http:// or https://.", "invalid-settings")
    if not settings.get("apiKey"):
        raise error(400, "Give one of slskd's API keys.", "invalid-settings")
    if settings.get("format", "best") not in slskd.FORMATS:
        raise error(400, f"format must be one of {', '.join(slskd.FORMATS)}.", "invalid-settings")


@app.get("/v1/manifest")
def manifest() -> dict[str, Any]:
    return MANIFEST


@app.post("/v1/health")
def health(body: HealthRequest) -> dict[str, Any]:
    try:
        check_settings(body.settings)
    except HTTPException as e:
        return {"status": "unhealthy", "message": e.detail["error"]}
    try:
        with slskd.client(body.settings) as http:
            state = slskd.application(http)
    except PermissionError as e:
        return {"status": "unhealthy", "message": str(e)}
    except Exception as e:
        return {"status": "unhealthy", "message": f"Couldn't reach slskd: {e}"}
    if not slskd.connected(state):
        return {"status": "unhealthy", "message": "slskd isn't signed in to Soulseek."}
    folder = slskd.downloads(body.settings)
    if not folder.is_dir():
        return {"status": "degraded", "message": f"Can't see slskd's downloads folder at {folder}: mount it there, so downloads can be moved into staging."}
    if slskd.shared_files(state) == 0:
        return {"status": "healthy", "message": "Signed in to Soulseek, sharing nothing: some peers won't send to those who don't share."}
    return {"status": "healthy", "message": "Signed in to Soulseek"}


@app.post("/v1/search")
def search(body: SearchRequest) -> dict[str, Any]:
    check_settings(body.settings)
    try:
        candidates = slskd.search(body.target, max(1, min(body.limit, 25)), body.settings)
    except Exception as e:
        raise error(502, f"Soulseek search failed: {e}", "source-unavailable")
    return {"candidates": dump(candidates)}


@app.post("/v1/fetch", status_code=202)
def fetch(body: FetchRequest) -> dict[str, Any]:
    check_settings(body.settings)
    directory = staging_directory(STAGING, body.staging_directory)
    job = jobs.start(body.job_id, lambda j: slskd.fetch(j, body.candidate.candidate_id, directory, body.settings))
    return job.status()


@app.get("/v1/fetch/{job_id}")
def status(job_id: str) -> dict[str, Any]:
    if (job := jobs.get(job_id)) is None:
        raise error(404, "No such job; the plugin may have restarted.", "unknown-job")
    return job.status()


@app.delete("/v1/fetch/{job_id}")
def cancel(job_id: str) -> dict[str, Any]:
    if (job := jobs.get(job_id)) is None:
        raise error(404, "No such job; the plugin may have restarted.", "unknown-job")
    job.cancelled.set()
    if job.state == "queued":
        job.state = "cancelled"
    return job.status()
