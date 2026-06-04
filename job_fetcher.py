import os
import re
import json
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ── Shared HTTP session with connection pooling ───────────────────────────────
# Reusing TCP connections cuts ~100–200 ms per request vs. opening a fresh
# socket every time.  Pool of 30 connections handles all parallel workers.
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=30,
    pool_maxsize=60,
    max_retries=Retry(total=1, backoff_factor=0.2, status_forcelist=[500, 502, 503]),
)
_session.mount("http://",  _adapter)
_session.mount("https://", _adapter)

# Loaded fresh each run from the config written by the web UI
with open("config.json", "r", encoding="utf-8") as f:
    _cfg = json.load(f)

ADZUNA_APP_ID    = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY   = os.getenv("ADZUNA_APP_KEY")
USAJOBS_API_KEY  = os.getenv("USAJOBS_API_KEY")
USAJOBS_EMAIL    = os.getenv("USAJOBS_EMAIL")
JOOBLE_API_KEY   = os.getenv("JOOBLE_API_KEY")
FINDWORK_API_KEY = os.getenv("FINDWORK_API_KEY")

TARGET_ROLES         = _cfg.get("target_roles", [])
GREENHOUSE_COMPANIES = _cfg.get("greenhouse_companies", [])

LEVER_COMPANIES = _cfg.get("lever_companies", [
    "coinbase", "robinhood", "chime", "gusto", "lattice", "figma",
    "discord", "retool", "brex", "clickup", "carta", "benchling",
    "duolingo", "coursera", "superhuman", "airtable", "coda",
    "opendoor", "affirm", "marqeta", "samsara", "plaid", "rippling",
    "scale-ai", "loom", "notion", "zapier", "hex", "dbt-labs",
    "cockroachdb", "temporal", "highspot", "gong", "outreach",
    "replit", "linear", "mercury", "ramp", "deel", "remote",
    "sourcegraph", "netlify", "hasura", "prisma", "supabase",
])

JOB_SOURCES = _cfg.get("job_sources", {
    "adzuna": True, "remotive": True, "arbeitnow": True, "greenhouse": True,
    "themuse": True, "remoteok": True, "jobicy": True,
    "usajobs": True, "jooble": True, "findwork": True,
    "indeed":        True,
    "lever":         True,
    "weworkremotely":True,
    "workingnomads": True,
    "hnhiring":      True,
})
MAX_YEARS = _cfg.get("max_years_experience", 0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exceeds_experience(description, title=""):
    if MAX_YEARS == 0:
        return False
    if title:
        t = title.lower()
        seniority_tiers = [
            (2, ["senior", "sr ", "sr.", "lead ", "staff ", "principal",
                 "director", "vp ", "head of", "architect"]),
            (4, ["staff ", "principal", "director", "vp ", "head of",
                 "architect", "distinguished"]),
            (7, ["principal", "director", "vp ", "head of", "distinguished"]),
        ]
        for threshold, words in seniority_tiers:
            if MAX_YEARS <= threshold and any(w in t for w in words):
                return True
    if not description:
        return False
    text = description.lower()
    patterns = [
        r"(\d+)\s*\+?\s*(?:to|-|–)?\s*\d*\s*years?",
        r"minimum\s+(?:of\s+)?(\d+)\s+years?",
        r"at\s+least\s+(\d+)\s+years?",
        r"(\d+)\s+or\s+more\s+years?",
        r"(\d+)\s+years?\s+(?:or\s+more|and\s+above)",
        r"experience[:\s]+(\d+)\s*\+?\s*years?",
    ]
    for pat in patterns:
        for m in re.findall(pat, text):
            try:
                if int(m) > MAX_YEARS:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def _parse_date(date_str, description=None):
    if not date_str and description:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", description)
        if match:
            date_str = match.group(1)
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(date_str[:19], fmt)
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=None)
    except Exception:
        return None


def _recent(date_str, description=None):
    dt = _parse_date(date_str, description)
    if dt is None:
        return True
    try:
        dt = dt.replace(tzinfo=None)
    except Exception:
        pass
    return datetime.utcnow() - dt <= timedelta(days=7)


