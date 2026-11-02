"""Hailys B2B scraper: crawl category listings, dedupe, scrape product pages, export."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from sp_scraper.exporters import (
    append_product_jsonl, load_products_jsonl, products_to_rows,
    write_catalogue_xlsx, write_shopify_csv,
)

from .extract import BASE, CATEGORY_MAP, _txt, extract_product, listing_total, parse_listing

log = logging.getLogger("hl_scraper")
COOKIE_FILE = Path(".auth/hailys_cookie.txt")
_local = threading.local()
PREFIX = "hl"
SITE = "hailys"

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


def _proxy_url() -> str | None:
    """Current local egress proxy; its port changes when the proxy restarts."""
    try:
        m = re.search(r"local proxy at (http://127\.0\.0\.1:\d+)", Path("/root/.ccr/README.md").read_text())
        if m:
            return m.group(1)
    except OSError:
        pass
    return os.environ.get("HTTPS_PROXY")


def _session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        proxy = _proxy_url()
        if proxy:
            s.proxies = {"https": proxy, "http": proxy}
        s.headers.update({"User-Agent": "Mozilla/5.0", "Cookie": COOKIE_FILE.read_text().strip()})
        _local.s = s
    return _local.s


def _get(url: str, tries: int = 8) -> str | None:
    for i in range(tries):
        try:
            r = _session().get(url, timeout=40)
            if r.status_code == 200 and "login[password]" not in r.text:
                return r.text
            if "login[password]" in r.text:
                log.error("Session expired (login page returned) for %s", url)
                return None
        except requests.RequestException as e:
            log.warning("retry %s: %s", url, str(e)[:120])
            if isinstance(e, requests.exceptions.ProxyError):
                if hasattr(_local, "s"):
                    del _local.s
            time.sleep(min(30, 3 * (i + 1)))
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


ZAB_GROUPS = ["accessoires", "blazer", "blusen", "hosen", "jacken", "jeans", "kleider", "maentel",
              "roecke", "schals-tuecher", "shirts", "strickwaren", "sweats", "westen"]

SITES = {
    "hailys": {"base": BASE, "cookie": ".auth/hailys_cookie.txt", "output": "output/hailys", "prefix": "hl"},
    "zabaione": {"base": "https://shop.zabaione.de", "cookie": ".auth/zabaione_cookie.txt",
                 "output": "output/zabaione", "prefix": "za"},
}


def discover_groups(home: str, prefix: str) -> list[tuple[str, str]]:
    """(slug, German label) of every group listed under a shop line in the live menu."""
    out: dict[str, str] = {}
    for slug, label in re.findall(rf'href="https://shop\.[a-z0-9.-]+{re.escape(prefix)}/([a-z0-9-]+)\.html"[^>]*>(.*?)</a>', home, re.S):
        label = _txt(label)
        if label and label != "Alle anzeigen":
            out.setdefault(slug, label)
    return list(out.items())


def category_for_group(label: str) -> str:
    return CATEGORY_MAP.get(label, "Other")


def line_prefixes() -> list[tuple[str, str]]:
    if SITE == "hailys":
        return [("/hailys", "HAILYS"), ("/hailys-teens", "TEENS"), ("/z-one", "Z-ONE")]
    return [("/zabaione", "ZABAIONE")]


def build_leaf_map(home: str) -> dict[str, str]:
    """Leaf category name (as on product pages) -> catalogue category, from the shop menu."""
    labels = {slug: label for prefix, _ in line_prefixes() for slug, label in discover_groups(home, prefix)}
    out: dict[str, str] = {}
    for line, group, label in re.findall(
            r'href="https://shop\.[a-z0-9.-]+/([a-z0-9-]+)/([a-z0-9-]+)/[a-z0-9-]+\.html"[^>]*>(.*?)</a>', home, re.S):
        if group in labels and line in {pf.strip("/") for pf, _ in line_prefixes()}:
            out.setdefault(_txt(label), category_for_group(labels[group]))
    return out


def build_queue() -> tuple[dict[str, dict], dict]:
    """Return {url: info} deduped; group listings define the category."""
    home = _get(f"{BASE}/") or ""
    sources = []
    for prefix, line in line_prefixes():
        for slug, label in discover_groups(home, prefix):
            sources.append((f"{prefix}/{slug}.html", line, label))
    log.info("Groups discovered: %d (%s)", len(sources), ", ".join(sorted({l for _, _, l in sources})))
    sources.append(("/pos.html", "POS", ""))
    extra = [("/nos.html", "NOS"), ("/new-in.html", "NEW IN"), ("/topseller.html", "TOPSELLER")]
    if SITE == "hailys":
        extra.insert(0, ("/black-friday.html", "BLACK WEEKS"))

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
            it.update(line=line, group=group, category=category_for_group(group) if group else "", tags=[])
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
    jsonl = out / f"{PREFIX}_products.jsonl"
    if args.fresh and jsonl.exists():
        jsonl.unlink()
    if _get(f"{BASE}/") is None:
        log.error("Not authenticated. Put the Cookie header value in %s", COOKIE_FILE)
        return 2

    queue, stats = build_queue()
    (out / f"{PREFIX}_queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    log.info("Unique product URLs: %d (listing hits %d, duplicates skipped %d)",
             len(queue), stats["listing_hits"], stats["dupes_skipped"])

    home = _get(f"{BASE}/") or ""
    leaf_map = build_leaf_map(home)
    log.info("Leaf category map: %d entries", len(leaf_map))

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
        if p and not p.category:
            p.category = leaf_map.get(p.attributes.get("leaf", ""), "")
        with lock:
            counter["n"] += 1
            if not p:
                failed.append(u)
                log.warning("[%d/%d] FAILED %s", counter["n"], len(todo), u)
                return
            p.season = ",".join(t for t in ("NOS", "TOPSELLER", "BLACK WEEKS") if t in info["tags"])
            append_product_jsonl(jsonl, p)
            if counter["n"] % 50 == 0:
                log.info("[%d/%d] %s", counter["n"], len(todo), p.style_name)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))

    products = load_products_jsonl(jsonl)
    uniq = finalize(products)
    log.info("Scraped %d products (%d failed, %d duplicate pages)",
             len(uniq), len(failed), len(products) - len(uniq))
    (out / f"{PREFIX}_failed.json").write_text(json.dumps(failed, indent=1))
    (out / f"{PREFIX}_stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False))
    _export(uniq, out)
    return 1 if failed else 0


def finalize(products):
    """One entry per product page; unique item codes; accessories that only carry a line name as category."""
    seen_url, uniq = set(), []
    for p in products:
        if p.url in seen_url:
            continue
        seen_url.add(p.url)
        if not p.category and p.attributes.get("leaf") in ("HAILYS", "HAILYS TEENS", "ZABAIONE"):
            p.category = "Accessories"
        uniq.append(p)
    counts: dict[str, int] = {}
    for p in uniq:
        for v in p.variants:
            counts[v.sku] = counts.get(v.sku, 0) + 1
    for p in uniq:
        if any(counts[v.sku] > 1 for v in p.variants):
            tail = re.sub(r"[^a-z0-9]+", "-", p.url.rsplit("/", 1)[-1].removesuffix(".html").lower()).strip("-")
            for v in p.variants:
                v.sku = f"{v.sku}-{tail}"
    return uniq


def _export(products, out: Path) -> None:
    excluded = [p for p in products if not p.category]
    products = [p for p in products if p.category]
    if excluded:
        with (out / f"{PREFIX}_excluded_non_fashion.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["article_no", "name", "colour", "url"])
            for p in excluded:
                w.writerow([p.style_code, p.style_name, p.color, p.url])
        log.info("Excluded %d uncategorised items (shop fittings) -> %s_excluded_non_fashion.csv", len(excluded), PREFIX)
    rows = products_to_rows(products, {"default_brand": SITE.capitalize()})
    write_catalogue_xlsx(rows, out / f"{PREFIX}_catalogue_all.xlsx")
    write_shopify_csv(rows, out / f"{PREFIX}_shopify_products_all.csv", {
        "shopify": {"status": "draft", "published": False, "price_multiplier": 1.0,
                    "markup_on_wholesale": 2.5, "track_inventory": True}})
    log.info("Exported %d variants / %d products -> %s", len(rows), len(products), out)


def cmd_export(args) -> int:
    out = Path(args.output)
    _export(finalize(load_products_jsonl(out / f"{PREFIX}_products.jsonl")), out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hl_scraper")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--site", choices=sorted(SITES), default="hailys")
    sub = ap.add_subparsers(dest="cmd")
    sc = sub.add_parser("scrape")
    sc.add_argument("--output", default=None)
    sc.add_argument("--fresh", action="store_true")
    sc.add_argument("--workers", type=int, default=5)
    ex = sub.add_parser("export")
    ex.add_argument("--output", default=None)
    a = ap.parse_args(argv)
    global BASE, COOKIE_FILE, PREFIX, SITE
    cfg = SITES[a.site]
    BASE, COOKIE_FILE, PREFIX, SITE = cfg["base"], Path(cfg["cookie"]), cfg["prefix"], a.site
    if getattr(a, "output", None) is None:
        a.output = cfg["output"]
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not a.cmd:
        ap.print_help()
        return 1
    return {"scrape": cmd_scrape, "export": cmd_export}[a.cmd](a)
