import re
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable

import structlog
from fastapi import FastAPI, HTTPException, Request
from structlog.contextvars import bind_contextvars, clear_contextvars

from grima import human, jackett, logging
from grima.debrid.models import StreamLink
from grima.debrid.providers import DebridService, get_provider
from grima.jackett_models import SearchQuery
from grima.stremio import Stream, StreamResponse, get_media_info
from grima.torrent import Torrent

app = FastAPI()

request_id = ContextVar("request_id", default="unknown")

logging.init()
log = structlog.get_logger(__name__)


@app.middleware("http")
async def add_request_id(request: Request, call_next: Callable[[Request], Any]):
    request_id.set(request.headers.get("X-Request-ID", str(uuid.uuid4())))
    clear_contextvars()
    bind_contextvars(request_id=request_id.get())
    response: Any = await call_next(request)
    response.headers["X-Request-ID"] = str(request_id.get())
    return response


@app.middleware("http")
async def add_process_time_header(request: Request, call_next: Callable[[Request], Any]):
    ll = log.bind(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        remote=request.client.host if request.client else None,
    )
    ll.info("http_request")

    start_time: datetime = datetime.now()
    response: Any = await call_next(request)
    process_time = datetime.now() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    ll.info("http_response", duration=f"{process_time.total_seconds():.3f}s")
    return response


@app.get("/manifest.json")
async def get_manifest() -> dict[str, Any]:
    return {
        "id": "community.blockloop.grima",
        "icon": "https://i.imgur.com/wEYQYN8.png",
        "version": "0.1.0",
        "catalogs": [],
        "resources": ["stream"],
        "types": ["movie", "series"],
        "name": "Grima",
        "description": "Stremio Grima Addon",
        "behaviorHints": {
            "configurable": "true",
        },
    }


@app.get("/stream/{type:str}/{id:str}.json")
async def search(
    type: str,
    id: str,
    streamService: str,
    jackettUrl: str,
    jackettApiKey: str,
    debridApiKey: str,
    maxResults: int = 5,
) -> StreamResponse:
    if type not in ["movie", "series"]:
        raise HTTPException(
            status_code=400, detail="Invalid type. Valid types are movie and series"
        )
    if not id.startswith("tt"):
        raise HTTPException(status_code=400, detail="Invalid id. Id must be an IMDB id with tt")

    start: datetime = datetime.now()
    imdb_id = id.split(":")[0]
    season_episode: list[int] = [int(i) for i in id.split(":")[1:]]
    log.info("searching for media", type=type, id=id)

    media_info = await get_media_info(id=imdb_id, type=type)
    if not media_info:
        log.error("error getting media info", type=type, id=id)
        return StreamResponse(streams=[], error="Error getting media info")
    log.info("found media info", type=type, id=id, media_info=media_info.model_dump())

    q = SearchQuery(
        name=media_info.name,
        type=type,
        year=re.split(r"\D", (media_info.releaseInfo or ""))[0],
    )

    if type == "series":
        q.season = str(season_episode[0])
        q.episode = str(season_episode[1])

    torrents: list[Torrent] = await jackett.search(
        debrid_api_key=debridApiKey,
        jackett_url=jackettUrl,
        jackett_api_key=jackettApiKey,
        max_results=max(10, maxResults),
        search_query=q,
        imdb=int(imdb_id.replace("tt", "")),
        timeout=60,
    )

    debrid_service: DebridService = get_provider(streamService)

    links: list[StreamLink] = await debrid_service.get_stream_links(
        torrents=torrents,
        debrid_token=debridApiKey,
        season_episode=season_episode,
        max_results=maxResults,
    )

    streams: list[Stream] = [
        Stream(
            title=media_info.name,
            url=link.url,
            name="\n".join(
                [
                    link.name,
                    f"💾{human.bytes(float(link.size))}",
                ]
            ),
        )
        for link in links
        if link
    ]
    log.info(
        "HTTP request duration",
        method="search",
        duration_ms=(datetime.now() - start).microseconds / 1000,
        type=type,
        id=id,
        num_streams=len(streams),
    )
    return StreamResponse(streams=streams)