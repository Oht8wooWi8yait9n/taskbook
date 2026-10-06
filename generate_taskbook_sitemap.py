#!/usr/bin/env python3
"""
NASA Task Book (taskbook.nasaprs.com) Harvester & Static Site Generator

Features:
1. Session Initialization: Captures Adobe ColdFusion session tokens (CFID, CFTOKEN, JSESSIONID)
   from https://taskbook.nasaprs.com/tbp/welcome.cfm.
2. Complete Catalog Discovery: Queries all 2,918+ research tasks spanning FY2004 to present
   across Space Biology, Physical Sciences, and Human Research (HRP).
3. Resilient Paginator: Paginates through all 117 pages with automatic rate-limit backoff.
4. Detail Harvester: Fetches detailed task pages, extracting:
   - Project Title, Fiscal Year, Division, Discipline, Risk, Status
   - Principal Investigator & Co-Is (degrees, affiliation, contacts)
   - Grant/Contract Number, Center, Grant Monitor, Solicitation
   - Complete Task Description & Objectives
   - Research Impact & Earth Benefits
   - Annual Task Progress Reports
   - Cumulative Bibliographies / Publications
5. Pre-Rendered Static Mirrors: Renders semantic, mobile-friendly HTML cards under tasks/<TASKID>.html
   with authoritative NASA source citations and direct PDF report links (tbpdf.cfm?id=...).
6. Structured JSONL Export: Produces taskbook_tasks.jsonl for programmatic analysis.
7. Interactive Catalog: Generates index.html with live client-side search and filters.
8. Dual XML Sitemaps:
   - taskbook_sitemap.xml & taskbook_urls.txt (GitHub Pages mirrors for Onyx Web Connector)
   - taskbook_direct_sitemap.xml & taskbook_direct_urls.txt (Original live NASA URLs)
"""

import hashlib
from html import escape, unescape
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE_URL = "https://taskbook.nasaprs.com"
WELCOME_URL = f"{BASE_URL}/tbp/welcome.cfm"
INDEX_URL = f"{BASE_URL}/tbp/index.cfm"
PDF_BASE_URL = f"{BASE_URL}/tbp/tbpdf.cfm"

GH_PAGES_BASE = "https://oht8woowi8yait9n.github.io/taskbook"
REPO_DIR = Path(__file__).resolve().parent
CACHE_DIR = REPO_DIR / ".taskbook_cache"
RAW_CACHE_DIR = CACHE_DIR / "raw"
TASKS_DIR = REPO_DIR / "tasks"

JSONL_PATH = REPO_DIR / "taskbook_tasks.jsonl"
INDEX_HTML_PATH = REPO_DIR / "index.html"
SITEMAP_PATH = REPO_DIR / "taskbook_sitemap.xml"
URLS_TXT_PATH = REPO_DIR / "taskbook_urls.txt"
DIRECT_SITEMAP_PATH = REPO_DIR / "taskbook_direct_sitemap.xml"
DIRECT_URLS_TXT_PATH = REPO_DIR / "taskbook_direct_urls.txt"
DISCOVERED_INDEX_PATH = CACHE_DIR / "discovered_tasks.json"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

RATE_LIMIT_DELAY = 0.35  # Polite crawl delay between requests
BACKOFF_DELAY_429 = 30   # Seconds to wait on HTTP 429 Too Many Requests


class TaskBookSession:
    """Manages ColdFusion session cookies and requests with rate limiting and retry backoff."""

    def __init__(self):
        self.ssl_ctx = ssl._create_unverified_context()
        self.cookie_jar = http.cookiejar.CookieJar()
        
        # Check for proxy configuration (env var or local proxy tunnel on port 8889)
        proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        if not proxy_url:
            try:
                import socket
                s = socket.socket()
                s.settimeout(0.5)
                s.connect(("127.0.0.1", 8889))
                s.close()
                proxy_url = "http://127.0.0.1:8889"
            except Exception:
                pass

        handlers = [
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            urllib.request.HTTPSHandler(context=self.ssl_ctx),
        ]
        if proxy_url:
            print(f"[*] Routing requests via proxy: {proxy_url}")
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))

        self.opener = urllib.request.build_opener(*handlers)
        self.last_request_time = 0.0
        self.session_initialized = False

    def init_session(self):
        """Visits welcome.cfm then index.cfm to establish CFID, CFTOKEN, and JSESSIONID."""
        print("[*] Initializing ColdFusion session at welcome.cfm...")
        self.get(WELCOME_URL, referer=BASE_URL)
        print("[*] Loading index.cfm to register form session state...")
        self.get(INDEX_URL, referer=WELCOME_URL)
        self.session_initialized = True
        cookies = [f"{c.name}={c.value}" for c in self.cookie_jar]
        print(f"[+] Session established with cookies: {', '.join(cookies[:3])}")

    def _throttle(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)

    def request(self, url: str, data: bytes = None, referer: str = None, max_retries: int = 5) -> str:
        for attempt in range(1, max_retries + 1):
            self._throttle()
            req_headers = dict(HEADERS)
            if referer:
                req_headers["Referer"] = referer
            if data is not None:
                req_headers["Content-Type"] = "application/x-www-form-urlencoded"

            req = urllib.request.Request(url, data=data, headers=req_headers)
            try:
                self.last_request_time = time.time()
                with self.opener.open(req, timeout=30) as resp:
                    return resp.read().decode("utf-8", errors="ignore")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait_time = BACKOFF_DELAY_429 * attempt
                    print(f"\n[!] HTTP 429 Too Many Requests. Cooling down for {wait_time}s (attempt {attempt}/{max_retries})...")
                    time.sleep(wait_time)
                    # Re-initialize session after cooldown if needed
                    try:
                        self.init_session()
                    except Exception:
                        pass
                elif e.code in (500, 502, 503, 504):
                    wait_time = 5 * attempt
                    print(f"\n[!] Server error {e.code}. Retrying in {wait_time}s (attempt {attempt}/{max_retries})...")
                    time.sleep(wait_time)
                else:
                    print(f"\n[!] HTTP error {e.code} requesting {url}")
                    if attempt == max_retries:
                        raise
            except Exception as e:
                wait_time = 5 * attempt
                print(f"\n[!] Network exception {e}. Retrying in {wait_time}s (attempt {attempt}/{max_retries})...")
                time.sleep(wait_time)
        return ""

    def get(self, url: str, referer: str = None) -> str:
        return self.request(url, referer=referer)

    def post(self, url: str, form_dict: dict, referer: str = None) -> str:
        data = urllib.parse.urlencode(form_dict).encode("utf-8")
        return self.request(url, data=data, referer=referer)


