from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp

_PLEX_HEADERS_BASE = {
    "Accept": "application/json",
    "X-Plex-Product": "maubot-plex-zapros",
    "X-Plex-Version": "1.0.0",
}


class PlexError(Exception):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PlexMovie:
    rating_key: str
    title: str
    year: Optional[int]

    def web_link(self, base_url: str) -> str:
        key = f"/library/metadata/{self.rating_key}"
        return (
            "https://app.plex.tv/desktop/#!/server/details"
            f"?key={key}&machineIdentifier=local"
        )

    def server_link(self, base_url: str) -> str:
        base = base_url.rstrip("/")
        return f"{base}/web/index.html#!/server/details?key=%2Flibrary%2Fmetadata%2F{self.rating_key}"


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _parse_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_movie_metadata(payload: dict) -> list[dict]:
    container = payload.get("MediaContainer") or payload
    movies: list[dict] = []
    hubs = container.get("Hub") or container.get("hub") or []
    if isinstance(hubs, dict):
        hubs = [hubs]
    for hub in hubs:
        if not isinstance(hub, dict):
            continue
        hub_id = (hub.get("hubIdentifier") or hub.get("type") or "").lower()
        meta = hub.get("Metadata") or hub.get("metadata") or []
        if isinstance(meta, dict):
            meta = [meta]
        for item in meta:
            if not isinstance(item, dict):
                continue
            kind = (item.get("type") or "").lower()
            if kind == "movie" or hub_id == "movie":
                movies.append(item)
    # Flat search fallback
    meta = container.get("Metadata") or []
    if isinstance(meta, dict):
        meta = [meta]
    for item in meta:
        if isinstance(item, dict) and (item.get("type") or "").lower() == "movie":
            movies.append(item)
    return movies


def _score_match(query_norm: str, title: str) -> int:
    title_norm = normalize_title(title)
    if title_norm == query_norm:
        return 100
    if query_norm in title_norm:
        return 80
    if title_norm in query_norm:
        return 70
    return 10


def pick_best_movie(query: str, items: list[dict]) -> Optional[PlexMovie]:
    if not items:
        return None
    q = normalize_title(query)
    best: Optional[tuple[int, PlexMovie]] = None
    for item in items:
        title = item.get("title") or item.get("originalTitle")
        rating_key = item.get("ratingKey") or item.get("ratingkey")
        if not title or not rating_key:
            continue
        movie = PlexMovie(
            rating_key=str(rating_key),
            title=str(title),
            year=_parse_year(item.get("year")),
        )
        score = _score_match(q, movie.title)
        if best is None or score > best[0]:
            best = (score, movie)
    if best and best[0] >= 70:
        return best[1]
    if best and best[0] >= 10:
        return best[1]
    return None


async def search_movie(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    query: str,
    limit: int = 10,
) -> Optional[PlexMovie]:
    if not token:
        raise PlexError("Plex token не задан в config плагина.")
    if not query.strip():
        raise PlexError("Пустой поисковый запрос.")
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, "hubs/search")
    headers = {
        **_PLEX_HEADERS_BASE,
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": client_identifier,
    }
    params = {"query": query.strip(), "limit": str(max(1, min(limit, 50)))}
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 401:
                raise PlexError("Неверный X-Plex-Token (401).", status=401)
            if resp.status >= 400:
                text = await resp.text()
                raise PlexError(
                    f"Plex вернул HTTP {resp.status}: {text[:200]}", status=resp.status
                )
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        raise PlexError(f"Ошибка сети при обращении к Plex: {e}") from e

    if not isinstance(data, dict):
        raise PlexError("Неожиданный ответ Plex (не JSON-объект).")
    items = _iter_movie_metadata(data)
    return pick_best_movie(query, items)
