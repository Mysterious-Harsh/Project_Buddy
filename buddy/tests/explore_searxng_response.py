"""
explore_searxng_response.py

Exploration script — dumps raw SearXNG JSON to understand what fields
come back, especially around images, thumbnails, and captions.

Run:
    mamba activate buddy
    python buddy/tests/explore_searxng_response.py
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urljoin

import requests

SEARXNG_URL = "http://127.0.0.1:8888"
TIMEOUT_S = 10
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

QUERIES = [
    ("AAPL stock price chart prediction 2024", "general"),
    ("AAPL stock price chart prediction 2024", "images"),
    ("AAPL stock chart technical analysis", "news"),
]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _search(query: str, categories: str = "general") -> dict:
    resp = requests.get(
        f"{SEARXNG_URL}/search",
        params={
            "q": query,
            "format": "json",
            "safesearch": "0",
            "categories": categories,
        },
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json()


def _all_keys(results: list[dict]) -> set[str]:
    keys: set[str] = set()
    for r in results:
        keys.update(r.keys())
    return keys


def _image_fields(result: dict) -> dict:
    """Pull every field that might carry image/visual data."""
    candidates = [
        "img_src", "thumbnail", "thumbnail_src", "image", "image_url",
        "img", "preview", "content_image", "og_image",
    ]
    return {k: result[k] for k in candidates if k in result}


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    # Quick connectivity check
    try:
        requests.get(SEARXNG_URL, timeout=4)
    except Exception as e:
        print(f"[ERROR] SearXNG not reachable at {SEARXNG_URL}: {e}")
        print("Start Buddy first so boot.py launches SearXNG, then re-run.")
        sys.exit(1)

    for query, category in QUERIES:
        _section(f'query="{query}"  categories={category}')

        try:
            data = _search(query, category)
        except Exception as e:
            print(f"  [FAILED] {e}")
            continue

        results = data.get("results", [])
        print(f"  total results returned : {len(results)}")
        print(f"  all field names across results: {sorted(_all_keys(results))}")

        # Show image-related fields per result
        print(f"\n  --- Image fields per result ---")
        for i, r in enumerate(results[:5]):
            img = _image_fields(r)
            title = r.get("title", "")[:60]
            url   = r.get("url", "")[:70]
            print(f"\n  [{i}] {title}")
            print(f"       url     : {url}")
            print(f"       engines : {r.get('engines', [])}")
            if img:
                for k, v in img.items():
                    val = str(v)[:120]
                    print(f"       {k:15s}: {val}")
            else:
                print(f"       (no image fields)")

        # Show one full raw result for deep inspection
        if results:
            _section(f"Full raw result[0] — category={category}")
            print(json.dumps(results[0], indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Fetch one result URL and inspect <img> tags with BeautifulSoup
    # ------------------------------------------------------------------
    _section("HTML <img> tag inspection — fetching first general result URL")

    try:
        general_data = _search(QUERIES[0][0], "general")
        general_results = general_data.get("results", [])
        if not general_results:
            print("  No results to fetch.")
        else:
            target_url = general_results[0].get("url", "")
            print(f"  Fetching: {target_url}")

            page_resp = requests.get(
                target_url,
                timeout=10,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            page_resp.raise_for_status()
            html = page_resp.text

            try:
                from bs4 import BeautifulSoup
            except ImportError:
                print("  [SKIP] BeautifulSoup not installed (pip install beautifulsoup4 lxml)")
            else:
                soup = BeautifulSoup(html, "lxml")
                imgs = soup.find_all("img")
                print(f"  Total <img> tags found: {len(imgs)}")
                print(f"\n  --- First 10 <img> tags ---")
                for i, img in enumerate(imgs[:10]):
                    src        = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
                    alt        = img.get("alt", "").strip()
                    title_attr = img.get("title", "").strip()
                    width      = img.get("width", "")
                    height     = img.get("height", "")

                    # Nearest figcaption
                    figcaption = ""
                    parent = img.find_parent("figure")
                    if parent:
                        fc = parent.find("figcaption")
                        if fc:
                            figcaption = fc.get_text(strip=True)[:80]

                    # Nearest heading above
                    heading = ""
                    for el in img.find_all_previous(["h1","h2","h3","h4","h5","h6"]):
                        heading = el.get_text(strip=True)[:60]
                        break

                    # Resolve relative src
                    if src and not src.startswith("data:"):
                        src = urljoin(target_url, src)

                    print(f"\n  [{i}]")
                    print(f"    src        : {src[:100]}")
                    print(f"    alt        : {alt[:80]}")
                    print(f"    title      : {title_attr[:80]}")
                    print(f"    size       : {width} x {height}")
                    print(f"    figcaption : {figcaption}")
                    print(f"    heading    : {heading}")

    except Exception as e:
        print(f"  [FAILED] {e}")

    print(f"\n{'='*60}")
    print("  Done.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
