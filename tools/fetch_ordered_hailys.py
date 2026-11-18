"""Re-fetch, live, the Hailys product pages for the styles/colours in a parsed TAM order (tools/parse_tam_order.py)."""
import json, sys
from pathlib import Path

sys.path.insert(0, ".")
import hl_scraper.cli as C
from hl_scraper.extract import extract_product
from sp_scraper.exporters import append_product_jsonl, load_products_jsonl

C.SITE, C.PREFIX, C.BASE, C.COOKIE_FILE = "hailys", "hl", "https://shop.hailys-fashion.com", Path(".auth/hailys_cookie.txt")
order = json.load(open(sys.argv[1]))
out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
known = [json.loads(l) for l in open("output/hailys/hl_products.jsonl")]
queue = json.load(open("output/hailys/hl_queue.json"))
if C._get(C.BASE + "/") is None:
    raise SystemExit("not logged in")
leaf_map = C.build_leaf_map(C._get(C.BASE + "/") or "")
jl = out / "hl_products.jsonl"
jl.unlink(missing_ok=True)
seen, missing = set(), []
for ln in order["lines"]:
    key = (ln["style_number"], ln["colour"])
    if key in seen:
        continue
    seen.add(key)
    hit = [p for p in known if p["style_code"] == key[0] and p["color"] == key[1]]
    if not hit:
        missing.append(key); continue
    u = hit[0]["url"]
    info = queue.get(u, {"name": hit[0]["style_name"], "tags": [], "category": hit[0]["category"], "flags": []})
    page = C._get(u)
    p = extract_product(page, u, info, info.get("category", "")) if page else None
    if not p:
        missing.append(key); continue
    if not p.category:
        p.category = leaf_map.get(p.attributes.get("leaf", ""), "") or hit[0]["category"]
    p.season = ",".join(t for t in ("NOS", "TOPSELLER", "BLACK WEEKS") if t in info.get("tags", []))
    append_product_jsonl(jl, p)
print("fetched", len(seen) - len(missing), "of", len(seen), "missing", missing)
