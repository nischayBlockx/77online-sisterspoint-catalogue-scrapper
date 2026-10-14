"""Turn a rendered product page into a :class:`Product`.

Every field is looked up from several sources, best first:

1. CSS selectors from ``config.yaml`` (``selectors.product``) - exact control
   once you know the site's markup.
2. schema.org JSON-LD (``Product`` / ``ProductGroup`` with offers/variants).
3. Spec tables / definition lists / "Label: value" lines, matched by label
   keywords in English and Danish.
4. OpenGraph meta tags and generic fallbacks (h1, gallery images, ...).
5. Variant data found in JSON the page loaded over XHR (B2B order grids are
   often JavaScript apps that fetch sizes/EANs/stock as JSON).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import Product, Variant

# Label keywords (lower case, matched as substrings) for spec tables.
FIELD_KEYWORDS: dict[str, list[str]] = {
    "retail_price": ["rrp", "retail", "recommended", "vejl", "udsalgspris", "srp", "msrp", "verkaufspreis", "uvp"],
    "wholesale_price": ["wholesale", "net price", "your price", "indkøbspris", "engros", "purchase price", "cost price", "netto", "ek-preis"],
    "composition": ["composition", "material", "materiale", "sammensætning", "fabric", "kvalitet", "quality"],
    "care": ["care", "washing", "vask", "pleje"],
    "origin": ["country of origin", "origin", "made in", "oprindelsesland", "produktionsland", "produceret i"],
    "tariff": ["hs code", "hs-code", "tariff", "customs", "toldnummer", "toldkode", "taric", "commodity"],
    "weight": ["weight", "vægt", "gewicht"],
    "season": ["season", "sæson", "kollektion", "collection", "drop"],
    "gender": ["gender", "køn"],
    "ean": ["ean", "gtin", "barcode", "stregkode"],
    "style_code": ["style no", "style number", "style", "article no", "article number", "art. no", "art.no", "item no", "item number", "varenr", "varenummer", "model no", "product no", "sku"],
    "color": ["colour", "color", "farve"],
    "brand": ["brand", "mærke", "marke"],
    "category": ["category", "kategori", "product type", "produkttype"],
    "description": ["description", "beskrivelse"],
}

PRICE_RE = re.compile(r"(\d[\d.,  ]*)")
EAN_RE = re.compile(r"\b(\d{8}|\d{12,14})\b")


# --------------------------------------------------------------------------- helpers
def clean(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def parse_price(text: Any) -> float | None:
    """'€ 1.234,50' / '1,234.50 EUR' / '12,95' / 12.95 -> float."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = PRICE_RE.search(str(text).replace("\xa0", " "))
    if not m:
        return None
    s = re.sub(r"[\s ]", "", m.group(1)).rstrip(".,")
    if "," in s and "." in s:
        # whichever separator comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    elif s.count(".") == 1 and len(s.rpartition(".")[2]) == 3:
        s = s.replace(".", "")  # thousands separator, e.g. "1.234"
    try:
        return float(s)
    except ValueError:
        return None


def field_for_label(label: str) -> str | None:
    label = clean(label).lower().rstrip(":").strip()
    if not label or len(label) > 40:
        return None
    for fld, words in FIELD_KEYWORDS.items():
        for w in words:
            if w in label:
                return fld
    return None


def select_text(soup: BeautifulSoup | Tag, selector: str | None) -> str:
    if not selector:
        return ""
    el = soup.select_one(selector)
    if el is None:
        return ""
    if el.name == "meta":
        return clean(el.get("content"))
    if el.name in ("input", "select") and el.get("value"):
        return clean(el.get("value"))
    return clean(el.get_text(" "))


def image_url(el: Tag, base_url: str) -> str:
    for attr in ("data-zoom-image", "data-large", "data-full", "data-src", "data-original", "data-lazy", "src", "href", "content"):
        v = el.get(attr)
        if v and not str(v).startswith("data:"):
            return urljoin(base_url, str(v).strip())
    srcset = el.get("data-srcset") or el.get("srcset")
    if srcset:
        # take the largest candidate (the last one)
        return urljoin(base_url, srcset.split(",")[-1].strip().split(" ")[0])
    return ""


