from enum import Enum
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_302_FOUND

from annatar import config
from annatar.api.core import streams
from annatar.config import UserConfig
from annatar.debrid.models import StreamLink
from annatar.debrid.providers import DebridService, get_provider
from annatar.debrid.real_debrid_provider import RealDebridProvider
from annatar.stremio import StreamResponse

router = APIRouter()

log = structlog.get_logger(__name__)


class MediaType(str, Enum):
    movie = "movie"
    series = "series"

    def __str__(self):
        return self.value

    @staticmethod
    def all() -> list[str]:
        return [media_type.value for media_type in MediaType]


@router.get("/")
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/configure", status_code=HTTP_302_FOUND)


@router.get("/manifest.json")
async def get_manifst_with_config() -> dict[str, Any]:
    return await get_manifest("")


@router.get("/{b64config:str}/manifest.json")
async def get_manifest(b64config: str) -> dict[str, Any]:
    user_config: UserConfig = config.parse_config(b64config)
    debrid: Optional[DebridService] = get_provider(
        user_config.debrid_service, user_config.debrid_api_key
    )
    app_name: str = config.APP_NAME
    if debrid:
        app_name = f"{app_name} {debrid.short_name()}"
    return {
        "id": config.APP_ID,
        "icon": "https://i.imgur.com/p4V821B.png",
        "version": config.VERSION.removeprefix("v"),
        "catalogs": [],
        "idPrefixes": ["tt"],
        "resources": ["stream"],
        "types": MediaType.all(),
        "name": app_name,
        "description": "Lord of Gifts. Search popular torrent sites and Debrid caches for streamable content.",
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False,
        },
    }


@router.get("/api/v2/hashes/{imdb_id:str}", description="Get hashes for a given IMDB ID")
async def get_hashes(
    imdb_id: Annotated[str, Path(description="IMDB ID", examples=["tt0120737"])],
    limit: Annotated[int, Query(description="Limit results", defualt=10)] = 10,
    season: Annotated[int | None, Query(description="Season", defualt=None)] = None,
    episode: Annotated[int | None, Query(description="Episode", defualt=None)] = None,
) -> dict[str, Any]:
    hashes = await streams.get_hashes(imdb_id=imdb_id, limit=limit, season=season, episode=episode)
    return {
        "hashes": hashes,
    }


@router.get(
    "/rd/{debrid_api_key:str}/{info_hash:str}/{file_id:int}",
    response_model=StreamResponse,
    response_model_exclude_none=True,
)
async def get_rd_stream(
    debrid_api_key: Annotated[str, Path(description="Debrid token")],
    info_hash: Annotated[str, Path(description="Torrent info hash")],
    file_id: Annotated[int, Path(description="ID of the file in the torrent")],
) -> RedirectResponse:
    rd: RealDebridProvider = RealDebridProvider(debrid_api_key)
    stream: Optional[StreamLink] = await rd.get_stream_for_torrent(
        info_hash=info_hash,
        file_id=file_id,
        debrid_token=debrid_api_key,
    )
    if not stream:
        raise HTTPException(status_code=404, detail="No stream found")

    return RedirectResponse(url=stream.url, status_code=HTTP_302_FOUND)


@router.get(
    "/{b64config:str}/stream/{type:str}/{id:str}.json",
    response_model=StreamResponse,
    response_model_exclude_none=True,
)
async def list_streams(
    request: Request,
    type: MediaType,
    id: Annotated[
        str,
        Path(
            title="imdb ID",
            examples=["tt8927938", "tt0108778:5:8"],
            regex=r"tt\d+(:\d:\d)?",
        ),
    ],
    b64config: Annotated[str, Path(description="base64 encoded json blob")],
) -> StreamResponse:
    user_config: UserConfig = config.parse_config(b64config)
    debrid: Optional[DebridService] = get_provider(
        user_config.debrid_service, user_config.debrid_api_key
    )
    if not debrid:
        raise HTTPException(status_code=400, detail="Invalid debrid service")

    imdb_id: str = id.split(":")[0]
    season_episode: list[int] = [int(i) for i in id.split(":")[1:]]
    res: StreamResponse = await streams.search(
        type=type,
        debrid=debrid,
        imdb_id=imdb_id,
        season_episode=season_episode,
        max_results=user_config.max_results,
        indexers=user_config.indexers,
        resolutions=user_config.resolutions,
    )

    for stream in res.streams:
        if stream.url.startswith("/"):
            stream.url = f"{request.url.scheme}://{request.url.netloc}{stream.url}"

    return res
