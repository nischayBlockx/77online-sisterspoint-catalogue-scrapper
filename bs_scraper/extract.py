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


# Bestseller category -> catalogue category (name keywords refine ambiguous ones)
CATEGORY_MAP = {
    "Knit": "Knitwear",
    "T-Shirts & Tops": "Tops", "Shirts": "Tops", "Sweatshirts": "Tops",
    "Jeans": "Bottoms", "Trousers": "Bottoms", "Skirts": "Bottoms", "Shorts": "Bottoms", "Tailoring": "Outerwear",
    "Outerwear": "Outerwear", "Dresses": "Dresses",
    "Socks": "Accessories", "Accessories": "Accessories", "Footwear": "Footwear",
}


def map_category(cat: str, name: str) -> str:
    up = (name or "").upper()
    if cat == "Tailoring" and any(w in up for w in ("PANT", "TROUSER", "SHORT", "SKIRT")):
        return "Bottoms"
    return CATEGORY_MAP.get(cat, cat)


def _quality_fields(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _tidy(text: str) -> str:
    return re.sub(r"\s*\n\s*-\s*", "; ", re.sub(r"[\r\t]+", " ", (text or "").strip())).replace("\n", " ").strip()


def _imgs(objs) -> list[str]:
    if isinstance(objs, dict):
        objs = [objs]
    urls = [(i.get("size1800") or i.get("size450")) for i in objs or [] if isinstance(i, dict)]
    return [u for u in urls if u]


def build_product(line: dict, detail: dict | None, sizes: dict | None, category: str = "") -> Product | None:
    """Full product: descriptions from the detail call, sizes/EANs/stock from the sizes call."""
    p = extract_product_from_api(line, category=category)
    if not p:
        return None
    detail = detail or {}
    q = _quality_fields(detail.get("qualityDescription", ""))
    p.composition = q.get("composition", "")
    gender = q.get("gender", "").lower()
    p.gender = {"female": "female", "male": "male"}.get(gender, gender)
    desc = _tidy(detail.get("salesDescription", ""))
    sust = _tidy(detail.get("sustainabilityDescription", ""))
    p.description = " ".join(x for x in (desc, f"Sustainability: {sust}" if sust else "") if x)
    splashes = {s.get("type") for m in line.get("masters") or [] for s in m.get("splashes") or []} | \
               {s.get("type") for s in detail.get("splashes") or []}
    p.season = "NOS" if any(t and "NeverOutOfStock" in t for t in splashes) else ""
    if q.get("manufacture"):
        p.attributes["manufacture"] = q["manufacture"]
    if detail.get("images"):
        p.images = _imgs(detail["images"]) or p.images

    if not sizes:
        return p
    lookups = sizes.get("lookups", {})
    masters = lookups.get("masters", {})
    avail = lookups.get("availabilities", {}).get("availabilities", {})
    by_id: dict[str, list[dict]] = {}
    for src in (detail.get("masters") or [], line.get("masters") or []):
        for m in src:
            by_id.setdefault("M_" + str(m.get("id", "")).replace("M_", ""), []).append(m)
    variants: list[Variant] = []
    for mid, m in masters.items():
        srcs = by_id.get(mid, [])
        mid_num = mid.replace("M_", "")
        colour = next((x.get("name") for x in srcs if x.get("name") and x.get("name") != mid_num), "") \
            or (m.get("colour") or {}).get("name") or p.color
        imgs = next((_imgs(x.get("images")) for x in srcs if _imgs(x.get("images"))), [])
        order = list(m.get("sizes") or [])
        for grp in m.get("variantsGroupedByLength") or []:
            length = re.sub(r"[^0-9A-Za-z.]", "", grp.get("length") or "")
            by_size = grp.get("variantsBySize") or {}
            for size in order + [x for x in by_size if x not in order]:
                for ean in by_size.get(size, []):
                    # sizes missing from the availability list have no stock at all
                    stock = (avail.get(str(ean)) or {}).get("totalAmount", 0)
                    variants.append(Variant(
                        color=colour, size=f"{size}/{length}" if length else size, ean=str(ean),
                        sku=f"{p.style_code}-{mid.replace('M_', '')}-{size}{('-' + length) if length else ''}",
                        stock=int(stock) if isinstance(stock, (int, float)) else None,
                        wholesale_price=p.wholesale_price, retail_price=p.retail_price, images=imgs))
    # colours of the style that have no orderable sizes: keep a record instead of exporting size-less rows
    with_sizes = {v.sku.split("-")[1] for v in variants}
    no_sizes = []
    for mid in masters:
        num = mid.replace("M_", "")
        if num not in with_sizes:
            srcs = by_id.get(mid, [])
            nm = next((x.get("name") for x in srcs if x.get("name") and x.get("name") != num), "") \
                or (masters[mid].get("colour") or {}).get("name") or ""
            no_sizes.append(f"{nm} ({num})")
    if no_sizes:
        p.attributes["colours_without_sizes"] = "; ".join(no_sizes)
    # same colour name on different masters: make the name unique within the style
    seen: dict[str, set[str]] = {}
    for v in variants:
        seen.setdefault(v.color, set()).add(v.sku.split("-")[1])
    for v in variants:
        if len(seen[v.color]) > 1:
            v.color = f"{v.color} ({v.sku.split('-')[1]})"
    if variants:
        p.variants = variants
        p.color = variants[0].color
    return p
