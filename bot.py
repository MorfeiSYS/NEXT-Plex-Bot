from __future__ import annotations

import asyncio
import inspect
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional, Type

import aiohttp
from maubot import Plugin, MessageEvent
from maubot.handlers import event
from maubot.matrix import parse_formatted
from mautrix.errors.request import MLimitExceeded
from mautrix.types import (
    EventID,
    EventType,
    Format,
    MessageType,
    ReactionEvent,
    RoomCreatePreset,
    TextMessageEventContent,
)
from mautrix.util.async_db import Scheme, UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from plex import (
    PlexError,
    PlexMovie,
    normalize_title,
    parse_movie_query,
    sanitize_plex_search_query,
    search_movie,
    search_movie_items,
)

upgrade_table = UpgradeTable()
_TITLE_QUOTED_RE = re.compile(r'^"([^"]+)"\s*$|\'([^\']+)\'\s*$')
_TITLE_QUOTED_YEAR_SUFFIX_RE = re.compile(
    r'^["\']([^"\']+)["\'](\s*\(\d{4}\)|\s+\d{4})\s*$'
)
_ZAPROS_DUP_RE = re.compile(r"!zapros\b", re.IGNORECASE)
_ZAPROS_MSG_RE = re.compile(r"^!zapros(?:\s+([\s\S]*))?\s*$", re.IGNORECASE)
_MATRIX_SPACE_RE = re.compile(r"[\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000]+")
_PLEX_SESSION_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
_DB_DISPLAY_UNSAFE_RE = re.compile(r"<[^>]*>")
_DB_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PLUGIN_ID = "NEXT.plex-zapros"
_maubot_db_ui_revert_done = False
_MOD_ACCEPT_KEYS = frozenset({"✅", "✅️", "☑️", "👍", "👍️", "white_check_mark"})
_MOD_REJECT_KEYS = frozenset({"⛔", "⛔️", "🚫", "no_entry", "👎", "👎️"})
_MOD_CMD_RE = re.compile(
    r"^(accept|approve|reject|deny)\s+(\d+)\s*$", re.IGNORECASE
)


def _normalize_matrix_spaces(text: str) -> str:
    return _MATRIX_SPACE_RE.sub(" ", text)


def _restore_maubot_database_ui() -> None:
    """Сброс патча UI Database из v1.0.30 (остаётся в процессе maubot до перезапуска)."""
    global _maubot_db_ui_revert_done
    if _maubot_db_ui_revert_done:
        return
    try:
        from maubot.instance import PluginInstance
        import maubot.management.api.instance_database as idb
        from maubot.lib.optionalalchemy import Engine
    except ImportError:
        return
    _maubot_db_ui_revert_done = True

    async def get_db_tables(self):  # type: ignore[no-untyped-def]
        if self.inst_db_tables is None:
            if isinstance(self.inst_db, Engine):
                self.inst_db_tables = self._introspect_sqlalchemy()
            elif self.inst_db.scheme == Scheme.SQLITE:
                self.inst_db_tables = await self._introspect_sqlite()
            else:
                self.inst_db_tables = await self._introspect_postgres()
        return self.inst_db_tables

    PluginInstance.get_db_tables = get_db_tables  # type: ignore[method-assign]

    q = idb.query
    closure = getattr(q, "__closure__", None)
    if closure:
        for cell in closure:
            fn = cell.cell_contents
            if (
                fn is not q
                and inspect.iscoroutinefunction(fn)
                and getattr(fn, "__module__", "").endswith("instance_database")
            ):
                idb.query = fn  # type: ignore[method-assign]
                break

    for inst in PluginInstance.cache.values():
        if inst.type == _PLUGIN_ID:
            inst.inst_db_tables = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _db_now() -> str:
    return _utc_now().isoformat()


def _iso_ts(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _status_ru(status: str) -> str:
    if status == "found":
        return "найден"
    if status == "pending":
        return "ожидает"
    if status == "awaiting_approval":
        return "на модерации"
    return status


def _normalize_reaction_key(key: str) -> str:
    return re.sub(r"[\ufe0e\ufe0f\u200d]", "", (key or "").strip().lower())


def _moderation_action_from_reaction(key: str) -> Optional[str]:
    raw = _normalize_reaction_key(key)
    if not raw:
        return None
    if raw in _MOD_ACCEPT_KEYS or raw.startswith("✅") or raw.startswith("☑"):
        return "accept"
    if raw in _MOD_REJECT_KEYS or raw.startswith("⛔") or raw.startswith("🚫"):
        return "reject"
    low = raw.lower()
    if "white_check_mark" in low:
        return "accept"
    if "no_entry" in low:
        return "reject"
    return None


def _parse_utc_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sanitize_db_display(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    text = _DB_CONTROL_CHARS_RE.sub("", text)
    text = _DB_DISPLAY_UNSAFE_RE.sub("", text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "")
        .replace(">", "")
        .replace('"', "&quot;")
    )


def _fmt_ts_short(value, offset_hours: float = 0) -> str:
    if value is None:
        return "—"
    dt = _parse_utc_ts(value)
    if dt is None:
        text = str(value)
        if len(text) >= 16:
            return text[:16].replace("T", " ")
        return text
    shifted = dt + timedelta(hours=offset_hours)
    return shifted.strftime("%Y-%m-%d %H:%M")


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS movie_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                requester_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                found_at TEXT,
                plex_rating_key TEXT,
                plex_title TEXT,
                plex_year INTEGER,
                last_check_message TEXT
            )
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS movie_requests (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                requester_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL,
                last_checked_at TIMESTAMPTZ,
                found_at TIMESTAMPTZ,
                plex_rating_key TEXT,
                plex_title TEXT,
                plex_year INTEGER,
                last_check_message TEXT
            )
            """
        )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS movie_requests_pending_uq
        ON movie_requests (title_norm, requester_id)
        WHERE status = 'pending'
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS movie_requests_status_idx
        ON movie_requests (status)
        """
    )