def _recent_monthly(date_str):
    dt = _parse_date(date_str)
    if dt is None:
        return True
    try:
        dt = dt.replace(tzinfo=None)
    except Exception:
        pass
    return datetime.utcnow() - dt <= timedelta(days=35)


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s{2,}", " ", text).strip()


def _matches_role(title):
    return any(role.lower() in title.lower() for role in TARGET_ROLES)


def _job(title, company, location, description, url, created):
    return {
        "title": title, "company": company, "location": location,
        "description": description, "redirect_url": url, "created": created,
    }


# ── Source: Adzuna ────────────────────────────────────────────────────────────
# Each target role fires a separate request — run them ALL in parallel.

def fetch_adzuna_jobs():
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []

    def _fetch_one(role):
        batch = []
        try:
            data = _session.get(
                "https://api.adzuna.com/v1/api/jobs/us/search/1",
                params={
                    "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                    "what": role, "results_per_page": 20, "sort_by": "date",
                },
                timeout=10,
            ).json()
        except Exception:
            return batch
        for j in data.get("results", []):
            title   = j.get("title", "")
            desc    = j.get("description", "")
            created = j.get("created")
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(
                title,
                j.get("company", {}).get("display_name", "Unknown"),
                j.get("location", {}).get("display_name", "Unknown"),
                desc, j.get("redirect_url"), created,
            ))
        return batch

    workers = min(len(TARGET_ROLES), 8) if TARGET_ROLES else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, TARGET_ROLES)
    return [j for batch in results for j in batch]


# ── Source: Remotive ──────────────────────────────────────────────────────────

def fetch_remotive_jobs():
    try:
        resp = _session.get("https://remotive.io/api/remote-jobs", timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    jobs = []
    for j in data.get("jobs", []):
        title   = j.get("title", "")
        desc    = j.get("description", "")
        created = j.get("publication_date")
        if not _matches_role(title) or not _recent(created, desc) or _exceeds_experience(desc, title):
            continue
        jobs.append(_job(title, j.get("company_name"), "Remote",
                         desc, j.get("url"), created))
    return jobs


# ── Source: Arbeitnow ─────────────────────────────────────────────────────────

def fetch_arbeitnow_jobs():
    try:
        data = _session.get(
            "https://www.arbeitnow.com/api/job-board-api", timeout=10
        ).json()
    except Exception:
        return []
    jobs = []
    for j in data.get("data", []):
        title    = j.get("title", "")
        desc     = j.get("description", "")
        created  = j.get("created_at")
        location = ", ".join(j.get("locations", []))
        if not _matches_role(title):
            continue
        if "United States" not in location and "Remote" not in location:
            continue
        if not _recent(created, desc) or _exceeds_experience(desc, title):
            continue
        jobs.append(_job(title, j.get("company"), location,
                         desc, j.get("url"), created))
    return jobs


# ── Source: Greenhouse ATS ────────────────────────────────────────────────────

def fetch_greenhouse_jobs():
    if not GREENHOUSE_COMPANIES:
        return []

    def _fetch_one_gh(company):
        batch = []
        try:
            data = _session.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs",
                timeout=10,
            ).json()
        except Exception:
            return batch
        for j in data.get("jobs", []):
            title    = j.get("title", "")
            location = j.get("location", {}).get("name", "")
            created  = j.get("updated_at")
            desc     = j.get("content", "")
            if not _matches_role(title):
                continue
            if "United States" not in location and "Remote" not in location:
                continue
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(title, company.capitalize(), location,
                              desc, j.get("absolute_url"), created))
        return batch

    workers = min(len(GREENHOUSE_COMPANIES), 10)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one_gh, GREENHOUSE_COMPANIES)
    return [j for batch in results for j in batch]


# ── Source: The Muse ──────────────────────────────────────────────────────────