def dedupe(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


# --------------------------------------------------------------------------- JSON-LD
def _jsonld_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                nodes.append(item)
                if "@graph" in item:
                    stack.append(item["@graph"])
    return nodes


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {t} if isinstance(t, str) else set(t or [])


def _name(v: Any) -> str:
    if isinstance(v, dict):
        return clean(v.get("name"))
    if isinstance(v, list):
        return ", ".join(_name(x) for x in v if _name(x))
    return clean(v)


def _jsonld_images(v: Any, base: str) -> list[str]:
    if isinstance(v, str):
        return [urljoin(base, v)]
    if isinstance(v, dict):
        return _jsonld_images(v.get("url") or v.get("contentUrl"), base)
    if isinstance(v, list):
        return [u for x in v for u in _jsonld_images(x, base)]
    return []


def _offer_list(offers: Any) -> list[dict]:
    if isinstance(offers, dict):
        if "offers" in offers and isinstance(offers["offers"], list):
            return offers["offers"]
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _ean(node: dict) -> str:
    for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "ean"):
        if node.get(k):
            return clean(node[k])
    return ""


def _stock(offer: dict) -> int | None:
    for k in ("inventoryLevel", "stock", "quantity"):
        v = offer.get(k)
        if isinstance(v, dict):
            v = v.get("value")
        if v not in (None, ""):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    avail = clean(offer.get("availability")).lower()
    if avail.endswith("outofstock") or avail.endswith("soldout"):
        return 0
    return None


def apply_jsonld(soup: BeautifulSoup, p: Product, base: str) -> None:
    nodes = _jsonld_nodes(soup)
    group = next((n for n in nodes if "ProductGroup" in _types(n)), None)
    prod = group or next((n for n in nodes if "Product" in _types(n)), None)
    if not prod:
        return
    p.style_name = p.style_name or clean(prod.get("name"))
    p.description = p.description or clean(BeautifulSoup(clean(prod.get("description")), "lxml").get_text(" "))
    p.brand = p.brand or _name(prod.get("brand"))
    p.style_code = p.style_code or clean(prod.get("productGroupID") or prod.get("mpn") or prod.get("sku") or prod.get("productID"))
    p.color = p.color or clean(prod.get("color"))
    p.composition = p.composition or _name(prod.get("material"))
    p.category = p.category or _name(prod.get("category"))
    p.gender = p.gender or _name(prod.get("audience", {}).get("suggestedGender") if isinstance(prod.get("audience"), dict) else "")
    if not p.weight and isinstance(prod.get("weight"), dict):
        p.weight = clean(prod["weight"].get("value"))
        p.weight_unit = clean(prod["weight"].get("unitCode") or prod["weight"].get("unitText")).lower()
    p.images = dedupe(p.images + _jsonld_images(prod.get("image"), base))

    variant_nodes = prod.get("hasVariant") or []
    if isinstance(variant_nodes, dict):
        variant_nodes = [variant_nodes]
    if not variant_nodes:
        # Single product - each offer may still be a size.
        offers = _offer_list(prod.get("offers"))
        if offers and p.wholesale_price is None:
            p.wholesale_price = parse_price(offers[0].get("price") or offers[0].get("lowPrice"))
        if len(offers) > 1 or _ean(prod) or any(_ean(o) for o in offers):
            for o in offers or [{}]:
                offered = o.get("itemOffered") if isinstance(o.get("itemOffered"), dict) else {}
                p.variants.append(Variant(
                    color=clean(o.get("color") or prod.get("color")),
                    size=_name(o.get("size") or offered.get("size")),
                    ean=_ean(o) or _ean(prod),
                    sku=clean(o.get("sku") or prod.get("sku")),
                    stock=_stock(o),
                    wholesale_price=parse_price(o.get("price")),
                ))
        return
    for v in variant_nodes:
        if not isinstance(v, dict):
            continue
        offer = (_offer_list(v.get("offers")) or [{}])[0]
        p.variants.append(Variant(
            color=clean(v.get("color")),
            size=_name(v.get("size")),
            ean=_ean(v) or _ean(offer),
            sku=clean(v.get("sku")),
            stock=_stock(offer),
            wholesale_price=parse_price(offer.get("price")),
            images=_jsonld_images(v.get("image"), base),
        ))


# --------------------------------------------------------------------------- spec tables
def label_value_pairs(soup: BeautifulSoup | Tag) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append((clean(dt.get_text(" ")), clean(dd.get_text(" "))))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            pairs.append((clean(cells[0].get_text(" ")), clean(cells[1].get_text(" "))))
    for el in soup.find_all(["li", "p", "div", "span"]):
        if el.find(["li", "p", "div", "table", "dl"]):
            continue
        text = clean(el.get_text(" "))
        if ":" in text and len(text) < 300:
            label, _, value = text.partition(":")
            if value.strip() and len(label) <= 40:
                pairs.append((label, value.strip()))
    return pairs


