#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests


DEFAULT_COLLECTION_URL = "https://mubi.com/en/cl/collections/new-on-mubi"
DEFAULT_CSV_PATH = "new_on_mubi.csv"
DEFAULT_DB_PATH = "mubi_notifications.db"
DEFAULT_ENV_PATH = ".env"
API_BASE = "https://api.mubi.com/v4"
OMDB_API_BASE = "https://www.omdbapi.com/"
LOGGER = logging.getLogger("mubi_notifier")
MAX_HTTP_ATTEMPTS = 4
MUBI_REQUEST_DELAY_SECONDS = 0.5
OMDB_REQUEST_DELAY_SECONDS = 1.0
TELEGRAM_SEND_DELAY_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
OMDB_RATING_FIELDS = [
    "imdb_id",
    "imdb_rating",
    "imdb_votes",
    "rotten_tomatoes_rating",
    "metacritic_rating",
    "omdb_checked_at",
]
OMDB_DB_COLUMNS = {
    "imdb_id": "TEXT",
    "imdb_rating": "TEXT",
    "imdb_votes": "TEXT",
    "rotten_tomatoes_rating": "TEXT",
    "metacritic_rating": "TEXT",
    "omdb_checked_at": "TEXT",
}


class OmdbUnavailableError(RuntimeError):
    pass


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


def get_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        LOGGER.warning(
            "Invalid %s value %r. Using default %s.", name, raw_value, default
        )
        return default
    return max(0.0, value)


