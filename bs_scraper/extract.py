"""Extract product data from Bestseller API responses into catalogue format."""
from __future__ import annotations

import re
from typing import Any

from sp_scraper.models import Product, Variant

BASE = "https://more.bestseller.com"


def extract_product_from_api(item: dict, category: str = "") -> Product | None:
    """Convert a Bestseller favourite list line item to a Product.

    Handles the actual API format: each line has id, styleNumber, name, brand,
    pricingDetails, primaryColour, masters (colour variants), etc.
    """
    if not item:
        return None

    style_code = item.get("styleNumber") or item.get("id", "").replace("F_", "") or ""
    style_name = item.get("name") or ""
    brand = item.get("brand") or ""

    pricing = item.get("pricingDetails") or {}
    wholesale_price = pricing.get("price") or pricing.get("originalPrice")
    retail_price = pricing.get("recommendedRetailPrice")

    primary_colour = item.get("primaryColour") or {}
    color = primary_colour.get("name") or ""

    cat_obj = item.get("category") or {}
    cat = category or cat_obj.get("name") or ""

    images = _extract_images_from_line(item)

    url = item.get("url") or ""
    if url and not url.startswith("http"):
        url = f"{BASE}{url}"

    p = Product(
        url=url,
        category=cat,
        brand=brand,
        style_code=style_code,
        style_name=style_name,
        description="",
        composition="",
        care="",
        origin="",
        tariff="",
        weight="",
        weight_unit="",
        season="",
        gender="",
        color=color,
        wholesale_price=wholesale_price,
        retail_price=retail_price,
        images=images,
    )

    variants = _extract_variants_from_masters(item, p)
    if variants:
        p.variants = variants
    else:
        p.variants = [Variant(
            color=color,
            size="one size",
            ean="",
            sku=style_code,
            stock=None,
            wholesale_price=wholesale_price,
            retail_price=retail_price,
        )]

    return p


def _extract_images_from_line(item: dict) -> list[str]:
    images = []
    primary = item.get("primaryImage") or {}
    for key in ("size1800", "size450", "jpegSize450"):
        url = primary.get(key)
        if url and url not in images:
            images.append(url)
            break

    for master in item.get("masters") or []:
        for img_obj in master.get("images") or []:
            url = img_obj.get("size1800") or img_obj.get("size450") or ""
            if url and url not in images:
                images.append(url)
    return images


def _extract_variants_from_masters(item: dict, p: Product) -> list[Variant]:
    """Each master is a colour variant."""
    variants = []
    masters = item.get("masters") or []
    if not masters:
        return variants

    pricing = item.get("pricingDetails") or {}
    wp = pricing.get("price") or pricing.get("originalPrice")
    rp = pricing.get("recommendedRetailPrice")

    for master in masters:
        mid = master.get("id", "").replace("M_", "")
        colour_obj = master.get("colour") or {}
        colour = colour_obj.get("name") or p.color

        master_images = []
        for img_obj in master.get("images") or []:
            url = img_obj.get("size1800") or img_obj.get("size450") or ""
            if url and url not in master_images:
                master_images.append(url)

        variants.append(Variant(
            color=colour,
            size="one size",
            ean="",
            sku=f"{p.style_code}-{mid}" if mid else p.style_code,
            stock=None,
            wholesale_price=wp,
            retail_price=rp,
            images=master_images,
        ))

    return variants
