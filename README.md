# Plex Zapros Maubot Plugin

Maubot-плагин для поиска фильмов на Plex Media Server и очереди запросов отсутствующего контента в Matrix.

## Возможности

- `!zapros "Название фильма"` — поиск на Plex (X-Plex-Token из config).
- Если фильм есть — ответ запросившему (`reply_mode`: `room` или `dm`).
- Если нет — запись в БД, уведомление операторов из `notify_users`, периодическая проверка Plex.
- `!zapros list` — список запросов (только пользователи из `notify_users`).
- Блок `queue_summary` в YAML-config инстанса (обновляется плагином для обзора в менеджере Maubot).

## Сборка

```powershell
.\build.ps1
```

или

```bash
chmod +x build.sh && ./build.sh
```

Архив: `NEXT.plex-zapros-v1.0.0.mbp`.

## Установка

1. Загрузите `.mbp` в [менеджер Maubot](https://docs.mau.fi/maubot/usage/basic.html).
2. Создайте client и instance, привяжите к комнате.
3. В config инстанса задайте:

| Поле | Описание |
|------|----------|
| `plex.base_url` | URL PMS, напр. `http://192.168.1.10:32400` |
| `plex.token` | [X-Plex-Token](https://support.plex.tv/articles/204050366-finding-an-authentication-token-x-plex-token/) |
| `plex.client_identifier` | Идентификатор клиента для Plex API |
| `notify_users` | MXID операторов (уведомления + `!zapros list`) |
| `reply_mode` | `room` — ответ в комнате; `dm` — личные сообщения |
| `poll_interval_hours` | Интервал опроса Plex для pending (по умолчанию 6) |
| `search_limit` | Лимит результатов hubs/search |

`queue_summary` обновляется автоматически; при ручном сохранении config не удаляйте блок, если он уже есть.

## Команды

```
!zapros "Интерстеллар"
!zapros list
```

## Проверка

1. Фильм, который уже есть на сервере — мгновенный ответ со ссылкой.
2. Несуществующий фильм — подтверждение, DM операторам, запись в `queue_summary`.
3. `!zapros list` от MXID не из `notify_users` — отказ.
4. Смена `plex.token` в UI Maubot — перезагрузка instance при необходимости.
5. Для ускоренного теста опроса временно `poll_interval_hours: 0.02`, добавьте контент на Plex, дождитесь цикла.

## Лицензия

MIT — см. [LICENSE](LICENSE).