def apply_spec_pairs(soup: BeautifulSoup | Tag, p: Product) -> None:
    for label, value in label_value_pairs(soup):
        fld = field_for_label(label)
        if not value:
            continue
        if fld is None:
            p.attributes.setdefault(clean(label).rstrip(":"), value)
            continue
        if fld in ("retail_price", "wholesale_price"):
            if getattr(p, fld) is None:
                setattr(p, fld, parse_price(value))
        elif fld == "ean":
            continue  # EANs belong to variants
        elif fld == "weight":
            if not p.weight:
                m = re.search(r"([\d.,]+)\s*([a-zA-Z]*)", value)
                if m:
                    p.weight, p.weight_unit = m.group(1), (m.group(2) or p.weight_unit).lower()
        elif not getattr(p, fld):
            setattr(p, fld, value)


# --------------------------------------------------------------------------- variants from markup
VARIANT_ATTRS = {
    "ean": ["data-ean", "data-gtin", "data-barcode"],
    "sku": ["data-sku", "data-item-code", "data-itemcode", "data-variant-sku"],
    "size": ["data-size", "data-size-name", "data-option-size"],
    "color": ["data-color", "data-colour", "data-color-name"],
    "stock": ["data-stock", "data-qty-available", "data-available", "data-inventory"],
    "price": ["data-price", "data-wholesale-price"],
}


def _attr(el: Tag, names: list[str]) -> str:
    for n in names:
        if el.get(n) not in (None, ""):
            return clean(el.get(n))
    return ""


def _to_int(v: str) -> int | None:
    m = re.search(r"-?\d+", v or "")
    return int(m.group()) if m else None


def variants_from_markup(soup: BeautifulSoup, sel: dict) -> list[Variant]:
    out: list[Variant] = []
    if sel.get("variant_row"):
        for row in soup.select(sel["variant_row"]):
            out.append(Variant(
                size=select_text(row, sel.get("variant_size")) or _attr(row, VARIANT_ATTRS["size"]),
                color=select_text(row, sel.get("variant_color")) or _attr(row, VARIANT_ATTRS["color"]),
                ean=select_text(row, sel.get("variant_ean")) or _attr(row, VARIANT_ATTRS["ean"]),
                sku=select_text(row, sel.get("variant_sku")) or _attr(row, VARIANT_ATTRS["sku"]),
                stock=_to_int(select_text(row, sel.get("variant_stock")) or _attr(row, VARIANT_ATTRS["stock"])),
                wholesale_price=parse_price(select_text(row, sel.get("variant_price")) or _attr(row, VARIANT_ATTRS["price"]) or None),
            ))
        return [v for v in out if v.size or v.ean or v.sku]

    # Generic: any element carrying an EAN/GTIN data attribute (size-grid inputs etc.)
    attr_names = VARIANT_ATTRS["ean"] + VARIANT_ATTRS["sku"]
    for el in soup.find_all(lambda t: any(t.has_attr(a) for a in attr_names)):
        v = Variant(
            ean=_attr(el, VARIANT_ATTRS["ean"]),
            sku=_attr(el, VARIANT_ATTRS["sku"]),
            size=_attr(el, VARIANT_ATTRS["size"]),
            color=_attr(el, VARIANT_ATTRS["color"]),
            stock=_to_int(_attr(el, VARIANT_ATTRS["stock"])),
            wholesale_price=parse_price(_attr(el, VARIANT_ATTRS["price"]) or None),
        )
        if not v.size:
            label = el.find_previous(["label", "th"]) if el.name == "input" else None
            v.size = clean(label.get_text(" ")) if label else clean(el.get_text(" "))[:20]
        out.append(v)
    if out:
        return out

    # Size buttons / dropdown (no EAN known).
    size_sel = sel.get("size_options") or "select[name*=size i] option, [class*=size] button, [class*=size] li, [data-size]"
    sizes = []
    for el in soup.select(size_sel):
        text = clean(el.get("data-size") or el.get_text(" "))
        if text and len(text) <= 15 and not re.search(r"choose|select|vælg|size$|størrelse", text, re.I):
            sizes.append(text)
    return [Variant(size=s) for s in dedupe(sizes)]


