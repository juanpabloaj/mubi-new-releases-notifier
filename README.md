# MUBI New On MUBI Notifier

This project fetches the `New on MUBI` collection, writes a ranked CSV, stores film state in SQLite, and can send one Telegram notification per newly detected film.

## What it does

- Fetches the current collection from MUBI using the public web page plus the MUBI API.
- Exports a CSV sorted by `score_rank`.
- Stores film metadata locally in SQLite.
- Sends Telegram messages only once per film.
- Tracks which films were already notified with `notified_at`.

## Files

- `new_on_mubi_notifier.py`: main script
- `.env`: Telegram configuration
- `new_on_mubi.csv`: latest CSV export
- `mubi_notifications.db`: local SQLite state, created on first notification run

## Requirements

- `uv`
- Python 3.10+

Create the virtual environment and install dependencies:

```bash
uv venv .venv
source .venv/bin/activate
uv sync
```

You can also run the script directly with `uv run` without activating the virtual environment.

## Environment variables

The script reads variables from `.env`.

Required for Telegram notifications:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## CSV contents

The CSV includes:

- `collection_rank`
- `title`
- `original_title`
- `year`
- `origin_country`
- `director`
- `score_10`
- `ratings_count`
- `score_rank`
- `popularity_rank`
- `combined_rank`
- `slug`
- `url`

The CSV is written sorted by `score_rank` ascending.

## Run without Telegram

This fetches the collection and writes the CSV only:

```bash
uv run new_on_mubi_notifier.py \
  --url "https://mubi.com/en/cl/collections/new-on-mubi" \
  --out "new_on_mubi.csv"
```

## Run with Telegram notifications

This fetches the collection, writes the CSV, updates SQLite, and sends notifications for films that have not been notified before:

```bash
uv run new_on_mubi_notifier.py \
  --url "https://mubi.com/en/cl/collections/new-on-mubi" \
  --out "new_on_mubi.csv" \
  --db-path "mubi_notifications.db" \
  --env-file ".env" \
  --notify
```

If you have already activated `.venv`, `python new_on_mubi_notifier.py` also works.

## Notification behavior

- The first `--notify` run sends all currently known films.
- Later runs only send newly added films.
- A film is marked as notified only after Telegram returns success.

Message format:

```text
<title> <score_10>
<original_title> | <year> | <origin_country> | <director>
<url>
```

## SQLite schema behavior

The SQLite database stores one row per film, keyed by `slug`.

It keeps:

- current metadata
- `first_seen_at`
- `last_seen_at`
- `notified_at`

This is what prevents duplicate Telegram notifications.

## Logging

The script uses Python `logging` and prints:

- extraction summary
- top rows by `score_rank`
- CSV output path
- number of Telegram notifications sent

## Notes

- `score_10` is the MUBI score converted to a 0-10 scale.
- `score_rank` is derived from `score_10`, descending.
- `combined_rank` is still exported, but Telegram messages currently use `score_10`.
