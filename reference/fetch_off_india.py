#!/usr/bin/env python3
"""One-time fetcher: pull Open Food Facts product rows tagged country=India
into reference/openfoodfacts_india.csv.

Use case: a brand + pack-size reference table to align/enrich the Mumbai
quick-commerce catalog parsed by src/product_fields.parse_name (M1/M2).
OFF is Open Database License (ODbL) — redistribution fine, keep attribution.

RESUMABLE: tracks progress in reference/.off_india_progress and APPENDS to the
CSV. Re-run as many times as needed; pages already fetched are skipped. This is
deliberately patient (slow pace + long backoff) because OFF edge-blocks
aggressive crawlers with HTTP 503/401.

Run:  python3 reference/fetch_off_india.py
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://world.openfoodfacts.org/api/v2/search"
FIELDS = ["code", "product_name", "brands", "quantity", "categories", "countries"]
OUT = os.path.join(HERE, "openfoodfacts_india.csv")
PROGRESS = os.path.join(HERE, ".off_india_progress")
PAGE_SIZE = 100
UA = "qcom-scraping-research/1.0 (local operator; OFF data used under ODbL)"
MAX_RETRIES = 8
BASE_WAIT = 10          # seconds; grows per attempt (BASE_WAIT * attempt)
BETWEEN_PAGES = 3.0    # polite gap between successful pages


def fetch_page(page):
    params = {
        "countries_tags": "en:india",
        "fields": ",".join(FIELDS),
        "page_size": PAGE_SIZE,
        "page": page,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status != 200:
                    last = f"HTTP {r.status}"
                    time.sleep(BASE_WAIT * attempt)
                    continue
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            # 401/429/503 all mean back off harder
            time.sleep(BASE_WAIT * attempt * (2 if e.code in (401, 429, 503) else 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(BASE_WAIT * attempt)
    print(f"  page {page}: gave up ({last})", file=sys.stderr)
    return None


def load_progress():
    try:
        with open(PROGRESS) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_progress(page):
    with open(PROGRESS, "w") as f:
        f.write(str(page))


def main():
    # First call to learn total page count (cheap; retries built in)
    first = fetch_page(1)
    if first is None:
        print("Could not reach OFF even for page 1 (still rate-limited). "
              "Wait and re-run later.", file=sys.stderr)
        return
    total = first.get("count", 0)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    done = load_progress()
    print(f"India count: {total}  total pages: {pages}  already done: {done}",
          file=sys.stderr)

    file_exists = os.path.exists(OUT)
    with open(OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not file_exists:
            w.writeheader()
            save_progress(0)
            done = 0

        def emit(products, page):
            for p in products:
                w.writerow({k: p.get(k, "") for k in FIELDS})
            save_progress(page)

        # emit page 1 if not already captured
        if done < 1:
            emit(first.get("products", []), 1)
            print(f"  wrote page 1 ({len(first.get('products', []))})", file=sys.stderr)

        for page in range(max(done + 1, 2), pages + 1):
            data = fetch_page(page)
            if data is None:
                print(f"Stopping at page {page} (rate-limited). Re-run to resume.",
                      file=sys.stderr)
                return
            emit(data.get("products", []), page)
            if page % 10 == 0:
                print(f"  page {page}/{pages} done", file=sys.stderr)
            time.sleep(BETWEEN_PAGES)

    print(f"DONE. Final CSV: {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
