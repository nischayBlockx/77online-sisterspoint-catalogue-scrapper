"""Hailys B2B scraper: crawl category listings, dedupe, scrape product pages, export."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from sp_scraper.exporters import (
    append_product_jsonl, load_products_jsonl, products_to_rows,
    write_catalogue_xlsx, write_shopify_csv,
)

from .extract import BASE, CATEGORY_MAP, extract_product, listing_total, parse_listing

log = logging.getLogger("hl_scraper")
COOKIE_FILE = Path(".auth/hailys_cookie.txt")
_local = threading.local()

# (listing path, brand line, group name used for the category mapping)
MAIN_GROUPS = ["accessoires", "blazer", "blusen", "hosen", "jacken", "hosenanzuege", "jeans", "kleider",
               "maentel", "roecke", "schals-tuecher", "schuhe", "shirts", "strickwaren",
               "struempfe-strumpfhosen", "sweats", "westen"]
TEEN_GROUPS = ["blusen", "hemden", "hosen", "jacken", "jeans", "kleider", "roecke", "shirts",
               "sonstige", "strickwaren", "sweats", "westen"]
ZONE_GROUPS = ["blusen", "hosen", "kleider", "shirts", "strickwaren"]
GROUP_NAME = {
    "accessoires": "Accessoires", "blazer": "Blazer", "blusen": "Blusen", "hosen": "Hosen",
    "jacken": "Jacken", "hosenanzuege": "Hosenanzüge", "jeans": "Jeans", "kleider": "Kleider",
    "maentel": "Mäntel", "roecke": "Röcke", "schals-tuecher": "Schals/Tücher", "schuhe": "Schuhe",
    "shirts": "Shirts", "strickwaren": "Strickwaren", "struempfe-strumpfhosen": "Strümpfe/Strumpfhosen",
    "sweats": "Sweats", "westen": "Westen", "hemden": "Hemden", "sonstige": "Sonstige",
}


def _session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Cookie": COOKIE_FILE.read_text().strip()})
        _local.s = s
    return _local.s


def _get(url: str, tries: int = 3) -> str | None:
    for i in range(tries):
        try:
            r = _session().get(url, timeout=40)
            if r.status_code == 200 and "login[password]" not in r.text:
                return r.text
            if "login[password]" in r.text:
                log.error("Session expired (login page returned) for %s", url)
                return None
        except requests.RequestException as e:
            log.warning("retry %s: %s", url, e)
    return None


def crawl_listing(path: str) -> list[dict]:
    items, seen, page = [], set(), 1
    total = None
    while True:
        t = _get(f"{BASE}{path}" + (f"?p={page}" if page > 1 else ""))
        if t is None:
            break
        if page == 1:
            total = listing_total(t)
            if total == 0:
                break
        tiles = [x for x in parse_listing(t) if x["url"] not in seen]
        if not tiles:
            break
        for x in tiles:
            seen.add(x["url"])
        items.extend(tiles)
        if total is not None and len(items) >= total:
            break
        page += 1
    if total is not None and len(items) != total:
        log.warning("%s: expected %s items, got %s", path, total, len(items))
    return items


def build_queue() -> tuple[dict[str, dict], dict]:
    """Return {url: info} deduped; main/teens/z-one groups define the category."""
    sources = []
    for g in MAIN_GROUPS:
        sources.append((f"/hailys/{g}.html", "HAILYS", GROUP_NAME[g]))
    for g in TEEN_GROUPS:
        sources.append((f"/hailys-teens/{g}.html", "TEENS", GROUP_NAME[g]))
    for g in ZONE_GROUPS:
        sources.append((f"/z-one/{g}.html", "Z-ONE", GROUP_NAME[g]))
    sources.append(("/pos.html", "POS", ""))
    extra = [("/nos.html", "NOS"), ("/new-in.html", "NEW IN"), ("/topseller.html", "TOPSELLER")]

    queue: dict[str, dict] = {}
    stats = {"listing_hits": 0, "dupes_skipped": 0, "per_source": {}}
    for path, line, group in sources:
        items = crawl_listing(path)
        stats["per_source"][path] = len(items)
        for it in items:
            stats["listing_hits"] += 1
            if it["url"] in queue:
                stats["dupes_skipped"] += 1
                continue
            it.update(line=line, group=group, category=CATEGORY_MAP.get(group, ""), tags=[])
            queue[it["url"]] = it
        log.info("%-40s %4d items (queue %d)", path, len(items), len(queue))
    for path, tag in extra:
        items = crawl_listing(path)
        stats["per_source"][path] = len(items)
        for it in items:
            if it["url"] in queue:
                queue[it["url"]]["tags"].append(tag)
            else:
                it.update(line=tag, group="", category="", tags=[tag])
                queue[it["url"]] = it
                stats.setdefault("only_in_" + tag, []).append(it["url"])
        log.info("%-40s %4d items (queue %d)", path, len(items), len(queue))
    return queue, stats


def cmd_scrape(args) -> int:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "hl_products.jsonl"
    if args.fresh and jsonl.exists():
        jsonl.unlink()
    if _get(f"{BASE}/") is None:
        log.error("Not authenticated. Put the Cookie header value in %s", COOKIE_FILE)
        return 2

    queue, stats = build_queue()
    (out / "hl_queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    log.info("Unique product URLs: %d (listing hits %d, duplicates skipped %d)",
             len(queue), stats["listing_hits"], stats["dupes_skipped"])

    done = {p.url for p in load_products_jsonl(jsonl)}
    todo = [u for u in queue if u not in done]
    log.info("%d to fetch (%d already done)", len(todo), len(done))

    lock = threading.Lock()
    failed: list[str] = []
    counter = {"n": 0}

    def work(u: str):
        info = queue[u]
        page = _get(u)
        p = extract_product(page, u, info, info["category"]) if page else None
        with lock:
            counter["n"] += 1
            if not p:
                failed.append(u)
                log.warning("[%d/%d] FAILED %s", counter["n"], len(todo), u)
                return
            if "NOS" in info["tags"]:
                p.season = "NOS"
            append_product_jsonl(jsonl, p)
            if counter["n"] % 50 == 0:
                log.info("[%d/%d] %s", counter["n"], len(todo), p.style_name)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))

    products = load_products_jsonl(jsonl)
    # dedupe on article number as a second safety net
    seen_art, uniq = set(), []
    for p in products:
        if p.style_code in seen_art:
            log.warning("Duplicate article number dropped: %s %s", p.style_code, p.url)
            continue
        seen_art.add(p.style_code)
        uniq.append(p)
    log.info("Scraped %d products (%d failed, %d duplicates by article no.)",
             len(uniq), len(failed), len(products) - len(uniq))
    (out / "hl_failed.json").write_text(json.dumps(failed, indent=1))
    (out / "hl_stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False))
    _export(uniq, out)
    return 1 if failed else 0


def _export(products, out: Path) -> None:
    rows = products_to_rows(products, {"default_brand": "Hailys"})
    write_catalogue_xlsx(rows, out / "hl_catalogue_all.xlsx")
    write_shopify_csv(rows, out / "hl_shopify_products_all.csv", {
        "shopify": {"status": "draft", "published": False, "price_multiplier": 1.0,
                    "markup_on_wholesale": 2.5, "track_inventory": True}})
    log.info("Exported %d variants / %d products -> %s", len(rows), len(products), out)


def cmd_export(args) -> int:
    out = Path(args.output)
    _export(load_products_jsonl(out / "hl_products.jsonl"), out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hl_scraper")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sc = sub.add_parser("scrape")
    sc.add_argument("--output", default="output/hailys")
    sc.add_argument("--fresh", action="store_true")
    sc.add_argument("--workers", type=int, default=5)
    ex = sub.add_parser("export")
    ex.add_argument("--output", default="output/hailys")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not a.cmd:
        ap.print_help()
        return 1
    return {"scrape": cmd_scrape, "export": cmd_export}[a.cmd](a)
