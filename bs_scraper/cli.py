"""CLI entry point for Bestseller MORE scraper."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sp_scraper.exporters import (
    append_product_jsonl, load_products_jsonl,
    products_to_rows, write_catalogue_xlsx, write_shopify_csv,
)

from .extract import extract_product_from_api
from .http_client import BestsellerHTTP, COOKIE_FILE

log = logging.getLogger("bs_scraper")


def _setup_logging(verbose: bool = False):
    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, datefmt="%H:%M:%S", stream=sys.stderr)


def _get_client(args) -> BestsellerHTTP | None:
    """Create an authenticated HTTP client from cookies."""
    client = BestsellerHTTP()
    cookie_path = Path(args.cookies) if hasattr(args, "cookies") and args.cookies else None

    if cookie_path:
        if not client.load_cookies(cookie_path):
            return None
    elif COOKIE_FILE.exists():
        client.load_cookies(COOKIE_FILE)
    else:
        # Try Playwright-exported state
        from .scraper import AUTH_STATE
        if AUTH_STATE.exists():
            data = json.loads(AUTH_STATE.read_text())
            cookies = data.get("cookies", [])
            if cookies:
                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
                client.load_cookies(COOKIE_FILE)
                log.info("Converted Playwright state to cookies")

    if not client.is_logged_in():
        log.error("Not authenticated. Provide cookies via --cookies flag.")
        log.error("How to get cookies:")
        log.error("  1. Open https://more.bestseller.com in your browser")
        log.error("  2. Open DevTools (F12) > Application > Cookies")
        log.error("  3. Export cookies as JSON, or:")
        log.error("  4. Open DevTools > Network, copy any request's Cookie header")
        log.error("  5. Save to .auth/bestseller_cookies.json as:")
        log.error('     {"cookie_name": "cookie_value", ...}')
        log.error("  Or use: python -m bs_scraper import-cookies 'cookie_string_here'")
        return None
    return client


def cmd_login(args):
    """Interactive login — opens browser for OTP."""
    from .scraper import BestsellerScraper
    with BestsellerScraper(headless=False) as bot:
        if bot.login_interactive():
            print("Login successful! Session saved.")
            return 0
        print("Login failed.")
        return 2


def cmd_import_cookies(args):
    """Import cookies from a string or file."""
    client = BestsellerHTTP()
    if args.cookie_input and Path(args.cookie_input).exists():
        if not client.load_cookies(Path(args.cookie_input)):
            return 1
    else:
        cookie_str = args.cookie_input or ""
        if not cookie_str:
            print("Paste your Cookie header value (from browser DevTools):")
            cookie_str = input().strip()
        if not cookie_str:
            print("No cookies provided.")
            return 1
        client.set_cookie_string(cookie_str)

    if client.is_logged_in():
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cookies = {c.name: c.value for c in client._session.cookies}
        COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
        print(f"Cookies saved to {COOKIE_FILE} — authenticated!")
        return 0
    else:
        print("Cookies are invalid or expired. Please try again.")
        return 2


def cmd_discover(args):
    """Save raw API data for debugging selectors."""
    client = _get_client(args)
    if not client:
        return 2
    out = Path(args.output) / "debug_bestseller"
    out.mkdir(parents=True, exist_ok=True)

    info = client.get_favourite_list(args.list_id)
    if info:
        (out / "01_list_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
        log.info("List info saved")

    page_data = client.get_favourite_list_page(args.list_id, page=0, page_size=20)
    if page_data:
        (out / "02_list_page0.json").write_text(json.dumps(page_data, indent=2, ensure_ascii=False))
        log.info("First page saved (%d bytes)", len(json.dumps(page_data)))
        keys = list(page_data.keys())
        log.info("Response keys: %s", keys)
        for k in keys:
            v = page_data[k]
            if isinstance(v, list):
                log.info("  '%s': list of %d items", k, len(v))
                if v and isinstance(v[0], dict):
                    log.info("    First item keys: %s", list(v[0].keys()))
            elif isinstance(v, dict):
                log.info("  '%s': dict with keys %s", k, list(v.keys()))
            else:
                log.info("  '%s': %s", k, repr(v)[:100])

    print(f"Debug files saved to {out}")
    return 0


def cmd_scrape(args):
    """Scrape products from a favourite list."""
    client = _get_client(args)
    if not client:
        return 2

    out_dir = Path(args.output)
    jsonl_path = out_dir / "bs_products.jsonl"

    if args.fresh and jsonl_path.exists():
        jsonl_path.unlink()
        log.info("Cleared previous data (--fresh)")

    existing = load_products_jsonl(jsonl_path)
    scraped_urls = {p.url for p in existing}
    if existing:
        log.info("Resuming: %d products already scraped", len(existing))

    log.info("Fetching favourite list %s...", args.list_id)
    items = client.get_all_favourite_products(args.list_id)
    if not items:
        log.error("No products found in favourite list")
        return 1

    log.info("Found %d items in favourite list", len(items))

    # Save raw API data for reference
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "bs_raw_items.json"
    raw_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    log.info("Raw API data saved to %s", raw_path)

    new_count = 0
    skipped = 0
    for i, item in enumerate(items, 1):
        p = extract_product_from_api(item, category=args.category or "Favourites")
        if not p or not p.style_code:
            log.warning("[%d/%d] Skipped: no style code — keys: %s",
                        i, len(items), list(item.keys())[:10] if isinstance(item, dict) else "?")
            skipped += 1
            continue
        if p.url in scraped_urls:
            continue

        append_product_jsonl(jsonl_path, p)
        scraped_urls.add(p.url)
        new_count += 1
        log.info("[%d/%d] %s | %s | %d variants",
                 i, len(items), p.style_code, p.style_name, len(p.variants))

    log.info("Scraped %d new products (%d total, %d skipped)", new_count, len(scraped_urls), skipped)

    _export(jsonl_path, out_dir, args)
    return 0


def cmd_export(args):
    """Re-export from existing JSONL."""
    out_dir = Path(args.output)
    jsonl_path = out_dir / "bs_products.jsonl"
    if not jsonl_path.exists():
        print(f"No data file found at {jsonl_path}. Run 'scrape' first.")
        return 1
    _export(jsonl_path, out_dir, args)
    return 0


def _export(jsonl_path: Path, out_dir: Path, args):
    products = load_products_jsonl(jsonl_path)
    if not products:
        log.warning("No products to export")
        return

    cfg = {"default_brand": "BESTSELLER", "default_gender": ""}
    rows = products_to_rows(products, cfg)

    xlsx_path = out_dir / "bs_catalogue_all.xlsx"
    write_catalogue_xlsx(rows, xlsx_path)

    csv_path = out_dir / "bs_shopify_products_all.csv"
    shop_cfg = {
        "shopify": {
            "status": "draft",
            "published": False,
            "price_multiplier": 1.0,
            "markup_on_wholesale": 2.5,
            "track_inventory": True,
        }
    }
    write_shopify_csv(rows, csv_path, shop_cfg)

    log.info("Exported %d variants / %d products -> %s, %s",
             len(rows), len(products), xlsx_path, csv_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bs_scraper", description="Bestseller MORE B2B Scraper")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--cookies", help="Path to cookies JSON file")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="Interactive login (opens browser)")

    p_import = sub.add_parser("import-cookies", help="Import cookies from browser")
    p_import.add_argument("cookie_input", nargs="?", default="",
                          help="Cookie string or path to JSON file")

    p_disc = sub.add_parser("discover", help="Save raw API data for debugging")
    p_disc.add_argument("--list-id", default="tbtayJwvFC", help="Favourite list ID")
    p_disc.add_argument("--output", default="output", help="Output directory")

    p_scrape = sub.add_parser("scrape", help="Scrape products from favourite list")
    p_scrape.add_argument("--list-id", default="tbtayJwvFC", help="Favourite list ID")
    p_scrape.add_argument("--category", default="", help="Category name override")
    p_scrape.add_argument("--output", default="output", help="Output directory")
    p_scrape.add_argument("--fresh", action="store_true", help="Ignore resume data")

    p_export = sub.add_parser("export", help="Re-export from existing data")
    p_export.add_argument("--output", default="output", help="Output directory")

    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    commands = {
        "login": cmd_login, "import-cookies": cmd_import_cookies,
        "discover": cmd_discover, "scrape": cmd_scrape, "export": cmd_export,
    }
    if not args.command:
        parser.print_help()
        return 1
    return commands[args.command](args)
