"""Debug entry point: scrape jobs and parse the returned DataFrame rows into JobPost models."""

import csv
import json
import logging
import os

import pandas as pd

from jobspy import scrape_jobs
from jobspy.model import Country, JobPost, JobType, Location

logger = logging.getLogger("debug_run")


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _split(value) -> list[str] | None:
    value = _clean(value)
    if not value:
        return None
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _job_types(value) -> list[JobType] | None:
    parts = _split(value)
    if not parts:
        return None
    lookup = {job_type.value: job_type for job_type in JobType}
    types = [lookup[part] for part in parts if part in lookup]
    return types or None


def _location(value) -> Location | None:
    parts = _split(value)
    if not parts:
        return None
    country: Country | str | None = None
    try:
        country = Country(parts[-1])
        parts = parts[:-1]
    except ValueError:
        if len(parts) > 2:
            country = parts.pop()
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None
    return Location(city=city, state=state, country=country)


def row_to_jobpost(row: dict) -> JobPost | None:
    try:
        data = {field: _clean(row.get(field)) for field in JobPost.model_fields}
        data["job_type"] = _job_types(row.get("job_type"))
        data["emails"] = _split(row.get("emails"))
        data["skills"] = _split(row.get("skills"))
        data["location"] = _location(row.get("location"))
        for int_field in ("company_reviews_count", "vacancy_count"):
            if (value := data.get(int_field)) is not None:
                data[int_field] = int(value)
        return JobPost.model_validate(data)
    except Exception:
        logger.exception("Failed to parse row: %s", row.get("job_url"))
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # e.g. http://scrapeops:<API_KEY>@residential-proxy.scrapeops.io:8181
    proxy = os.environ.get("SCRAPEOPS_PROXY")

    df = scrape_jobs(
        site_name=["linkedin"],
        search_term="Software Engineer, Information technology, AI, ML, Business analyst, Project management, Cloud, DevOps, Networking",
        location="Sri Lanka",
        hours_old=2,
        results_wanted=50,
        fetch_description=True,
        proxies=[proxy] if proxy else None,
    )
    print(f"Scraped {len(df)} rows")
    if df.attrs.get("errors"):
        print(f"Errors: {df.attrs['errors']}")
    if df.attrs.get("warnings"):
        print(f"Warnings: {df.attrs['warnings']}")

    posts = [post for post in (row_to_jobpost(r) for r in df.to_dict("records")) if post]
    print(f"Parsed {len(posts)} JobPost models")

    df.to_csv("jobs.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)
    print(f"Saved {len(df)} rows to jobs.csv")

    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(
            [post.model_dump(mode="json") for post in posts],
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    print(f"Saved {len(posts)} JobPost models to jobs.json")
