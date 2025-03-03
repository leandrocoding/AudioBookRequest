import json
import logging
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import urlencode, urljoin

from aiohttp import ClientResponse, ClientSession
from sqlmodel import Session

from app.internal.models import ProwlarrSource, TorrentSource, UsenetSource
from app.util.cache import SimpleCache, StringConfigCache

logger = logging.getLogger(__name__)


class ProwlarrMisconfigured(ValueError):
    pass


ProwlarrConfigKey = Literal[
    "prowlarr_api_key",
    "prowlarr_base_url",
    "prowlarr_source_ttl",
    "prowlarr_categories",
]


class ProwlarrConfig(StringConfigCache[ProwlarrConfigKey]):
    def raise_if_invalid(self, session: Session):
        if not self.get_base_url(session):
            raise ProwlarrMisconfigured("Prowlarr base url not set")
        if not self.get_api_key(session):
            raise ProwlarrMisconfigured("Prowlarr base url not set")

    def is_valid(self, session: Session) -> bool:
        return (
            self.get_base_url(session) is not None
            and self.get_api_key(session) is not None
        )

    def get_api_key(self, session: Session) -> Optional[str]:
        return self.get(session, "prowlarr_api_key")

    def set_api_key(self, session: Session, api_key: str):
        self.set(session, "prowlarr_api_key", api_key)

    def get_base_url(self, session: Session) -> Optional[str]:
        path = self.get(session, "prowlarr_base_url")
        if path:
            return path.rstrip("/")
        return None

    def set_base_url(self, session: Session, base_url: str):
        self.set(session, "prowlarr_base_url", base_url)

    def get_source_ttl(self, session: Session) -> int:
        return self.get_int(session, "prowlarr_source_ttl", 24 * 60 * 60)

    def set_source_ttl(self, session: Session, source_ttl: int):
        self.set_int(session, "prowlarr_source_ttl", source_ttl)

    def get_categories(self, session: Session) -> list[int]:
        categories = self.get(session, "prowlarr_categories")
        if categories is None:
            return [3030]
        return json.loads(categories)

    def set_categories(self, session: Session, categories: list[int]):
        self.set(session, "prowlarr_categories", json.dumps(categories))


prowlarr_config = ProwlarrConfig()
prowlarr_source_cache = SimpleCache[list[ProwlarrSource]]()


def flush_prowlarr_cache():
    prowlarr_source_cache.flush()


async def start_download(
    session: Session,
    client_session: ClientSession,
    guid: str,
    indexer_id: int,
) -> ClientResponse:
    prowlarr_config.raise_if_invalid(session)
    base_url = prowlarr_config.get_base_url(session)
    api_key = prowlarr_config.get_api_key(session)
    assert base_url is not None and api_key is not None

    url = urljoin(base_url, "/api/v1/search")
    logger.debug("Starting download for %s", guid)
    async with client_session.post(
        url,
        json={"guid": guid, "indexerId": indexer_id},
        headers={"X-Api-Key": api_key},
    ) as response:
        if not response.ok:
            print(response)
            logger.error("Failed to start download for %s: %s", guid, response)
        else:
            logger.debug("Download successfully started for %s", guid)
        return response


async def query_prowlarr(
    session: Session,
    client_session: ClientSession,
    query: Optional[str],
    indexer_ids: Optional[list[int]] = None,
    force_refresh: bool = False,
) -> list[ProwlarrSource]:
    if not query:
        return []

    base_url = prowlarr_config.get_base_url(session)
    api_key = prowlarr_config.get_api_key(session)
    assert base_url is not None and api_key is not None

    if not force_refresh:
        source_ttl = prowlarr_config.get_source_ttl(session)
        cached_sources = prowlarr_source_cache.get(source_ttl, query)
        if cached_sources:
            return cached_sources

    params: dict[str, Any] = {
        "query": query,
        "type": "search",
        "limit": 100,
        "offset": 0,
    }

    if len(x := prowlarr_config.get_categories(session)) > 0:
        params["categories"] = x

    if indexer_ids is not None:
        params["indexerIds"] = indexer_ids

    url = urljoin(base_url, f"/api/v1/search?{urlencode(params, doseq=True)}")

    logger.info("Querying prowlarr: %s", url)

    async with client_session.get(
        url,
        headers={"X-Api-Key": api_key},
    ) as response:
        search_results = await response.json()

    sources: list[ProwlarrSource] = []
    for result in search_results:
        if result["protocol"] not in ["torrent", "usenet"]:
            print("Skipping source with unknown protocol", result["protocol"])
            continue
        if result["protocol"] == "torrent":
            sources.append(
                TorrentSource(
                    protocol="torrent",
                    guid=result["guid"],
                    indexer_id=result["indexerId"],
                    indexer=result["indexer"],
                    title=result["title"],
                    seeders=result.get("seeders", 0),
                    leechers=result.get("leechers", 0),
                    size=result.get("size", 0),
                    info_url=result["infoUrl"],
                    indexer_flags=[x.lower() for x in result.get("indexerFlags", [])],
                    download_url=result.get("downloadUrl"),
                    magnet_url=result.get("magnetUrl"),
                    publish_date=datetime.fromisoformat(result["publishDate"]),
                )
            )
        else:
            sources.append(
                UsenetSource(
                    protocol="usenet",
                    guid=result["guid"],
                    indexer_id=result["indexerId"],
                    indexer=result["indexer"],
                    title=result["title"],
                    grabs=result.get("grabs"),
                    size=result.get("size", 0),
                    info_url=result["infoUrl"],
                    indexer_flags=[x.lower() for x in result.get("indexerFlags", [])],
                    download_url=result.get("downloadUrl"),
                    magnet_url=result.get("magnetUrl"),
                    publish_date=datetime.fromisoformat(result["publishDate"]),
                )
            )

    prowlarr_source_cache.set(sources, query)

    return sources
