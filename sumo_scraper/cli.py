"""Sisters Point B2B (Sumoshop) scraper: login over HTTP, crawl lists, read stock per size, export."""
from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from sp_scraper.exporters import (
    append_product_jsonl, load_products_jsonl, products_to_rows,
    write_catalogue_xlsx, write_shopify_csv,
)

from .extract import BASE, extract_product, product_links

log = logging.getLogger("sumo_scraper")

# category lists in priority order -> catalogue category
CATEGORY_LISTS = [
    ("/kategori/tops", "Tops"), ("/kategori/shirts-blouses", "Tops"), ("/kategori/t-shirts", "Tops"),
    ("/kategori/knit", "Knitwear"), ("/kategori/sweatshirts", "Tops"),
    ("/kategori/pants-jeans", "Bottoms"), ("/kategori/skirts", "Bottoms"), ("/kategori/shorts", "Bottoms"),
    ("/kategori/outerwear", "Outerwear"), ("/kategori/dress", "Dresses"), ("/kategori/jumpsuit", "Dresses"),
    ("/kategori/underwear", "Other"), ("/kategori/accessories", "Accessories"),
]
TAG_LISTS = [("/noos-styles", "NOS"), ("/fokusgruppe/bestsellers", "TOPSELLER"),
             ("/great-offers-discount", "GREAT OFFERS"), ("/fokusgruppe/party", "PARTY"), ("/nyheder", "")]

_lock = threading.Lock()
_session: requests.Session | None = None
_login_at = 0.0


def _proxy_url() -> str | None:
    try:
        m = re.search(r"local proxy at (http://127\.0\.0\.1:\d+)", Path("/root/.ccr/README.md").read_text())
        if m:
            return m.group(1)
    except OSError:
        pass
    return None


def _env() -> dict:
    return dict(l.strip().split("=", 1) for l in Path(".env").read_text().splitlines() if "=" in l)


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    proxy = _proxy_url()
    if proxy:
        s.proxies = {"https": proxy, "http": proxy}
    return s


def login() -> bool:
    global _session, _login_at
    env = _env()
    s = _new_session()
    t = s.get(f"{BASE}/login", timeout=40).text
    fid = re.search(r'name="formid"[^>]*value="([^"]*)"', t) or re.search(r'value="([^"]*)"[^>]*name="formid"', t)
    action = re.search(r'<form action="([^"]+)"', t)
    if not action:
        return False
    r = s.post(action.group(1).replace("&amp;", "&"), timeout=40, data={
        "formid": fid.group(1) if fid else "login", "email": env["SP_USERNAME"], "password": env["SP_PASSWORD"]})
    ok = 'id="login-form"' not in r.text
    if ok:
        _session, _login_at = s, time.time()
    return ok


def get(url: str, tries: int = 6) -> str | None:
    global _session
    for i in range(tries):
        try:
            if _session is None:
                with _lock:
                    if _session is None and not login():
                        log.error("Login failed")
                        return None
            r = _session.get(url, timeout=40)
            if 'id="login-form"' in r.text:
                with _lock:
                    if time.time() - _login_at > 20:
                        log.info("Session expired, logging in again")
                        login()
                continue
            if r.status_code == 200:
                return r.text
        except requests.RequestException as e:
            log.warning("retry %s: %s", url, str(e)[:100])
            if isinstance(e, requests.exceptions.ProxyError):
                proxy = _proxy_url()
                if proxy and _session is not None:
                    _session.proxies = {"https": proxy, "http": proxy}
            time.sleep(min(30, 3 * (i + 1)))
    return None


def crawl_list(path: str, maxp: int = 80) -> list[str]:
    out: list[str] = []
    for p in range(1, maxp):
        t = get(f"{BASE}{path}?p={p}")
        new = [x for x in product_links(t or "") if x not in out]
        if not new:
            break
        out += new
    return out


def build_queue() -> dict[str, dict]:
    queue: dict[str, dict] = {}
    for path, cat in CATEGORY_LISTS:
        items = crawl_list(path)
        for u in items:
            q = queue.setdefault(u, {"lists": [], "list_category": "", "tags": []})
            q["lists"].append(path)
            q["list_category"] = q["list_category"] or cat
        log.info("%-28s %4d (queue %d)", path, len(items), len(queue))
    for path, tag in TAG_LISTS:
        items = crawl_list(path)
        for u in items:
            q = queue.setdefault(u, {"lists": [], "list_category": "", "tags": []})
            q["lists"].append(path)
            if tag and tag not in q["tags"]:
                q["tags"].append(tag)
        log.info("%-28s %4d (queue %d)", path, len(items), len(queue))
    return queue


