"""Parse Sisters Point (Sumoshop) product pages: exact stock per size from the page's own data block."""
from __future__ import annotations

import html
import json
import re

from sp_scraper.models import Product, Variant

BASE = "https://b2b.sisterspoint.com"


def _txt(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def _price(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"(\d+)(?:\.(\d+)|\.-)?", s.replace(",", ""))
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or '0'}")


def product_links(page: str) -> list[str]:
    out: list[str] = []
    for h in re.findall(r'href="(?:https://b2b\.sisterspoint\.com)?(/vare/[^"#?]+)"', page):
        if h not in out:
            out.append(h)
    return out


def _by_id(page: str, id_: str) -> str:
    m = re.search(rf'<[a-z0-9]+ id="{re.escape(id_)}"[^>]*>(.*?)</(?:div|span|h1|h2)>', page, re.S)
    return _txt(m.group(1)) if m else ""


def _items(page: str) -> tuple[dict, list[list[str]]]:
    """Variant data block: {pid: {params:[colour,size], availability, arriving, arrivingdate}}."""
    m = re.search(r'"params":\s*(\[\[.*?\]\]),\s*"items":\s*(\{.*?\}\s*\}),\s*"pids"', page, re.S)
    if not m:
        return {}, []
    try:
        return json.loads(m.group(2)), json.loads(m.group(1).replace("'", '"'))
    except ValueError:
        return {}, []


def norm_size(s: str) -> str:
    s = (s or "").strip()
    if not s or s.lower() == "one size":
        return "one size"
    return re.sub(r"(?i)^str\.?\s*(\d)$", r"Str\1", s)


def parse_assortment(label: str) -> list[tuple[str, int]] | None:
    """'XS:1 - S:2', '(S:2 - M:4)', 'S/2 - M/3', 'XS/S 4 - M/L 4', 'ONE SIZE 6', '(Str1: 2 / Str2: 4)' -> [(size, qty)]; None for a single size."""
    raw = (label or "").strip()
    if not raw:
        return None
    paren = raw.startswith("(") and raw.endswith(")")
    inner = raw[1:-1] if paren else raw
    toks = [t for t in re.split(r"\s*-\s*", inner) if t]
    if len(toks) == 1 and " / " in inner:
        toks = re.split(r"\s+/\s+", inner)
    pairs = []
    for t in toks:
        m = re.fullmatch(r"(.+?)\s*[:/]\s*(\d+)", t) or re.fullmatch(r"(.+?)\s+(\d+)", t)
        if not m:
            return None
        pairs.append((norm_size(m.group(1)), int(m.group(2))))
    if len(pairs) == 1 and not (paren or re.search(r"[:/]", inner) or re.match(r"(?i)one size\s+\d+$", inner)):
        return None
    return pairs


