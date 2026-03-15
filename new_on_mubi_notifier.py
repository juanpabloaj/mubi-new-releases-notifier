#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


DEFAULT_COLLECTION_URL = "https://mubi.com/en/cl/collections/new-on-mubi"
DEFAULT_CSV_PATH = "new_on_mubi.csv"
DEFAULT_DB_PATH = "mubi_notifications.db"
DEFAULT_ENV_PATH = ".env"
API_BASE = "https://api.mubi.com/v4"
LOGGER = logging.getLogger("mubi_notifier")


def load_dotenv(env_path: str) -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    for text in reversed(scripts):
        candidate = text.strip()
        if not candidate.startswith("{"):
            continue
        if '"props"' not in candidate or '"page"' not in candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def to_score_10(raw_score: Any) -> Optional[float]:
    if raw_score is None:
        return None
    try:
        value = float(raw_score)
    except (TypeError, ValueError):
        return None
    if value <= 5.0:
        return round(value * 2.0, 1)
    return round(value, 1)


def join_directors(directors: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not directors:
        return None
    names = [director.get("name") for director in directors if director.get("name")]
    return ", ".join(names) if names else None


def normalize_film_data(film: Dict[str, Any]) -> Dict[str, Any]:
    countries = film.get("historic_countries") or []
    return {
        "title": film.get("title"),
        "original_title": film.get("original_title") or film.get("title"),
        "year": film.get("year"),
        "origin_country": ", ".join(countries) if countries else None,
        "director": join_directors(film.get("directors")),
        "slug": film.get("slug"),
        "score_10": to_score_10(film.get("average_rating")),
        "ratings_count": film.get("number_of_ratings"),
    }


def parse_url_defaults(collection_url: str) -> Dict[str, str]:
    parsed = urlparse(collection_url)
    parts = [part for part in parsed.path.split("/") if part]
    language = "en"
    country = "CL"
    if len(parts) >= 2 and len(parts[0]) in (2, 5) and len(parts[1]) == 2:
        language = parts[0]
        country = parts[1].upper()
    return {"language": language, "country": country}


def bootstrap_context(session: requests.Session, collection_url: str) -> Dict[str, Any]:
    response = session.get(collection_url, timeout=45)
    response.raise_for_status()
    data = extract_next_data(response.text)
    if not data:
        raise RuntimeError("Could not extract embedded JSON from the collection page.")
    page_props = data.get("props", {}).get("pageProps", {})
    collection = page_props.get("collection") or {}
    http_context = data.get("props", {}).get("httpContext", {})
    return {"collection": collection, "http_context": http_context}


def build_api_headers(collection_url: str, http_context: Dict[str, Any]) -> Dict[str, str]:
    defaults = parse_url_defaults(collection_url)
    anonymous_user_id = http_context.get("ANONYMOUS_USER_ID") or str(uuid.uuid4())
    return {
        "CLIENT": "web",
        "accept-language": http_context.get("accept-language") or defaults["language"],
        "Client-Country": http_context.get("Client-Country") or defaults["country"],
        "ANONYMOUS_USER_ID": anonymous_user_id,
    }


def fetch_collection_films(
    session: requests.Session,
    collection_slug: str,
    headers: Dict[str, str],
    total_items: Optional[int],
    per_page: int = 12,
) -> List[Dict[str, Any]]:
    films: List[Dict[str, Any]] = []
    page_num = 1
    while True:
        response = session.get(
            f"{API_BASE}/collections/{collection_slug}/films",
            params={"page": page_num, "per_page": per_page},
            headers=headers,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page_films = payload.get("films", [])
        if not page_films:
            break
        films.extend(page_films)
        if total_items and len(films) >= total_items:
            break
        if len(page_films) < per_page:
            break
        page_num += 1
    return films


def scrape_collection(collection_url: str) -> List[Dict[str, Any]]:
    session = requests.Session()
    bootstrap = bootstrap_context(session, collection_url)
    collection = bootstrap["collection"]
    http_context = bootstrap["http_context"]

    collection_slug = collection.get("slug")
    if not collection_slug:
        raise RuntimeError("Could not find the collection slug in the page payload.")

    headers = build_api_headers(collection_url, http_context)
    api_films = fetch_collection_films(
        session,
        collection_slug,
        headers,
        total_items=collection.get("total_items"),
        per_page=12,
    )

    rows: List[Dict[str, Any]] = []
    for index, film in enumerate(api_films, start=1):
        row = normalize_film_data(film)
        row["collection_rank"] = index
        row["url"] = f"https://mubi.com/films/{row['slug']}"
        rows.append(row)
    return rows


def add_rankings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_score = sorted(
        rows,
        key=lambda row: (
            row["score_10"] is not None,
            row["score_10"] or 0,
            row.get("ratings_count") or 0,
        ),
        reverse=True,
    )
    by_popularity = sorted(
        rows,
        key=lambda row: (
            row.get("ratings_count") is not None,
            row.get("ratings_count") or 0,
        ),
        reverse=True,
    )

    score_rank = {row["slug"]: index + 1 for index, row in enumerate(by_score)}
    popularity_rank = {row["slug"]: index + 1 for index, row in enumerate(by_popularity)}

    enriched_rows = []
    for row in rows:
        enriched = dict(row)
        enriched["score_rank"] = score_rank.get(row["slug"])
        enriched["popularity_rank"] = popularity_rank.get(row["slug"])
        if enriched["score_rank"] and enriched["popularity_rank"]:
            enriched["combined_rank"] = round(
                (enriched["score_rank"] + enriched["popularity_rank"]) / 2.0,
                2,
            )
        else:
            enriched["combined_rank"] = None
        enriched_rows.append(enriched)
    return enriched_rows


def rows_for_csv(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row["score_rank"] is None,
            row["score_rank"] if row["score_rank"] is not None else 9999,
            row["popularity_rank"] if row["popularity_rank"] is not None else 9999,
            row["combined_rank"] if row["combined_rank"] is not None else 9999,
        ),
    )


def write_csv(rows: List[Dict[str, Any]], output_path: str) -> None:
    fieldnames = [
        "collection_rank",
        "title",
        "original_title",
        "year",
        "origin_country",
        "director",
        "score_10",
        "ratings_count",
        "score_rank",
        "popularity_rank",
        "combined_rank",
        "slug",
        "url",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_for_csv(rows))


def print_preview(rows: List[Dict[str, Any]], limit: int = 10) -> None:
    ordered_rows = rows_for_csv(rows)
    LOGGER.info("Extracted films: %s", len(ordered_rows))
    LOGGER.info("Top rows by score rank:")
    for row in ordered_rows[:limit]:
        LOGGER.info(
            "%2s. %s | %s | %s | %s | %s | %s",
            row["score_rank"],
            row["title"],
            row["score_10"],
            row["original_title"],
            row["year"],
            row["origin_country"],
            row["director"],
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS films (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            original_title TEXT,
            year INTEGER,
            origin_country TEXT,
            director TEXT,
            score_10 REAL,
            ratings_count INTEGER,
            score_rank INTEGER,
            popularity_rank INTEGER,
            combined_rank REAL,
            collection_rank INTEGER,
            url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            notified_at TEXT
        )
        """
    )
    connection.commit()


def sync_films_to_db(connection: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    synced_at = utc_now_iso()
    for row in rows:
        connection.execute(
            """
            INSERT INTO films (
                slug, title, original_title, year, origin_country, director,
                score_10, ratings_count, score_rank, popularity_rank, combined_rank,
                collection_rank, url, first_seen_at, last_seen_at, notified_at
            ) VALUES (
                :slug, :title, :original_title, :year, :origin_country, :director,
                :score_10, :ratings_count, :score_rank, :popularity_rank, :combined_rank,
                :collection_rank, :url, :first_seen_at, :last_seen_at, NULL
            )
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                original_title = excluded.original_title,
                year = excluded.year,
                origin_country = excluded.origin_country,
                director = excluded.director,
                score_10 = excluded.score_10,
                ratings_count = excluded.ratings_count,
                score_rank = excluded.score_rank,
                popularity_rank = excluded.popularity_rank,
                combined_rank = excluded.combined_rank,
                collection_rank = excluded.collection_rank,
                url = excluded.url,
                last_seen_at = excluded.last_seen_at
            """,
            {
                **row,
                "first_seen_at": synced_at,
                "last_seen_at": synced_at,
            },
        )
    connection.commit()


def get_unnotified_rows(connection: sqlite3.Connection, current_slugs: List[str]) -> List[Dict[str, Any]]:
    if not current_slugs:
        return []
    placeholders = ", ".join("?" for _ in current_slugs)
    query = f"""
        SELECT
            slug, title, original_title, year, origin_country, director,
            score_10, ratings_count, score_rank, popularity_rank, combined_rank,
            collection_rank, url, first_seen_at, last_seen_at, notified_at
        FROM films
        WHERE slug IN ({placeholders})
          AND notified_at IS NULL
        ORDER BY score_rank ASC, collection_rank ASC
    """
    cursor = connection.execute(query, current_slugs)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def mark_notified(connection: sqlite3.Connection, slug: str) -> None:
    connection.execute(
        "UPDATE films SET notified_at = ? WHERE slug = ?",
        (utc_now_iso(), slug),
    )
    connection.commit()


def format_telegram_message(row: Dict[str, Any]) -> str:
    score_text = f"{row['score_10']:.1f}" if row.get("score_10") is not None else "N/A"
    year_text = str(row["year"]) if row.get("year") is not None else "Unknown year"
    country_text = row.get("origin_country") or "Unknown country"
    director_text = row.get("director") or "Unknown director"
    return (
        f"{row['title']} {score_text}\n"
        f"{row['original_title']} | {year_text} | {country_text} | {director_text}\n"
        f"{row['url']}"
    )


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        payload = {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 429:
            retry_after = payload.get("parameters", {}).get("retry_after", 5)
            LOGGER.warning(
                "Telegram rate limit hit. Waiting %s seconds before retry %s/%s.",
                retry_after,
                attempt,
                max_attempts,
            )
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return

    raise RuntimeError("Telegram rate limit retries exhausted.")


def notify_new_films(rows: List[Dict[str, Any]], env_path: str, db_path: str) -> int:
    load_dotenv(env_path)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to send notifications.")

    with sqlite3.connect(db_path) as connection:
        init_db(connection)
        sync_films_to_db(connection, rows)
        pending_rows = get_unnotified_rows(connection, [row["slug"] for row in rows])
        for row in pending_rows:
            send_telegram_message(bot_token, chat_id, format_telegram_message(row))
            mark_notified(connection, row["slug"])
            time.sleep(1)
        return len(pending_rows)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Extract films from a MUBI collection, write a CSV, and optionally notify Telegram "
            "for newly added films using SQLite state."
        )
    )
    parser.add_argument("--url", default=DEFAULT_COLLECTION_URL, help="MUBI collection URL.")
    parser.add_argument("--out", default=DEFAULT_CSV_PATH, help="Output CSV path.")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_PATH, help="Environment file path.")
    parser.add_argument("--notify", action="store_true", help="Send Telegram notifications for newly seen films.")
    args = parser.parse_args()

    rows = add_rankings(scrape_collection(args.url))
    if not rows:
        LOGGER.error("No films could be extracted.")
        return 1

    write_csv(rows, args.out)
    print_preview(rows, limit=12)
    LOGGER.info("CSV written to: %s", args.out)

    if args.notify:
        sent_count = notify_new_films(rows, env_path=args.env_file, db_path=args.db_path)
        LOGGER.info("Telegram notifications sent: %s", sent_count)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
