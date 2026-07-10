"""
Real Expolume scraper (v3).

This calls the exact search endpoint the site's own filter panel uses,
found by inspecting the Network tab while manually applying filters in a
browser. This is far more reliable than clicking through their UI:
- No date-picker or checkbox automation to break if their design changes.
- No "Load more" button clicking.
- No ad overlays blocking clicks.

The only requirement is a valid Cloudflare "clearance" cookie, which we get
by first loading the normal page once in a real browser (letting Cloudflare's
JS challenge pass), then reusing that same authenticated browser session to
call the search endpoint directly with our own region/date parameters.
"""

import calendar
import json
import re
import urllib.parse
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SEARCH_ENDPOINT = "https://expolume.com/"
WARMUP_URL = "https://expolume.com/expo/"

DATE_PATTERN = re.compile(
    r"([A-Z][a-z]{2} \d{1,2}, \d{4})\s*To\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)


def get_target_month():
    """
    Default: NEXT calendar month (since the whole point is to have next
    month's shortlist ready before the current month ends). Change this
    function if you'd rather target the current month instead.
    """
    now = datetime.now(timezone.utc)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return year, month


def build_search_url(year, month, limit=200):
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{last_day}"
    params = {
        "vx": "1",
        "action": "search_posts",
        "type": "expo",
        "keywords": "",
        "industries": "",
        "regions": "china",
        "relations": "",
        "recurring-date": f"{start}..{end}",
        "sort": "latest",
        "limit": str(limit),
        "__template_id": "65809",
        "__get_total_count": "1",
    }
    return SEARCH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def extract_fairs_from_html(html):
    """
    Parses the HTML fragment the search endpoint returns. Same
    ancestor-climbing approach as before (look for a parent whose text
    contains a date range), just done with BeautifulSoup instead of a
    live page, since we now have raw HTML rather than a browser page.
    """
    soup = BeautifulSoup(html, "html.parser")
    fairs = []
    seen = set()

    for link in soup.select('a[href*="/expo/"]'):
        href = link.get("href", "")
        if not href or href.rstrip("/") == "https://expolume.com/expo":
            continue
        title = link.get_text(strip=True)
        if not title or href in seen:
            continue
        seen.add(href)

        context = ""
        node = link.parent
        for _ in range(6):
            if node is None:
                break
            text = node.get_text("\n", strip=True)
            if DATE_PATTERN.search(text):
                context = text
                break
            node = node.parent
        if not context and link.parent:
            context = link.parent.get_text("\n", strip=True)

        match = DATE_PATTERN.search(context)
        dates = f"{match.group(1)} To {match.group(2)}" if match else None

        venue = None
        if match:
            lines = [l.strip() for l in context.split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if match.group(1) in line:
                    if i + 1 < len(lines):
                        venue = lines[i + 1]
                    break

        fairs.append({"name": title, "url": href, "dates": dates, "venue": venue})

    return fairs


def fetch_html(year, month):
    search_url = build_search_url(year, month)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context()
        page = context.new_page()

        # Step 1: visit the normal page once so Cloudflare's challenge
        # gets solved and the browser context holds a valid clearance
        # cookie for everything that follows.
        page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(10):
            if "Just a moment" not in page.title():
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(1500)

        # Step 2: call the search endpoint directly, using the same
        # authenticated context (shares cookies with the page above).
        response = context.request.get(
            search_url,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": WARMUP_URL,
                "Accept": "*/*",
            },
        )
        html = response.text()

        browser.close()
        return html, search_url


def main():
    year, month = get_target_month()
    print(f"Requesting China fairs for {year}-{month:02d}")

    html, search_url = fetch_html(year, month)
    fairs = extract_fairs_from_html(html)
    print(f"Found {len(fairs)} fairs")

    if not fairs:
        print("No fairs extracted. Raw response snippet:")
        print(html[:1500])

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "target_month": f"{year}-{month:02d}",
        "source_url": search_url,
        "fairs": fairs,
    }

    with open("data/fairs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(fairs)} fairs to data/fairs.json")


if __name__ == "__main__":
    main()