"""Build master data (Excel + Shopify CSV + original-resolution images) for a parsed TAM order.

Usage: python3 tools/build_master_order.py order.json hailys_products.jsonl out_dir care.json
"""
import csv, io, json, re, sys, zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

sys.path.insert(0, ".")
import sp_scraper.exporters as X
from sp_scraper.models import Product, Variant

order = json.load(open(sys.argv[1]))
prods = [json.loads(l) for l in open(sys.argv[2])]
out = Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
care_ids = json.load(open(sys.argv[4]))

CARE = {"1": ("Maschinenwäsche 30 °C, Schonwaschgang", "Machine wash 30 °C, gentle cycle"),
        "9": ("Handwäsche", "Hand wash"),
        "17": ("Nicht waschen", "Do not wash"),
        "18": ("Nicht im Trockner trocknen", "Do not tumble dry"),
        "23": ("Maschinenwäsche 30 °C", "Machine wash 30 °C")}
order_cat = {"Strickwaren": "Knitwear", "Strickwaren NOS": "Knitwear", "Cut & Sew": "Tops", "Taschen": "Accessories",
             "Strümpfe": "Accessories", "Jacken": "Outerwear", "Kopfbedeckungen": "Accessories", "Sweats": "Tops",
             "Shirts casual": "Tops"}
by = {(p["style_code"], p["color"]): p for p in prods}
s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
m = re.search(r"local proxy at (http://127\.0\.0\.1:\d+)", Path("/root/.ccr/README.md").read_text())
if m:
    s.proxies = {"https": m.group(1), "http": m.group(1)}


def hires(u: str) -> str:
    return re.sub(r"/cache/[^/]+/", "/", u)


def ean_ok(e: str) -> bool:
    d = [int(x) for x in e]
    return len(d) == 13 and (10 - sum(d[i] * (3 if i % 2 else 1) for i in range(12)) % 10) % 10 == d[12]


# ---- images: original files
img_info = OrderedDict()
for p in prods:
    for u in p["images"]:
        img_info[hires(u)] = None


def grab(u):
    for _ in range(3):
        try:
            r = s.get(u, timeout=60)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                im = Image.open(io.BytesIO(r.content))
                return u, r.content, im.size
        except Exception:
            pass
    return u, None, None


with ThreadPoolExecutor(8) as ex:
    for u, data, size in ex.map(grab, list(img_info)):
        img_info[u] = (data, size)
img_dir = out / "images"
img_dir.mkdir(exist_ok=True)
fname = {}
for p in prods:
    for i, u in enumerate(p["images"], 1):
        fname[hires(u)] = re.sub(r"[^A-Za-z0-9]+", "-", f"{p['style_code']}_{p['color']}").strip("-") + f"_{i}.jpg"
zf = zipfile.ZipFile(out / "Hailys_Order-A261118-000053_Images-Original-Resolution.zip", "w", zipfile.ZIP_STORED)
for u, (data, size) in img_info.items():
    if data:
        zf.writestr(fname[u], data)
zf.close()

# ---- one row per ordered colour x size
lines = OrderedDict()
for ln in order["lines"]:
    lines.setdefault((ln["style_number"], ln["colour"]), []).append(ln)
rows, check = [], []
for (style, colour), lns in lines.items():
    p = by[(style, colour)]
    hi = [hires(u) for u in p["images"]]
    care = [CARE.get(i, (f"symbol {i}", f"symbol {i}")) for i in dict.fromkeys(care_ids.get(p["url"], []))]
    care_txt = "; ".join(f"{de} / {en}" for de, en in care)
    ean = re.search(r"/(\d{13})_", p["images"][0]).group(1) if p["images"] else ""
    site_stock = {v["size"]: v["stock"] for v in p["variants"]}
    for ln in lns:
        for size, qty in ln["sizes"].items():
            code = f"{style}-{re.sub(r'[^a-z0-9]+', '-', colour.lower()).strip('-')}-{re.sub(r'[^a-z0-9]+', '-', size.lower()).strip('-')}"
            row = {c: "" for c in X.CATALOGUE_COLUMNS}
            row.update({
                "Brand": "Hailys", "ItemMainCategory": p["category"], "ItemCode": code,
                "ItemDescription": f"{p['style_name']} {colour} {size}", "Color": colour, "Size": size,
                "StyleCode": style, "StyleName": p["style_name"],
                "Wholesale_Price_Euro": X.fmt_decimal(ln["price_per_item"]), "Retail_Price_Euro": X.fmt_decimal(ln["rrp"]),
                "Season": "NOS" if "NOS" in ln["order_category"] else (p["season"] or ""), "Gender": p["gender"] or "female",
                "ProductsComposition": p["composition"], "CareInstruction": care_txt,
                "ProductText": f"{p['style_name']} - {colour}. Material: {p['composition']}." + (f" Care: {care_txt}." if care_txt else ""),
                "ImageProductP1": hi[0] if len(hi) > 0 else "", "ImageProductP2": hi[1] if len(hi) > 1 else "",
                "ImageProductP30": hi[2] if len(hi) > 2 else "",
                "CreationDate": order["order_date"], "Available Stock": qty, "ProductLink": p["url"],
                "Original_Wholesale_Price_Euro": "", "Discount_Percent": "",
                "On_Offer": "Yes (supplier Sale flag, original price not published)" if "Sale" in str(p["attributes"].get("flags", "")) else "No",
            })
            rows.append({**row, "_extra": OrderedDict([
                ("Order_Number", order["order_number"]), ("Order_Date", order["order_date"]), ("Lot_Number", ln["lot_no"]),
                ("Delivery", ln["lot_delivery"]), ("Order_Category_Supplier", ln["order_category"]),
                ("Ordered_Qty", qty), ("Order_Net_Price_Item_EUR", ln["price_per_item"]), ("RRP_EUR", ln["rrp"]),
                ("Order_Line_Value_EUR", round(qty * ln["price_per_item"], 2)),
                ("Site_Price_Now_EUR", p["wholesale_price"]), ("Site_Stock_Pieces_Now", site_stock.get(size, "")),
                ("EAN_StyleColour_from_photo_name", ean if ean and ean_ok(ean) else ""),
                ("ImageHighRes_1", hi[0] if len(hi) > 0 else ""), ("ImageHighRes_2", hi[1] if len(hi) > 1 else ""),
                ("ImageHighRes_3", hi[2] if len(hi) > 2 else "")])})
    check.append((style, colour, sum(sum(l["sizes"].values()) for l in lns), p["wholesale_price"], lns[0]["price_per_item"]))

