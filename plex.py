from __future__ import annotations

import re
from dataclasses import dataclass, replace
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


_SEARCH_KINDS = frozenset({"movie", "show"})


def media_type_ru(media_type: str) -> str:
    return "Сериал" if (media_type or "").lower() == "show" else "Фильм"


def _library_field(item: dict) -> Optional[str]:
    for key in ("librarySectionTitle", "librarySectiontitle"):
        raw = item.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def content_kind_ru(
    library_section_title: Optional[str], media_type: str
) -> str:
    """Подпись типа контента по названию секции Plex и type movie/show."""
    lib = (library_section_title or "").strip().lower()
    is_show = (media_type or "movie").lower() == "show"
    if not lib:
        return media_type_ru(media_type)
    if "мультсериал" in lib:
        return "Мультсериал"
    if "мультфильм" in lib:
        return "Мультфильм"
    if "мультики" in lib:
        return "Мультсериал" if is_show else "Мультфильм"
    if any(x in lib for x in ("мульт", "cartoon", "animation", "анимац")):
        return "Мультсериал" if is_show else "Мультфильм"
    if any(
        x in lib
        for x in (
            "сериал",
            "serial",
            "tv show",
            "tv-shows",
            "телесериал",
            "тв-шоу",
            "тв шоу",
        )
    ) or lib in ("shows", "show", "tv"):
        return "Сериал" if is_show else media_type_ru(media_type)
    if any(x in lib for x in ("фильм", "movie", "кино", "cinema", "movies")):
        return "Фильм" if not is_show else "Сериал"
    return media_type_ru(media_type)


@dataclass(frozen=True)
class PlexMovie:
    rating_key: str
    title: str
    year: Optional[int]
    media_type: str = "movie"
    library_section_title: Optional[str] = None

    def kind_ru(self) -> str:
        return content_kind_ru(self.library_section_title, self.media_type)

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