def fetch_themuse_jobs():
    if not TARGET_ROLES:
        return []

    def _fetch_page(args):
        _role, page = args
        batch = []
        try:
            data = _session.get(
                "https://www.themuse.com/api/public/jobs",
                params={"page": page, "location": "United States"},
                timeout=10,
            ).json()
        except Exception:
            return batch
        for j in data.get("results", []):
            title = j.get("name", "")
            if not _matches_role(title):
                continue
            company  = j.get("company", {}).get("name", "Unknown")
            location = ", ".join(
                loc.get("name", "") for loc in j.get("locations", [])
            ) or "Unknown"
            desc    = j.get("contents", "")
            url     = j.get("refs", {}).get("landing_page", "")
            created = j.get("publication_date")
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(title, company, location, desc, url, created))
        return batch

    combos  = [(role, page) for role in TARGET_ROLES for page in range(3)]
    workers = min(len(combos), 12)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_page, combos)
    return [j for batch in results for j in batch]


# ── Source: RemoteOK ──────────────────────────────────────────────────────────

def fetch_remoteok_jobs():
    try:
        resp = _session.get(
            "https://remoteok.io/api",
            headers={"User-Agent": "JobMatchAI/1.0"},
            timeout=15,
        )
        if resp.status_code == 429 or resp.status_code != 200 or not resp.text.strip():
            return []
        data = resp.json()
    except Exception:
        return []
    jobs = []
    for j in data:
        if not isinstance(j, dict):
            continue
        title = j.get("position", "")
        if not _matches_role(title):
            continue
        desc    = j.get("description", "")
        created = j.get("date")
        if not _recent(created, desc) or _exceeds_experience(desc, title):
            continue
        jobs.append(_job(
            title, j.get("company", "Unknown"), "Remote",
            desc, j.get("url", ""), created,
        ))
    return jobs


# ── Source: Jobicy ────────────────────────────────────────────────────────────
# Each target role fires its own request — run in parallel.

def fetch_jobicy_jobs():
    def _fetch_one(role):
        batch = []
        try:
            data = _session.get(
                "https://jobicy.com/api/v2/remote-jobs",
                params={
                    "geo": "usa",
                    "count": 50,
                    "tag": role.lower().replace(" ", "-"),
                },
                timeout=10,
            ).json()
        except Exception:
            return batch
        for j in data.get("jobs", []):
            title   = j.get("jobTitle", "")
            desc    = j.get("jobDescription", "")
            created = j.get("pubDate")
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(
                title,
                j.get("companyName", "Unknown"),
                j.get("jobGeo", "USA"),
                desc, j.get("jobURL", ""), created,
            ))
        return batch

    workers = min(len(TARGET_ROLES), 8) if TARGET_ROLES else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, TARGET_ROLES)
    return [j for batch in results for j in batch]


# ── Source: USAJobs (Federal Government) ─────────────────────────────────────
# Each target role fires its own request — run in parallel.

def fetch_usajobs_jobs():
    if not USAJOBS_API_KEY or not USAJOBS_EMAIL:
        return []

    def _fetch_one(role):
        batch = []
        try:
            data = _session.get(
                "https://data.usajobs.gov/api/search",
                params={
                    "Keyword": role,
                    "ResultsPerPage": 25,
                    "DatePosted": 7,
                },
                headers={
                    "Authorization-Key": USAJOBS_API_KEY,
                    "User-Agent":        USAJOBS_EMAIL,
                    "Host":              "data.usajobs.gov",
                },
                timeout=15,
            ).json()
        except Exception:
            return batch
        items = data.get("SearchResult", {}).get("SearchResultItems", [])
        for item in items:
            d        = item.get("MatchedObjectDescriptor", {})
            title    = d.get("PositionTitle", "")
            org      = d.get("OrganizationName", "Federal Government")
            locs     = d.get("PositionLocation", [])
            location = ", ".join(l.get("LocationName", "") for l in locs) or "USA"
            details  = d.get("UserArea", {}).get("Details", {})
            desc     = details.get("JobSummary", "") or d.get("QualificationSummary", "")
            created  = d.get("PublicationStartDate")
            url      = d.get("PositionURI", "")
            if _exceeds_experience(desc, title):
                continue
            batch.append(_job(title, org, location, desc, url, created))
        return batch

    workers = min(len(TARGET_ROLES), 8) if TARGET_ROLES else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, TARGET_ROLES)
    return [j for batch in results for j in batch]


