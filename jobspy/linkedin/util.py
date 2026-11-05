import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from jobspy.linkedin.constant import remote_keywords
from jobspy.model import Country, Location


def criteria(soup: BeautifulSoup, heading: str) -> str | None:
    """
    The value under a heading of the job page's criteria list ("Seniority level", "Employment type", ...)
    """
    h3_tag = soup.find(
        "h3",
        class_="description__job-criteria-subheader",
        string=lambda text: text is not None and heading in text,
    )
    span = (
        h3_tag.find_next_sibling("span", class_="description__job-criteria-text")
        if h3_tag
        else None
    )
    return span.get_text(strip=True) if span else None


def is_job_remote(title: str, location: Location) -> bool:
    """
    A remote keyword in the title or location
    """
    text = f"{title} {location.display_location()}".lower()
    return any(keyword in text for keyword in remote_keywords)


def parse_location(text: str) -> Location:
    """
    "City, State, Country", "City, ST" (US jobs), "City, Country", or one name: a country or an area
    """
    parts = text.split(", ")
    if len(parts) == 3:
        city, state, country = parts
        return Location(city=city, state=state, country=country)
    if len(parts) == 2:
        city, last = parts
        if re.fullmatch(r"[A-Z]{2}", last):
            return Location(city=city, state=last, country=Country.USA)
        return Location(city=city, country=last)
    try:
        Country(text)
        return Location(country=text)
    except ValueError:
        return Location(city=text)


def parse_time_ago(text: str) -> timedelta | None:
    """
    "Just now", "30 seconds ago", "5 minutes ago", "3 hours ago" or "2 days ago" as a
    timedelta; None for anything coarser ("1 week ago") or unreadable
    """
    text = text.strip().lower()
    if "now" in text:
        return timedelta()
    match = re.match(r"(\d+(?:\.\d+)?)\s*(second|minute|hour|day)", text)
    if not match:
        return None
    amount, unit = float(match[1]), match[2]
    return timedelta(**{f"{unit}s": amount})


def parse_company_website(soup: BeautifulSoup) -> str | None:
    """
    The company's own website from its LinkedIn company page
    """
    website_div = soup.find("div", attrs={"data-test-id": "about-us__website"})
    a_tag = website_div.find("a") if website_div else None
    if not a_tag:
        return None
    url_values = parse_qs(urlparse(str(a_tag.get("href", ""))).query).get("url")
    if url_values:
        return url_values[0]
    return a_tag.get_text(strip=True) or None