@upgrade_table.register(description="Index created_at for retention purge")
async def upgrade_v2(conn, scheme: Scheme) -> None:
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS movie_requests_created_at_idx
        ON movie_requests (created_at)
        """
    )


@upgrade_table.register(description="Daily !zapros usage counters")
async def upgrade_v3(conn, scheme: Scheme) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zapros_daily_usage (
            requester_id TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (requester_id, usage_date)
        )
        """
    )


@upgrade_table.register(description="Daily found-on-Plex usage counter column")
async def upgrade_v4(conn, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        rows = await conn.fetch("PRAGMA table_info(zapros_daily_usage)")
        if not any(str(r["name"]) == "found_request_count" for r in rows):
            await conn.execute(
                """
                ALTER TABLE zapros_daily_usage
                ADD COLUMN found_request_count INTEGER NOT NULL DEFAULT 0
                """
            )
    else:
        await conn.execute(
            """
            ALTER TABLE zapros_daily_usage
            ADD COLUMN IF NOT EXISTS found_request_count INTEGER NOT NULL DEFAULT 0
            """
        )


@upgrade_table.register(description="times_requested counter on movie_requests")
async def upgrade_v5(conn, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        rows = await conn.fetch("PRAGMA table_info(movie_requests)")
        if not any(str(r["name"]) == "times_requested" for r in rows):
            await conn.execute(
                """
                ALTER TABLE movie_requests
                ADD COLUMN times_requested INTEGER NOT NULL DEFAULT 1
                """
            )
    else:
        await conn.execute(
            """
            ALTER TABLE movie_requests
            ADD COLUMN IF NOT EXISTS times_requested INTEGER NOT NULL DEFAULT 1
            """
        )


@upgrade_table.register(description="Moderation notify map and queue index")
async def upgrade_v6(conn, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderation_notify_map (
                event_id TEXT PRIMARY KEY,
                request_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute("DROP INDEX IF EXISTS movie_requests_pending_uq")
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS movie_requests_pending_uq
            ON movie_requests (title_norm, requester_id)
            WHERE status IN ('pending', 'awaiting_approval')
            """
        )
    else:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderation_notify_map (
                event_id TEXT PRIMARY KEY,
                request_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await conn.execute("DROP INDEX IF EXISTS movie_requests_pending_uq")
        await conn.execute(
            """
            CREATE UNIQUE INDEX movie_requests_pending_uq
            ON movie_requests (title_norm, requester_id)
            WHERE status IN ('pending', 'awaiting_approval')
            """
        )


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("plex")
        helper.copy("notify_users")
        helper.copy("reply_mode")
        helper.copy("notify_mode")
        helper.copy("notify_room")
        helper.copy("retention_days")
        helper.copy("poll_interval_hours")
        helper.copy("poll_startup_seconds")
        helper.copy("poll_between_pending_seconds")
        helper.copy("search_limit")
        helper.copy("search_results_max")
        helper.copy("timezone_offset_hours")
        helper.copy("title_max_length")
        helper.copy("list_per_user_max")
        helper.copy("list_users_max")
        helper.copy("daily_requests_per_user_max")
        helper.copy("daily_found_requests_per_user_max")
        helper.copy("matrix_send_pause_seconds")
        for key in ("queue_summary", "movie_requests"):
            if key in self:
                helper.base[key] = self[key]
            elif key not in helper.base:
                helper.base[key] = (
                    {"updated_at": None, "pending_count": 0, "items": []}
                    if key == "queue_summary"
                    else []
                )


class PlexZaprosBot(Plugin):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._poll_task: Optional[asyncio.Task] = None
        self._plex_session: Optional[aiohttp.ClientSession] = None
        self._interactive_plex: int = 0
        self._interactive_plex_lock = asyncio.Lock()
        self._dm_rooms: dict[str, str] = {}
        self._room_labels: dict[str, str] = {}
        self._matrix_send_lock = asyncio.Lock()
        self._last_matrix_send: float = 0.0

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def pre_start(self) -> None:
        _restore_maubot_database_ui()

    async def start(self) -> None:
        self.config.load_and_update()
        try:
            await self._purge_old_requests()
        except Exception as e:
            self.log.warning(f"Очистка БД при старте не удалась: {e}")
        try:
            await self.refresh_queue_summary()
        except Exception as e:
            self.log.warning(f"Не удалось обновить снимок очереди: {e}")
        self._plex_session = aiohttp.ClientSession(
            loop=self.loop,
            timeout=_PLEX_SESSION_TIMEOUT,
            connector=aiohttp.TCPConnector(limit=8),
            headers={"Accept": "application/json"},
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._plex_session and not self._plex_session.closed:
            await self._plex_session.close()
        self._plex_session = None

    def _conf(self, key: str, default):
        return self.config.get(key, default)

    def _plex_http(self) -> aiohttp.ClientSession:
        if self._plex_session and not self._plex_session.closed:
            return self._plex_session
        return self.http

    async def _wait_background_plex_idle(self) -> None:
        """Фоновый опрос ждёт, пока не завершится интерактивный !zapros."""
        while True:
            async with self._interactive_plex_lock:
                if self._interactive_plex == 0:
                    return
            await asyncio.sleep(0.2)

    @asynccontextmanager
    async def _interactive_plex_guard(self) -> AsyncIterator[None]:
        async with self._interactive_plex_lock:
            self._interactive_plex += 1
        try:
            yield
        finally:
            async with self._interactive_plex_lock:
                self._interactive_plex -= 1

    def _timezone_offset_hours(self) -> float:
        try:
            return float(self._conf("timezone_offset_hours", 0))
        except (TypeError, ValueError):
            return 0.0

    def _plex_str(self, key: str, default: str) -> str:
        plex = self._conf("plex", {})
        if not plex:
            return default
        val = plex.get(key, default)
        if val is None or val == "":
            return default
        return str(val)

    def _notify_users(self) -> list[str]:
        users = self._conf("notify_users", [])
        out: list[str] = []
        for u in users:
            s = str(u).strip()
            if s:
                out.append(s)
        return out

    def _notify_mode(self) -> str:
        explicit = self._conf("notify_mode", None)
        if explicit is not None and str(explicit).strip():
            return str(explicit).lower()
        return str(self._conf("reply_mode", "room")).lower()

    def _is_operator(self, evt: MessageEvent) -> bool:
        return str(evt.sender) in self._notify_users()

    def _is_operator_mxid(self, mxid: str) -> bool:
        return str(mxid) in self._notify_users()

    def _matrix_send_pause_seconds(self) -> float:
        try:
            return max(0.1, float(self._conf("matrix_send_pause_seconds", 0.55)))
        except (TypeError, ValueError):
            return 0.55

    async def _client_send_text_ratelimited(self, room_id: str, text: str):
        pause = self._matrix_send_pause_seconds()
        last_err: Optional[Exception] = None
        async with self._matrix_send_lock:
            for attempt in range(8):
                gap = pause - (self.loop.time() - self._last_matrix_send)
                if gap > 0:
                    await asyncio.sleep(gap)
                try:
                    event_id = await self.client.send_text(str(room_id), text)
                    self._last_matrix_send = self.loop.time()
                    return event_id
                except MLimitExceeded as e:
                    last_err = e
                    await asyncio.sleep(min(4.0, max(1.0, pause) * (attempt + 1)))
            if last_err:
                raise last_err
            raise RuntimeError("send_text failed")

    async def _respond_ratelimited(self, evt: MessageEvent, text: str) -> None:
        pause = self._matrix_send_pause_seconds()
        last_err: Optional[Exception] = None
        async with self._matrix_send_lock:
            for attempt in range(8):
                gap = pause - (self.loop.time() - self._last_matrix_send)
                if gap > 0:
                    await asyncio.sleep(gap)
                try:
                    await evt.respond(text)
                    self._last_matrix_send = self.loop.time()
                    return
                except MLimitExceeded as e:
                    last_err = e
                    await asyncio.sleep(min(4.0, max(1.0, pause) * (attempt + 1)))
            if last_err:
                raise last_err

    async def _client_react_ratelimited(
        self, room_id: str, event_id: str, key: str
    ) -> None:
        pause = self._matrix_send_pause_seconds()
        last_err: Optional[Exception] = None
        async with self._matrix_send_lock:
            for attempt in range(8):
                gap = pause - (self.loop.time() - self._last_matrix_send)
                if gap > 0:
                    await asyncio.sleep(gap)
                try:
                    await self.client.react(str(room_id), EventID(event_id), key)
                    self._last_matrix_send = self.loop.time()
                    return
                except MLimitExceeded as e:
                    last_err = e
                    await asyncio.sleep(min(4.0, max(1.0, pause) * (attempt + 1)))
            if last_err:
                raise last_err

    async def _request_row(self, request_id: int) -> Optional[dict]:
        return await self.database.fetchrow(
            "SELECT * FROM movie_requests WHERE id=$1", request_id
        )

    async def _register_moderation_notify(
        self,
        event_id: str,
        request_id: int,
        room_id: str,
        kind: str,
    ) -> None:
        await self.database.execute(
            "DELETE FROM moderation_notify_map WHERE event_id=$1", event_id
        )
        await self.database.execute(
            """
            INSERT INTO moderation_notify_map
            (event_id, request_id, room_id, kind, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            event_id,
            request_id,
            room_id,
            kind,
            _db_now(),
        )

    async def _lookup_moderation_notify(self, event_id: str) -> Optional[dict]:
        return await self.database.fetchrow(
            "SELECT * FROM moderation_notify_map WHERE event_id=$1", event_id
        )

    async def _send_moderation_card(
        self, room_id: str, body: str, request_id: int, kind: str
    ) -> Optional[str]:
        rid = str(room_id)
        text = body.rstrip()
        try:
            event_id = await self._client_send_text_ratelimited(rid, text)
        except Exception as e:
            self.log.error(f"Не удалось отправить карточку модерации в {rid}: {e}")
            return None
        eid = str(event_id)
        await self._register_moderation_notify(eid, request_id, rid, kind)
        for emoji in ("✅", "⛔"):
            try:
                await self._client_react_ratelimited(rid, eid, emoji)
            except Exception as e:
                self.log.warning(
                    f"Не удалось поставить реакцию {emoji!r} на {eid}: {e}"
                )
        return eid

    async def _moderation_reply(
        self,
        text: str,
        reply_evt: Optional[MessageEvent] = None,
        room_id: Optional[str] = None,
    ) -> None:
        if reply_evt is not None:
            await self._respond_ratelimited(reply_evt, text)
            return
        if room_id:
            await self._client_send_text_ratelimited(str(room_id), text)

    async def _moderation_accept(
        self,
        request_id: int,
        operator: str,
        reply_evt: Optional[MessageEvent] = None,
        room_id: Optional[str] = None,
    ) -> None:
        if not self._is_operator_mxid(operator):
            await self._moderation_reply(
                "Модерация доступна только операторам.", reply_evt, room_id
            )
            return
        row = await self._request_row(request_id)
        if not row:
            await self._moderation_reply(
                f"Запрос #{request_id} не найден (уже обработан).",
                reply_evt,
                room_id,
            )
            return
        status = str(row["status"])
        title = str(row["title"])
        requester = str(row["requester_id"])
        req_room = str(row["room_id"])
        if status == "awaiting_approval":
            now = _db_now()
            await self.database.execute(
                """
                UPDATE movie_requests
                SET status='pending', last_checked_at=$1
                WHERE id=$2 AND status='awaiting_approval'
                """,
                now,
                request_id,
            )
            await self._deliver(
                requester,
                req_room,
                f"✅ Ваш запрос #{request_id} «{title}» одобрён; "
                f"повторная проверка Plex каждые "
                f"{self._conf('poll_interval_hours', 6)} ч.",
            )
            await self.refresh_queue_summary()
            await self._moderation_reply(
                f"Запрос #{request_id} «{title}» принят оператором {operator}.",
                reply_evt,
                room_id,
            )
            return
        if status == "pending":
            await self._deliver(
                requester,
                req_room,
                f"✅ Запрос #{request_id} «{title}» в очереди на добавление.",
            )
            await self._moderation_reply(
                f"Запрос #{request_id} уже в очереди (подтверждение отправлено).",
                reply_evt,
                room_id,
            )
            return
        await self._moderation_reply(
            f"Запрос #{request_id} в статусе «{_status_ru(status)}», "
            "принятие недоступно.",
            reply_evt,
            room_id,
        )

    async def _moderation_reject(
        self,
        request_id: int,
        operator: str,
        reply_evt: Optional[MessageEvent] = None,
        room_id: Optional[str] = None,
    ) -> None:
        if not self._is_operator_mxid(operator):
            await self._moderation_reply(
                "Модерация доступна только операторам.", reply_evt, room_id
            )
            return
        row = await self._request_row(request_id)
        if not row:
            await self._moderation_reply(
                f"Запрос #{request_id} не найден (уже обработан).",
                reply_evt,
                room_id,
            )
            return
        status = str(row["status"])
        if status not in ("awaiting_approval", "pending"):
            await self._moderation_reply(
                f"Запрос #{request_id} нельзя отклонить "
                f"(статус «{_status_ru(status)}»).",
                reply_evt,
                room_id,
            )
            return
        title = str(row["title"])
        requester = str(row["requester_id"])
        req_room = str(row["room_id"])
        await self.database.execute(
            "DELETE FROM moderation_notify_map WHERE request_id=$1", request_id
        )
        await self.database.execute(
            "DELETE FROM movie_requests WHERE id=$1", request_id
        )
        await self._deliver(
            requester,
            req_room,
            f"⛔️ Ваш запрос #{request_id} «{title}» отклонён оператором.",
        )
        await self.refresh_queue_summary()
        await self._moderation_reply(
            f"Запрос #{request_id} «{title}» отклонён оператором {operator}.",
            reply_evt,
            room_id,
        )

    def _parse_movie_title(self, body: str) -> Optional[str]:
        body = body.strip()
        if not body:
            return None
        m = _TITLE_QUOTED_YEAR_SUFFIX_RE.match(body)
        if m:
            title = f"{m.group(1).strip()}{m.group(2)}"
            return sanitize_plex_search_query(title) or None
        m = _TITLE_QUOTED_RE.match(body)
        if m:
            title = (m.group(1) or m.group(2) or "").strip()
            return sanitize_plex_search_query(title) or None
        if body.lower().startswith("list"):
            return None
        return sanitize_plex_search_query(body) or None

    def _search_results_max(self) -> int:
        try:
            return max(1, min(50, int(self._conf("search_results_max", 5))))
        except (TypeError, ValueError):
            return 5

    def _title_max_length(self) -> int:
        try:
            n = int(self._conf("title_max_length", 100))
        except (TypeError, ValueError):
            n = 100
        return max(10, min(100, n))

    def _list_per_user_max(self) -> int:
        try:
            n = int(self._conf("list_per_user_max", 5))
        except (TypeError, ValueError):
            n = 5
        return max(1, min(50, n))

    def _list_users_max(self) -> int:
        try:
            n = int(self._conf("list_users_max", 10))
        except (TypeError, ValueError):
            n = 10
        return max(1, min(50, n))

    def _daily_requests_per_user_max(self) -> int:
        try:
            n = int(self._conf("daily_requests_per_user_max", 0))
        except (TypeError, ValueError):
            n = 0
        return max(0, min(100, n))

    def _daily_found_requests_per_user_max(self) -> int:
        try:
            n = int(self._conf("daily_found_requests_per_user_max", 1000))
        except (TypeError, ValueError):
            n = 1000
        return max(0, min(1000, n))

    def _local_usage_date(self) -> str:
        local = _utc_now() + timedelta(hours=self._timezone_offset_hours())
        return local.strftime("%Y-%m-%d")

    async def _has_pending_duplicate(self, title: str, requester: str) -> bool:
        row = await self.database.fetchrow(
            """
            SELECT 1 FROM movie_requests
            WHERE title_norm=$1 AND requester_id=$2
              AND status IN ('pending', 'awaiting_approval')
            """,
            normalize_title(title),
            requester,
        )
        return row is not None

    async def _try_consume_pending_daily_quota(
        self, requester_id: str
    ) -> Optional[str]:
        limit = self._daily_requests_per_user_max()
        if limit <= 0:
            return None
        return await self._increment_daily_usage(
            requester_id,
            pending=True,
            limit=limit,
            over_msg=(
                f"Превышен суточный лимит запросов на добавление ({limit} в сутки). "
                "Попробуйте завтра."
            ),
        )

    async def _try_consume_found_daily_quota(
        self, requester_id: str
    ) -> Optional[str]:
        limit = self._daily_found_requests_per_user_max()
        if limit <= 0:
            return None
        return await self._increment_daily_usage(
            requester_id,
            pending=False,
            limit=limit,
            over_msg=(
                f"Превышен суточный лимит запросов с найденным на Plex "
                f"({limit} в сутки). Попробуйте завтра."
            ),
        )

    async def _increment_daily_usage(
        self,
        requester_id: str,
        *,
        pending: bool,
        limit: int,
        over_msg: str,
    ) -> Optional[str]:
        usage_date = self._local_usage_date()
        row = await self.database.fetchrow(
            """
            SELECT request_count, found_request_count FROM zapros_daily_usage
            WHERE requester_id=$1 AND usage_date=$2
            """,
            requester_id,
            usage_date,
        )
        if row:
            pending_n = int(row["request_count"])
            found_n = int(row["found_request_count"])
        else:
            pending_n = found_n = 0
        current = pending_n if pending else found_n
        if current >= limit:
            return over_msg
        if row:
            if pending:
                await self.database.execute(
                    """
                    UPDATE zapros_daily_usage
                    SET request_count = request_count + 1
                    WHERE requester_id=$1 AND usage_date=$2
                    """,
                    requester_id,
                    usage_date,
                )
            else:
                await self.database.execute(
                    """
                    UPDATE zapros_daily_usage
                    SET found_request_count = found_request_count + 1
                    WHERE requester_id=$1 AND usage_date=$2
                    """,
                    requester_id,
                    usage_date,
                )
        else:
            pending_init = 1 if pending else 0
            found_init = 0 if pending else 1
            await self.database.execute(
                """
                INSERT INTO zapros_daily_usage
                (requester_id, usage_date, request_count, found_request_count)
                VALUES ($1, $2, $3, $4)
                """,
                requester_id,
                usage_date,
                pending_init,
                found_init,
            )
        return None

    def _format_movies_table(self, movies: list[PlexMovie], requested: str) -> str:
        lines = [
            f"Найдено несколько похожих фильмов/сериалов для «{requested}»:",
            "",
        ]
        for i, m in enumerate(movies, 1):
            year = str(m.year) if m.year else "—"
            lines.append(f"{i}. [{m.kind_ru()}] {m.title} ({year})")
        lines.extend(
            [
                "",
                'Уточните запрос, напр.: !zapros "Матрица 1999" или "Матрица (1999)".',
            ]
        )
        return "\n".join(lines)

    def _format_found(self, movie: PlexMovie, requested: str) -> str:
        year = f" ({movie.year})" if movie.year else ""
        return (
            f"✅ {movie.kind_ru()} «{movie.title}»{year} найден на Plex "
            f"(запрос: «{requested}»)."
        )

    def _format_found_later(self, movie: PlexMovie, requested: str) -> str:
        year = f" ({movie.year})" if movie.year else ""
        return (
            f"✅ Запрошенный контент «{requested}» теперь есть на Plex: "
            f"{movie.kind_ru()} «{movie.title}»{year}."
        )

    async def _record_found(
        self,
        title: str,
        requester: str,
        room_id: str,
        movie: PlexMovie,
        check_message: str,
    ) -> None:
        """Повтор title+пользователь — обновление строки и times_requested, без нового id."""
        title_norm = normalize_title(title)
        safe_title = _sanitize_db_display(title) or title
        safe_plex_title = _sanitize_db_display(movie.title) or movie.title
        safe_msg = _sanitize_db_display(check_message) or check_message
        now = _db_now()
        row = await self.database.fetchrow(
            """
            SELECT id FROM movie_requests
            WHERE title_norm=$1 AND requester_id=$2
              AND status IN ('pending', 'found')
            ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            title_norm,
            requester,
        )
        if row:
            await self.database.execute(
                """
                UPDATE movie_requests
                SET status='found', found_at=$1, last_checked_at=$1,
                    room_id=$2, title=$3,
                    plex_rating_key=$4, plex_title=$5, plex_year=$6,
                    last_check_message=$7,
                    times_requested = times_requested + 1
                WHERE id=$8
                """,
                now,
                room_id,
                safe_title,
                movie.rating_key,
                safe_plex_title,
                movie.year,
                safe_msg,
                row["id"],
            )
            return
        await self.database.execute(
            """
            INSERT INTO movie_requests (
                title, title_norm, requester_id, room_id, status,
                created_at, found_at, last_checked_at,
                plex_rating_key, plex_title, plex_year, last_check_message,
                times_requested
            ) VALUES ($1,$2,$3,$4,'found',$5,$5,$5,$6,$7,$8,$9,1)
            """,
            safe_title,
            title_norm,
            requester,
            room_id,
            now,
            movie.rating_key,
            safe_plex_title,
            movie.year,
            safe_msg,
        )

    def _plex_search_kwargs(self, title: str) -> dict:
        name, year = parse_movie_query(title)
        limit = int(self._conf("search_limit", 10))
        # Plex hubs/search надёжнее по одному названию; год — при выборе из выдачи
        return dict(
            session=self._plex_http(),
            base_url=self._plex_str("base_url", "http://127.0.0.1:32400"),
            token=self._plex_str("token", ""),
            client_identifier=self._plex_str(
                "client_identifier", "maubot-plex-zapros"
            ),
            query=name,
            limit=limit,
            requested_year=year,
        )

    async def _search_plex(self, title: str) -> Optional[PlexMovie]:
        return await search_movie(**self._plex_search_kwargs(title))

    async def _search_plex_interactive(
        self, title: str
    ) -> tuple[list[PlexMovie], bool]:
        movies, ambiguous = await search_movie_items(**self._plex_search_kwargs(title))
        if ambiguous:
            movies = movies[: self._search_results_max()]
        return movies, ambiguous

    async def _is_direct_with(self, user_id: str, room_id: str) -> bool:
        bot = str(self.client.mxid)
        try:
            members = await self.client.get_joined_members(room_id)
        except Exception:
            return False
        mxids = {str(m) for m in members}
        return len(mxids) == 2 and user_id in mxids and bot in mxids

    async def _room_human_label(self, room_id: str) -> str:
        rid = str(room_id)
        cached = self._room_labels.get(rid)
        if cached:
            return cached
        name: Optional[str] = None
        alias: Optional[str] = None
        try:
            ev = await self.client.get_state_event(rid, EventType.ROOM_NAME)
            if ev.content and ev.content.name:
                name = str(ev.content.name).strip()
        except Exception:
            pass
        try:
            ev = await self.client.get_state_event(rid, EventType.ROOM_CANONICAL_ALIAS)
            if ev.content and ev.content.alias:
                alias = str(ev.content.alias).strip()
        except Exception:
            pass
        label: Optional[str] = None
        if name and alias:
            label = f"{name} ({alias})"
        elif name:
            label = name
        elif alias:
            label = alias
        if not label:
            bot = str(self.client.mxid)
            try:
                members = await self.client.get_joined_members(rid)
                mxids = [str(m) for m in members]
                if len(mxids) == 2 and bot in mxids:
                    other = next(m for m in mxids if m != bot)
                    disp = await self.client.get_displayname(other)
                    who = (disp or "").strip() or other
                    label = f"Личные сообщения с {who}"
            except Exception:
                pass
        if not label:
            label = f"https://matrix.to/#/{rid}"
        self._room_labels[rid] = label
        return label

    async def _dm_room_for(self, user_id: str) -> Optional[str]:
        cached = self._dm_rooms.get(user_id)
        if cached:
            return cached
        try:
            rooms = await self.client.get_joined_rooms()
        except Exception:
            return None
        for room_id in rooms:
            rid = str(room_id)
            if await self._is_direct_with(user_id, rid):
                self._dm_rooms[user_id] = rid
                return rid
        return None

    async def _send_room_text(
        self, room_id: str, text: str, *, markdown: bool = True
    ) -> None:
        rid = str(room_id)
        if markdown:
            body, html = await parse_formatted(text, render_markdown=True)
            content = TextMessageEventContent(
                msgtype=MessageType.TEXT,
                body=body,
                format=Format.HTML,
                formatted_body=html,
            )
            await self.client.send_message_event(rid, EventType.ROOM_MESSAGE, content)
        else:
            await self.client.send_text(rid, text)

    async def _deliver(
        self,
        user_id: str,
        room_id: str,
        text: str,
        *,
        in_room: Optional[MessageEvent] = None,
        markdown: bool = True,
    ) -> None:
        mode = str(self._conf("reply_mode", "room")).lower()
        if mode == "dm":
            if in_room is not None and str(in_room.sender) == user_id:
                rid = str(in_room.room_id)
                if await self._is_direct_with(user_id, rid):
                    self._dm_rooms[user_id] = rid
                    await in_room.respond(text, markdown=markdown)
                    return
            await self._send_dm(user_id, text, markdown=markdown)
            if in_room is not None and str(in_room.sender) == user_id:
                src = str(in_room.room_id)
                dm = await self._dm_room_for(user_id)
                if dm and dm != src:
                    await in_room.respond(
                        "Ответ отправлен в личные сообщения с ботом."
                    )
            return
        if in_room is not None:
            await in_room.respond(text, markdown=markdown)
            return
        await self._send_room_text(room_id, text, markdown=markdown)

    async def _send_dm(
        self, user_id: str, text: str, *, markdown: bool = True
    ) -> None:
        room_id = await self._dm_room_for(user_id)
        if not room_id:
            room_id = await self.client.create_room(
                preset=RoomCreatePreset.PRIVATE,
                invitees=[user_id],
                is_direct=True,
            )
            self._dm_rooms[user_id] = str(room_id)
        await self._send_room_text(room_id, text, markdown=markdown)

    async def _operator_notify_room(
        self, request_room_id: str, requester: str
    ) -> Optional[str]:
        """Куда постить операторское уведомление; None — только личка операторам."""
        admin_room = str(self._conf("notify_room", "") or "").strip()
        mode = self._notify_mode()
        if mode == "fixed":
            return admin_room or None
        if mode not in ("room", "both"):
            return None
        if admin_room:
            return admin_room
        if await self._is_direct_with(requester, request_room_id):
            return None
        return str(request_room_id)

    async def _notify_operators_dm_card(
        self, body: str, request_id: int, kind: str
    ) -> None:
        for op in self._notify_users():
            try:
                dm = await self._dm_room_for(op)
                if not dm:
                    dm = await self.client.create_room(
                        preset=RoomCreatePreset.PRIVATE,
                        invitees=[op],
                        is_direct=True,
                    )
                    self._dm_rooms[op] = str(dm)
                await self._send_moderation_card(str(dm), body, request_id, kind)
                self.log.info(f"Карточка модерации #{request_id} оператору {op} (DM)")
            except Exception as e:
                self.log.error(f"Не удалось уведомить оператора {op}: {e}")

    async def _notify_operators_new_request(
        self, title: str, requester: str, room_id: str, request_id: int
    ) -> None:
        ops = self._notify_users()
        if not ops:
            self.log.warning(
                "notify_users пуст — уведомление о новом запросе не отправлено"
            )
            return
        room_label = await self._room_human_label(room_id)
        msg = (
            f"Новый запрос контента #{request_id}: «{title}»\n"
            f"От: {requester}\nКомната: {room_label}\n"
            f"Принять ✅ | Отклонить ⛔"
        )
        mode = self._notify_mode()

        if mode not in ("dm", "room", "both", "fixed"):
            self.log.warning(
                f"Неизвестный notify_mode={mode!r}, ожидается "
                "dm, room, both или fixed"
            )
            return

        if mode == "fixed":
            target = await self._operator_notify_room(room_id, requester)
            if not target:
                self.log.error(
                    "notify_mode=fixed, но notify_room не задан — уведомление пропущено"
                )
                return
            await self._send_moderation_card(target, msg, request_id, "new")
            self.log.info(f"Карточка модерации #{request_id} в комнату {target}")
            return

        if mode in ("dm", "both"):
            await self._notify_operators_dm_card(msg, request_id, "new")

        if mode in ("room", "both"):
            target = await self._operator_notify_room(room_id, requester)
            if target:
                await self._send_moderation_card(target, msg, request_id, "new")
                self.log.info(f"Карточка модерации #{request_id} в комнату {target}")
            elif mode == "room":
                self.log.info(
                    "Запрос из лички, notify_room не задан — дублируем в DM операторам"
                )
                await self._notify_operators_dm_card(msg, request_id, "new")

    async def _insert_pending(
        self, title: str, requester: str, room_id: str, check_message: str
    ) -> tuple[Optional[int], bool]:
        """(id, is_new) — is_new False, если уже был pending для пары title+пользователь."""
        title_norm = normalize_title(title)
        safe_title = _sanitize_db_display(title) or title
        safe_msg = _sanitize_db_display(check_message) or check_message
        row = await self.database.fetchrow(
            """
            SELECT id FROM movie_requests
            WHERE title_norm=$1 AND requester_id=$2
              AND status IN ('pending', 'awaiting_approval')
            """,
            title_norm,
            requester,
        )
        if row:
            now = _db_now()
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=$1, last_check_message=$2,
                    times_requested = times_requested + 1
                WHERE id=$3
                """,
                now,
                safe_msg,
                row["id"],
            )
            return int(row["id"]), False
        now = _db_now()
        row = await self.database.fetchrow(
            """
            INSERT INTO movie_requests (
                title, title_norm, requester_id, room_id, status,
                created_at, last_checked_at, last_check_message,
                times_requested
            ) VALUES ($1,$2,$3,$4,'awaiting_approval',$5,$5,$6,1)
            RETURNING id
            """,
            safe_title,
            title_norm,
            requester,
            room_id,
            now,
            safe_msg,
        )
        if row:
            return int(row["id"]), True
        return None, False

    async def refresh_queue_summary(self) -> None:
        pending_count = await self.database.fetchval(
            "SELECT COUNT(*) FROM movie_requests WHERE status='pending'"
        )
        rows = await self.database.fetch(
            """
            SELECT id, title, requester_id, status, created_at, last_checked_at,
                   last_check_message, plex_title
            FROM movie_requests
            WHERE status IN ('pending', 'found')
            ORDER BY created_at DESC
            LIMIT 25
            """
        )
        items = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "title": _sanitize_db_display(r["title"]),
                    "status": _sanitize_db_display(r["status"]),
                    "requester_id": _sanitize_db_display(r["requester_id"]),
                    "created_at": _iso_ts(r["created_at"]),
                    "last_checked_at": _iso_ts(r["last_checked_at"]),
                    "last_check_message": _sanitize_db_display(
                        r["last_check_message"]
                    ),
                    "plex_title": _sanitize_db_display(r["plex_title"]),
                }
            )
        snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pending_count": int(pending_count or 0),
            "items": items,
        }
        self.config["queue_summary"] = snapshot
        self.config["movie_requests"] = items
        self.config.save()

    async def _purge_old_requests(self) -> None:
        days = int(self._conf("retention_days", 90))
        if days <= 0:
            return
        cutoff = _utc_now() - timedelta(days=days)
        try:
            scheme = self.database.scheme
        except AttributeError:
            scheme = Scheme.POSTGRES
        cutoff_arg = cutoff.isoformat() if scheme == Scheme.SQLITE else cutoff
        try:
            result = await self.database.execute(
                "DELETE FROM movie_requests WHERE created_at < $1",
                cutoff_arg,
            )
            if result:
                self.log.info(
                    f"Очистка БД: удалены записи старше {days} дн. ({result})"
                )
        except Exception as e:
            self.log.error(f"Ошибка очистки БД (retention): {e}")
        cutoff_local = (
            _utc_now()
            + timedelta(hours=self._timezone_offset_hours())
            - timedelta(days=days)
        )
        cutoff_date = cutoff_local.strftime("%Y-%m-%d")
        try:
            result = await self.database.execute(
                "DELETE FROM zapros_daily_usage WHERE usage_date < $1",
                cutoff_date,
            )
            if result:
                self.log.info(
                    f"Очистка счётчиков суточных запросов старше {days} дн. ({result})"
                )
        except Exception as e:
            self.log.error(f"Ошибка очистки zapros_daily_usage: {e}")

    async def _process_pending_row(self, row) -> None:
        title = row["title"]
        await self._wait_background_plex_idle()
        try:
            movie = await self._search_plex(title)
        except PlexError as e:
            now = _db_now()
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=$1, last_check_message=$2
                WHERE id=$3
                """,
                now,
                _sanitize_db_display(str(e)),
                row["id"],
            )
            return
        if not movie:
            now = _db_now()
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=$1, last_check_message=$2
                WHERE id=$3
                """,
                now,
                "На сервере не найден",
                row["id"],
            )
            return
        now = _db_now()
        await self.database.execute(
            """
            UPDATE movie_requests
            SET status='found', found_at=$1, last_checked_at=$1,
                plex_rating_key=$2, plex_title=$3, plex_year=$4,
                last_check_message=$5
            WHERE id=$6
            """,
            now,
            movie.rating_key,
            _sanitize_db_display(movie.title),
            movie.year,
            "Найден при периодической проверке",
            row["id"],
        )
        await self._deliver(
            row["requester_id"],
            row["room_id"],
            self._format_found_later(movie, title),
        )

    def _poll_gap_seconds(self) -> float:
        return max(0.0, float(self._conf("poll_between_pending_seconds", 2)))

    def _poll_startup_seconds(self) -> float:
        return max(0.0, float(self._conf("poll_startup_seconds", 15)))

    async def _poll_loop(self) -> None:
        await asyncio.sleep(self._poll_startup_seconds())
        gap = self._poll_gap_seconds()
        while True:
            try:
                rows = await self.database.fetch(
                    "SELECT * FROM movie_requests WHERE status='pending' ORDER BY id"
                )
                for i, row in enumerate(rows):
                    await self._process_pending_row(row)
                    if i + 1 < len(rows) and gap > 0:
                        await asyncio.sleep(gap)
                await self._purge_old_requests()
                await self.refresh_queue_summary()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.error(f"Ошибка в цикле опроса Plex: {e}")
            hours = float(self._conf("poll_interval_hours", 6))
            await asyncio.sleep(max(60.0, hours * 3600.0))

    @event.on(EventType.ROOM_MESSAGE)
    async def on_zapros_message(self, evt: MessageEvent) -> None:
        if str(evt.sender) == str(self.client.mxid):
            return
        if evt.content.msgtype != MessageType.TEXT:
            return
        body_raw = evt.content.body or ""
        if not body_raw.startswith("!"):
            return
        norm = _normalize_matrix_spaces(body_raw).strip()
        m = _ZAPROS_MSG_RE.match(norm)
        if not m:
            return
        body = (m.group(1) or "").strip() or None
        self.log.info(
            f"!zapros от {evt.sender} в {evt.room_id}: {(body or '')[:80]!r}"
        )
        try:
            await self._zapros_cmd(evt, body)
        except MLimitExceeded:
            self.log.warning("!zapros: лимит запросов homeserver (429), ответ не отправлен")
        except Exception as e:
            self.log.exception(f"Ошибка при обработке !zapros: {e}")
            try:
                await self._respond_ratelimited(
                    evt,
                    "Внутренняя ошибка при обработке запроса. Проверьте логи инстанса.",
                )
            except MLimitExceeded:
                self.log.warning("Не удалось отправить ответ об ошибке: 429")

    async def _zapros_cmd(self, evt: MessageEvent, body: Optional[str]) -> None:
        raw = (body or "").strip()
        if not raw:
            await evt.respond(
                'Использование: !zapros "Матрица" или !zapros "Матрица 1999" / "Матрица (1999)"'
            )
            return
        if _ZAPROS_DUP_RE.search(raw):
            await evt.respond(
                "Некорректный запрос: в сообщении повторяется команда !zapros. "
                'Отправьте один запрос, например: !zapros "Матрица 1999"'
            )
            return
        if raw.lower() == "list" or raw.lower().startswith("list "):
            if not self._is_operator(evt):
                await evt.respond("Команда list доступна только операторам.")
                return
            await self._cmd_list(evt)
            return
        mod_cmd = _MOD_CMD_RE.match(raw)
        if mod_cmd:
            if not self._is_operator(evt):
                await evt.respond(
                    "Команды accept/reject доступны только операторам."
                )
                return
            verb = mod_cmd.group(1).lower()
            req_id = int(mod_cmd.group(2))
            if verb in ("accept", "approve"):
                await self._moderation_accept(req_id, str(evt.sender), evt)
            else:
                await self._moderation_reject(req_id, str(evt.sender), evt)
            return
        title = self._parse_movie_title(raw)
        if not title:
            await evt.respond(
                'Укажите название, например: !zapros "Матрица" или !zapros Матрица 1999'
            )
            return
        limit = self._title_max_length()
        if len(title.strip()) > limit:
            await evt.respond(
                f"Слишком длинное название ({len(title.strip())} симв.). "
                f"Максимум {limit} симв."
            )
            return
        title = title.strip()
        requester = str(evt.sender)
        await evt.respond(f"Ищу на Plex: «{title}»…")
        try:
            async with self._interactive_plex_guard():
                movies, ambiguous = await self._search_plex_interactive(title)
        except PlexError as e:
            await evt.respond(str(e))
            return
        if ambiguous and movies:
            await self._deliver(
                str(evt.sender),
                str(evt.room_id),
                self._format_movies_table(movies, title),
                in_room=evt,
                markdown=False,
            )
            return
        movie = movies[0] if movies else None
        if movie:
            if not self._is_operator(evt):
                quota_err = await self._try_consume_found_daily_quota(requester)
                if quota_err:
                    await evt.respond(quota_err)
                    return
            await self._record_found(
                title,
                requester,
                str(evt.room_id),
                movie,
                "Найден при запросе",
            )
            await self._deliver(
                str(evt.sender),
                str(evt.room_id),
                self._format_found(movie, title),
                in_room=evt,
            )
            await self.refresh_queue_summary()
            return
        if not self._is_operator(evt):
            if not await self._has_pending_duplicate(title, requester):
                quota_err = await self._try_consume_pending_daily_quota(requester)
                if quota_err:
                    await evt.respond(quota_err)
                    return
        req_id, is_new = await self._insert_pending(
            title,
            requester,
            str(evt.room_id),
            "Не найден при первичной проверке",
        )
        if is_new and req_id is not None:
            await self._notify_operators_new_request(
                title, str(evt.sender), str(evt.room_id), req_id
            )
        await self._deliver(
            str(evt.sender),
            str(evt.room_id),
            f"⛔️ Контент «{title}» на Plex не найден. Запрос отправлен на модерацию"
            f"{f' (#{req_id})' if req_id else ''}.",
            in_room=evt,
        )
        await self.refresh_queue_summary()

    async def _requester_label(self, requester_id: str) -> str:
        try:
            disp = await self.client.get_displayname(requester_id)
        except Exception:
            disp = None
        if disp and str(disp).strip():
            return f"{str(disp).strip()} ({requester_id})"
        return requester_id

    def _list_entry_line(self, r) -> str:
        tz = self._timezone_offset_hours()
        created = _fmt_ts_short(r["created_at"], tz) if r["created_at"] else "?"
        checked = _fmt_ts_short(r["last_checked_at"], tz)
        st = _status_ru(str(r["status"]))
        if r["status"] == "found" and r["plex_title"]:
            tail = f"Plex: «{r['plex_title']}»"
        else:
            tail = r["last_check_message"] or "—"
        return (
            f"#{r['id']} [{st}] «{r['title']}» "
            f"созд. {created}, пров. {checked}; {tail} ✅ | ⛔"
        )

    async def _cmd_list(self, evt: MessageEvent) -> None:
        per_user = self._list_per_user_max()
        users_max = self._list_users_max()
        fetch_limit = per_user * users_max
        rows = await self.database.fetch(
            """
            SELECT id, title, status, requester_id, created_at, last_checked_at,
                   last_check_message, plex_title
            FROM movie_requests
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            fetch_limit,
        )
        if not rows:
            await evt.respond("Запросов пока нет.")
            return
        by_user: dict[str, list] = defaultdict(list)
        user_order: list[str] = []
        shown = 0
        for r in rows:
            uid = str(r["requester_id"])
            if uid not in by_user:
                if len(user_order) >= users_max:
                    continue
                user_order.append(uid)
            if len(by_user[uid]) >= per_user:
                continue
            by_user[uid].append(r)
            shown += 1

        if not shown:
            await evt.respond("Запросов пока нет.")
            return

        header = (
            f"**Запросы контента** (только ожидающие)\n"
            f"(до {per_user} на пользователя, до {users_max} пользователей)"
        )
        try:
            await self._respond_ratelimited(evt, header)
        except MLimitExceeded:
            self.log.warning("!zapros list: 429 на заголовке")
            return
        list_room = str(evt.room_id)
        for uid in user_order:
            try:
                await self._respond_ratelimited(
                    evt, f"**{await self._requester_label(uid)}**"
                )
            except MLimitExceeded:
                self.log.warning("!zapros list: 429, список оборван")
                return
            for r in by_user[uid]:
                line = self._list_entry_line(r)
                card = await self._send_moderation_card(
                    list_room, line, int(r["id"]), "list"
                )
                if card is None:
                    return

    @event.on(EventType.REACTION)
    async def on_moderation_reaction(self, evt: ReactionEvent) -> None:
        if str(evt.sender) == str(self.client.mxid):
            return
        if not self._is_operator_mxid(str(evt.sender)):
            return
        rel = evt.content.relates_to
        if not rel or not rel.event_id:
            return
        action = _moderation_action_from_reaction(rel.key or "")
        if not action:
            return
        row = await self._lookup_moderation_notify(str(rel.event_id))
        if not row:
            return
        req_id = int(row["request_id"])
        room_id = str(evt.room_id)
        operator = str(evt.sender)
        if action == "accept":
            await self._moderation_accept(req_id, operator, room_id=room_id)
        else:
            await self._moderation_reject(req_id, operator, room_id=room_id)