def clean_text(s: str) -> str:
    """Removes HTML tags, decodes HTML entities, and normalizes whitespace."""
    if not s:
        return ""
    # Remove script and style tags completely
    s = re.sub(r"<script.*?</script>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style.*?</style>", "", s, flags=re.DOTALL | re.IGNORECASE)
    # Replace breaks and tags with spaces
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    # Replace non-breaking spaces and normalize whitespace
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_field_between(text: str, start_pat: str, end_pat: str) -> str:
    """Extracts cleaned text between two regex patterns."""
    m = re.search(start_pat + r"(.*?)" + end_pat, text, re.DOTALL | re.IGNORECASE)
    if m:
        return clean_text(m.group(1))
    return ""


def discover_all_tasks(session: TaskBookSession) -> list[dict]:
    """
    Submits master search for year=-1 (All Reports FY04 - present) across all divisions
    and traverses all 117 pages to collect all task IDs and table summary records.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if DISCOVERED_INDEX_PATH.exists():
        try:
            with open(DISCOVERED_INDEX_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if len(cached) >= 2900:
                print(f"[+] Loaded {len(cached)} discovered tasks from cache ({DISCOVERED_INDEX_PATH})")
                return cached
        except Exception as e:
            print(f"[!] Could not load cache: {e}")

    if not session.session_initialized:
        session.init_session()

    print("[*] Submitting master search for All Reports (FY04 - present)...")
    search_payload = [
        ("StartSearch", "Start Search"),
        ("division", "2"),  # Human Research
        ("division", "3"),  # Space Biology
        ("division", "1"),  # Physical Sciences
        ("year", "-1"),     # All Reports FY04 - present
        ("searchDateRange", "-1"),
        ("searchRangeStartDate", "01/01/2026"),
        ("searchRangeEndDate", "10/06/2026"),
        ("lname", ""),
        ("fname", ""),
        ("coilname", ""),
        ("coifname", ""),
        ("keyword", ""),
        ("ptitle", ""),
        ("projnote", ""),
        ("oscm_keyword", ""),
        ("pgrantnumber", ""),
        ("action", "public_retrieve_result"),
    ]
    post_data = urllib.parse.urlencode(search_payload).encode("utf-8")
    first_page_html = session.request(INDEX_URL, data=post_data, referer=INDEX_URL)

    # Detect total pages
    m_page = re.search(r"Page\s+No\.\s*(\d+)\s+of\s+(\d+)", first_page_html)
    total_pages = int(m_page.group(2)) if m_page else 117
    print(f"[+] Total search result pages to scan: {total_pages}")

    all_tasks = []
    seen_ids = set()

    def parse_page_tasks(html_content: str, page_num: int):
        rows = re.findall(r"<tr[^>]*>.*?</tr>", html_content, re.DOTALL)
        count_on_page = 0
        for r in rows:
            m_id = re.search(r"action=public_query_taskbook_content&TASKID=([A-Fa-f0-9]{32})", r)
            if not m_id:
                continue
            tid = m_id.group(1).upper()
            if tid in seen_ids:
                continue

            tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)
            clean_tds = [re.sub(r"<[^>]+>", " ", td).replace("&nbsp;", " ").strip() for td in tds]

            pi_name = clean_tds[1] if len(clean_tds) > 1 else ""
            institution = clean_tds[2] if len(clean_tds) > 2 else ""
            title = clean_tds[4] if len(clean_tds) > 4 else ""
            division = clean_tds[5] if len(clean_tds) > 5 else ""
            grant_no = clean_tds[6] if len(clean_tds) > 6 else ""
            dates = clean_tds[10] if len(clean_tds) > 10 else ""
            last_updated = clean_tds[11] if len(clean_tds) > 11 else ""

            task_meta = {
                "task_id": tid,
                "pi_name": pi_name,
                "institution": institution,
                "title": title,
                "division": division,
                "grant_no": grant_no,
                "dates": dates,
                "last_updated": last_updated,
                "page": page_num,
            }
            all_tasks.append(task_meta)
            seen_ids.add(tid)
            count_on_page += 1
        return count_on_page

    p1_count = parse_page_tasks(first_page_html, 1)
    print(f"    Page 1/{total_pages}: captured {p1_count} tasks (total so far: {len(all_tasks)})")

    # Traverse pages 2 to total_pages
    for p in range(2, total_pages + 1):
        page_url = f"{INDEX_URL}?action=sort&sort=asc&col=pi&p={p}"
        page_html = session.get(page_url, referer=INDEX_URL)
        p_count = parse_page_tasks(page_html, p)
        if p % 10 == 0 or p == total_pages:
            print(f"    Page {p}/{total_pages}: captured {p_count} tasks (total so far: {len(all_tasks)})")

    print(f"[✓] Discovered a total of {len(all_tasks)} research tasks across {total_pages} pages.")
    with open(DISCOVERED_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(all_tasks, f, indent=2)
    return all_tasks


def fetch_task_details(session: TaskBookSession, task_meta: dict, fetch_network: bool = True) -> dict:
    """Fetches and parses the full detail page for an individual task."""
    tid = task_meta["task_id"]
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_cache_file = RAW_CACHE_DIR / f"{tid}.html"

    html = ""
    if raw_cache_file.exists():
        with open(raw_cache_file, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    elif fetch_network and session:
        detail_url = f"{INDEX_URL}?action=public_query_taskbook_content&TASKID={tid}"
        html = session.get(detail_url, referer=INDEX_URL)
        if html and "Project Title:" in html:
            with open(raw_cache_file, "w", encoding="utf-8") as f:
                f.write(html)

    # Parse details
    title = extract_field_between(html, r"Project Title:\s*(?:&nbsp;)?", r"(?:Reduce\s*Images|Fiscal Year|<table)")
    if not title:
        title = task_meta.get("title", "")
    # Remove trailing button text
    title = re.sub(r"\s+Reduce(\s*Images?.*)?$", "", title, flags=re.IGNORECASE).strip()

    raw_dates = task_meta.get("dates", "").replace("<br>", " ")
    date_parts = raw_dates.split()
    fallback_start = date_parts[0] if date_parts else ""
    fallback_end = date_parts[1] if len(date_parts) > 1 else ""

    fiscal_year = extract_field_between(html, r"Fiscal Year:\s*(?:&nbsp;)?", r"(?:Division|Research Discipline)")
    division = extract_field_between(html, r"Division:\s*(?:&nbsp;)?", r"(?:Research Discipline|Start Date)") or task_meta.get("division", "")
    discipline = extract_field_between(html, r"Research Discipline/Element:\s*(?:&nbsp;)?", r"(?:Start Date|End Date)")
    start_date = extract_field_between(html, r"Start Date:\s*(?:&nbsp;)?", r"(?:End Date|Task Last Updated)") or fallback_start
    end_date = extract_field_between(html, r"End Date:\s*(?:&nbsp;)?", r"(?:Task Last Updated|Download Task Book)") or fallback_end
    last_updated = extract_field_between(html, r"Task Last Updated:\s*(?:&nbsp;)?", r"(?:Download Task Book|Principal Investigator)") or task_meta.get("last_updated", "")

    pi_affiliation = extract_field_between(html, r"Principal Investigator/Affiliation:\s*(?:&nbsp;)?", r"(?:Address|Email)")
    address = extract_field_between(html, r"Address:\s*(?:&nbsp;)?", r"(?:Email|Phone)")
    email = extract_field_between(html, r"Email:\s*(?:&nbsp;)?", r"(?:Phone|Congressional)")
    phone = extract_field_between(html, r"Phone:\s*(?:&nbsp;)?", r"(?:Congressional|Web)")
    cong_district = extract_field_between(html, r"Congressional District:\s*(?:&nbsp;)?", r"(?:Web|Organization Type)")
    org_type = extract_field_between(html, r"Organization Type:\s*(?:&nbsp;)?", r"(?:Organization Name|Joint Agency)")
    org_name = extract_field_between(html, r"Organization Name:\s*(?:&nbsp;)?", r"(?:Joint Agency|Comments)")

    # Co-Investigators
    co_is = []
    m_coi = re.search(r"Co-Investigator\(s\)/Affiliation\(s\):(.*?)(?:Grant/Contract No|Project Information|Responsible Center)", html, re.DOTALL | re.IGNORECASE)
    if m_coi:
        coi_text = clean_text(m_coi.group(1))
        if coi_text and coi_text.lower() != "none":
            co_is = [c.strip() for c in re.split(r";|\n", coi_text) if c.strip()]

    # Project Information & Administration
    grant_no = extract_field_between(html, r"Grant/Contract No\.:\s*(?:&nbsp;)?", r"(?:Project Type|Flight Program)") or task_meta.get("grant_no", "")
    resp_center = extract_field_between(html, r"Responsible Center:\s*(?:&nbsp;)?", r"(?:Grant Monitor|Center Contact)")
    grant_monitor = extract_field_between(html, r"Grant Monitor:\s*(?:&nbsp;)?", r"(?:Center Contact|Unique ID)")
    center_contact = extract_field_between(html, r"Center Contact:\s*(?:&nbsp;)?", r"(?:Unique ID|Solicitation)")
    unique_id = extract_field_between(html, r"Unique ID:\s*(?:&nbsp;)?", r"(?:Solicitation|Grant/Contract)")
    solicitation = extract_field_between(html, r"Solicitation / Funding Source:\s*(?:&nbsp;)?", r"(?:Grant/Contract|Project Type)")
    project_type = extract_field_between(html, r"Project Type:\s*(?:&nbsp;)?", r"(?:Flight Program|No\. of Post Docs)")
    flight_program = extract_field_between(html, r"Flight Program:\s*(?:&nbsp;)?", r"(?:No\. of Post Docs|Flight Assignment)")
    flight_notes = extract_field_between(html, r"Flight Assignment/Project Notes:\s*(?:&nbsp;)?", r"(?:Task Description|Rationale for)")

    # Student counts
    phd_cand = extract_field_between(html, r"No\. of PhD Candidates:\s*(?:&nbsp;)?", r"(?:No\. of Master's|No\. of Bachelor's)")
    masters_cand = extract_field_between(html, r"No\. of Master's Candidates:\s*(?:&nbsp;)?", r"(?:No\. of Bachelor's|No\. of PhD Degrees)")
    bachelors_cand = extract_field_between(html, r"No\. of Bachelor's Candidates:\s*(?:&nbsp;)?", r"(?:No\. of PhD Degrees|Space Biology Element)")

    # Narrative Content
    description = extract_field_between(html, r"Task Description:\s*(?:&nbsp;)?", r"(?:Rationale for|Research Impact|Task Progress)")
    research_impact = extract_field_between(html, r"(?:Research Impact/Earth Benefits|Rationale for HRP Directed Research):\s*(?:&nbsp;)?", r"(?:Task Progress|Bibliography)")
    task_progress = extract_field_between(html, r"Task Progress:\s*(?:&nbsp;)?", r"(?:Bibliography|Show Cumulative|Back to Search)")

    # Bibliography text
    bib_text = extract_field_between(html, r"Bibliography:\s*(?:&nbsp;)?", r"(?:Back to Search|document\.getElementById)")
    if "Show Cumulative Bibliography" in bib_text:
        bib_text = bib_text.replace("Show Cumulative Bibliography", "").strip()

    live_url = f"{INDEX_URL}?action=public_query_taskbook_content&TASKID={tid}"
    pdf_url = f"{PDF_BASE_URL}?id={tid}"
    gh_pages_url = f"{GH_PAGES_BASE}/tasks/{tid}"

    # Assemble structured object
    return {
        "task_id": tid,
        "unique_id": unique_id,
        "title": title,
        "fiscal_year": fiscal_year,
        "division": division,
        "discipline_element": discipline,
        "start_date": start_date or task_meta.get("dates", "").split("<br>")[0],
        "end_date": end_date or (task_meta.get("dates", "").split("<br>")[1] if "<br>" in task_meta.get("dates", "") else ""),
        "last_updated": last_updated,
        "pi_name": task_meta.get("pi_name", ""),
        "pi_affiliation": pi_affiliation or task_meta.get("institution", ""),
        "pi_address": address,
        "pi_email": email,
        "pi_phone": phone,
        "congressional_district": cong_district,
        "organization_type": org_type,
        "organization_name": org_name or task_meta.get("institution", ""),
        "co_investigators": co_is,
        "grant_contract_no": grant_no,
        "responsible_center": resp_center,
        "grant_monitor": grant_monitor,
        "center_contact": center_contact,
        "solicitation": solicitation,
        "project_type": project_type,
        "flight_program": flight_program,
        "flight_notes": flight_notes,
        "student_counts": {
            "phd_candidates": phd_cand,
            "masters_candidates": masters_cand,
            "bachelors_candidates": bachelors_cand,
        },
        "description": description,
        "research_impact": research_impact,
        "task_progress": task_progress,
        "bibliography": bib_text,
        "live_url": live_url,
        "pdf_url": pdf_url,
        "gh_pages_url": gh_pages_url,
    }


def render_html_page(task: dict) -> str:
    """Pre-renders an accessible, responsive, semantic HTML page for a task."""
    tid = task["task_id"]
    title = escape(task["title"] or f"Task {tid}")
    pi_name = escape(task["pi_name"] or "N/A")
    institution = escape(task["pi_affiliation"] or "N/A")
    division = escape(task["division"] or "N/A")
    fy = escape(task["fiscal_year"] or "N/A")
    disc = escape(task["discipline_element"] or "N/A")
    start = escape(task["start_date"] or "N/A")
    end = escape(task["end_date"] or "N/A")
    grant = escape(task["grant_contract_no"] or "N/A")
    center = escape(task["responsible_center"] or "N/A")
    solicitation = escape(task["solicitation"] or "N/A")
    desc = escape(task["description"] or "No description provided.")
    impact = escape(task["research_impact"] or "No research impact/Earth benefits recorded.")
    progress = escape(task["task_progress"] or "No progress report recorded.")
    bib = escape(task["bibliography"] or "None recorded.")
    raw_live_url = task["live_url"]
    live_url = escape(task["live_url"])
    pdf_url = escape(task["pdf_url"])

    coi_html = ""
    if task["co_investigators"]:
        coi_list = "".join(f"<li>{escape(c)}</li>" for c in task["co_investigators"])
        coi_html = f"<div class='info-block'><strong>Co-Investigators:</strong><ul>{coi_list}</ul></div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA Task Book: {title}</title>
  <link rel="canonical" href="{live_url}">
  <meta property="og:url" content="{live_url}">
  <meta property="og:title" content="{title}">
  <meta property="og:site_name" content="NASA Task Book">
  <script>
    // Seamlessly forward human visitors directly to the official NASA Task Book page
    if (!navigator.webdriver && !/bot|crawl|spider|slurp|facebookexternalhit/i.test(navigator.userAgent)) {{
      window.location.replace("{raw_live_url}");
    }}
  </script>
  <style>
    :root {{
      --nasa-blue: #0b3d91;
      --nasa-red: #d83933;
      --text-dark: #1b1b1b;
      --bg-light: #f8f9fa;
      --card-bg: #ffffff;
      --border-color: #dfe1e5;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.6;
      color: var(--text-dark);
      background-color: var(--bg-light);
      margin: 0;
      padding: 0;
    }}
    header {{
      background-color: var(--nasa-blue);
      color: white;
      padding: 1.25rem 2rem;
      border-bottom: 4px solid var(--nasa-red);
    }}
    header h1 {{
      margin: 0;
      font-size: 1.4rem;
      font-weight: 700;
    }}
    header a {{
      color: #90caf9;
      text-decoration: none;
      font-size: 0.9rem;
    }}
    header a:hover {{
      text-decoration: underline;
    }}
    .container {{
      max-width: 1000px;
      margin: 2rem auto;
      padding: 0 1.5rem;
    }}
    .citation-box {{
      background-color: #e8f0fe;
      border-left: 5px solid var(--nasa-blue);
      padding: 1rem 1.25rem;
      margin-bottom: 1.5rem;
      border-radius: 4px;
    }}
    .citation-box a {{
      color: var(--nasa-blue);
      font-weight: 600;
      word-break: break-all;
    }}
    .card {{
      background: var(--card-bg);
      border-radius: 8px;
      border: 1px solid var(--border-color);
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      padding: 1.75rem;
      margin-bottom: 1.5rem;
    }}
    h2 {{
      color: var(--nasa-blue);
      border-bottom: 2px solid #eaeaea;
      padding-bottom: 0.5rem;
      margin-top: 0;
      font-size: 1.3rem;
    }}
    h3 {{
      font-size: 1.1rem;
      color: #333;
      margin-top: 1.5rem;
      margin-bottom: 0.5rem;
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }}
    .badge {{
      background: #e9ecef;
      color: #495057;
      padding: 0.25rem 0.6rem;
      border-radius: 4px;
      font-size: 0.85rem;
      font-weight: 600;
    }}
    .badge-primary {{
      background: var(--nasa-blue);
      color: white;
    }}
    .badge-secondary {{
      background: var(--nasa-red);
      color: white;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 1rem;
      margin-bottom: 1rem;
    }}
    .meta-item {{
      background: #fdfdfd;
      border: 1px solid #f0f0f0;
      padding: 0.75rem;
      border-radius: 4px;
    }}
    .meta-label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      color: #6c757d;
      font-weight: 700;
      display: block;
      margin-bottom: 0.25rem;
    }}
    .meta-value {{
      font-size: 0.95rem;
      font-weight: 500;
    }}
    p {{
      margin: 0.75rem 0;
      text-align: justify;
    }}
    .btn {{
      display: inline-block;
      padding: 0.5rem 1rem;
      font-size: 0.9rem;
      font-weight: 600;
      text-decoration: none;
      border-radius: 4px;
      margin-right: 0.5rem;
    }}
    .btn-primary {{
      background-color: var(--nasa-blue);
      color: white;
    }}
    .btn-outline {{
      background-color: white;
      color: var(--nasa-blue);
      border: 1px solid var(--nasa-blue);
    }}
    .btn:hover {{
      opacity: 0.9;
    }}
    footer {{
      text-align: center;
      padding: 2rem;
      font-size: 0.85rem;
      color: #6c757d;
      border-top: 1px solid var(--border-color);
      margin-top: 3rem;
    }}
  </style>
</head>
<body>
  <header>
    <div style="max-width: 1000px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center;">
      <h1>NASA Task Book</h1>
      <a href="../index.html">&larr; Back to Task Catalog</a>
    </div>
  </header>

  <div class="container">
    <div class="citation-box">
      <div style="margin-bottom: 0.5rem;">
        <strong>Authoritative NASA Source:</strong>
        <a href="{live_url}" target="_blank" rel="noopener noreferrer">{live_url}</a>
      </div>
      <div>
        <strong>Official PDF Report:</strong>
        <a href="{pdf_url}" target="_blank" rel="noopener noreferrer">{pdf_url}</a>
      </div>
    </div>

    <div class="card">
      <div class="badges">
        <span class="badge badge-primary">{division}</span>
        <span class="badge badge-secondary">{fy}</span>
        <span class="badge">Task ID: {tid}</span>
      </div>

      <h2>{title}</h2>

      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">Principal Investigator</span>
          <span class="meta-value">{pi_name}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Affiliation / Organization</span>
          <span class="meta-value">{institution}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Discipline / Element</span>
          <span class="meta-value">{disc}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Period of Performance</span>
          <span class="meta-value">{start} &ndash; {end}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Grant / Contract No.</span>
          <span class="meta-value">{grant}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Responsible Center</span>
          <span class="meta-value">{center}</span>
        </div>
      </div>

      {coi_html}

      <div style="margin-top: 1rem;">
        <a href="{live_url}" class="btn btn-primary" target="_blank" rel="noopener noreferrer">View Live on NASA Task Book</a>
        <a href="{pdf_url}" class="btn btn-outline" target="_blank" rel="noopener noreferrer">Download Task Book PDF</a>
      </div>
    </div>

    <div class="card">
      <h2>Project Information & Administration</h2>
      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">Solicitation / Funding Source</span>
          <span class="meta-value">{solicitation}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Project Type</span>
          <span class="meta-value">{escape(task["project_type"] or "N/A")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Grant Monitor</span>
          <span class="meta-value">{escape(task["grant_monitor"] or "N/A")}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Task Last Updated</span>
          <span class="meta-value">{escape(task["last_updated"] or "N/A")}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Task Description</h2>
      <p>{desc}</p>
    </div>

    <div class="card">
      <h2>Research Impact & Earth Benefits</h2>
      <p>{impact}</p>
    </div>

    <div class="card">
      <h2>Task Progress Report</h2>
      <p>{progress}</p>
    </div>

    <div class="card">
      <h2>Cumulative Bibliography & Citations</h2>
      <p>{bib}</p>
    </div>
  </div>

  <footer>
    <p>NASA Task Book Static Mirror &middot; Official NASA Research Projects Catalog &middot; <a href="{live_url}" target="_blank">taskbook.nasaprs.com</a></p>
  </footer>
</body>
</html>
"""