# ── Source: Jooble ────────────────────────────────────────────────────────────
# Each target role fires its own request — run in parallel.

def fetch_jooble_jobs():
    if not JOOBLE_API_KEY:
        return []

    def _fetch_one(role):
        batch = []
        try:
            data = _session.post(
                f"https://jooble.org/api/{JOOBLE_API_KEY}",
                json={
                    "keywords":     role,
                    "location":     "United States",
                    "resultonpage": 20,
                },
                timeout=15,
            ).json()
        except Exception:
            return batch
        for j in data.get("jobs", []):
            title   = j.get("title", "")
            desc    = j.get("snippet", "")
            created = j.get("updated")
            if not _matches_role(title):
                continue
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(
                title, j.get("company", "Unknown"),
                j.get("location", "USA"),
                desc, j.get("link", ""), created,
            ))
        return batch

    workers = min(len(TARGET_ROLES), 8) if TARGET_ROLES else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, TARGET_ROLES)
    return [j for batch in results for j in batch]


# ── Source: FindWork ──────────────────────────────────────────────────────────
# Each target role fires its own request — run in parallel.

def fetch_findwork_jobs():
    if not FINDWORK_API_KEY:
        return []

    def _fetch_one(role):
        batch = []
        try:
            data = _session.get(
                "https://findwork.dev/api/jobs/",
                params={"search": role, "location": "usa", "order_by": "-date"},
                headers={"Authorization": f"Token {FINDWORK_API_KEY}"},
                timeout=10,
            ).json()
        except Exception:
            return batch
        for j in data.get("results", []):
            title   = j.get("role", "")
            desc    = j.get("text", "")
            created = j.get("date_posted")
            if not _recent(created, desc) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(
                title, j.get("company_name", "Unknown"),
                j.get("location", "Unknown"),
                desc, j.get("url", ""), created,
            ))
        return batch

    workers = min(len(TARGET_ROLES), 8) if TARGET_ROLES else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, TARGET_ROLES)
    return [j for batch in results for j in batch]


# ── Source: Indeed RSS ───────────────────────────────────────────────────────
# One request per target role — fire them all in parallel.

def fetch_indeed_rss_jobs():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobMatchAI/2.0)"}

    def _fetch_one(role):
        batch = []
        try:
            url  = (
                "https://www.indeed.com/rss"
                f"?q={requests.utils.quote(role)}"
                "&sort=date&fromage=7"
            )
            resp = _session.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return batch
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                raw_title = (item.findtext("title") or "").strip()
                parts     = [p.strip() for p in raw_title.split(" - ")]
                title     = parts[0] if parts else raw_title
                company   = parts[1] if len(parts) > 1 else "Unknown"
                location  = parts[2] if len(parts) > 2 else "USA"
                desc      = _strip_html(item.findtext("description") or "")
                url_job   = item.findtext("link") or ""
                created   = item.findtext("pubDate") or ""
                if not _recent(created) or _exceeds_experience(desc, title):
                    continue
                batch.append(_job(title, company, location, desc, url_job, created))
        except Exception:
            pass
        return batch

    roles   = TARGET_ROLES[:6]   # cap at 6 roles to stay polite to Indeed
    workers = min(len(roles), 6) if roles else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one, roles)
    return [j for batch in results for j in batch]


# ── Source: Lever ATS ────────────────────────────────────────────────────────

