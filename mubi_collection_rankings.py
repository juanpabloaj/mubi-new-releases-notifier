#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


DEFAULT_COLLECTION_URL = "https://mubi.com/en/cl/collections/new-on-mubi"
API_BASE = "https://api.mubi.com/v4"


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


def normalize_film_data(film: Dict[str, Any]) -> Dict[str, Any]:
    countries = film.get("historic_countries") or []
    return {
        "title": film.get("title"),
        "original_title": film.get("original_title") or film.get("title"),
        "year": film.get("year"),
        "origin_country": ", ".join(countries) if countries else None,
        "slug": film.get("slug"),
        "score_10": to_score_10(film.get("average_rating")),
        "ratings_count": film.get("number_of_ratings"),
    }


def parse_url_defaults(collection_url: str) -> Dict[str, str]:
    parsed = urlparse(collection_url)
    parts = [p for p in parsed.path.split("/") if p]
    language = "en"
    country = "CL"
    if len(parts) >= 2 and len(parts[0]) in (2, 5) and len(parts[1]) == 2:
        language = parts[0]
        country = parts[1].upper()
    return {"language": language, "country": country}


def bootstrap_context(session: requests.Session, collection_url: str) -> Dict[str, Any]:
    resp = session.get(collection_url, timeout=45)
    resp.raise_for_status()
    data = extract_next_data(resp.text)
    if not data:
        raise RuntimeError("Could not extract embedded JSON from the collection page.")
    page_props = data.get("props", {}).get("pageProps", {})
    collection = page_props.get("collection") or {}
    http_context = data.get("props", {}).get("httpContext", {})
    return {"collection": collection, "http_context": http_context}


def build_api_headers(collection_url: str, http_context: Dict[str, Any]) -> Dict[str, str]:
    defaults = parse_url_defaults(collection_url)
    anon_id = http_context.get("ANONYMOUS_USER_ID") or str(uuid.uuid4())
    return {
        "CLIENT": "web",
        "accept-language": http_context.get("accept-language") or defaults["language"],
        "Client-Country": http_context.get("Client-Country") or defaults["country"],
        "ANONYMOUS_USER_ID": anon_id,
    }


def fetch_collection_films(
    session: requests.Session,
    collection_slug: str,
    headers: Dict[str, str],
    total_items: Optional[int],
    per_page: int = 12,
) -> List[Dict[str, Any]]:
    films: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = f"{API_BASE}/collections/{collection_slug}/films"
        resp = session.get(url, params={"page": page, "per_page": per_page}, headers=headers, timeout=45)
        resp.raise_for_status()
        payload = resp.json()
        page_films = payload.get("films", [])
        if not page_films:
            break
        films.extend(page_films)
        if total_items and len(films) >= total_items:
            break
        if len(page_films) < per_page:
            break
        page += 1
    return films


def scrape_collection(collection_url: str) -> List[Dict[str, Any]]:
    session = requests.Session()
    bootstrap = bootstrap_context(session, collection_url)
    collection = bootstrap["collection"]
    http_context = bootstrap["http_context"]

    collection_slug = collection.get("slug")
    if not collection_slug:
        raise RuntimeError("Could not find the collection slug in the page payload.")
    total_items = collection.get("total_items")
    headers = build_api_headers(collection_url, http_context)

    api_films = fetch_collection_films(session, collection_slug, headers, total_items=total_items, per_page=12)
    results: List[Dict[str, Any]] = []
    for idx, film in enumerate(api_films, start=1):
        normalized = normalize_film_data(film)
        normalized["collection_rank"] = idx
        normalized["url"] = f"https://mubi.com/films/{normalized['slug']}"
        results.append(normalized)
    return results


def add_rankings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_score = sorted(
        rows,
        key=lambda x: (x["score_10"] is not None, x["score_10"] or 0, x.get("ratings_count") or 0),
        reverse=True,
    )
    by_popularity = sorted(
        rows,
        key=lambda x: ((x.get("ratings_count") is not None), x.get("ratings_count") or 0),
        reverse=True,
    )

    score_rank = {row["slug"]: i + 1 for i, row in enumerate(by_score)}
    popularity_rank = {row["slug"]: i + 1 for i, row in enumerate(by_popularity)}

    enriched = []
    for row in rows:
        slug = row["slug"]
        merged = dict(row)
        merged["score_rank"] = score_rank.get(slug)
        merged["popularity_rank"] = popularity_rank.get(slug)
        if merged["score_rank"] and merged["popularity_rank"]:
            merged["combined_rank"] = round((merged["score_rank"] + merged["popularity_rank"]) / 2.0, 2)
        else:
            merged["combined_rank"] = None
        enriched.append(merged)
    return enriched


def write_csv(rows: List[Dict[str, Any]], output_path: str) -> None:
    rows_for_csv = sorted(
        rows,
        key=lambda r: (
            r["combined_rank"] is None,
            r["combined_rank"] if r["combined_rank"] is not None else 9999,
            r["score_rank"] if r["score_rank"] is not None else 9999,
            r["popularity_rank"] if r["popularity_rank"] is not None else 9999,
        ),
    )
    fieldnames = [
        "collection_rank",
        "title",
        "original_title",
        "year",
        "origin_country",
        "score_10",
        "ratings_count",
        "score_rank",
        "popularity_rank",
        "combined_rank",
        "slug",
        "url",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_for_csv)


def print_preview(rows: List[Dict[str, Any]], limit: int = 10) -> None:
    rows_sorted = sorted(rows, key=lambda x: x["collection_rank"])
    print(f"Extracted films: {len(rows_sorted)}")
    print("First results in collection order:")
    for row in rows_sorted[:limit]:
        print(
            f"{row['collection_rank']:>2}. {row['title']} "
            f"| original: {row['original_title']} | {row['year']} "
            f"| country: {row['origin_country']} "
            f"| score: {row['score_10']} | ratings: {row['ratings_count']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract films from a MUBI collection with original title, year, origin country, and rankings."
    )
    parser.add_argument("--url", default=DEFAULT_COLLECTION_URL, help="MUBI collection URL.")
    parser.add_argument("--out", default="mubi_collection_rankings.csv", help="Output CSV path.")
    args = parser.parse_args()

    rows = scrape_collection(args.url)
    if not rows:
        print("No films could be extracted.", file=sys.stderr)
        return 1

    rows = add_rankings(rows)
    write_csv(rows, args.out)
    print_preview(rows, limit=12)
    print(f"\nCSV written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
