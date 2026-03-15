# Repository Guidelines

## Project Structure & Module Organization

This repository is intentionally small and centered on one Python entrypoint:

- `new_on_mubi_notifier.py`: fetches the MUBI collection, writes the CSV, stores SQLite state, and optionally sends Telegram notifications.
- `new_on_mubi.csv`: latest generated export.
- `mubi_notifications.db`: local SQLite state for deduplicating Telegram notifications.
- `.env`: local runtime configuration.
- `README.md`: usage and setup notes.

If the project grows, keep source code under a dedicated package directory such as `src/` and place tests under `tests/`.

## Build, Test, and Development Commands

- `python -m pip install requests`: install the current runtime dependency.
- `python new_on_mubi_notifier.py --out new_on_mubi.csv`: fetch collection data and regenerate the CSV.
- `python new_on_mubi_notifier.py --out new_on_mubi.csv --db-path mubi_notifications.db --env-file .env --notify`: run the full notifier flow, including SQLite sync and Telegram sends.
- `python -m py_compile new_on_mubi_notifier.py`: quick syntax validation before committing.

## Coding Style & Naming Conventions

- Use Python 3.10+ compatible code.
- Follow PEP 8 with 4-space indentation.
- Keep code, identifiers, comments, and log messages in English.
- Prefer descriptive snake_case names such as `fetch_collection_films` and `notify_new_films`.
- Keep side effects explicit: fetch, persist, and notify should stay in separate functions.

## Testing Guidelines

There is no formal test suite yet. For now:

- run `python -m py_compile new_on_mubi_notifier.py`
- run the script once without `--notify`
- inspect the generated CSV and log output

When tests are added, use `pytest` and name files `test_*.py`.

## Commit & Pull Request Guidelines

Current history uses short, imperative commit messages, for example: `mubi new releases to csv`.

Keep commits focused and use concise summaries such as:

- `add sqlite notification state`
- `rename notifier output files`

PRs should include:

- what changed
- why it changed
- how it was validated
- any `.env` or Telegram setup impact

## Security & Configuration Tips

- Do not commit real bot tokens or private chat IDs.
- Treat `.env` as local-only.
- SQLite state is part of runtime behavior; do not delete it casually if notification deduplication matters.