def fetch_lever_jobs():
    if not LEVER_COMPANIES:
        return []

    def _fetch_one_lever(company):
        batch = []
        try:
            data = _session.get(
                f"https://api.lever.co/v0/postings/{company}?mode=json",
                timeout=10,
            ).json()
            if not isinstance(data, list):
                return batch
        except Exception:
            return batch

        for j in data:
            if not isinstance(j, dict):
                continue
            title = j.get("text", "")
            if not _matches_role(title):
                continue
            cats     = j.get("categories", {}) or {}
            location = cats.get("location", "") or cats.get("allLocations", "") or "Remote"
            if isinstance(location, list):
                location = ", ".join(location)
            loc_lower = location.lower()
            if location and "remote" not in loc_lower and "united states" not in loc_lower \
                    and "usa" not in loc_lower and ", ca" not in loc_lower \
                    and ", ny" not in loc_lower and ", tx" not in loc_lower \
                    and ", wa" not in loc_lower and ", il" not in loc_lower:
                if any(country in loc_lower for country in ["uk", "canada", "germany",
                        "india", "australia", "france", "spain", "netherlands"]):
                    continue
            desc = _strip_html(j.get("description", "") or j.get("descriptionPlain", "") or "")
            url  = j.get("hostedUrl", "") or j.get("applyUrl", "")
            created_ms = j.get("createdAt")
            created    = None
            if created_ms:
                try:
                    created = datetime.utcfromtimestamp(
                        int(created_ms) / 1000
                    ).strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    pass
            if not _recent(created) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(
                title,
                j.get("company_name", company.replace("-", " ").title()),
                location or "Remote",
                desc, url, created,
            ))
        return batch

    workers = min(len(LEVER_COMPANIES), 12)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(_fetch_one_lever, LEVER_COMPANIES)
    return [j for batch in results for j in batch]


# ── Source: We Work Remotely ─────────────────────────────────────────────────
# Previously fetched 5 RSS feeds sequentially (~5 s). Now all 5 fire at once.

def fetch_weworkremotely_jobs():
    FEEDS = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-data-science-jobs.rss",
        "https://weworkremotely.com/categories/remote-management-product-jobs.rss",
        "https://weworkremotely.com/remote-jobs.rss",
    ]
    headers = {"User-Agent": "JobMatchAI/2.0"}

    def _fetch_feed(feed_url):
        batch = []
        try:
            resp = _session.get(feed_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return batch
            root = ET.fromstring(resp.content)
        except Exception:
            return batch

        for item in root.findall(".//item"):
            raw_title = (item.findtext("title") or "").strip()
            if ": " in raw_title:
                company, title = raw_title.split(": ", 1)
            else:
                company, title = "Unknown", raw_title
            title   = title.strip()
            company = company.strip()
            if not title or not _matches_role(title):
                continue
            url_job = item.findtext("link") or ""
            desc    = _strip_html(item.findtext("description") or "")
            created = item.findtext("pubDate") or ""
            if not _recent(created) or _exceeds_experience(desc, title):
                continue
            batch.append(_job(title, company, "Remote", desc, url_job, created))
        return batch

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        results = ex.map(_fetch_feed, FEEDS)

    # Deduplicate by URL across all feeds
    seen = set()
    jobs = []
    for batch in results:
        for j in batch:
            url = j.get("redirect_url", "")
            if url and url not in seen:
                seen.add(url)
                jobs.append(j)
    return jobs


# ── Source: Working Nomads ───────────────────────────────────────────────────

def fetch_workingnomads_jobs():
    try:
        resp = _session.get(
            "https://www.workingnomads.com/api/exposed_jobs/",
            headers={"User-Agent": "JobMatchAI/2.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    jobs = []
    for j in data:
        if not isinstance(j, dict):
            continue
        title = j.get("title", "")
        if not _matches_role(title):
            continue
        desc    = _strip_html(j.get("description", "") or j.get("excerpt", "") or "")
        created = j.get("pub_date") or j.get("created", "")
        if not _recent(created) or _exceeds_experience(desc, title):
            continue
        jobs.append(_job(
            title,
            j.get("company", "Unknown"),
            j.get("location", "Remote") or "Remote",
            desc, j.get("url", ""), created,
        ))
    return jobs


# ── Source: Hacker News "Who is Hiring" ─────────────────────────────────────

def fetch_hn_hiring_jobs():
    jobs = []
    try:
        search = _session.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query":          "Ask HN: Who is hiring",
                "tags":           "story,ask_hn",
                "numericFilters": "points>50",
                "hitsPerPage":    5,
            },
            timeout=12,
        ).json()

        hits = search.get("hits", [])
        if not hits:
            return []

        latest    = sorted(hits, key=lambda h: h.get("created_at", ""), reverse=True)[0]
        thread_id = latest.get("objectID")
        if not thread_id:
            return []

        # Fetch first 2 pages of comments (≈ 400 comments) in parallel
        page_nums = [0, 1]

        def _fetch_page(page):
            try:
                return _session.get(
                    "https://hn.algolia.com/api/v1/search",
                    params={
                        "tags":        f"comment,story_{thread_id}",
                        "hitsPerPage": 200,
                        "page":        page,
                    },
                    timeout=12,
                ).json().get("hits", [])
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=2) as ex:
            pages = list(ex.map(_fetch_page, page_nums))

        for batch in pages:
            for comment in batch:
                text = comment.get("comment_text") or ""
                if not text:
                    continue
                plain = _strip_html(text)
                if not any(role.lower() in plain.lower() for role in TARGET_ROLES):
                    continue
                first_line = plain.split("\n")[0].strip()
                pipe_parts = [p.strip() for p in first_line.split("|")]
                if len(pipe_parts) >= 2:
                    company  = pipe_parts[0]
                    title    = pipe_parts[1]
                    location = pipe_parts[2] if len(pipe_parts) > 2 else "Remote"
                else:
                    company  = "See posting"
                    title    = first_line[:80] if first_line else "Various Roles"
                    location = "Remote"
                if not title or not _matches_role(title):
                    continue
                created = comment.get("created_at", "")
                if not _recent_monthly(created) or _exceeds_experience(plain, title):
                    continue
                url = f"https://news.ycombinator.com/item?id={comment.get('objectID', '')}"
                jobs.append(_job(title, company, location, plain[:3000], url, created))

    except Exception:
        pass
    return jobs


