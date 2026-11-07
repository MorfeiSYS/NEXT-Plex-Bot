from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Type

from maubot import Plugin, MessageEvent
from maubot.handlers import command
from mautrix.types import RoomCreatePreset
from mautrix.util.async_db import Scheme, UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from plex import PlexError, PlexMovie, normalize_title, search_movie

upgrade_table = UpgradeTable()
_TITLE_QUOTED_RE = re.compile(r'^"([^"]+)"\s*$|\'([^\']+)\'\s*$')


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn, scheme: Scheme) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS movie_requests (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            title_norm TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("plex")
        helper.copy("notify_users")
        helper.copy("reply_mode")
        helper.copy("poll_interval_hours")
        helper.copy("search_limit")
        if "queue_summary" in self:
            helper.base["queue_summary"] = self["queue_summary"]
        elif "queue_summary" in helper.base:
            pass
        else:
            helper.base["queue_summary"] = {
                "updated_at": None,
                "pending_count": 0,
                "items": [],
            }


class PlexZaprosBot(Plugin):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._poll_task: Optional[asyncio.Task] = None
        self._dm_rooms: dict[str, str] = {}

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.config.load_and_update()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _plex_cfg(self) -> dict:
        return self.config["plex"] or {}

    def _notify_users(self) -> list[str]:
        users = self.config.get("notify_users") or []
        return [str(u) for u in users]

    def _is_operator(self, evt: MessageEvent) -> bool:
        return str(evt.sender) in self._notify_users()

    def _parse_movie_title(self, body: str) -> Optional[str]:
        body = body.strip()
        m = _TITLE_QUOTED_RE.match(body)
        if m:
            return (m.group(1) or m.group(2) or "").strip()
        if body and not body.lower().startswith("list"):
            return body
        return None

    def _format_found(self, movie: PlexMovie, requested: str) -> str:
        year = f" ({movie.year})" if movie.year else ""
        link = movie.web_link(self._plex_cfg().get("base_url", ""))
        return (
            f"Фильм «{movie.title}»{year} найден на Plex "
            f"(запрос: «{requested}»).\n{link}"
        )

    async def _search_plex(self, title: str) -> Optional[PlexMovie]:
        plex = self._plex_cfg()
        limit = int(self.config.get("search_limit") or 10)
        return await search_movie(
            self.http,
            base_url=plex.get("base_url") or "http://127.0.0.1:32400",
            token=plex.get("token") or "",
            client_identifier=plex.get("client_identifier") or "maubot-plex-zapros",
            query=title,
            limit=limit,
        )

    async def _deliver(
        self,
        user_id: str,
        room_id: str,
        text: str,
        *,
        in_room: Optional[MessageEvent] = None,
    ) -> None:
        mode = (self.config.get("reply_mode") or "room").lower()
        if mode == "dm":
            await self._send_dm(user_id, text)
            return
        if in_room is not None:
            await in_room.respond(text)
            return
        await self.client.send_text(room_id, text)

    async def _send_dm(self, user_id: str, text: str) -> None:
        room_id = self._dm_rooms.get(user_id)
        if not room_id:
            room_id = await self.client.create_room(
                preset=RoomCreatePreset.PRIVATE,
                invitees=[user_id],
                is_direct=True,
            )
            self._dm_rooms[user_id] = str(room_id)
        await self.client.send_text(room_id, text)

    async def _notify_operators_new_request(
        self, title: str, requester: str, room_id: str, request_id: int
    ) -> None:
        msg = (
            f"Новый запрос контента #{request_id}: «{title}»\n"
            f"От: {requester}\nКомната: {room_id}"
        )
        for op in self._notify_users():
            try:
                await self._send_dm(op, msg)
            except Exception as e:
                self.log.warning(f"Не удалось уведомить оператора {op}: {e}")

    async def _insert_pending(
        self, title: str, requester: str, room_id: str, check_message: str
    ) -> Optional[int]:
        title_norm = normalize_title(title)
        row = await self.database.fetchrow(
            """
            SELECT id FROM movie_requests
            WHERE title_norm=$1 AND requester_id=$2 AND status='pending'
            """,
            title_norm,
            requester,
        )
        if row:
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=NOW(), last_check_message=$1
                WHERE id=$2
                """,
                check_message,
                row["id"],
            )
            return int(row["id"])
        row = await self.database.fetchrow(
            """
            INSERT INTO movie_requests (
                title, title_norm, requester_id, room_id, status,
                last_checked_at, last_check_message
            ) VALUES ($1,$2,$3,$4,'pending',NOW(),$5)
            RETURNING id
            """,
            title,
            title_norm,
            requester,
            room_id,
            check_message,
        )
        return int(row["id"]) if row else None

    async def refresh_queue_summary(self) -> None:
        pending_count = await self.database.fetchval(
            "SELECT COUNT(*) FROM movie_requests WHERE status='pending'"
        )
        rows = await self.database.fetch(
            """
            SELECT id, title, requester_id, status, created_at, last_checked_at,
                   last_check_message, plex_title
            FROM movie_requests
            ORDER BY created_at DESC
            LIMIT 25
            """
        )
        items = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "requester_id": r["requester_id"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "last_checked_at": (
                        r["last_checked_at"].isoformat() if r["last_checked_at"] else None
                    ),
                    "last_check_message": r["last_check_message"],
                    "plex_title": r["plex_title"],
                }
            )
        self.config["queue_summary"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pending_count": int(pending_count or 0),
            "items": items,
        }
        self.config.save()

    async def _process_pending_row(self, row) -> None:
        title = row["title"]
        try:
            movie = await self._search_plex(title)
        except PlexError as e:
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=NOW(), last_check_message=$1
                WHERE id=$2
                """,
                str(e),
                row["id"],
            )
            return
        if not movie:
            await self.database.execute(
                """
                UPDATE movie_requests
                SET last_checked_at=NOW(), last_check_message=$1
                WHERE id=$2
                """,
                "На сервере не найден",
                row["id"],
            )
            return
        await self.database.execute(
            """
            UPDATE movie_requests
            SET status='found', found_at=NOW(), last_checked_at=NOW(),
                plex_rating_key=$1, plex_title=$2, plex_year=$3,
                last_check_message=$4
            WHERE id=$5
            """,
            movie.rating_key,
            movie.title,
            movie.year,
            "Найден при периодической проверке",
            row["id"],
        )
        text = (
            f"Запрошенный фильм «{title}» теперь есть на Plex: "
            f"«{movie.title}»"
            f"{f' ({movie.year})' if movie.year else ''}.\n"
            f"{movie.web_link(self._plex_cfg().get('base_url', ''))}"
        )
        await self._deliver(row["requester_id"], row["room_id"], text)

    async def _poll_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                rows = await self.database.fetch(
                    "SELECT * FROM movie_requests WHERE status='pending' ORDER BY id"
                )
                for row in rows:
                    await self._process_pending_row(row)
                    await asyncio.sleep(2)
                await self.refresh_queue_summary()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.error(f"Ошибка в цикле опроса Plex: {e}")
            hours = float(self.config.get("poll_interval_hours") or 6)
            await asyncio.sleep(max(60.0, hours * 3600.0))

    @command.new("zapros", require_subcommand=False)
    @command.argument("body", pass_raw=True, required=False)
    async def zapros_cmd(self, evt: MessageEvent, body: Optional[str]) -> None:
        raw = (body or "").strip()
        if not raw:
            await evt.respond('Использование: !zapros "Название фильма"')
            return
        if raw.lower() == "list" or raw.lower().startswith("list "):
            if not self._is_operator(evt):
                await evt.respond("Команда list доступна только операторам из notify_users.")
                return
            await self._cmd_list(evt)
            return
        title = self._parse_movie_title(raw)
        if not title:
            await evt.respond('Укажите название в кавычках: !zapros "Название фильма"')
            return
        try:
            movie = await self._search_plex(title)
        except PlexError as e:
            await evt.respond(str(e))
            return
        if movie:
            await self._deliver(
                str(evt.sender),
                str(evt.room_id),
                self._format_found(movie, title),
                in_room=evt,
            )
            await self.refresh_queue_summary()
            return
        req_id = await self._insert_pending(
            title,
            str(evt.sender),
            str(evt.room_id),
            "Не найден при первичной проверке",
        )
        if req_id is not None:
            await self._notify_operators_new_request(
                title, str(evt.sender), str(evt.room_id), req_id
            )
        await self._deliver(
            str(evt.sender),
            str(evt.room_id),
            f"Фильм «{title}» на Plex не найден. Запрос сохранён"
            f"{f' (#{req_id})' if req_id else ''}; повторная проверка каждые "
            f"{self.config.get('poll_interval_hours', 6)} ч.",
            in_room=evt,
        )
        await self.refresh_queue_summary()

    async def _cmd_list(self, evt: MessageEvent) -> None:
        rows = await self.database.fetch(
            """
            SELECT id, title, status, requester_id, created_at, last_checked_at,
                   last_check_message, plex_title
            FROM movie_requests
            ORDER BY created_at DESC
            LIMIT 30
            """
        )
        if not rows:
            await evt.respond("Запросов пока нет.")
            return
        lines = ["Запросы фильмов (до 30 последних):"]
        for r in rows:
            created = (
                r["created_at"].strftime("%Y-%m-%d %H:%M")
                if r["created_at"]
                else "?"
            )
            checked = (
                r["last_checked_at"].strftime("%Y-%m-%d %H:%M")
                if r["last_checked_at"]
                else "—"
            )
            extra = r["plex_title"] or r["last_check_message"] or ""
            lines.append(
                f"#{r['id']} [{r['status']}] «{r['title']}» "
                f"| {r['requester_id']} | созд.: {created} | пров.: {checked}"
                + (f" | {extra}" if extra else "")
            )
        await evt.respond("\n".join(lines))