def write_sitemaps(tasks: list[dict]):
    """Generates sitemap.xml and urls.txt for both GitHub Pages and direct NASA URLs."""
    print("[*] Generating dual XML sitemaps and plain text URL lists...")
    now_iso = time.strftime("%Y-%m-%d")

    # 1. GitHub Pages XML Sitemap
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        '  <url>',
        f'    <loc>{escape(GH_PAGES_BASE)}/</loc>',
        f'    <lastmod>{now_iso}</lastmod>',
        '    <changefreq>weekly</changefreq>',
        '    <priority>1.0</priority>',
        '  </url>',
    ]
    gh_urls = [f"{GH_PAGES_BASE}/"]

    # 2. Direct NASA XML Sitemap
    direct_xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    direct_urls = []

    for t in tasks:
        gh_url = t["gh_pages_url"]
        live_url = t["live_url"]
        gh_urls.append(gh_url)
        direct_urls.append(live_url)

        xml_lines.append("  <url>")
        xml_lines.append(f"    <loc>{escape(gh_url)}</loc>")
        xml_lines.append(f"    <lastmod>{now_iso}</lastmod>")
        xml_lines.append("    <changefreq>weekly</changefreq>")
        xml_lines.append("    <priority>0.8</priority>")
        xml_lines.append("  </url>")

        direct_xml_lines.append("  <url>")
        direct_xml_lines.append(f"    <loc>{escape(live_url)}</loc>")
        direct_xml_lines.append(f"    <lastmod>{now_iso}</lastmod>")
        direct_xml_lines.append("    <changefreq>weekly</changefreq>")
        direct_xml_lines.append("    <priority>0.8</priority>")
        direct_xml_lines.append("  </url>")

    xml_lines.append("</urlset>")
    direct_xml_lines.append("</urlset>")

    with open(SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(xml_lines) + "\n")
    with open(URLS_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(gh_urls) + "\n")

    with open(DIRECT_SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(direct_xml_lines) + "\n")
    with open(DIRECT_URLS_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(direct_urls) + "\n")

    print(f"[+] Wrote {SITEMAP_PATH} ({len(tasks)} tasks + root)")
    print(f"[+] Wrote {URLS_TXT_PATH} ({len(gh_urls)} URLs)")
    print(f"[+] Wrote {DIRECT_SITEMAP_PATH} ({len(direct_urls)} URLs)")
    print(f"[+] Wrote {DIRECT_URLS_TXT_PATH} ({len(direct_urls)} URLs)")


