"""Command line entry point: ``python -m sp_scraper <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import OrderedDict
from pathlib import Path

from .config import load_config
from .exporters import (
    load_products_jsonl,
    products_to_rows,
    read_catalogue_xlsx,
    slugify,
    write_catalogue_xlsx,
    write_shopify_csv,
)


def export(cfg: dict, jsonl: Path | None = None) -> None:
    out = Path(cfg["output_dir"])
    products = load_products_jsonl(jsonl or out / "products.jsonl")
    if not products:
        print(f"No scraped products found in {jsonl or out / 'products.jsonl'}")
        return
    rows = products_to_rows(products, cfg)
    write_catalogue_xlsx(rows, out / "catalogue_all.xlsx")
    n = write_shopify_csv(rows, out / "shopify_products_all.csv", cfg)
    print(f"All categories: {len(rows)} variants / {n} products -> {out}/catalogue_all.xlsx, {out}/shopify_products_all.csv")

    by_cat: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        by_cat.setdefault(r["ItemMainCategory"] or "uncategorised", []).append(r)
    for cat, cat_rows in by_cat.items():
        slug = slugify(cat) or "uncategorised"
        write_catalogue_xlsx(cat_rows, out / "by_category" / f"catalogue_{slug}.xlsx")
        n = write_shopify_csv(cat_rows, out / "by_category" / f"shopify_{slug}.csv", cfg)
        print(f"  {cat}: {len(cat_rows)} variants / {n} products")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sp_scraper", description="Sisters Point B2B catalogue scraper -> Excel + Shopify CSV")
    ap.add_argument("-c", "--config", help="config file (default: config.yaml if present)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="test the login and save the session")
    sub.add_parser("categories", help="list the categories the bot can see")
    sub.add_parser("discover", help="log in and save HTML + screenshots of each page type to output/debug/")
    sc = sub.add_parser("scrape", help="scrape products and export Excel + Shopify CSV")
    sc.add_argument("--category", action="append", help="only categories whose name/URL contains this (repeatable)")
    sc.add_argument("--limit", type=int, default=0, help="max products per category (for test runs)")
    sc.add_argument("--fresh", action="store_true", help="ignore previous results instead of resuming")
    sub.add_parser("export", help="re-build Excel/CSV from output/products.jsonl")
    cv = sub.add_parser("convert", help="convert a catalogue .xlsx in the reference format to a Shopify CSV")
    cv.add_argument("xlsx")
    cv.add_argument("-o", "--out", help="output CSV path")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if args.headed:
        cfg["headless"] = False

    if args.cmd == "export":
        export(cfg)
        return 0
    if args.cmd == "convert":
        rows = read_catalogue_xlsx(Path(args.xlsx))
        out = Path(args.out or Path(cfg["output_dir"]) / (Path(args.xlsx).stem + "_shopify.csv"))
        n = write_shopify_csv(rows, out, cfg)
        print(f"{len(rows)} variants / {n} products -> {out}")
        return 0

    from .scraper import LoginError, SistersPointScraper

    try:
        with SistersPointScraper(cfg) as bot:
            if args.cmd == "login":
                bot.login()
                print("Login OK - session saved to .auth/state.json")
            elif args.cmd == "categories":
                bot.login()
                for c in bot.discover_categories():
                    print(f"- name: \"{c['name']}\"\n  url: \"{c['url']}\"")
            elif args.cmd == "discover":
                report = bot.discover()
                print(json.dumps({k: v for k, v in report.items() if k != "product_sample"}, indent=2, ensure_ascii=False))
                print(f"\nFull report + screenshots: {bot.debug_dir}")
            elif args.cmd == "scrape":
                jsonl = Path(cfg["output_dir"]) / "products.jsonl"
                if args.fresh and jsonl.exists():
                    jsonl.unlink()
                bot.run(only=args.category, limit=args.limit)
    except LoginError as e:
        print(f"LOGIN FAILED: {e}", file=sys.stderr)
        return 2
    if args.cmd == "scrape":
        export(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
