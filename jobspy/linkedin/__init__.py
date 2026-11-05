from __future__ import annotations

import random
import re
import time
from datetime import date, datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from jobspy.linkedin.constant import (
    currencies,
    empty_page,
    headers,
    hourly_threshold,
    jobs_per_page,
    lower_limit,
    max_results,
    monthly_threshold,
    pay_intervals,
    salary_range,
    upper_limit,
)
from jobspy.linkedin.util import (
    criteria,
    is_job_remote,
    parse_company_website,
    parse_location,
    parse_time_ago,
)
from jobspy.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobPost,
    Scraper,
    ScraperInput,
    annual_factor,
)
from jobspy.util import (
    RequestsRotating,
    check_status,
    create_session,
    description_fields,
    get_enum_from_job_type,
    plain_converter,
)


class LinkedIn(Scraper):
    base_url = "https://www.linkedin.com"
    delay = (3, 7)

    def scrape(self, scraper_input: ScraperInput) -> list[JobPost]:
        self.scraper_input = scraper_input
        self.company_cache: dict[str, dict[str, object]] = {}
        self.session = create_session(
            RequestsRotating(scraper_input.proxies, clear_cookies=True),
            scraper_input,
            headers,
        )
        for name in ("is_remote", "job_type"):
            if getattr(scraper_input, name):
                self.warn(f"{name} isn't supported, ignoring it")
        params = self._search_params()
        job_list: list[JobPost] = []
        seen = set()
        first_start = start = scraper_input.offset
        page = 1

        while len(job_list) < scraper_input.results_wanted and start < max_results:
            if start > first_start:
                time.sleep(random.uniform(*self.delay))
            self.log.info(f"search page: {page}")
            try:
                response = self.session.get(
                    f"{self.base_url}/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params={**params, "start": start},
                )
                check_status(response)
            except Exception as e:
                self.fail(str(e))
                break

            soup = BeautifulSoup(response.text, "html.parser")
            job_cards = soup.find_all(class_="base-search-card")
            if not job_cards:
                if response.text.strip() != empty_page:
                    self.fail("unexpected page with no jobs (blocked?)")
                elif start:
                    self.log.warning(
                        f"empty page at start={start} (the end of the results, or throttled)"
                    )
                break

            new_cards = []
            for job_card in job_cards:
                job_id = str(job_card.get("data-entity-urn", "")).split(":")[-1]
                if job_id and job_id not in seen:
                    seen.add(job_id)
                    new_cards.append((job_id, job_card))

            for job_id, job_card in new_cards:
                try:
                    job_post = self._process_job(job_card, job_id)
                except Exception as e:
                    self.log.warning(f"skipping job: {e}")
                    continue
                job_list.append(job_post)
                if len(job_list) >= scraper_input.results_wanted:
                    break

            if not new_cards or len(job_cards) < jobs_per_page:
                break
            start += jobs_per_page
            page += 1

        return job_list

    def _search_params(self) -> dict[str, str | int]:
        scraper_input = self.scraper_input
        params: dict[str, str | int | None] = {
            "keywords": scraper_input.search_term,
            "location": scraper_input.location,
            "distance": scraper_input.distance,
        }
        if scraper_input.easy_apply:
            params["f_AL"] = "true"
        if scraper_input.linkedin_company_ids:
            params["f_C"] = ",".join(map(str, scraper_input.linkedin_company_ids))
        if scraper_input.hours_old:
            params["f_TPR"] = f"r{scraper_input.hours_old * 3600}"
        return {name: value for name, value in params.items() if value is not None}

    def _process_job(self, job_card: Tag, job_id: str) -> JobPost:
        title_tag = job_card.find("h3", class_="base-search-card__title")
        if title_tag is None:
            raise ValueError("no title")
        title = title_tag.get_text(strip=True)

        company_tag = job_card.find("h4", class_="base-search-card__subtitle")
        if company_tag is None:
            raise ValueError("no company")
        company_link = company_tag.find("a", href=True)
        location_tag = job_card.find("span", class_="job-search-card__location")
        if location_tag is None:
            raise ValueError("no location")
        location = parse_location(location_tag.text.strip())
        time_tag = job_card.find("time")
        try:
            date_posted = (
                date.fromisoformat(str(time_tag["datetime"])) if time_tag else None
            )
        except (TypeError, KeyError, ValueError):
            date_posted = None
        date_estimated = self._estimate_posted_at(time_tag, date_posted)
        job_details = (
            self._fetch_details(job_id) if self.scraper_input.fetch_description else {}
        )
        company_url = str(company_link["href"]).split("?")[0] if company_link else None
        if self.scraper_input.linkedin_fetch_company_details and company_url:
            job_details |= self._fetch_company_details(company_url)

        return JobPost(
            id=f"li-{job_id}",
            title=title,
            company=company_tag.get_text(strip=True),
            company_url=company_url,
            location=location,
            is_remote=is_job_remote(title, location),
            date_posted=date_posted,
            date_estimated=date_estimated,
            job_url=f"{self.base_url}/jobs/view/{job_id}",
        ).model_copy(update=job_details)

    @staticmethod
    def _estimate_posted_at(
        time_tag: Tag | None, date_posted: date | None
    ) -> datetime | None:
        """
        Now minus the card's "N hours ago"; midnight of date_posted when that's coarser
        """
        delta = parse_time_ago(time_tag.get_text(" ", strip=True)) if time_tag else None
        if delta is not None:
            return (datetime.now() - delta).replace(microsecond=0)
        if date_posted is not None:
            return datetime.combine(date_posted, datetime.min.time())
        return None

    @staticmethod
    def _parse_salary(text: str) -> Compensation:
        """ "$117,000.00/yr - $234,000.00/yr" """
        match = re.fullmatch(
            r"([^\d\s]+) ?([\d,]+\.\d\d)(/\w+) - [^\d\s]+ ?([\d,]+\.\d\d)/\w+",
            " ".join(text.split()),
        )
        if not match:
            return Compensation()
        currency, low, interval, high = match.groups()
        return Compensation(
            interval=pay_intervals.get(interval),
            min_amount=float(low.replace(",", "")),
            max_amount=float(high.replace(",", "")),
            currency=currencies.get(currency, currency),
        )

    @staticmethod
    def _guess_salary(description: str | None) -> Compensation:
        """The first "$min - $max" of the text; the interval from the size of the amounts"""
        match = salary_range.search(description or "")
        if not match:
            return Compensation()
        low, high = (float(match[i].replace(",", "")) for i in (1, 3))
        if match[2] or match[4]:
            low, high = (round(x * 1000, 2) if x < 1000 else x for x in (low, high))
        if low < 1:
            return Compensation()
        if low < hourly_threshold:
            interval, below = CompensationInterval.HOURLY, hourly_threshold
        elif low < monthly_threshold:
            interval, below = CompensationInterval.MONTHLY, monthly_threshold
        else:
            interval, below = CompensationInterval.YEARLY, upper_limit + 1
        factor = annual_factor[interval.value]
        if high >= below or not (
            lower_limit <= low * factor < high * factor <= upper_limit
        ):
            return Compensation()
        return Compensation(
            interval=interval, min_amount=low, max_amount=high, currency="USD"
        )

    def _fetch_details(self, job_id: str) -> dict[str, object]:
        """
        The job page's description, pay, logo and criteria; empty when the page can't be read
        """
        try:
            response = self.session.get(f"{self.base_url}/jobs/view/{job_id}")
            check_status(response)
        except Exception as e:
            self.log.warning(f"job {job_id}: {e}")
            return {}
        path = urlparse(response.url).path
        if not path.startswith("/jobs/view/"):
            self.log.warning(f"job {job_id}: redirected to {path}")
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        div_content = soup.find("div", class_="show-more-less-html__markup")
        if div_content:
            div_content.attrs.clear()
        html = div_content.prettify(formatter="html") if div_content else None
        fields = description_fields(html, self.scraper_input.description_format)
        logo_image = soup.find("img", class_="artdeco-entity-image")
        salary_tag = soup.find("div", class_="compensation__salary")
        employment_type = criteria(soup, "Employment type")
        job_type = (
            get_enum_from_job_type(employment_type.lower().replace("-", ""))
            if employment_type
            else None
        )
        job_level = criteria(soup, "Seniority level")
        pay = self._parse_salary(salary_tag.get_text() if salary_tag else "")
        source = None
        if pay == Compensation() and self.scraper_input.country_indeed == Country.USA:
            pay, source = self._guess_salary(plain_converter(html)), "description"
        return {
            **fields,
            **pay.model_dump(),
            "salary_source": source,
            "job_type": [job_type] if job_type else None,
            "job_level": job_level.lower() if job_level else None,
            "job_function": criteria(soup, "Job function"),
            "company_industry": criteria(soup, "Industries"),
            "company_logo": logo_image.get("data-delayed-url") if logo_image else None,
        }

    def _fetch_company_details(self, company_url: str) -> dict[str, object]:
        """
        The company page's website, cached per company; empty when the page can't be read
        """
        slug = company_url.rstrip("/").split("/")[-1]
        if not slug:
            return {}
        if slug in self.company_cache:
            return self.company_cache[slug]
        details: dict[str, object] = {}
        try:
            response = self.session.get(f"{self.base_url}/company/{slug}")
            check_status(response)
            path = urlparse(response.url).path
            if path.startswith("/company/"):
                soup = BeautifulSoup(response.text, "html.parser")
                details = {"company_url_direct": parse_company_website(soup)}
            else:
                self.log.warning(f"company {slug}: redirected to {path}")
        except Exception as e:
            self.log.warning(f"company {slug}: {e}")
        self.company_cache[slug] = details
        return details
