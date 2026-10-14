"""Configuration: config.yaml (optional) + credentials from environment / .env."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "base_url": "https://b2b.sisterspoint.com/",
    "login_url": "",            # defaults to base_url
    "output_dir": "output",
    "headless": True,
    "delay_seconds": 1.0,       # pause between page loads - be polite
    "timeout_seconds": 45,
    "max_pages_per_category": 200,
    "max_products_per_category": 0,  # 0 = unlimited
    "default_brand": "Sisters Point",
    "default_gender": "female",
    "decimal_comma": True,      # "12,95" like the reference catalogue
    # Categories to scrape. Leave empty to auto-discover from the site menu.
    # e.g. [{name: "Dresses", url: "https://b2b.sisterspoint.com/dresses"}]
    "categories": [],
    "category_include_regex": "",
    "category_exclude_regex": r"(login|logout|account|cart|basket|checkout|wishlist|contact|about|terms|privacy|cookie|faq|search|order|profile|password|register|lang|javascript:|mailto:|tel:)",
    # Only URLs matching this are treated as product pages ("" = guess from listing markup)
    "product_url_regex": "",
    "capture_json": True,       # record XHR JSON on product pages to find EAN/size/stock
    "selectors": {
        "login": {
            "username": "",
            "password": "",
            "submit": "",
            "logged_in": "",    # element only visible after login (e.g. "a[href*=logout]")
            "cookie_accept": "",
        },
        "category_links": "nav a[href], header a[href], [class*=menu] a[href], [class*=navigation] a[href]",
        "product_links": "",
        "next_page": "a[rel=next], link[rel=next], .pagination a.next, .pagination .next a, li.next a, a.next, a[aria-label*=next i], a[title*=next i]",
        "load_more": "button:has-text('Load more'), button:has-text('Show more'), button:has-text('Vis flere'), a:has-text('Load more')",
        "product": {
            # All optional - leave empty to rely on auto-detection.
            "name": "",
            "style_code": "",
            "brand": "",
            "color": "",
            "description": "",
            "composition": "",
            "care": "",
            "origin": "",
            "season": "",
            "wholesale_price": "",
            "retail_price": "",
            "images": "",
            "details": "",        # container holding the spec table
            "size_options": "",
            "variant_row": "",    # one element per size/EAN row in the order grid
            "variant_size": "",
            "variant_color": "",
            "variant_ean": "",
            "variant_sku": "",
            "variant_stock": "",
            "variant_price": "",
            "color_links": "",    # links to sibling colour pages (scraped too)
        },
    },
    "shopify": {
        "status": "draft",            # draft | active
        "published": False,
        "price_multiplier": 1.0,      # e.g. EUR -> your shop currency
        "markup_on_wholesale": 2.5,   # used when no retail/RRP price is available
        "compare_at_retail": False,
        "track_inventory": True,
        "vendor": "",                 # override Brand as Vendor
        "product_category": "",       # Shopify taxonomy, e.g. "Apparel & Accessories > Clothing"
        "tags": [],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else Path("config.yaml")
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            cfg = _merge(cfg, yaml.safe_load(fh) or {})
    elif path:
        raise FileNotFoundError(p)
    cfg["base_url"] = os.environ.get("SP_BASE_URL", cfg["base_url"])
    cfg["username"] = os.environ.get("SP_USERNAME", cfg.get("username", ""))
    cfg["password"] = os.environ.get("SP_PASSWORD", cfg.get("password", ""))
    cfg["login_url"] = cfg.get("login_url") or cfg["base_url"]
    return cfg