def extract_product(page: str, url: str) -> Product | None:
    name = _by_id(page, "commodity-show-title")
    if not name:
        return None
    offer = _price(_by_id(page, "commodity-show-offer"))
    original = _price(_by_id(page, "commodity-show-original"))
    price = offer if offer is not None else _price(_by_id(page, "commodity-show-price"))
    rrp = _price(_by_id(page, "commodity-show-retailprice"))
    info = {k.strip().lower(): _txt(v) for k, v in re.findall(r'<dt id="[^"]*">(.*?)</dt>\s*<dd id="[^"]*">(.*?)</dd>', page, re.S)}
    code = info.get("externalcode", "")
    description = _by_id(page, "commodity-show-description")
    cat_m = re.search(r'item_category:\s*"([^"]*)"', page)
    item_category = html.unescape(cat_m.group(1)) if cat_m else ""
    images = []
    for u in re.findall(r'href="(https://b2b\.sisterspoint\.com/shared/[^"]+_900x1300c\.\w+)"', page):
        if u not in images:
            images.append(u)

    items, _ = _items(page)
    rows = []  # (colour, size label, availability, arriving, arriving date)
    for pid, it in items.items():
        params = it.get("params", [])
        colour, size = (params[0], params[1]) if len(params) >= 2 else (info.get("color", ""), params[0] if params else "")
        rows.append((colour.strip(), size.strip(), int(it.get("availability") or 0),
                     int(it.get("arriving") or 0), it.get("arrivingdate", "")))

    colour_default = info.get("color", "")
    assort = [r for r in rows if parse_assortment(r[1])]
    singles = [r for r in rows if not parse_assortment(r[1])]
    multi = bool(assort) and bool(singles)
    variants: list[Variant] = []
    notes: list[str] = []
    incoming: list[str] = []
    ratio_sizes: list[str] = []
    n = 0

    def code_for(colour: str, size: str, suffix: str) -> str:
        return "-".join(x for x in (code or name, _slug(colour or colour_default), _slug(size)) if x) + suffix

    for colour, label, sets, arr, arr_date in assort:
        n += 1
        suffix = f"-o{n}" if multi else ""
        pairs = parse_assortment(label) or []
        pack = sum(q for _, q in pairs)
        ratio_sizes = [s for s, _ in pairs]
        for s, q in pairs:
            variants.append(Variant(color=colour or colour_default, size=s, ean="", sku=code_for(colour, s, suffix),
                                    stock=sets * q, wholesale_price=price, retail_price=rrp))
        tag = f"Offer {n}: " if multi else ""
        notes.append(f"{tag}assortment {' '.join(f'{s}:{q}' for s, q in pairs)} ({pack} pcs); {sets} assortments in stock = {sets * pack} pcs")
        if arr:
            incoming.append(f"assortments {arr} ({arr_date})")

    if singles:
        n += 1
        suffix = f"-o{n}" if multi else ""
        tag = f"Offer {n}: " if multi else ""
        by_colour: dict[str, dict[str, int]] = {}
        for colour, size, avail, arr, arr_date in singles:
            by_colour.setdefault(colour or colour_default, {})[norm_size(size)] = avail
            if arr:
                incoming.append(f"{colour} {size} {arr} ({arr_date})")
        sold_out = []
        for colour, sz in by_colour.items():
            wanted = list(sz) + [s for s in ratio_sizes if s not in sz]
            for s in wanted:
                st = sz.get(s, 0)
                if s not in sz:
                    sold_out.append(s)
                variants.append(Variant(color=colour, size=s, ean="", sku=code_for(colour, s, suffix),
                                        stock=st, wholesale_price=price, retail_price=rrp))
        txt = f"{tag}single sizes, stock per size: " + ", ".join(
            f"{s} {v}" for sz in by_colour.values() for s, v in sz.items())
        if sold_out:
            txt += f"; sold out: {', '.join(dict.fromkeys(sold_out))}"
        notes.append(txt)

    if offer is not None and original is not None:
        notes.insert(0, f"Offer price EUR {offer:.2f} (was EUR {original:.2f})")
    if incoming:
        notes.append("Incoming: " + "; ".join(incoming))
    if not variants:
        variants = [Variant(color=colour_default, size="one size", ean="", sku=code_for(colour_default, "one size", ""),
                            stock=0, wholesale_price=price, retail_price=rrp)]
        notes.append("No orderable variants on the page: stock 0")

    colours = list(dict.fromkeys(v.color for v in variants))
    care = next((v for k, v in info.items() if re.search(r"wash|care", k)), "")
    return Product(
        url=url,
        brand="Sisters Point",
        style_code=code,
        style_name=name,
        description=" | ".join(x for x in (description, "; ".join(notes)) if x),
        composition=info.get("material", ""),
        care=care,
        gender="female",
        color=colours[0] if colours else colour_default,
        wholesale_price=price,
        retail_price=rrp,
        images=images,
        variants=variants,
        attributes={"item_category": item_category, "availability": _by_id(page, "commodity-show-availability"),
                    **{k: v for k, v in info.items() if k not in ("color", "externalcode", "material")}},
    )