def generate_index_html(tasks: list[dict]):
    """Generates the main index.html client-side searchable catalog dashboard."""
    print("[*] Generating index.html client-side searchable catalog...")
    total_count = len(tasks)
    divisions = sorted(list({t["division"] for t in tasks if t.get("division")}))
    years = sorted(list({t["fiscal_year"] for t in tasks if t.get("fiscal_year")}), reverse=True)

    # Prepare compact JSON rows for fast in-browser filtering
    compact_records = []
    for t in tasks:
        compact_records.append({
            "id": t["task_id"],
            "t": t["title"],
            "pi": t["pi_name"],
            "inst": t["pi_affiliation"],
            "div": t["division"],
            "fy": t["fiscal_year"],
            "grant": t["grant_contract_no"],
            "start": t["start_date"],
            "end": t["end_date"],
        })

    json_payload = json.dumps(compact_records, separators=(",", ":"))

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA Task Book: Biological and Physical Sciences & Human Research</title>
  <style>
    :root {{
      --nasa-blue: #0b3d91;
      --nasa-red: #d83933;
      --text-dark: #1b1b1b;
      --bg-light: #f8f9fa;
      --card-bg: #ffffff;
      --border-color: #dfe1e5;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      margin: 0;
      padding: 0;
      background: var(--bg-light);
      color: var(--text-dark);
    }}
    header {{
      background: var(--nasa-blue);
      color: white;
      padding: 1.5rem 2rem;
      border-bottom: 4px solid var(--nasa-red);
    }}
    .header-content {{
      max-width: 1200px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
    }}
    .header-content h1 {{
      margin: 0;
      font-size: 1.6rem;
    }}
    .header-content p {{
      margin: 0.25rem 0 0 0;
      font-size: 0.95rem;
      opacity: 0.9;
    }}
    .container {{
      max-width: 1200px;
      margin: 2rem auto;
      padding: 0 1.5rem;
    }}
    .stats-bar {{
      display: flex;
      gap: 1.5rem;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
    }}
    .stat-card {{
      background: white;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 1rem 1.5rem;
      flex: 1;
      min-width: 200px;
    }}
    .stat-card .num {{
      font-size: 1.8rem;
      font-weight: 700;
      color: var(--nasa-blue);
    }}
    .stat-card .label {{
      font-size: 0.85rem;
      color: #6c757d;
      text-transform: uppercase;
      font-weight: 600;
    }}
    .controls {{
      background: white;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 1.25rem;
      margin-bottom: 1.5rem;
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      align-items: center;
    }}
    .controls input[type="text"] {{
      flex: 2;
      min-width: 260px;
      padding: 0.6rem 0.8rem;
      font-size: 0.95rem;
      border: 1px solid #ced4da;
      border-radius: 4px;
    }}
    .controls select {{
      flex: 1;
      min-width: 180px;
      padding: 0.6rem 0.8rem;
      font-size: 0.95rem;
      border: 1px solid #ced4da;
      border-radius: 4px;
    }}
    .table-container {{
      background: white;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }}
    th {{
      background: #f1f3f5;
      color: #495057;
      text-align: left;
      padding: 0.75rem 1rem;
      border-bottom: 2px solid var(--border-color);
      font-weight: 600;
    }}
    td {{
      padding: 0.75rem 1rem;
      border-bottom: 1px solid #e9ecef;
      vertical-align: top;
    }}
    tr:hover td {{
      background: #f8f9fa;
    }}
    .project-link {{
      color: var(--nasa-blue);
      font-weight: 600;
      text-decoration: none;
    }}
    .project-link:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
      background: #e9ecef;
      color: #495057;
    }}
    .pagination {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 1rem;
      background: white;
      border-top: 1px solid var(--border-color);
    }}
    .pagination button {{
      padding: 0.4rem 0.8rem;
      font-size: 0.9rem;
      border: 1px solid #ced4da;
      background: white;
      border-radius: 4px;
      cursor: pointer;
    }}
    .pagination button:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
    }}
    footer {{
      text-align: center;
      padding: 2rem;
      font-size: 0.85rem;
      color: #6c757d;
      margin-top: 2rem;
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-content">
      <div>
        <h1>NASA Task Book</h1>
        <p>Biological and Physical Sciences & Human Research Program Research Catalog</p>
      </div>
      <div>
        <a href="taskbook_sitemap.xml" style="color: white; margin-right: 1rem; text-decoration: none;">XML Sitemap</a>
        <a href="https://taskbook.nasaprs.com" target="_blank" style="color: #90caf9; text-decoration: none;">Live Task Book &rarr;</a>
      </div>
    </div>
  </header>

  <div class="container">
    <div class="stats-bar">
      <div class="stat-card">
        <div class="num" id="stat-total">{total_count}</div>
        <div class="label">Total Research Projects</div>
      </div>
      <div class="stat-card">
        <div class="num">{len(divisions)}</div>
        <div class="label">Divisions Active</div>
      </div>
      <div class="stat-card">
        <div class="num" id="stat-showing">{total_count}</div>
        <div class="label">Matching Results</div>
      </div>
    </div>

    <div class="controls">
      <input type="text" id="searchInput" placeholder="Search by Project Title, PI Name, Institution, Grant...">
      <select id="divisionFilter">
        <option value="">All Divisions</option>
        {"".join(f'<option value="{escape(d)}">{escape(d)}</option>' for d in divisions)}
      </select>
      <select id="yearFilter">
        <option value="">All Fiscal Years</option>
        {"".join(f'<option value="{escape(y)}">{escape(y)}</option>' for y in years)}
      </select>
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th style="width: 35%;">Project Title</th>
            <th style="width: 25%;">Principal Investigator</th>
            <th style="width: 15%;">Division</th>
            <th style="width: 10%;">Fiscal Year</th>
            <th style="width: 15%;">Actions</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
      <div class="pagination">
        <span id="pageInfo">Showing 1 to 25</span>
        <div>
          <button id="prevBtn" onclick="prevPage()">&larr; Previous</button>
          <button id="nextBtn" onclick="nextPage()">Next &rarr;</button>
        </div>
      </div>
    </div>
  </div>

  <footer>
    <p>NASA Task Book Mirror &middot; Generated by <a href="https://github.com/Oht8wooWi8yait9n/taskbook">Oht8wooWi8yait9n/taskbook</a> &middot; Authoritative source: <a href="https://taskbook.nasaprs.com" target="_blank">taskbook.nasaprs.com</a></p>
  </footer>

  <script>
    const records = {json_payload};
    let filtered = records;
    let currentPage = 1;
    const pageSize = 25;

    const searchInput = document.getElementById('searchInput');
    const divisionFilter = document.getElementById('divisionFilter');
    const yearFilter = document.getElementById('yearFilter');
    const tableBody = document.getElementById('tableBody');
    const statShowing = document.getElementById('stat-showing');
    const pageInfo = document.getElementById('pageInfo');
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');

    function applyFilter() {{
      const q = searchInput.value.toLowerCase().trim();
      const div = divisionFilter.value;
      const yr = yearFilter.value;

      filtered = records.filter(r => {{
        if (div && r.div !== div) return false;
        if (yr && r.fy !== yr) return false;
        if (q) {{
          const str = (r.t + ' ' + r.pi + ' ' + r.inst + ' ' + r.grant).toLowerCase();
          if (!str.includes(q)) return false;
        }}
        return true;
      }});

      statShowing.textContent = filtered.length;
      currentPage = 1;
      renderTable();
    }}

    function renderTable() {{
      const start = (currentPage - 1) * pageSize;
      const end = Math.min(start + pageSize, filtered.length);
      const pageItems = filtered.slice(start, end);

      if (filtered.length === 0) {{
        tableBody.innerHTML = '<tr><td colspan="5" style="text-align: center; padding: 2rem;">No matching research projects found.</td></tr>';
        pageInfo.textContent = 'Showing 0 of 0';
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
      }}

      let html = '';
      for (const r of pageItems) {{
        html += `<tr>
          <td><a class="project-link" href="tasks/${{r.id}}.html">${{r.t}}</a></td>
          <td><strong>${{r.pi}}</strong><br><span style="color: #6c757d; font-size: 0.85rem;">${{r.inst}}</span></td>
          <td><span class="badge">${{r.div}}</span></td>
          <td>${{r.fy}}</td>
          <td>
            <a href="tasks/${{r.id}}.html" style="font-size: 0.85rem; margin-right: 0.5rem;">Mirror</a>
            <a href="https://taskbook.nasaprs.com/tbp/tbpdf.cfm?id=${{r.id}}" target="_blank" style="font-size: 0.85rem; color: var(--nasa-red);">PDF</a>
          </td>
        </tr>`;
      }}
      tableBody.innerHTML = html;

      pageInfo.textContent = `Showing ${{start + 1}} to ${{end}} of ${{filtered.length}}`;
      prevBtn.disabled = currentPage === 1;
      nextBtn.disabled = end >= filtered.length;
    }}

    function prevPage() {{
      if (currentPage > 1) {{
        currentPage--;
        renderTable();
      }}
    }}

    function nextPage() {{
      if ((currentPage * pageSize) < filtered.length) {{
        currentPage++;
        renderTable();
      }}
    }}

    searchInput.addEventListener('input', applyFilter);
    divisionFilter.addEventListener('change', applyFilter);
    yearFilter.addEventListener('change', applyFilter);

    renderTable();
  </script>
</body>
</html>
"""
    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[+] Wrote {INDEX_HTML_PATH}")


def main():
    print("=" * 70)
    print(" NASA Task Book (taskbook.nasaprs.com) Harvester & Static Mirror")
    print(f" Target Directory: {REPO_DIR}")
    print("=" * 70)

    session = TaskBookSession()

    # Step 1: Discover all tasks across 117 pages
    tasks_index = discover_all_tasks(session)

    # Step 2: Harvest details and generate static HTML cards
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    full_tasks = []
    total = len(tasks_index)
    print(f"\n[*] Harvesting detailed metadata and pre-rendering {total} tasks...")

    # Load any already completed tasks from jsonl cache
    completed_map = {}
    if JSONL_PATH.exists():
        try:
            with open(JSONL_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        obj = json.loads(line)
                        completed_map[obj["task_id"]] = obj
            print(f"[+] Found {len(completed_map)} previously parsed tasks in {JSONL_PATH}")
        except Exception as e:
            print(f"[!] Error reading existing JSONL: {e}")

    fetch_limit = int(os.environ.get("FETCH_DETAILS_LIMIT", "50"))
    if "--all" in sys.argv:
        fetch_limit = total
    print(f"[*] Detail network fetch limit: {fetch_limit} tasks (cached tasks always fully loaded)")

    network_fetches_count = 0
    for idx, t_meta in enumerate(tasks_index, 1):
        tid = t_meta["task_id"]
        html_file = TASKS_DIR / f"{tid}.html"

        if tid in completed_map:
            full_task = completed_map[tid]
        else:
            should_fetch_network = (network_fetches_count < fetch_limit) and not (RAW_CACHE_DIR / f"{tid}.html").exists()
            try:
                full_task = fetch_task_details(session, t_meta, fetch_network=should_fetch_network)
                if should_fetch_network:
                    network_fetches_count += 1
                completed_map[tid] = full_task
            except Exception as e:
                print(f"\n[!] Failed to harvest task {tid} ({t_meta.get('title')}): {e}")
                continue

        full_task["gh_pages_url"] = f"{GH_PAGES_BASE}/tasks/{tid}"
        # Render HTML card
        html_content = render_html_page(full_task)
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        full_tasks.append(full_task)

        if idx % 100 == 0 or idx == total:
            print(f"    Harvested {idx}/{total} tasks ({(idx/total)*100:.1f}%)...")
            # Save checkpoint of JSONL
            with open(JSONL_PATH, "w", encoding="utf-8") as f:
                for t in full_tasks:
                    f.write(json.dumps(t) + "\n")

    # Final JSONL write
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for t in full_tasks:
            f.write(json.dumps(t) + "\n")
    print(f"[+] Wrote {JSONL_PATH} with {len(full_tasks)} task records.")

    # Step 3: Write sitemaps
    write_sitemaps(full_tasks)

    # Step 4: Write index.html dashboard
    generate_index_html(full_tasks)

    print("\n" + "=" * 70)
    print(f"[✓] NASA Task Book pipeline completed successfully!")
    print(f"    - Pre-rendered HTML Cards: {len(full_tasks)} in {TASKS_DIR}")
    print(f"    - Structured JSON Lines:   {JSONL_PATH}")
    print(f"    - Interactive Catalog:     {INDEX_HTML_PATH}")
    print(f"    - GitHub Pages Sitemap:    {SITEMAP_PATH}")
    print(f"    - Direct Live Sitemap:     {DIRECT_SITEMAP_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