def _title_fold(title: str) -> str:
    t = normalize_title(title)
    t = re.sub(r"[.:,;!?…·\-–—]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Plex hubs/search иногда отвечает 500 на кавычки и управляющие символы в query.
_PLEX_QUERY_UNSAFE_RE = re.compile(r'["\'`\x00-\x08\x0b\x0c\x0e-\x1f\\<>]')
_UNICODE_QUOTES_RE = re.compile(
    "[\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u00ab\u00bb\u2039\u203a]"
)


def sanitize_plex_search_query(query: str) -> str:
    """Очищает строку перед hubs/search: кавычки, управляющие и прочие опасные символы."""
    q = query.replace("\xa0", " ")
    q = _UNICODE_QUOTES_RE.sub("", q).strip()
    if q and q[0] in '"\'`' and (len(q) == 1 or q[-1] != q[0]):
        q = q[1:].lstrip()
    if q and q[-1] in '"\'`' and (len(q) == 1 or q[0] != q[-1]):
        q = q[:-1].rstrip()
    q = _PLEX_QUERY_UNSAFE_RE.sub("", q)
    return re.sub(r"\s+", " ", q).strip()


def plex_search_variants(query: str) -> list[str]:
    """Варианты строки для hubs/search (полное название и укороченные)."""
    primary = sanitize_plex_search_query(query)
    if not primary:
        return []
    variants: list[str] = []

    def add(part: str) -> None:
        s = sanitize_plex_search_query(part)
        if s and s not in variants:
            variants.append(s)

    add(primary)
    for sep in (". ", ": ", " — ", " - ", " – "):
        if sep in primary:
            add(primary.split(sep, 1)[0])
    words = primary.split()
    if len(words) > 4:
        add(" ".join(words[:4]))
    if len(words) > 2:
        add(" ".join(words[:2]))
    return variants


_YEAR_PAREN_RE = re.compile(r"\s*\((\d{4})\)\s*$")
_YEAR_TAIL_RE = re.compile(r"\s+(\d{4})\s*$")
_YEAR_GLUED_RE = re.compile(r"^(.+)(\d{4})$")


def parse_movie_query(text: str) -> tuple[str, Optional[int]]:
    """Извлекает год (скобки, пробел, слитно в конце) и оставшееся название."""
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
    elif (m := _YEAR_TAIL_RE.search(text)):
        year = _film_year(m.group(1))
        if year is not None:
            text = text[: m.start()].strip()
        else:
            year = None
    elif (m := _YEAR_GLUED_RE.match(text)):
        cand_year = _film_year(m.group(2))
        name = m.group(1).strip()
        if cand_year is not None and name and not name[-1].isdigit():
            year = cand_year
            text = name
    return text, year


def plex_search_query(title: str, year: Optional[int]) -> str:
    title = sanitize_plex_search_query(title)
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


def _iter_search_metadata(payload: dict) -> list[dict]:
    container = payload.get("MediaContainer") or payload
    out: list[dict] = []
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
            if kind in ("episode", "season"):
                continue
            if kind == "movie" or (hub_id == "movie" and kind != "show"):
                out.append(item)
            elif kind == "show" or hub_id == "show":
                out.append(item)
    meta = container.get("Metadata") or []
    if isinstance(meta, dict):
        meta = [meta]
    for item in meta:
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").lower()
        if kind in _SEARCH_KINDS:
            out.append(item)
    return out


def _score_match(query_norm: str, title: str) -> int:
    q_fold = _title_fold(query_norm)
    title_norm = normalize_title(title)
    title_fold = _title_fold(title)
    if title_norm == query_norm or (q_fold and title_fold == q_fold):
        return 100
    if query_norm in title_norm or (q_fold and q_fold in title_fold):
        return 80
    if title_norm in query_norm or (title_fold and title_fold in q_fold):
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
        kind = (item.get("type") or "movie").lower()
        if kind not in _SEARCH_KINDS:
            kind = "movie"
        out.append(
            PlexMovie(
                rating_key=rk,
                title=str(title),
                year=_parse_year(item.get("year")),
                media_type=kind,
                library_section_title=_library_field(item),
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
        show_similar = [m for m in similar if m.media_type == "show"]
        if show_similar:
            if len(show_similar) == 1:
                return show_similar, False
            return show_similar, True
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


def _plex_request_headers(token: str, client_identifier: str) -> dict:
    return {
        **_PLEX_HEADERS_BASE,
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": client_identifier,
    }


async def _fetch_library_section_title(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    rating_key: str,
) -> Optional[str]:
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, f"library/metadata/{rating_key}")
    headers = _plex_request_headers(token, client_identifier)
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    container = data.get("MediaContainer") or data
    meta = container.get("Metadata") or []
    if isinstance(meta, dict):
        meta = [meta]
    for item in meta:
        if isinstance(item, dict):
            title = _library_field(item)
            if title:
                return title
    return _library_field(container)


async def enrich_library_titles(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    movies: list[PlexMovie],
) -> list[PlexMovie]:
    out: list[PlexMovie] = []
    for m in movies:
        if m.library_section_title:
            out.append(m)
            continue
        lib = await _fetch_library_section_title(
            session,
            base_url=base_url,
            token=token,
            client_identifier=client_identifier,
            rating_key=m.rating_key,
        )
        out.append(replace(m, library_section_title=lib) if lib else m)
    return out


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
    query = sanitize_plex_search_query(query)
    if not query:
        raise PlexError(
            "Пустой поисковый запрос. Закройте кавычки и укажите название."
        )
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, "hubs/search")
    headers = _plex_request_headers(token, client_identifier)
    params = {"query": query, "limit": str(max(1, min(limit, 50)))}
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 401:
                raise PlexError("Неверный X-Plex-Token (401).", status=401)
            if resp.status == 500:
                raise PlexError(
                    "Plex вернул ошибку 500 при поиске. "
                    "Проверьте название: закройте кавычки, уберите лишние символы.",
                    status=500,
                )
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
    return _iter_search_metadata(data)


def _merge_metadata_items(batch: list[dict], merged: list[dict], seen: set[str]) -> None:
    for item in batch:
        if not isinstance(item, dict):
            continue
        rk = str(item.get("ratingKey") or item.get("ratingkey") or "")
        if not rk or rk in seen:
            continue
        seen.add(rk)
        merged.append(item)


async def _plex_hubs_search_variants(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    client_identifier: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Поиск по вариантам запроса, если Plex не отдаёт результат на полное название."""
    seen: set[str] = set()
    merged: list[dict] = []
    for i, cand in enumerate(plex_search_variants(query)):
        batch = await _plex_hubs_search(
            session,
            base_url=base_url,
            token=token,
            client_identifier=client_identifier,
            query=cand,
            limit=limit,
        )
        _merge_metadata_items(batch, merged, seen)
        if merged and i == 0:
            break
        if merged:
            break
    return merged


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
    items = await _plex_hubs_search_variants(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        query=query,
        limit=limit,
    )
    movies, ambiguous = classify_search_results(
        query, items, requested_year=requested_year
    )
    movies = await enrich_library_titles(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        movies=movies,
    )
    return movies, ambiguous


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
    items = await _plex_hubs_search_variants(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        query=query,
        limit=limit,
    )
    movie = pick_best_movie(query, items, requested_year=requested_year)
    if not movie:
        return None
    enriched = await enrich_library_titles(
        session,
        base_url=base_url,
        token=token,
        client_identifier=client_identifier,
        movies=[movie],
    )
    return enriched[0] if enriched else movie
