"""Parse Hailys (Magento B2B) listing tiles and product pages."""
from __future__ import annotations

import html
import json
import re

from sp_scraper.models import Product, Variant

BASE = "https://shop.hailys-fashion.com"

# Hailys group name (German) -> catalogue category
CATEGORY_MAP = {
    "Accessoires": "Accessories",
    "Modeschmuck": "Accessories",
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
    m = re.search(r"Artikel \d+ - \d+ von (\d+)", x) or re.search(r"(\d+) (?:Artikel|Eintr)", x)
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


_STOCK_RE = re.compile(r"(?:Auf Lager|In stock):\s*</strong>\s*([\d.]+)", re.I)
_MORE_RE = re.compile(r"(?:Mehr als|More than)\s*([\d.]+)\s*Lots?", re.I)
_SOON_RE = re.compile(r"(?:Voraussichtlicher Liefertermin|Expected delivery date|Expected delivery)[:\s]*([\d/.\-]+)", re.I)


def _parse_container(kind: str, seg: str) -> dict | None:
    tbl = re.search(r'<table class="matrix">(.*?)</table>', seg, re.S)
    if not tbl or "</thead>" not in tbl.group(1):
        return None
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
    stock = _STOCK_RE.search(seg)
    more = None if stock else _MORE_RE.search(seg)
    soon = _SOON_RE.search(seg)
    return {
        "kind": kind, "head": head, "rows": rows,
        "unit": _price(unit.group(1)) if unit else None,
        "rrp": _price(rrp.group(1)) if rrp else None,
        "lot": _price(lot.group(1)) if lot else None,
        "lots_in_stock": int((stock or more).group(1).replace(".", "")) if (stock or more) else None,
        "stock_is_minimum": bool(more),
        "soon": soon.group(1) if soon else "",
    }


def _offers(page: str) -> list[dict]:
    """Every order block on the page: one per lot offer / single-item block, in page order."""
    start = page.find('<div class="matrix-groups')
    if start < 0:
        return []
    ends = [e for e in (page.find('<section class="cell xsells', start), page.find("Das könnte Sie auch interessieren", start)) if e > 0]
    area = page[start:min(ends) if ends else len(page)]
    groups = list(re.finditer(r'<div class="matrix-group (singles|lots)">', area))
    out = []
    for i, gm in enumerate(groups):
        seg = area[gm.end(): groups[i + 1].start() if i + 1 < len(groups) else len(area)]
        parts = re.split(r'<div class="matrix-container [^"]*">', seg)
        for part in (parts[1:] or [seg]):
            o = _parse_container(gm.group(1), part)
            if o:
                out.append(o)
    return out


def page_leaf_category(page: str) -> str:
    m = re.search(r'"category"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    if not m:
        return ""
    try:
        return html.unescape(json.loads('"' + m.group(1) + '"'))
    except ValueError:
        return html.unescape(m.group(1))


def _images(page: str) -> list[str]:
    seen, out = set(), []
    for size in ("980x1288", "920x1280"):
        for u in re.findall(rf"https://shop\.[a-z0-9.-]+/media/catalog/product/cache/{size}/[^\"\s<>]+\.(?:jpg|png|webp)", page):
            fn = u.rsplit("/", 1)[-1]
            if fn not in seen:
                seen.add(fn)
                out.append(u)
        if out:
            break
    return out


def item_code(art_no: str, colour: str, size: str) -> str:
    slug = lambda t: re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return "-".join(x for x in (art_no, slug(colour), slug(size)) if x)


def extract_product(page: str, url: str, tile: dict, category: str) -> Product | None:
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S)
    full = _txt(h1.group(1)) if h1 else tile.get("name", "")
    art_no = _attr(page, "art_no")
    if not full or not art_no:
        return None
    style_name, title_colour = split_name(full)
    offers = _offers(page)
    multi = len(offers) > 1
    fmt = lambda v: f"{v:.2f}".replace(".", ",")

    variants: list[Variant] = []
    notes: list[str] = []
    for n, o in enumerate(offers, 1):
        suffix = f"-o{n}" if multi else ""
        sizes = [s for s in o["head"][1:] if s and s != "\xa0"]
        unit, rrp = o["unit"], o["rrp"]
        tag = f"Offer {n}: " if multi else ""
        if o["kind"] == "singles":
            colour = o["head"][0] or title_colour
            stock = {}
            for label, tds in o["rows"]:
                if re.match(r"(verf|avail)", label, re.I):
                    for s, v in zip(sizes, tds):
                        stock[s] = int(v.replace(".", "")) if v.replace(".", "").isdigit() else None
            for s in sizes:
                variants.append(Variant(color=colour, size=s, ean="", sku=item_code(art_no, colour, s) + suffix,
                                        stock=stock.get(s), wholesale_price=unit, retail_price=rrp))
            note = f"{tag}single items, stock per size: " + ", ".join(f"{s} {stock.get(s, 0)}" for s in sizes)
        else:
            lots_in = o["lots_in_stock"]
            per_lot_total, pack_bits = 0, []
            for colour, qtys in o["rows"]:
                colour = colour or title_colour
                for s, q in zip(sizes, qtys):
                    if not q.isdigit() or int(q) == 0:
                        continue
                    q = int(q)
                    per_lot_total += q
                    pieces = lots_in * q if lots_in is not None else (0 if o["soon"] else None)
                    variants.append(Variant(color=colour, size=s, ean="", sku=item_code(art_no, colour, s) + suffix,
                                            stock=pieces, wholesale_price=unit, retail_price=rrp))
                    pack_bits.append(f"{colour} {s} x{q}")
            note = f"{tag}sold as lot of {per_lot_total} pcs ({', '.join(pack_bits)})"
            if lots_in is not None and o.get("stock_is_minimum"):
                note += f"; more than {lots_in} lots in stock (portal shows no exact number), so at least {lots_in * per_lot_total} pcs"
            elif lots_in is not None:
                note += f"; {lots_in} lots in stock = {lots_in * per_lot_total} pcs"
            if o["lot"] is not None:
                note += f"; lot price {fmt(o['lot'])} EUR"
        if unit is not None:
            note += f"; piece price {fmt(unit)} EUR"
        if o["soon"]:
            note += f"; incoming, expected delivery {o['soon']}"
        notes.append(note)

    if not variants:
        variants = [Variant(color=title_colour, size="one size", ean="", sku=item_code(art_no, title_colour, "one size"),
                            stock=0)]
        notes.append("No order block on the page: not orderable at the moment, stock 0")
    colours = list(dict.fromkeys(v.color for v in variants))
    first = offers[0] if offers else {}

    return Product(
        url=url,
        category=category,
        brand=_attr(page, "brand") or "Hailys",
        style_code=art_no,
        style_name=style_name,
        description=". ".join(notes),
        composition=_attr(page, "material"),
        care="",
        gender={"Woman": "female", "Teens": "teens"}.get(tile.get("subline", ""), tile.get("subline", "").lower()),
        color=colours[0] if len(colours) == 1 else (title_colour or colours[0]),
        wholesale_price=first.get("unit"),
        retail_price=first.get("rrp"),
        images=_images(page),
        variants=variants,
        attributes={"flags": ",".join(tile.get("flags", [])), "tile_name": full, "leaf": page_leaf_category(page)},
    )