def retry_delay_seconds(response: Optional[requests.Response], attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        retry_after = payload.get("parameters", {}).get("retry_after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
    return (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)


def _request_with_retries(
    request_func: Callable[..., requests.Response],
    method: str,
    url: str,
    *,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
    **kwargs: Any,
) -> requests.Response:
    last_error: Optional[requests.RequestException] = None
    for attempt in range(1, max_attempts + 1):
        response = None
        try:
            response = request_func(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_attempts:
                raise
            delay = retry_delay_seconds(response, attempt)
            LOGGER.warning(
                "HTTP %s %s failed: %s. Waiting %.1f seconds before retry %s/%s.",
                method.upper(),
                url,
                exc,
                delay,
                attempt,
                max_attempts,
            )
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt == max_attempts:
            return response

        delay = retry_delay_seconds(response, attempt)
        LOGGER.warning(
            "HTTP %s %s returned %s. Waiting %.1f seconds before retry %s/%s.",
            method.upper(),
            url,
            response.status_code,
            delay,
            attempt,
            max_attempts,
        )
        time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("HTTP retry loop exited unexpectedly.")


def request_with_retries(
    method: str,
    url: str,
    *,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
    **kwargs: Any,
) -> requests.Response:
    return _request_with_retries(
        requests.request,
        method,
        url,
        max_attempts=max_attempts,
        **kwargs,
    )


def session_request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
    **kwargs: Any,
) -> requests.Response:
    return _request_with_retries(
        session.request,
        method,
        url,
        max_attempts=max_attempts,
        **kwargs,
    )


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


def with_empty_omdb_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(row)
    for field in OMDB_RATING_FIELDS:
        enriched.setdefault(field, None)
    return enriched


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
    response = session_request_with_retries(session, "GET", collection_url, timeout=45)
    response.raise_for_status()
    data = extract_next_data(response.text)
    if not data:
        raise RuntimeError("Could not extract embedded JSON from the collection page.")
    page_props = data.get("props", {}).get("pageProps", {})
    collection = page_props.get("collection") or {}
    http_context = data.get("props", {}).get("httpContext", {})
    return {"collection": collection, "http_context": http_context}


def build_api_headers(
    collection_url: str, http_context: Dict[str, Any]
) -> Dict[str, str]:
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
    request_delay_seconds: float = MUBI_REQUEST_DELAY_SECONDS,
) -> List[Dict[str, Any]]:
    films: List[Dict[str, Any]] = []
    page_num = 1
    while True:
        response = session_request_with_retries(
            session,
            "GET",
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
        time.sleep(request_delay_seconds)
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
        request_delay_seconds=get_float_env(
            "MUBI_REQUEST_DELAY_SECONDS",
            MUBI_REQUEST_DELAY_SECONDS,
        ),
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
    popularity_rank = {
        row["slug"]: index + 1 for index, row in enumerate(by_popularity)
    }

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


def parse_omdb_ratings(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    ratings_by_source = {
        rating.get("Source"): rating.get("Value")
        for rating in payload.get("Ratings", [])
        if rating.get("Source") and rating.get("Value")
    }
    return {
        "imdb_id": payload.get("imdbID"),
        "imdb_rating": ratings_by_source.get("Internet Movie Database")
        or (
            f"{payload['imdbRating']}/10"
            if payload.get("imdbRating") and payload.get("imdbRating") != "N/A"
            else None
        ),
        "imdb_votes": payload.get("imdbVotes")
        if payload.get("imdbVotes") and payload.get("imdbVotes") != "N/A"
        else None,
        "rotten_tomatoes_rating": ratings_by_source.get("Rotten Tomatoes"),
        "metacritic_rating": ratings_by_source.get("Metacritic")
        or (
            f"{payload['Metascore']}/100"
            if payload.get("Metascore") and payload.get("Metascore") != "N/A"
            else None
        ),
    }


def fetch_omdb_ratings(
    session: requests.Session,
    api_key: str,
    title: str,
    year: Optional[int],
) -> Optional[Dict[str, Optional[str]]]:
    params: Dict[str, Any] = {"apikey": api_key, "t": title, "type": "movie"}
    if year:
        params["y"] = year
    response = session_request_with_retries(
        session,
        "GET",
        OMDB_API_BASE,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("Response") != "True":
        error = payload.get("Error", "Unknown OMDb error")
        if "not found" not in error.lower():
            raise OmdbUnavailableError(error)
        LOGGER.info("OMDb match not found for: %s (%s)", title, year or "unknown year")
        return None
    return parse_omdb_ratings(payload)


def get_cached_omdb_ratings(
    connection: sqlite3.Connection, slug: str
) -> Optional[Dict[str, Optional[str]]]:
    cursor = connection.execute(
        """
        SELECT
            imdb_id, imdb_rating, imdb_votes, rotten_tomatoes_rating,
            metacritic_rating, omdb_checked_at
        FROM films
        WHERE slug = ?
          AND omdb_checked_at IS NOT NULL
        """,
        (slug,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return dict(zip(OMDB_RATING_FIELDS, row))


def update_omdb_cache(
    connection: sqlite3.Connection,
    slug: str,
    ratings: Dict[str, Optional[str]],
    checked_at: str,
) -> None:
    connection.execute(
        """
        UPDATE films
        SET
            imdb_id = ?,
            imdb_rating = ?,
            imdb_votes = ?,
            rotten_tomatoes_rating = ?,
            metacritic_rating = ?,
            omdb_checked_at = ?
        WHERE slug = ?
        """,
        (
            ratings.get("imdb_id"),
            ratings.get("imdb_rating"),
            ratings.get("imdb_votes"),
            ratings.get("rotten_tomatoes_rating"),
            ratings.get("metacritic_rating"),
            checked_at,
            slug,
        ),
    )
    connection.commit()


def enrich_rows_with_omdb(
    connection: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    api_key: Optional[str],
    request_delay_seconds: float = OMDB_REQUEST_DELAY_SECONDS,
) -> List[Dict[str, Any]]:
    rows = [with_empty_omdb_fields(row) for row in rows]
    if not api_key:
        return rows

    session = requests.Session()
    enriched_rows = []
    for index, row in enumerate(rows):
        cached = get_cached_omdb_ratings(connection, row["slug"])
        if cached:
            enriched_rows.append({**row, **cached})
            continue

        ratings = None
        titles = [row["title"]]
        if row.get("original_title") and row["original_title"] not in titles:
            titles.append(row["original_title"])
        for title in titles:
            if not title:
                continue
            try:
                ratings = fetch_omdb_ratings(session, api_key, title, row.get("year"))
            except (requests.RequestException, OmdbUnavailableError) as exc:
                LOGGER.warning(
                    "OMDb enrichment disabled after failure for %s: %s", title, exc
                )
                enriched_rows.append(row)
                enriched_rows.extend(rows[index + 1 :])
                return enriched_rows
            time.sleep(request_delay_seconds)
            if ratings:
                break

        checked_at = utc_now_iso()
        ratings = ratings or {}
        update_omdb_cache(connection, row["slug"], ratings, checked_at)
        enriched_rows.append(
            {
                **row,
                **ratings,
                "omdb_checked_at": checked_at,
            }
        )
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
        "imdb_rating",
        "imdb_votes",
        "rotten_tomatoes_rating",
        "metacritic_rating",
        "imdb_id",
        "score_rank",
        "popularity_rank",
        "combined_rank",
        "slug",
        "url",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_for_csv(rows))


def log_pending_rows(rows: List[Dict[str, Any]], limit: int = 10) -> None:
    LOGGER.info("Pending new films: %s", len(rows))
    if not rows:
        return
    LOGGER.info("Pending films to be added or notified:")
    for row in rows[:limit]:
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
            imdb_id TEXT,
            imdb_rating TEXT,
            imdb_votes TEXT,
            rotten_tomatoes_rating TEXT,
            metacritic_rating TEXT,
            omdb_checked_at TEXT,
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
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(films)").fetchall()
    }
    for column_name, column_type in OMDB_DB_COLUMNS.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE films ADD COLUMN {column_name} {column_type}"
            )
    connection.commit()


def sync_films_to_db(
    connection: sqlite3.Connection, rows: List[Dict[str, Any]]
) -> None:
    synced_at = utc_now_iso()
    for row in rows:
        row = with_empty_omdb_fields(row)
        connection.execute(
            """
            INSERT INTO films (
                slug, title, original_title, year, origin_country, director,
                score_10, ratings_count, imdb_id, imdb_rating, imdb_votes,
                rotten_tomatoes_rating, metacritic_rating, omdb_checked_at,
                score_rank, popularity_rank, combined_rank, collection_rank, url,
                first_seen_at, last_seen_at, notified_at
            ) VALUES (
                :slug, :title, :original_title, :year, :origin_country, :director,
                :score_10, :ratings_count, :imdb_id, :imdb_rating, :imdb_votes,
                :rotten_tomatoes_rating, :metacritic_rating, :omdb_checked_at,
                :score_rank, :popularity_rank, :combined_rank, :collection_rank, :url,
                :first_seen_at, :last_seen_at, NULL
            )
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                original_title = excluded.original_title,
                year = excluded.year,
                origin_country = excluded.origin_country,
                director = excluded.director,
                score_10 = excluded.score_10,
                ratings_count = excluded.ratings_count,
                imdb_id = COALESCE(excluded.imdb_id, films.imdb_id),
                imdb_rating = COALESCE(excluded.imdb_rating, films.imdb_rating),
                imdb_votes = COALESCE(excluded.imdb_votes, films.imdb_votes),
                rotten_tomatoes_rating = COALESCE(
                    excluded.rotten_tomatoes_rating,
                    films.rotten_tomatoes_rating
                ),
                metacritic_rating = COALESCE(
                    excluded.metacritic_rating,
                    films.metacritic_rating
                ),
                omdb_checked_at = COALESCE(
                    excluded.omdb_checked_at,
                    films.omdb_checked_at
                ),
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


def get_unnotified_rows(
    connection: sqlite3.Connection, current_slugs: List[str]
) -> List[Dict[str, Any]]:
    if not current_slugs:
        return []
    placeholders = ", ".join("?" for _ in current_slugs)
    query = f"""
        SELECT
            slug, title, original_title, year, origin_country, director,
            score_10, ratings_count, imdb_id, imdb_rating, imdb_votes,
            rotten_tomatoes_rating, metacritic_rating, omdb_checked_at,
            score_rank, popularity_rank, combined_rank, collection_rank, url,
            first_seen_at, last_seen_at, notified_at
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


def format_external_ratings(row: Dict[str, Any]) -> Optional[str]:
    parts = []
    if row.get("imdb_rating"):
        parts.append(f"IMDb {row['imdb_rating']}")
    if row.get("rotten_tomatoes_rating"):
        parts.append(f"RT {row['rotten_tomatoes_rating']}")
    if row.get("metacritic_rating"):
        parts.append(f"Metacritic {row['metacritic_rating']}")
    return " | ".join(parts) if parts else None


def format_telegram_message(row: Dict[str, Any]) -> str:
    score_text = f"{row['score_10']:.1f}" if row.get("score_10") is not None else "N/A"
    year_text = str(row["year"]) if row.get("year") is not None else "Unknown year"
    country_text = row.get("origin_country") or "Unknown country"
    director_text = row.get("director") or "Unknown director"
    lines = [f"{row['title']} - MUBI {score_text}"]
    external_ratings = format_external_ratings(row)
    if external_ratings:
        lines.append(external_ratings)
    lines.extend(
        [
            f"{row['original_title']} | {year_text} | {country_text} | {director_text}",
            row["url"],
        ]
    )
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    response = request_with_retries(
        "POST",
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        max_attempts=6,
        data={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def notify_rows(
    connection: sqlite3.Connection, rows: List[Dict[str, Any]], env_path: str
) -> int:
    load_dotenv(env_path)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to send notifications."
        )

    send_delay_seconds = get_float_env(
        "TELEGRAM_SEND_DELAY_SECONDS",
        TELEGRAM_SEND_DELAY_SECONDS,
    )
    for row in rows:
        send_telegram_message(bot_token, chat_id, format_telegram_message(row))
        mark_notified(connection, row["slug"])
        time.sleep(send_delay_seconds)
    return len(rows)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Extract films from a MUBI collection, optionally enrich external ratings, "
            "write a CSV, and optionally notify Telegram for newly added films using "
            "SQLite state."
        )
    )
    parser.add_argument(
        "--url", default=DEFAULT_COLLECTION_URL, help="MUBI collection URL."
    )
    parser.add_argument("--out", default=DEFAULT_CSV_PATH, help="Output CSV path.")
    parser.add_argument(
        "--db-path", default=DEFAULT_DB_PATH, help="SQLite database path."
    )
    parser.add_argument(
        "--env-file", default=DEFAULT_ENV_PATH, help="Environment file path."
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send Telegram notifications for newly seen films.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file)
    rows = [
        with_empty_omdb_fields(row) for row in add_rankings(scrape_collection(args.url))
    ]
    if not rows:
        LOGGER.error("No films could be extracted.")
        return 1

    with sqlite3.connect(args.db_path) as connection:
        init_db(connection)
        sync_films_to_db(connection, rows)
        rows = enrich_rows_with_omdb(
            connection,
            rows,
            api_key=os.getenv("OMDB_API_KEY"),
            request_delay_seconds=get_float_env(
                "OMDB_REQUEST_DELAY_SECONDS",
                OMDB_REQUEST_DELAY_SECONDS,
            ),
        )
        write_csv(rows, args.out)
        LOGGER.info("CSV written to: %s", args.out)

        sync_films_to_db(connection, rows)
        pending_rows = get_unnotified_rows(connection, [row["slug"] for row in rows])

        log_pending_rows(pending_rows, limit=12)

        if args.notify:
            sent_count = notify_rows(connection, pending_rows, env_path=args.env_file)
            LOGGER.info("Telegram notifications sent: %s", sent_count)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