# ── Aggregator ────────────────────────────────────────────────────────────────

def fetch_today_jobs():
    """
    Fetch all enabled job sources IN PARALLEL using threads.
    Every source — including those that previously looped over target roles
    one-by-one — now fires all sub-requests concurrently.
    """
    sources = []
    if JOB_SOURCES.get("adzuna"):          sources.append(("Adzuna",          fetch_adzuna_jobs))
    if JOB_SOURCES.get("remotive"):        sources.append(("Remotive",         fetch_remotive_jobs))
    if JOB_SOURCES.get("arbeitnow"):       sources.append(("Arbeitnow",        fetch_arbeitnow_jobs))
    if JOB_SOURCES.get("greenhouse"):      sources.append(("Greenhouse ATS",   fetch_greenhouse_jobs))
    if JOB_SOURCES.get("themuse"):         sources.append(("The Muse",         fetch_themuse_jobs))
    if JOB_SOURCES.get("remoteok"):        sources.append(("RemoteOK",         fetch_remoteok_jobs))
    if JOB_SOURCES.get("jobicy"):          sources.append(("Jobicy",           fetch_jobicy_jobs))
    if JOB_SOURCES.get("usajobs"):         sources.append(("USAJobs",          fetch_usajobs_jobs))
    if JOB_SOURCES.get("jooble"):          sources.append(("Jooble",           fetch_jooble_jobs))
    if JOB_SOURCES.get("findwork"):        sources.append(("FindWork",         fetch_findwork_jobs))
    if JOB_SOURCES.get("indeed"):          sources.append(("Indeed RSS",       fetch_indeed_rss_jobs))
    if JOB_SOURCES.get("lever"):           sources.append(("Lever ATS",        fetch_lever_jobs))
    if JOB_SOURCES.get("weworkremotely"):  sources.append(("WeWorkRemotely",   fetch_weworkremotely_jobs))
    if JOB_SOURCES.get("workingnomads"):   sources.append(("WorkingNomads",    fetch_workingnomads_jobs))
    if JOB_SOURCES.get("hnhiring"):        sources.append(("HN Hiring",        fetch_hn_hiring_jobs))

    all_jobs  = []
    board_num = 0
    with ThreadPoolExecutor(max_workers=len(sources) or 1) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in sources}
        for future in as_completed(future_to_name):
            board_num += 1
            try:
                fetched = future.result()
                all_jobs.extend(fetched)
                if fetched:
                    print(f"  Job board {board_num}: {len(fetched)} jobs")
            except Exception:
                pass

    unique = {j["redirect_url"]: j for j in all_jobs if j.get("redirect_url")}
    print(f"Total unique jobs fetched: {len(unique)}")
    return list(unique.values())
