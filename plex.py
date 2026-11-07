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


_YEAR_PAREN_RE = re.compile(r"\s*\((\d{4})\)\s*$")
_YEAR_TAIL_RE = re.compile(r"\s+(\d{4})\s*$")


def parse_movie_query(text: str) -> tuple[str, Optional[int]]:
    """Извлекает год (4 цифры в конце или в скобках) и оставшееся название."""
    text = text.strip()
    year: Optional[int] = None
    def _film_year(raw: str) -> Optional[int]:
        y = _parse_year(raw)
        if y is not None and 1888 <= y <= 2100:
            return y
        return None

    m = _YEAR_PAREN_RE.search(text)
    if m:
        year = _film_year(m.group(1))
        if year is not None:
            text = text[: m.start()].strip()
        else:
            year = None
    else:
        m = _YEAR_TAIL_RE.search(text)
        if m:
            year = _film_year(m.group(1))
            if year is not None:
                text = text[: m.start()].strip()
            else:
                year = None
    return text, year


def plex_search_query(title: str, year: Optional[int]) -> str:
    title = title.strip()
    if year is None:
        return title
    return f"{title} {year}"


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


def _score_movie(
    query_norm: str, movie: PlexMovie, requested_year: Optional[int]
) -> int:
    score = _score_match(query_norm, movie.title)
    if requested_year is not None and movie.year is not None:
        if movie.year == requested_year:
            score += 25
        else:
            score -= 10
    return score


def _movies_from_items(items: list[dict]) -> list[PlexMovie]:
    out: list[PlexMovie] = []
    seen: set[str] = set()
    for item in items:
        title = item.get("title") or item.get("originalTitle")
        rating_key = item.get("ratingKey") or item.get("ratingkey")
        if not title or not rating_key:
            continue
        rk = str(rating_key)
        if rk in seen:
            continue
        seen.add(rk)
        out.append(
            PlexMovie(
                rating_key=rk,
                title=str(title),
                year=_parse_year(item.get("year")),
            )
        )
    return out


def classify_search_results(
    query: str,
    items: list[dict],
    *,
    requested_year: Optional[int] = None,
    min_similar: int = 70,
) -> tuple[list[PlexMovie], bool]:
    """(список для ответа, True если несколько похожих — показывать таблицу)."""
    if not items:
        return [], False
    q = normalize_title(query)
    scored: list[tuple[int, PlexMovie]] = []
    for movie in _movies_from_items(items):
        score = _score_movie(q, movie, requested_year)
        if score < 10:
            continue
        scored.append((score, movie))
    scored.sort(key=lambda x: (-x[0], x[1].title.lower()))
    if not scored:
        return [], False

    similar = [m for s, m in scored if s >= min_similar]
    if requested_year is not None:
        year_hits = [m for m in similar if m.year == requested_year]
        if len(year_hits) == 1:
            return year_hits, False
        if len(year_hits) > 1:
            return year_hits, True
        # Год задан, но среди похожих нет фильма с этим годом
        return [], False

    if len(similar) > 1:
        return similar, True
    if len(similar) == 1:
        return similar, False
    return [scored[0][1]], False


def pick_best_movie(
    query: str, items: list[dict], *, requested_year: Optional[int] = None
) -> Optional[PlexMovie]:
    movies, _ambiguous = classify_search_results(
        query, items, requested_year=requested_year
    )
    return movies[0] if movies else None


async def _plex_hubs_search(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
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
    return _iter_movie_metadata(data)


async def search_movie_items(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    query: str,
    limit: int = 10,
    requested_year: Optional[int] = None,
) -> tuple[list[PlexMovie], bool]:
    items = await _plex_hubs_search(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        query=query,
        limit=limit,
    )
    return classify_search_results(query, items, requested_year=requested_year)


async def search_movie(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    query: str,
    limit: int = 10,
    requested_year: Optional[int] = None,
) -> Optional[PlexMovie]:
    items = await _plex_hubs_search(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        query=query,
        limit=limit,
    )
    return pick_best_movie(query, items, requested_year=requested_year)
