"""
Placeholder scraper.

This is a STAND-IN for now, just so you can see the whole pipeline
(GitHub Action -> script -> data file -> commit) working end-to-end.

Next step (once this is running) will be to replace the fake data below
with real requests to expolume.com and real HTML parsing.
"""

import json
from datetime import datetime, timezone


def scrape_fairs():
    # TODO: replace this with real scraping logic.
    # For now, return fake data so we can prove the pipeline works.
    return [
        {
            "name": "Example China Trade Fair",
            "dates": "2026-08-10 to 2026-08-12",
            "city": "Shanghai",
            "category": "Example category",
        }
    ]


def main():
    fairs = scrape_fairs()
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "fairs": fairs,
    }
    with open("data/fairs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(fairs)} fairs to data/fairs.json")


if __name__ == "__main__":
    main()
