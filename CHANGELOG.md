# Changelog

Все заметные изменения проекта описаны в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии следуют [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

## [1.0.43] - 2026-11-09

### Added

- `command_triggers` — список префиксов команд (без `!`, напр. `zapros`, `kino`); подкоманды `list` / `wait` / `accept` / `reject` общие для всех триггеров.

### Fixed

- Запрос с указанным годом (`Одиссея 2027`, `Одиссея (2027)` и т.п.): если в выдаче Plex нет записи с этим годом, заявка уходит на модерацию, а не ложное «найдено» (раньше для одного похожего сериала с другим годом срабатывал fallback).

## [1.0.42] - 2026-11-08

### Fixed

- Уведомления операторам в личку (`notify_mode: dm` / `both`): поиск DM через `m.direct` и state store, обновление `m.direct` при создании лички.
- `reply_mode: both` — ответ в личку и в комнату запроса.
- `notify_room` без лишних кавычек в YAML.

## [1.0.41] - 2026-11-08

### Added

- `!zapros wait` — список заявок на модерации (`awaiting_approval`) с карточками ✅/⛔ (только операторы).
- Суточное напоминание операторам о зависших `awaiting_approval` (`stuck_approval_hours`, `stuck_reminder_interval_hours`).

### Changed

- `!zapros list` — явное сообщение, если очередь `pending` пуста; в заголовке указано «ожидают на Plex».

## [1.0.40] - 2026-11-08

### Fixed

- `!zapros`: обработка через `ROOM_MESSAGE` с нормализацией NBSP и прочих unicode-пробелов после команды (Element/Telegram).

### Changed

- Справка по команде: примеры с «Матрица» / «Матрица 1999» / «Матрица (1999)».

## [1.0.39] - 2026-11-08

### Changed

- Сразу отвечает «Ищу на Plex: …» до запроса к серверу.
- Название можно передавать без кавычек.

### Fixed

- NBSP в тексте запроса к Plex.

## [1.0.38] - 2026-11-08

### Added

- Fallback `hubs/search` по укороченному названию (до точки, первые слова).

### Changed

- Сопоставление результатов с игнором пунктуации.
- Фильтр hub-блоков как в v1.0.34.

## [1.0.37] - 2026-11-08

### Added

- `sanitize_plex_search_query` перед `hubs/search` (непарные кавычки, опасные символы).

### Fixed

- Понятное сообщение при HTTP 500 от Plex.

## [1.0.36] - 2026-11-08

### Added

- Подпись «найдено» по `librarySectionTitle` (Фильм / Сериал / Мультфильм / Мультсериал).
- Дозапрос `library/metadata` при отсутствии секции в выдаче поиска.

## [1.0.35] - 2026-11-08

### Added

- Поиск сериалов (`show`) в `hubs/search`.

### Changed

- Ослаблен отсев по году для сериалов.
- Тексты ответа с учётом типа контента.

## [1.0.34] - 2026-11-08

### Changed

- Карточки модерации: реакции ✅/⛔ от бота (пауза `matrix_send_pause_seconds`).

## [1.0.33] - 2026-11-08

### Changed

- `list` / модерация: без автоподсказок и автопроставления реакций.
- Пауза `matrix_send_pause_seconds` и повтор при Matrix 429.

## [1.0.32] - 2026-11-08

### Added

- Модерация: статус `awaiting_approval`, карточки операторам (✅/⛔, `!zapros accept|reject <id>`).
- Релизы `.mbp` в каталоге `build/`.

### Changed

- `!zapros list`: отдельная карточка на каждую запись.

## [1.0.31] - 2026-11-08

### Changed

- Снят патч UI Database из v1.0.30: снова видна `sqlite_sequence`, стандартные `SELECT * … LIMIT 100`.

## [1.0.29] - 2026-11-08

### Fixed

- `daily_found_requests_per_user_max`: диапазон 0–1000 (значение 5 не поднимается до 100).

### Changed

- `found` / `pending` без дублей — `times_requested`; повтор `!zapros` обновляет ту же строку.

## [1.0.28] - 2026-11-08

### Fixed

- Миграция БД v4: `ADD COLUMN found_request_count` совместима с SQLite (без `IF NOT EXISTS`).

## [1.0.27] - 2026-11-08

### Added

- `daily_found_requests_per_user_max` (100–1000) для заявок «найден на Plex».
- Счётчик `found_request_count` в БД.

### Changed

- `daily_requests_per_user_max` только для новых pending («не найден на Plex»).

## [1.0.26] - 2026-11-08

### Added

- Суточный лимит `!zapros` на пользователя (`daily_requests_per_user_max`, `zapros_daily_usage`).

### Changed

- Операторы из `notify_users` без суточного лимита.
- Границы суток по `timezone_offset_hours`.

## [1.0.25] - 2026-11-08

### Fixed

- `config.save()` в event loop Maubot (без `asyncio.to_thread`); устранён `RuntimeError: no running event loop` после `!zapros`.

[Unreleased]: https://github.com/MorfeiSYS/NEXT-Plex-Bot/compare/v1.0.34...HEAD
[1.0.40]: https://github.com/MorfeiSYS/NEXT-Plex-Bot/compare/v1.0.34...HEAD
[1.0.34]: https://github.com/MorfeiSYS/NEXT-Plex-Bot/compare/v1.0.23...v1.0.34
[1.0.23]: https://github.com/MorfeiSYS/NEXT-Plex-Bot/releases/tag/v1.0.23
