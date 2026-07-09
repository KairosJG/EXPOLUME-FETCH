"""
Real Expolume scraper.

Uses Playwright (a headless browser) instead of plain requests, because
Expolume's region/date filters are applied by JavaScript in the browser,
not by the server reading the URL. A plain "download the HTML" approach
would silently ignore our filters and return the default unfiltered list.

WHAT THIS SCRIPT DOES:
1. Works out the target month (see get_target_month() below).
2. Builds the filtered Expolume URL for China fairs in that month.
3. Opens that URL in a headless Chromium browser.
4. Clicks "Load more" repeatedly until all results for that month are loaded.
5. Extracts each fair's name, real link, dates, and venue.
6. Saves everything to data/fairs.json.

NOTE FOR NEXT ITERATION:
I couldn't test this against the live site from my own environment (no
internet access there), so this is a best-effort first pass based on how
the page is structured. If it comes back empty or wrong when you run it,
send me the log output (the workflow prints a debug snippet in that case)
and we'll adjust the extraction logic together.
"""

import calendar
import json
import re
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

BASE_URL = "https://expolume.com/expo/"


def get_target_month():
    """
    Decides which month to scrape.

    Default: NEXT calendar month (since the whole point is to have next
    month's shortlist ready before the current month ends). Change this
    function if you'd rather target the current month instead.
    """
    now = datetime.now(timezone.utc)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return year, month


def build_url(year, month):
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{last_day}"
    return (
        f"{BASE_URL}?type=expo&regions=china"
        f"&recurring-date={start}..{end}&sort=latest"
    )


def load_all_results(page, max_clicks=50):
    """Keep clicking "Load more" until it's gone or stops appearing."""
    for _ in range(max_clicks):
        load_more = page.get_by_text("Load more", exact=True)
        if load_more.count() == 0:
            break
        try:
            if not load_more.first.is_visible():
                break
            load_more.first.click()
            page.wait_for_timeout(1200)
        except Exception:
            break


DATE_PATTERN = re.compile(
    r"([A-Z][a-z]{2} \d{1,2}, \d{4})\s*To\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)


def extract_fairs(page):
    """
    Pulls every link that points to an individual fair page (/expo/<slug>/),
    then looks at the surrounding text for dates and venue. This avoids
    depending on exact CSS class names, which can change without notice.
    """
    raw = page.evaluate(
        """
        () => {
            const results = [];
            const seen = new Set();
            const links = Array.from(document.querySelectorAll('a[href*="/expo/"]'));
            for (const link of links) {
                const href = link.href;
                if (href.replace(/\\/$/, '') === 'https://expolume.com/expo') continue;
                const title = link.textContent.trim();
                if (!title || seen.has(href)) continue;
                seen.add(href);
                const container = link.closest('article') || link.parentElement;
                results.push({
                    title: title,
                    url: href,
                    context: container ? container.innerText : ''
                });
            }
            return results;
        }
        """
    )

    fairs = []
    for item in raw:
        match = DATE_PATTERN.search(item["context"])
        dates = f'{match.group(1)} To {match.group(2)}' if match else None

        # Venue: try the line right after the date line in the context text
        venue = None
        if match:
            lines = [l.strip() for l in item["context"].split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if match.group(1) in line:
                    if i + 1 < len(lines):
                        venue = lines[i + 1]
                    break

        fairs.append(
            {
                "name": item["title"],
                "url": item["url"],
                "dates": dates,
                "venue": venue,
            }
        )
    return fairs


def scrape_fairs(url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)  # let the filter JS finish applying
        load_all_results(page)
        fairs = extract_fairs(page)

        if not fairs:
            # Debug aid: print a snippet of the page so we can see what
            # actually loaded, in case the extraction logic needs fixing.
            print("No fairs extracted. Page title was:", page.title())
            print("Body snippet:", page.inner_text("body")[:1000])

        browser.close()
        return fairs


def main():
    year, month = get_target_month()
    url = build_url(year, month)
    print(f"Scraping: {url}")

    fairs = scrape_fairs(url)

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "target_month": f"{year}-{month:02d}",
        "source_url": url,
        "fairs": fairs,
    }

    with open("data/fairs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(fairs)} fairs to data/fairs.json")


if __name__ == "__main__":
    main()
