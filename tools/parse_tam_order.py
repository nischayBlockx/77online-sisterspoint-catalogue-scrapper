"""Parse a TAM Fashion (Hailys / Zabaione) order confirmation PDF into one record per ordered size.

Usage: python3 tools/parse_tam_order.py order.pdf out.json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys


def _eur(s: str) -> float:
    return float(s.replace("€", "").replace(".", "").replace(",", "."))


def parse(pdf: str) -> dict:
    text = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True, check=True).stdout
    order = {"order_number": "", "order_date": "", "customer_no": "", "lines": [], "total_pieces": None}
    m = re.search(r"Ordernumber\s+([A-Z]\d+ \d+)", text)
    order["order_number"] = m.group(1) if m else ""
    m = re.search(r"Orderdate\s+([\d.]+)", text)
    order["order_date"] = m.group(1) if m else ""
    m = re.search(r"Customer No\.\s+([\d.]+)", text)
    order["customer_no"] = m.group(1) if m else ""
    m = re.search(r"Gesamt/Total\s+St.ck/Pieces:\s+(\d+)", text)
    order["total_pieces"] = int(m.group(1)) if m else None

    blk: dict | None = None
    sizes: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if "Marke/Brand:" in line:
            blk = {"brand": re.search(r"Brand:\s+(\S+)", line).group(1), "lot_delivery": re.search(r"Delivery:\s+(.+?)\s*$", line).group(1).strip()}
            sizes = []
            continue
        if blk is None:
            continue
        m = re.search(r"Style number:\s+(\S+)\s+(\d{6,})\s+UVP/RRP per Item:\s+([\d.,]+)€", line)
        if m:
            blk.update(style_number=m.group(1), lot_no=m.group(2), rrp=_eur(m.group(3)))
            continue
        m = re.search(r"Style name:\s+(.+?)\s{2,}(\d+)\s+Einzelpreis/Price per Item:\s+([\d.,]+)€", line)
        if m:
            blk.update(style_name=m.group(1).strip(), lot_multiplier=int(m.group(2)), price_per_item=_eur(m.group(3)))
            continue
        m = re.search(r"Category:\s+(.+?)\s{2,}Gesamtpreis/Total Price:\s+([\d.,]+)€", line)
        if m:
            blk.update(order_category=m.group(1).strip(), line_total=_eur(m.group(2)))
            continue
        if line.lstrip().startswith("Farbe/Color"):
            body = line.split("Farbe/Color", 1)[1].replace("Total", "")
            toks = body.split()
            sizes = ["one size" if toks[i:i + 2] == ["one", "size"] else toks[i] for i in range(len(toks)) if toks[i] != "size"]
            sizes = [s for s in sizes if s != "ne"] if "one size" not in sizes else sizes
            if not sizes:  # pdftotext sometimes clips "one size" to "ne size"
                sizes = ["one size"]
            continue
        if sizes and re.search(r"€\s*$", line) and not line.lstrip().startswith(("Gesamt", "Zwischen")) and "Total Price" not in line:
            toks = line.split()
            price = _eur(toks.pop())
            total = int(toks.pop())
            qtys = [int(t) for t in toks[-len(sizes):]]
            colour = " ".join(toks[:-len(sizes)])
            order["lines"].append({**{k: v for k, v in blk.items()}, "colour": colour,
                                   "sizes": dict(zip(sizes, qtys)), "pieces": total, "colour_total": price})
    return order


if __name__ == "__main__":
    out = parse(sys.argv[1])
    json.dump(out, open(sys.argv[2], "w"), indent=1, ensure_ascii=False)
    n = sum(l["pieces"] for l in out["lines"])
    print(out["order_number"], out["order_date"], "lines", len(out["lines"]), "pieces", n, "stated", out["total_pieces"],
          "value", round(sum(l["colour_total"] for l in out["lines"]), 2))