# --------------------------------------------------------------------------- variants from XHR JSON
_JSON_KEYS = {
    "ean": {"ean", "gtin", "gtin13", "barcode", "eancode", "ean13"},
    "size": {"size", "sizename", "size_name", "sizecode", "size_code", "sizelabel"},
    "color": {"color", "colour", "colorname", "color_name", "colourname"},
    "sku": {"sku", "itemcode", "item_code", "itemno", "variantsku", "articlenumber", "variantid"},
    "stock": {"stock", "available", "availablestock", "available_stock", "inventory", "qty", "quantity", "stockquantity", "instock"},
    "price": {"price", "wholesaleprice", "wholesale_price", "netprice", "net_price"},
    "retail": {"rrp", "retailprice", "retail_price", "recommendedprice", "srp", "msrp"},
}


def _pick(d: dict, kind: str) -> Any:
    for k, v in d.items():
        if k.lower() in _JSON_KEYS[kind] and not isinstance(v, (dict, list)):
            return v
        if k.lower() in _JSON_KEYS[kind] and isinstance(v, dict) and "name" in v:
            return v["name"]
    return None


def variants_from_json(payloads: list[Any]) -> list[Variant]:
    found: dict[str, Variant] = {}
    stack = list(payloads)
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            ean = _pick(item, "ean")
            if ean and EAN_RE.fullmatch(clean(ean)):
                v = Variant(
                    ean=clean(ean),
                    size=clean(_pick(item, "size")),
                    color=clean(_pick(item, "color")),
                    sku=clean(_pick(item, "sku")),
                    stock=_to_int(clean(_pick(item, "stock"))) if _pick(item, "stock") is not True else None,
                    wholesale_price=parse_price(_pick(item, "price")),
                    retail_price=parse_price(_pick(item, "retail")),
                )
                found.setdefault(v.ean, v)
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return list(found.values())


# --------------------------------------------------------------------------- Sumoshop (b2b.sisterspoint.com)
_SUMO_RE = re.compile(
    r'sumoSelector\(\s*\{(.*?)\}\s*\)',
    re.DOTALL,
)
_SUMO_ITEMS_RE = re.compile(
    r'"items"\s*:\s*\{(.*?)\}\s*,\s*"pids"',
    re.DOTALL,
)
_SUMO_ITEM_RE = re.compile(
    r'"(\d+)"\s*:\s*\{[^}]*"params"\s*:\s*\[([^\]]*)\][^}]*\}',
)


def _parse_sumo_sizes(size_str: str) -> list[str]:
    """Parse ' XS:1 - S:2 - M:2 - L:2 - XL:1' into ['XS','S','M','L','XL']."""
    sizes = []
    for part in size_str.split("-"):
        part = part.strip()
        if ":" in part:
            sizes.append(part.split(":")[0].strip())
        elif "/" in part:
            sizes.append(part.split("/")[0].strip())
    return sizes


def apply_sumoshop(soup: BeautifulSoup, p: Product, url: str) -> None:
    """Extract data from the Sumoshop B2B platform used by sisterspoint.com."""
    # Product title from h1
    h1 = soup.select_one("#commodity-show-title, h1.headline")
    if h1:
        p.style_name = p.style_name or clean(h1.get_text(" "))

    # Spec table: <dt id="color-dt">color</dt><dd id="color-dd">D. Chocolate</dd>
    for dt in soup.select("dt[id]"):
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        key = (dt.get("id") or "").replace("-dt", "").lower()
        val = clean(dd.get_text(" "))
        if not val:
            continue
        if key == "color" or key == "farve":
            p.color = p.color or val
        elif key == "externalcode":
            p.style_code = p.style_code or val
        elif key == "material" or key == "materiale":
            p.composition = p.composition or val
        elif key == "description" or key == "beskrivelse":
            p.description = p.description or val
        elif key == "season" or key == "sæson":
            p.season = p.season or val
        elif key == "weight" or key == "vægt":
            if not p.weight:
                m = re.search(r"([\d.,]+)\s*([a-zA-Z]*)", val)
                if m:
                    p.weight, p.weight_unit = m.group(1), (m.group(2) or "kg").lower()

    # Prices from the product page (not from related products)
    show = soup.select_one("#commodity-show")
    if show:
        price_el = show.select_one(".price")
        if price_el and p.wholesale_price is None:
            p.wholesale_price = parse_price(price_el.get_text(" "))
        retail_el = show.select_one(".retailprice")
        if retail_el and p.retail_price is None:
            p.retail_price = parse_price(retail_el.get_text(" "))

    # High-res images from gallery
    gallery_imgs = soup.select("#commodity-show-images a.popup[href]")
    if gallery_imgs:
        seen = set()
        imgs = []
        for a in gallery_imgs:
            href = a.get("href", "")
            if href and href not in seen and not href.endswith(".svg"):
                seen.add(href)
                imgs.append(urljoin(url, href))
        p.images = imgs or p.images

    # sumoSelector JS: variant data (color, sizes, availability, PIDs)
    html_text = str(soup)
    m = _SUMO_RE.search(html_text)
    if not m:
        return
    blob = m.group(1)

    # Extract items
    items_m = _SUMO_ITEMS_RE.search(blob)
    if not items_m:
        return
    items_str = items_m.group(1)

    for pid_m in _SUMO_ITEM_RE.finditer(items_str):
        pid = pid_m.group(1)
        params_str = pid_m.group(2)
        # params is like: "D. Chocolate", " XS:1 - S:2 - M:2 - L:2 - XL:1"
        params = [s.strip().strip('"').strip("'") for s in params_str.split(",", 1)]
        if len(params) < 2:
            continue
        color = params[0].strip()
        size_str = params[1].strip().strip('"').strip("'")
        sizes = _parse_sumo_sizes(size_str)
        if not sizes:
            continue
        for size in sizes:
            p.variants.append(Variant(
                color=color,
                size=size,
                sku=f"{p.style_code}-{pid}" if p.style_code else pid,
            ))


