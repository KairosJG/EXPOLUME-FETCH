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
from io import BytesIO

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SEARCH_ENDPOINT = "https://expolume.com/"
WARMUP_URL = "https://expolume.com/expo/"

DATE_PATTERN = re.compile(
    r"([A-Z][a-z]{2} \d{1,2}, \d{4})\s*To\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)


def get_date_range(months_ahead=6):
    """
    Returns (start_str, end_str) covering from the 1st of the current month
    through the end of `months_ahead` months from now. We scrape this whole
    window in one request; the dashboard itself handles filtering down to
    a specific month, so the scraper doesn't need to run separately per
    month anymore.
    """
    now = datetime.now(timezone.utc)
    start = now.replace(day=1)

    end_month = now.month + months_ahead
    end_year = now.year
    while end_month > 12:
        end_month -= 12
        end_year += 1
    last_day = calendar.monthrange(end_year, end_month)[1]
    end = datetime(end_year, end_month, last_day, tzinfo=timezone.utc)

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def build_search_url(start_str, end_str, page=1, limit=500):
    params = {
        "vx": "1",
        "action": "search_posts",
        "type": "expo",
        "keywords": "",
        "industries": "",
        "regions": "china",
        "relations": "",
        "recurring-date": f"{start_str}..{end_str}",
        "sort": "latest",
        "pg": str(page),
        "limit": str(limit),
        "__template_id": "65809",
        "__get_total_count": "1",
    }
    return SEARCH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def extract_fairs_from_html(html):
    """
    Parses the HTML fragment the search endpoint returns.

    For each fair link, we climb up through parent elements looking for
    one whose text contains a date range. IMPORTANT: at each step we check
    whether that ancestor now contains MORE THAN ONE fair link — if so,
    we've crossed out of this fair's own card and into a container that
    holds multiple cards. We stop immediately in that case and use the
    last known single-card-scoped text, rather than risk grabbing a
    neighboring fair's date/venue by mistake (this was causing occasional
    wrong, sometimes past, dates to show up).
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
        last_safe_text = ""
        last_safe_node = link.parent
        node = link.parent
        for _ in range(8):
            if node is None:
                break
            links_here = {
                a.get("href") for a in node.select('a[href*="/expo/"]') if a.get("href")
            }
            if len(links_here) > 1:
                # This ancestor spans multiple cards — stop, don't use its text.
                break
            text = node.get_text("\n", strip=True)
            last_safe_text = text
            last_safe_node = node
            if DATE_PATTERN.search(text):
                context = text
                break
            node = node.parent
        if not context:
            context = last_safe_text

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

        # Grab the fair's thumbnail/logo image, if present, from within
        # this same card-scoped container. Some sites lazy-load images
        # (real image in data-src, a placeholder in src), so check both.
        image_url = None
        img_tag = last_safe_node.find("img") if last_safe_node else None
        if img_tag:
            image_url = (
                img_tag.get("data-src")
                or img_tag.get("data-lazy-src")
                or img_tag.get("src")
            )
            if image_url and image_url.startswith("data:"):
                # A base64 placeholder, not a real lazy-loaded image — skip it.
                image_url = None

        fairs.append({
            "name": title,
            "url": href,
            "dates": dates,
            "venue": venue,
            "image_url": image_url,
        })

    return fairs


def fetch_all_fairs(start_str, end_str, page_size=500, max_pages=10,
                     official_url_cache=None, gallery_cache=None):
    """
    Fetches every page of results for the given date range, merging by
    URL to avoid duplicates. Then, still using the same authenticated
    browser session, visits each fair's own page to grab its official
    website link and a small photo gallery from that official site —
    skipping anything already resolved in a previous run to keep repeat
    runs faster.
    """
    official_url_cache = official_url_cache or {}
    gallery_cache = gallery_cache or {}
    all_fairs = []
    seen_urls = set()
    last_url_used = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context()
        page = context.new_page()

        page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(10):
            if "Just a moment" not in page.title():
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(1500)

        for page_num in range(1, max_pages + 1):
            search_url = build_search_url(start_str, end_str, page=page_num, limit=page_size)
            last_url_used = search_url
            response = context.request.get(
                search_url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": WARMUP_URL,
                    "Accept": "*/*",
                },
            )
            html = response.text()
            page_fairs = extract_fairs_from_html(html)

            new_fairs = [f for f in page_fairs if f["url"] not in seen_urls]
            print(f"  Page {page_num}: {len(page_fairs)} fairs on page, {len(new_fairs)} new")

            if not new_fairs:
                break

            for f in new_fairs:
                seen_urls.add(f["url"])
                all_fairs.append(f)

            if len(page_fairs) < page_size:
                break

        to_fetch = [f for f in all_fairs if f["url"] not in official_url_cache]
        print(f"Fetching official website + gallery for {len(to_fetch)} fair(s) "
              f"({len(all_fairs) - len(to_fetch)} reused from last run)...")

        for i, fair in enumerate(all_fairs, start=1):
            try:
                if fair["url"] in official_url_cache:
                    fair["official_url"] = official_url_cache[fair["url"]]
                else:
                    fair["official_url"] = fetch_official_website(context, fair["url"])

                if fair["url"] in gallery_cache:
                    fair["gallery"] = gallery_cache[fair["url"]]
                else:
                    gallery = fetch_gallery(context, fair.get("official_url"))
                    if not gallery and fair.get("image_url"):
                        gallery = [fair["image_url"]]
                    fair["gallery"] = gallery
            except Exception:
                fair.setdefault("official_url", None)
                fair.setdefault("gallery", [fair["image_url"]] if fair.get("image_url") else [])

            if i % 50 == 0:
                print(f"  ...{i}/{len(all_fairs)} fair pages checked")

        browser.close()

    return all_fairs, last_url_used


def parse_single_date(date_str):
    try:
        return datetime.strptime(date_str.strip(), "%b %d, %Y")
    except (ValueError, AttributeError):
        return None


def is_already_past(fair, today):
    """
    Returns True if this fair's date range is entirely in the past.
    If we can't parse a date at all, we keep the fair rather than risk
    silently dropping something we just don't understand the format of.
    """
    dates_str = fair.get("dates")
    if not dates_str:
        return False
    end_str = dates_str.split(" To ")[-1]
    end_date = parse_single_date(end_str)
    if end_date is None:
        return False
    return end_date.date() < today


def fetch_official_website(context, fair_url):
    """
    Visits a single fair's own Expolume page and looks for its
    "Official Website" button/link, returning that URL if found.
    """
    try:
        response = context.request.get(fair_url, headers={"Referer": WARMUP_URL})
        html = response.text()
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() == "official website":
                return a["href"]
        return None
    except Exception:
        return None


try:
    from PIL import Image
except ImportError:
    Image = None


def looks_like_a_photo(image_bytes, min_dim=250):
    """
    Heuristic check for "is this a real photo, not a logo/icon/QR code/banner".
    Not perfect (no actual image understanding), but filters out most
    obvious junk:
    - Too small -> likely an icon, not an event photo.
    - Extreme aspect ratio -> likely a banner/strip, not a photo.
    - Very few distinct colors -> likely a flat graphic, logo, or QR code
      (real photos almost always have hundreds+ of distinct colors).
    """
    if Image is None:
        return True  # Pillow not installed -> don't filter, fail open
    try:
        im = Image.open(BytesIO(image_bytes))
        w, h = im.size
        if w < min_dim or h < min_dim:
            return False
        ratio = w / h if h else 0
        if ratio > 4 or ratio < 0.25:
            return False
        small = im.convert("RGB").resize((80, 80))
        colors = small.getcolors(maxcolors=100000)
        unique_count = len(colors) if colors else 100000
        if unique_count < 200:
            return False
        return True
    except Exception:
        return True  # couldn't decode -> don't over-filter, let it through


def extract_gallery_images(html, base_url, max_images=4):
    """
    Tries to find up to `max_images` real event photos on an official
    fair website. Strategy:
    1. og:image meta tags (some sites list more than one).
    2. Fallback: scan <img> tags on the page, skipping obvious non-content
       images (logos, icons, tiny images) based on filename hints and
       width/height attributes when available.
    """
    soup = BeautifulSoup(html, "html.parser")
    images = []
    seen = set()

    for meta in soup.find_all("meta", property="og:image"):
        src = meta.get("content")
        if src and src not in seen:
            images.append(src)
            seen.add(src)
        if len(images) >= max_images:
            return images

    skip_keywords = ["logo", "icon", "sprite", "avatar", "flag", "badge", "placeholder", "spinner", "loading"]
    for img in soup.find_all("img"):
        if len(images) >= max_images:
            break
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if not src or src.startswith("data:"):
            continue
        low = src.lower()
        if any(k in low for k in skip_keywords):
            continue
        try:
            w = int(img.get("width", 0) or 0)
            h = int(img.get("height", 0) or 0)
            if (0 < w < 100) or (0 < h < 100):
                continue
        except ValueError:
            pass
        full_src = urllib.parse.urljoin(base_url, src)
        if full_src not in seen:
            images.append(full_src)
            seen.add(full_src)

    return images


def fetch_gallery(context, official_url, max_images=4, max_candidates=10):
    if not official_url:
        return []
    try:
        response = context.request.get(official_url, timeout=10000)
        html = response.text()
        candidates = extract_gallery_images(html, official_url, max_images=max_candidates)
    except Exception:
        return []

    good = []
    for src in candidates:
        if len(good) >= max_images:
            break
        try:
            img_response = context.request.get(src, timeout=6000)
            img_bytes = img_response.body()
            if looks_like_a_photo(img_bytes):
                good.append(src)
        except Exception:
            continue

    return good


def load_previous_official_urls(path="data/fairs.json"):
    """
    Reuses official_url and gallery values already fetched in a previous
    run, keyed by fair url, so we don't re-visit every fair's page (and
    every fair's separate official website) on every single run — only
    ones we haven't resolved yet.
    """
    official_cache = {}
    gallery_cache = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        for fair in old_data.get("fairs", []):
            if fair.get("official_url"):
                official_cache[fair["url"]] = fair["official_url"]
            if fair.get("gallery"):
                gallery_cache[fair["url"]] = fair["gallery"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return official_cache, gallery_cache


def main():
    start_str, end_str = get_date_range(months_ahead=6)
    print(f"Requesting China fairs from {start_str} to {end_str}")

    official_url_cache, gallery_cache = load_previous_official_urls()

    fairs, search_url = fetch_all_fairs(
        start_str, end_str,
        official_url_cache=official_url_cache,
        gallery_cache=gallery_cache,
    )
    print(f"Found {len(fairs)} fairs total (all pages combined)")

    today = datetime.now(timezone.utc).date()
    before_count = len(fairs)
    fairs = [f for f in fairs if not is_already_past(f, today)]
    dropped = before_count - len(fairs)
    if dropped:
        print(f"Dropped {dropped} fair(s) whose dates have already passed")

    if not fairs:
        print("No fairs extracted.")

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "range": f"{start_str}..{end_str}",
        "source_url": search_url,
        "fairs": fairs,
    }

    with open("data/fairs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(fairs)} fairs to data/fairs.json")


if __name__ == "__main__":
    main()