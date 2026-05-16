import sqlite3

import pytest
import requests

import new_on_mubi_notifier as notifier


def sample_row(slug: str, title: str, score_10: float, ratings_count: int) -> dict:
    return {
        "collection_rank": 1,
        "title": title,
        "original_title": title,
        "year": 2024,
        "duration": 88,
        "origin_country": "Chile",
        "director": "Test Director",
        "slug": slug,
        "score_10": score_10,
        "ratings_count": ratings_count,
        "url": f"https://mubi.com/films/{slug}",
    }


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [
        (None, None),
        ("bad", None),
        (4.25, 8.5),
        ("5", 10.0),
        (7.86, 7.9),
    ],
)
def test_to_score_10(raw_score, expected):
    assert notifier.to_score_10(raw_score) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://mubi.com/en/cl/collections/new-on-mubi",
            {"language": "en", "country": "CL"},
        ),
        ("https://mubi.com/es/ar/films/example", {"language": "es", "country": "AR"}),
        (
            "https://mubi.com/collections/new-on-mubi",
            {"language": "en", "country": "CL"},
        ),
    ],
)
def test_parse_url_defaults(url, expected):
    assert notifier.parse_url_defaults(url) == expected


def test_add_rankings_orders_by_score_then_rating_count():
    rows = [
        sample_row("mid", "Mid", 7.0, 100),
        sample_row("top_less_popular", "Top Less Popular", 8.0, 10),
        sample_row("top_more_popular", "Top More Popular", 8.0, 50),
    ]

    ranked = {row["slug"]: row for row in notifier.add_rankings(rows)}

    assert ranked["top_more_popular"]["score_rank"] == 1
    assert ranked["top_less_popular"]["score_rank"] == 2
    assert ranked["mid"]["score_rank"] == 3
    assert ranked["top_more_popular"]["popularity_rank"] == 2
    assert ranked["top_more_popular"]["combined_rank"] == 1.5


def test_parse_omdb_ratings_prefers_ratings_array_and_falls_back_to_fields():
    payload = {
        "imdbID": "tt123",
        "imdbRating": "7.1",
        "imdbVotes": "1,234",
        "Metascore": "68",
        "Ratings": [
            {"Source": "Internet Movie Database", "Value": "7.2/10"},
            {"Source": "Rotten Tomatoes", "Value": "95%"},
        ],
    }

    assert notifier.parse_omdb_ratings(payload) == {
        "imdb_id": "tt123",
        "imdb_rating": "7.2/10",
        "imdb_votes": "1,234",
        "rotten_tomatoes_rating": "95%",
        "metacritic_rating": "68/100",
    }


def test_format_telegram_message_includes_external_ratings_when_available():
    row = sample_row("arco", "Arco", 9.4, 100)
    row.update(
        {
            "imdb_rating": "7.5/10",
            "rotten_tomatoes_rating": "93%",
            "metacritic_rating": "73/100",
        }
    )

    assert notifier.format_telegram_message(row) == (
        "Arco - MUBI 9.4\n"
        "IMDb 7.5/10 | RT 93% | Metacritic 73/100\n"
        "Arco | 2024 | 88 min | Chile | Test Director\n"
        "https://mubi.com/films/arco"
    )


def test_format_telegram_message_omits_external_ratings_when_missing():
    row = sample_row("arco", "Arco", 9.4, 100)

    assert notifier.format_telegram_message(row) == (
        "Arco - MUBI 9.4\n"
        "Arco | 2024 | 88 min | Chile | Test Director\n"
        "https://mubi.com/films/arco"
    )


def test_format_telegram_message_uses_unknown_runtime_when_missing():
    row = sample_row("arco", "Arco", 9.4, 100)
    row["duration"] = None

    assert notifier.format_telegram_message(row) == (
        "Arco - MUBI 9.4\n"
        "Arco | 2024 | Unknown runtime | Chile | Test Director\n"
        "https://mubi.com/films/arco"
    )


def test_retry_delay_seconds_uses_telegram_retry_after():
    response = requests.Response()
    response.status_code = 429
    response._content = b'{"parameters": {"retry_after": 7}}'
    response.headers["Content-Type"] = "application/json"

    assert notifier.retry_delay_seconds(response, attempt=1) == 7.0


def test_retry_delay_seconds_uses_header_retry_after_first():
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "3"
    response._content = b'{"parameters": {"retry_after": 7}}'

    assert notifier.retry_delay_seconds(response, attempt=1) == 3.0


def test_sqlite_sync_preserves_first_seen_and_filters_notified_rows():
    connection = sqlite3.connect(":memory:")
    notifier.init_db(connection)
    rows = notifier.add_rankings([sample_row("arco", "Arco", 9.4, 100)])

    notifier.sync_films_to_db(connection, rows)
    first_seen_at = connection.execute(
        "SELECT first_seen_at FROM films WHERE slug = 'arco'"
    ).fetchone()[0]

    updated_rows = notifier.add_rankings([sample_row("arco", "Arco Updated", 9.2, 120)])
    notifier.sync_films_to_db(connection, updated_rows)
    title, duration, updated_first_seen_at = connection.execute(
        "SELECT title, duration, first_seen_at FROM films WHERE slug = 'arco'"
    ).fetchone()

    assert title == "Arco Updated"
    assert duration == 88
    assert updated_first_seen_at == first_seen_at
    assert len(notifier.get_unnotified_rows(connection, ["arco"])) == 1

    notifier.mark_notified(connection, "arco")

    assert notifier.get_unnotified_rows(connection, ["arco"]) == []


def test_init_db_adds_omdb_columns_to_existing_table():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE films (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )

    notifier.init_db(connection)

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(films)").fetchall()
    }
    assert set(notifier.OMDB_DB_COLUMNS).issubset(columns)
    assert "duration" in columns