cols = X.CATALOGUE_COLUMNS
extra_cols = list(rows[0]["_extra"])
wb = Workbook(); ws = wb.active; ws.title = "Master Data"
ws.append(extra_cols[:5] + cols + extra_cols[5:])
for r in rows:
    e = r["_extra"]
    ws.append([e[k] for k in extra_cols[:5]] + [r[c] for c in cols] + [e[k] for k in extra_cols[5:]])
wo = wb.create_sheet("Order Lines (as on PDF)")
wo.append(["Order", "Style number", "Style name", "Colour", "Category (supplier)", "Lot no.", "Sizes ordered", "Pieces", "Net price / item EUR", "RRP EUR", "Line value EUR"])
for l in order["lines"]:
    wo.append([order["order_number"], l["style_number"], l["style_name"], l["colour"], l["order_category"], l["lot_no"],
               ", ".join(f"{k} x{v}" for k, v in l["sizes"].items()), l["pieces"], l["price_per_item"], l["rrp"], l["colour_total"]])
wi = wb.create_sheet("Images")
wi.append(["Style", "Colour", "Photo", "Original URL", "Width px", "Height px", "File size KB", "File in zip"])
for p in prods:
    for i, u in enumerate(p["images"], 1):
        h = hires(u); data, size = img_info[h]
        wi.append([p["style_code"], p["color"], i, h, size[0] if size else "", size[1] if size else "", round(len(data) / 1024) if data else "", fname[h]])
wr = wb.create_sheet("Checks")
tot_pcs = sum(sum(l["sizes"].values()) for l in order["lines"])
wr.append(["Check", "Result"])
wr.append(["Pieces on PDF", order["total_pieces"]]); wr.append(["Pieces in master rows", sum(r["_extra"]["Ordered_Qty"] for r in rows)])
wr.append(["Order value (sum of lines) EUR", round(sum(l["colour_total"] for l in order["lines"]), 2)])
wr.append(["Order value (rows) EUR", round(sum(r["_extra"]["Order_Line_Value_EUR"] for r in rows), 2)])
wr.append(["Styles / colours", f"{len({c[0] for c in check})} / {len(check)}"])
wr.append(["Site price differs from order price", ", ".join(f"{a} {b}: site {d}, order {e}" for a, b, c, d, e in check if abs(float(d) - float(e)) > 0.005) or "none"])
wr.append(["Photos downloaded at original resolution", f"{sum(1 for v in img_info.values() if v[0])} of {len(img_info)}"])
wr.append(["Smallest photo (px)", "%d x %d" % min((v[1] for v in img_info.values() if v[0]), key=lambda z: z[0] * z[1])])
wr.append(["Notes", "EAN per size is not published by Hailys; EAN_StyleColour comes from the photo file name (valid EAN-13). Available Stock = ordered quantity (incoming). Site has no marketing text; ProductText is built from the site's material and care data."])
fill = PatternFill("solid", fgColor="1F6B57")
for sh in wb:
    for c in sh[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = fill
    sh.freeze_panes = "A2"
    for col in sh.columns:
        sh.column_dimensions[col[0].column_letter].width = 22
wb.save(out / "Hailys_Order-A261118-000053_Master-Data.xlsx")

# ---- Shopify CSV (draft, ordered quantity as inventory)
cfg = {"shopify": {"status": "draft", "published": False, "price_multiplier": 1.0, "markup_on_wholesale": 2.5, "track_inventory": True}}
srows = X.rows_to_shopify([{k: v for k, v in r.items() if k != "_extra"} for r in rows], cfg)
with (out / "Hailys_Order-A261118-000053_Shopify-Import.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=X.SHOPIFY_COLUMNS); w.writeheader(); w.writerows(srows)
print("rows", len(rows), "pieces", sum(r["_extra"]["Ordered_Qty"] for r in rows), "of", order["total_pieces"],
      "images", sum(1 for v in img_info.values() if v[0]), "/", len(img_info), "shopify products", sum(1 for r in srows if r["Title"]))
