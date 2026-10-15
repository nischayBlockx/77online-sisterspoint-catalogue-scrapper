"""Extract product data from Bestseller API responses into catalogue format."""
from __future__ import annotations

import re
from typing import Any

from sp_scraper.models import Product, Variant


def extract_product_from_api(item: dict, category: str = "") -> Product | None:
    """Convert a Bestseller API product/family object to a Product."""
    if not item:
        return None

    style_code = (
        item.get("styleNumber") or item.get("styleOptionNumber")
        or item.get("articleNumber") or item.get("productNumber") or ""
    )
    style_name = (
        item.get("styleName") or item.get("name")
        or item.get("productName") or item.get("familyName") or ""
    )
    brand = (
        item.get("brandName") or item.get("brand")
        or item.get("presentationBrand") or ""
    )
    color = item.get("colourName") or item.get("colour") or item.get("color") or ""
    description = item.get("description") or item.get("productDescription") or ""
    composition = item.get("composition") or item.get("material") or ""
    care = item.get("washingInstructions") or item.get("careInstructions") or ""
    origin = item.get("countryOfOrigin") or item.get("origin") or ""
    season = item.get("season") or item.get("seasonName") or ""
    gender = item.get("gender") or item.get("targetGroup") or ""
    tariff = item.get("hsCode") or item.get("customsTariffNumber") or ""
    weight = ""
    weight_unit = ""

    wholesale_price = _parse_price(item.get("wholesalePrice") or item.get("unitNetPrice") or item.get("price"))
    retail_price = _parse_price(item.get("retailPrice") or item.get("recommendedRetailPrice") or item.get("rrp"))

    images = _extract_images(item)

    cat = category or item.get("categoryName") or item.get("productCategory") or ""

    p = Product(
        url=_product_url(item),
        category=cat,
        brand=brand,
        style_code=style_code,
        style_name=style_name,
        description=description,
        composition=composition,
        care=care,
        origin=origin,
        tariff=re.sub(r"\s", "", str(tariff)),
        weight=weight,
        weight_unit=weight_unit,
        season=season,
        gender=gender,
        color=color,
        wholesale_price=wholesale_price,
        retail_price=retail_price,
        images=images,
    )

    # Extract variants (sizes/colors)
    variants = _extract_variants(item, p)
    if variants:
        p.variants = variants
    else:
        p.variants = [Variant(
            color=color,
            size=item.get("size") or "one size",
            ean=str(item.get("ean") or item.get("gtin") or item.get("barcode") or ""),
            sku=style_code,
            stock=item.get("stock") or item.get("availableQuantity"),
            wholesale_price=wholesale_price,
            retail_price=retail_price,
        )]

    return p


def _parse_price(val) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", ".").strip()
    m = re.search(r"(\d+\.?\d*)", s)
    return float(m.group(1)) if m else None


def _product_url(item: dict) -> str:
    slug = item.get("slug") or item.get("productSlug") or ""
    fid = item.get("familyId") or item.get("id") or item.get("productId") or ""
    if slug:
        return f"https://more.bestseller.com/en/product/{slug}"
    if fid:
        return f"https://more.bestseller.com/en/product/{fid}"
    return ""


def _extract_images(item: dict) -> list[str]:
    images = []
    for key in ("imageUrl", "primaryImageUrl", "image", "thumbnail"):
        url = item.get(key)
        if url and url not in images:
            images.append(url)

    for img_list_key in ("images", "imageUrls", "media", "productImages"):
        img_list = item.get(img_list_key)
        if isinstance(img_list, list):
            for img in img_list:
                url = img if isinstance(img, str) else (img.get("url") or img.get("imageUrl") or "")
                if url and url not in images:
                    images.append(url)

    # Ensure full URLs
    return [u if u.startswith("http") else f"https://more.bestseller.com{u}" for u in images]


def _extract_variants(item: dict, p: Product) -> list[Variant]:
    variants = []

    # Check for nested variants/sizes/styles arrays
    for key in ("variants", "sizes", "styles", "styleOptions", "skus", "items"):
        var_list = item.get(key)
        if not isinstance(var_list, list) or not var_list:
            continue

        for v in var_list:
            if not isinstance(v, dict):
                continue
            color = v.get("colourName") or v.get("colour") or v.get("color") or p.color
            size = v.get("sizeName") or v.get("size") or v.get("sizeCode") or "one size"
            ean = str(v.get("ean") or v.get("gtin") or v.get("barcode") or "")
            sku = v.get("sku") or v.get("styleOptionNumber") or v.get("articleNumber") or ""
            stock = v.get("stock") or v.get("availableQuantity") or v.get("quantity")
            wp = _parse_price(v.get("wholesalePrice") or v.get("unitNetPrice") or v.get("price"))
            rp = _parse_price(v.get("retailPrice") or v.get("recommendedRetailPrice") or v.get("rrp"))

            # Extract variant images
            v_images = _extract_images(v) if any(v.get(k) for k in ("imageUrl", "images", "media")) else []

            variants.append(Variant(
                color=color,
                size=size,
                ean=ean,
                sku=sku or f"{p.style_code}-{size}",
                stock=stock,
                wholesale_price=wp,
                retail_price=rp,
                images=v_images,
            ))
        if variants:
            return variants

    return variants