# --------------------------------------------------------------------------- main entry
def extract_product(html: str, url: str, selectors: dict | None = None,
                    json_payloads: list[Any] | None = None, category: str = "") -> Product:
    sel = selectors or {}
    soup = BeautifulSoup(html, "lxml")
    p = Product(url=url, category=category)

    # 1. explicit selectors
    p.style_name = select_text(soup, sel.get("name"))
    p.style_code = select_text(soup, sel.get("style_code"))
    p.brand = select_text(soup, sel.get("brand"))
    p.color = select_text(soup, sel.get("color"))
    p.description = select_text(soup, sel.get("description"))
    p.composition = select_text(soup, sel.get("composition"))
    p.care = select_text(soup, sel.get("care"))
    p.origin = select_text(soup, sel.get("origin"))
    p.season = select_text(soup, sel.get("season"))
    p.wholesale_price = parse_price(select_text(soup, sel.get("wholesale_price")) or None)
    p.retail_price = parse_price(select_text(soup, sel.get("retail_price")) or None)
    if sel.get("images"):
        p.images = dedupe(image_url(el, url) for el in soup.select(sel["images"]))

    # strip "Label:" prefixes picked up by explicit selectors
    for fld in ("style_code", "brand", "color", "composition", "care", "origin", "season"):
        val = getattr(p, fld)
        if ":" in val and field_for_label(val.split(":", 1)[0]):
            setattr(p, fld, val.split(":", 1)[1].strip())

    # 2-3. structured data, Sumoshop, and spec tables
    apply_jsonld(soup, p, url)
    apply_sumoshop(soup, p, url)
    spec_root = soup.select_one(sel["details"]) if sel.get("details") else None
    apply_spec_pairs(spec_root or soup.body or soup, p)

    # 4. generic fallbacks
    if not p.style_name:
        h1 = soup.find("h1")
        p.style_name = clean(h1.get_text(" ")) if h1 else clean((soup.find("meta", property="og:title") or {}).get("content"))
    if not p.description:
        meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        p.description = clean(meta.get("content")) if meta else ""
    if not p.images:
        gallery = soup.select("[class*=gallery] img, [class*=product-image] img, [class*=slider] img, [class*=carousel] img")
        p.images = dedupe(image_url(el, url) for el in gallery)
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            p.images = dedupe(p.images + [urljoin(url, og["content"])])
    p.images = [i for i in p.images if not re.search(r"(logo|icon|placeholder|sprite|\.svg)", i, re.I)]
    if p.wholesale_price is None:
        el = soup.select_one("[itemprop=price], .price, [class*=price]")
        if el:
            p.wholesale_price = parse_price(el.get("content") or el.get_text(" "))

    # 5. variants
    if not p.variants:
        p.variants = variants_from_markup(soup, sel)
    json_variants = variants_from_json(json_payloads or [])
    if json_variants and not any(v.ean for v in p.variants):
        by_size = {v.size.lower(): v for v in p.variants if v.size}
        merged = []
        for jv in json_variants:
            mv = by_size.get(jv.size.lower())
            if mv and mv.stock is not None and jv.stock is None:
                jv.stock = mv.stock
            merged.append(jv)
        p.variants = merged
    if not p.variants:
        p.variants = [Variant(size="one size")]
    for v in p.variants:
        v.color = v.color or p.color
        v.size = v.size or "one size"
    return p
