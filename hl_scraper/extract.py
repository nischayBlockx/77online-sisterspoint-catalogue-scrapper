"""Parse Hailys (Magento B2B) listing tiles and product pages."""
from __future__ import annotations

import html
import re

from sp_scraper.models import Product, Variant

BASE = "https://shop.hailys-fashion.com"

# Hailys group name (German) -> catalogue category
CATEGORY_MAP = {
    "Accessoires": "Accessories",
    "Schals/Tücher": "Accessories",
    "Strümpfe/Strumpfhosen": "Accessories",
    "Schuhe": "Footwear",
    "Strickwaren": "Knitwear",
    "Jacken": "Outerwear",
    "Mäntel": "Outerwear",
    "Blazer": "Outerwear",
    "Westen": "Outerwear",
    "Shirts": "Tops",
    "Blusen": "Tops",
    "Hemden": "Tops",
    "Sweats": "Tops",
    "Hosen": "Bottoms",
    "Hosenanzüge": "Bottoms",
    "Jeans": "Bottoms",
    "Röcke": "Bottoms",
    "Kleider": "Dresses",
    "Sonstige": "Dresses",
}

_TILE = re.compile(r'<article[^>]*>\s*<a href="([^"]+)" class="wrapper"(.*?)</article>', re.S)


def _txt(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _price(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"(\d[\d.]*,\d{2})", s)
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def parse_listing(page: str) -> list[dict]:
    out = []
    for href, body in _TILE.findall(page):
        name = re.search(r'<p class="name">(.*?)</p>', body, re.S)
        sub = re.search(r'<p class="subline">(.*?)</p>', body, re.S)
        flags = [_txt(f) for f in re.findall(r'<span class="flag[^"]*">(.*?)</span>', body, re.S)]
        out.append({
            "url": href.split("?")[0],
            "name": _txt(name.group(1)) if name else "",
            "subline": _txt(sub.group(1)) if sub else "",
            "flags": flags,
        })
    return out


def listing_total(page: str) -> int | None:
    x = _txt(re.sub(r"<(script|style)[\s\S]*?</\1>", "", page))
    m = re.search(r"Artikel \d+ - \d+ von (\d+)", x) or re.search(r"(\d+) Artikel", x)
    return int(m.group(1)) if m else None


def _attr(page: str, cls: str) -> str:
    m = re.search(rf'<li class="{cls}">\s*<span class="lbl">.*?</span>\s*<span class="value">(.*?)</span>', page, re.S)
    return _txt(m.group(1)) if m else ""


def split_name(full: str) -> tuple[str, str]:
    """'LG MW C JN Pi44na - dblue' -> ('LG MW C JN Pi44na', 'dblue')."""
    if " - " in full:
        a, b = full.rsplit(" - ", 1)
        return a.strip(), b.strip()
    return full.strip(), ""


def _matrix_groups(page: str) -> list[dict]:
    groups = []
    for kind in ("singles", "lots"):
        for m in re.finditer(rf'<div class="matrix-group {kind}">(.*?)(?=<div class="matrix-group |</div></div><section|<section class="cell xsells)', page, re.S):
            seg = m.group(1)
            tbl = re.search(r'<table class="matrix">(.*?)</table>', seg, re.S)
            if not tbl:
                continue
            t = tbl.group(1)
            head = [_txt(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", t.split("</thead>")[0], re.S)]
            rows = []
            for r in re.findall(r"<tr>(.*?)</tr>", t.split("</thead>")[1], re.S):
                th = re.search(r"<th[^>]*>(.*?)</th>", r, re.S)
                tds = [_txt(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                rows.append((_txt(th.group(1)) if th else "", tds))
            unit = re.search(r'unit_price">.*?class="price">(.*?)</span>', seg, re.S)
            rrp = re.search(r'class="rrp">.*?class="price">(.*?)</span>', seg, re.S)
            lot = re.search(r'class="lot-price">.*?class="price">(.*?)</span>', seg, re.S)
            soon = re.search(r"Voraussichtlicher Liefertermin:\s*([\d/]+)", seg)
            groups.append({
                "kind": kind, "head": head, "rows": rows,
                "unit": _price(unit.group(1)) if unit else None,
                "rrp": _price(rrp.group(1)) if rrp else None,
                "lot": _price(lot.group(1)) if lot else None,
                "soon": soon.group(1) if soon else "",
            })
    return groups


def _images(page: str) -> list[str]:
    seen, out = set(), []
    for size in ("980x1288", "920x1280"):
        for u in re.findall(rf"https://shop\.hailys-fashion\.com/media/catalog/product/cache/{size}/[^\"\s<>]+\.(?:jpg|png|webp)", page):
            fn = u.rsplit("/", 1)[-1]
            if fn not in seen:
                seen.add(fn)
                out.append(u)
        if out:
            break
    return out


def extract_product(page: str, url: str, tile: dict, category: str) -> Product | None:
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S)
    full = _txt(h1.group(1)) if h1 else tile.get("name", "")
    art_no = _attr(page, "art_no")
    if not full or not art_no:
        return None
    style_name, colour = split_name(full)
    groups = _matrix_groups(page)

    singles = next((g for g in groups if g["kind"] == "singles"), None)
    lots = next((g for g in groups if g["kind"] == "lots"), None)
    if singles and singles["head"]:
        colour = singles["head"][0] or colour
    elif lots and lots["rows"] and lots["rows"][0][0]:
        colour = lots["rows"][0][0] or colour

    main = singles or lots
    wholesale = main["unit"] if main else None
    retail = main["rrp"] if main else None
    images = _images(page)

    stock: dict[str, int | None] = {}
    sizes: list[str] = []
    if singles:
        sizes = [s for s in singles["head"][1:] if s]
        for label, tds in singles["rows"]:
            if label.lower().startswith("verf"):
                for s, v in zip(sizes, tds):
                    stock[s] = int(v) if v.isdigit() else None
    elif lots:
        sizes = [s for s in lots["head"][1:] if s and s != "\xa0"]

    desc_bits = []
    if lots:
        pack = [f"{s} x{q}" for s, q in zip(sizes, lots["rows"][0][1])] if lots["rows"] else []
        piece_count = sum(int(q) for q in (lots["rows"][0][1] if lots["rows"] else []) if q.isdigit())
        desc_bits.append(f"Sold as lot ({piece_count} pcs): {', '.join(pack)}"
                         + (f"; lot price {lots['lot']:.2f} EUR".replace(".", ",") if lots["lot"] else ""))
    if singles and lots:
        desc_bits.append("Also available as single items")
    if main and main["soon"]:
        desc_bits.append(f"Expected delivery {main['soon']}")

    p = Product(
        url=url,
        category=category,
        brand=_attr(page, "brand") or "Hailys",
        style_code=art_no,
        style_name=style_name,
        description=". ".join(desc_bits),
        composition=_attr(page, "material"),
        care="",
        gender={"Woman": "female", "Teens": "teens"}.get(tile.get("subline", ""), tile.get("subline", "").lower()),
        color=colour,
        wholesale_price=wholesale,
        retail_price=retail,
        images=images,
        attributes={"flags": ",".join(tile.get("flags", [])), "tile_name": full},
    )
    if not sizes:
        sizes = ["one size"]
    p.variants = [
        Variant(color=colour, size=s, ean="", sku=f"{art_no}-{re.sub(r'[^A-Za-z0-9]+', '', s)}",
                stock=stock.get(s), wholesale_price=wholesale, retail_price=retail)
        for s in sizes
    ]
    return p