def finalize(products: list, queue: dict) -> list:
    """One product per page; category from the page's own category, falling back to the list it was found in."""
    uniq, seen = [], set()
    for p in products:
        if p.url not in seen:
            seen.add(p.url)
            uniq.append(p)
    # learn: page category name -> catalogue category, from products found in category lists
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in uniq:
        q = queue.get(p.url.replace(BASE, ""), {})
        if q.get("list_category"):
            votes[p.attributes.get("item_category", "")][q["list_category"]] += 1
    learned = {k: c.most_common(1)[0][0] for k, c in votes.items() if k}
    for p in uniq:
        q = queue.get(p.url.replace(BASE, ""), {})
        p.category = learned.get(p.attributes.get("item_category", "")) or q.get("list_category") or ""
        p.season = ",".join(q.get("tags", []))
    # colour names for pages that don't show one: same colour code (suffix of the style-colour code) elsewhere
    names: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in uniq:
        if p.color and "-" in p.style_code:
            names[p.style_code.rsplit("-", 1)[1]][p.color] += 1
    for p in uniq:
        if not p.color and "-" in p.style_code:
            code = p.style_code.rsplit("-", 1)[1]
            nm = names[code].most_common(1)[0][0] if names.get(code) else f"colour {code}"
            p.color = nm
            for v in p.variants:
                v.color = v.color or nm
    for p in uniq:
        cat = p.attributes.get("item_category", "")
        if not p.category:
            p.category = {"* Dress *": "Dresses", "Gloves": "Accessories", "Blazer": "Outerwear"}.get(cat, "")
    # one category per style (all its colours)
    by_style: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in uniq:
        by_style[p.style_code.rsplit("-", 1)[0]][p.category] += 1
    for p in uniq:
        p.category = by_style[p.style_code.rsplit("-", 1)[0]].most_common(1)[0][0]
    counts = collections.Counter(v.sku for p in uniq for v in p.variants)
    for p in uniq:
        if any(counts[v.sku] > 1 for v in p.variants):
            tail = p.url.rsplit("/", 1)[-1].split("-", 1)[0]
            for v in p.variants:
                v.sku = f"{v.sku}-{tail}"
    return uniq


def _export(products: list, out: Path) -> None:
    rows = products_to_rows(products, {"default_brand": "Sisters Point", "default_gender": "female"})
    write_catalogue_xlsx(rows, out / "sp_catalogue_all.xlsx")
    write_shopify_csv(rows, out / "sp_shopify_products_all.csv", {
        "shopify": {"status": "draft", "published": False, "price_multiplier": 1.0,
                    "markup_on_wholesale": 2.5, "track_inventory": True}})
    log.info("Exported %d rows / %d products -> %s", len(rows), len(products), out)


def cmd_scrape(args) -> int:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "sp_products.jsonl"
    if args.fresh and jsonl.exists():
        jsonl.unlink()
    if not login():
        log.error("Login failed: check SP_USERNAME / SP_PASSWORD in .env")
        return 2
    queue = build_queue()
    (out / "sp_queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    log.info("Unique products: %d", len(queue))
    done = {p.url.replace(BASE, "") for p in load_products_jsonl(jsonl)}
    todo = [u for u in queue if u not in done]
    log.info("%d to fetch (%d already done)", len(todo), len(done))
    wlock, failed, n = threading.Lock(), [], {"i": 0}

    def work(u: str):
        page = get(BASE + u)
        p = extract_product(page, BASE + u) if page else None
        with wlock:
            n["i"] += 1
            if not p:
                failed.append(u)
                log.warning("[%d/%d] FAILED %s", n["i"], len(todo), u)
                return
            append_product_jsonl(jsonl, p)
            if n["i"] % 50 == 0:
                log.info("[%d/%d] %s", n["i"], len(todo), p.style_name)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))
    (out / "sp_failed.json").write_text(json.dumps(failed))
    products = finalize(load_products_jsonl(jsonl), queue)
    log.info("Scraped %d products (%d failed)", len(products), len(failed))
    if len(products) >= len(queue) - len(failed):
        _export(products, out)
    return 1 if failed else 0


def cmd_export(args) -> int:
    out = Path(args.output)
    queue = json.loads((out / "sp_queue.json").read_text())
    _export(finalize(load_products_jsonl(out / "sp_products.jsonl"), queue), out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sumo_scraper")
    sub = ap.add_subparsers(dest="cmd")
    sc = sub.add_parser("scrape")
    sc.add_argument("--output", default="output/sistersPoint")
    sc.add_argument("--fresh", action="store_true")
    sc.add_argument("--workers", type=int, default=5)
    ex = sub.add_parser("export")
    ex.add_argument("--output", default="output/sistersPoint")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not a.cmd:
        ap.print_help()
        return 1
    return {"scrape": cmd_scrape, "export": cmd_export}[a.cmd](a)
