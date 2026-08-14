# FSTEC Monitor

Монитор версий документов раздела `fstec.ru/dokumenty/vse-dokumenty`. Отдельно архивирует исходный HTML, нормализованный HTML-текст, PDF/ODT и извлечённый текст вложений. Различает бинарную замену файла и содержательное изменение.

## Возможности

- рекурсивный обход категорий и пагинации;
- неизменяемое content-addressed хранилище по SHA-256;
- SQLite для одиночной установки или PostgreSQL в Docker Compose;
- HTML raw/structural/semantic hashes;
- PDF и ODT binary/semantic hashes;
- события: новый/удалённый документ или файл, изменение HTML, бинарная либо содержательная замена вложения;
- Telegram-уведомления;
- повторные попытки, backoff и малая нагрузка на сайт;
- systemd timer каждые два часа.

## Быстрый запуск без Docker

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
fstec-monitor init
fstec-monitor baseline
fstec-monitor run
```

Первый полный проход выполняйте именно через `baseline`: он сохраняет текущие версии без рассылки ложных событий.

## Docker Compose

```bash
cp .env.example .env
# Замените пароль PostgreSQL одновременно в docker-compose.yml и DATABASE_URL.
docker compose build
docker compose run --rm monitor fstec-monitor baseline
docker compose run --rm monitor fstec-monitor run
```

## Telegram

Заполните в `.env`:

```dotenv
FSTEC_TELEGRAM_BOT_TOKEN=123456:token
FSTEC_TELEGRAM_CHAT_ID=123456789
```

## systemd

Рекомендуемые пути:

- код: `/opt/fstec-monitor`;
- venv: `/opt/fstec-monitor/.venv`;
- env: `/etc/fstec-monitor.env`;
- данные: `/var/lib/fstec-monitor`.

В env укажите:

```dotenv
FSTEC_DATABASE_URL=sqlite:////var/lib/fstec-monitor/fstec-monitor.db
FSTEC_STORAGE_DIR=/var/lib/fstec-monitor/objects
```

Затем скопируйте unit-файлы из `systemd/`, выполните `systemctl daemon-reload` и `systemctl enable --now fstec-monitor.timer`.

## Важные свойства

Старые объекты никогда не перезаписываются. Ошибка HTTP не считается удалением. Изменение счётчиков скачивания не должно влиять на semantic hash, однако исходный HTML сохраняется для аудита. Парсер специально написан эвристически: при изменении шаблона сайта исходные снимки позволят повторно обработать архив.
