"""One Excel for the design team: a row per style/colour with the photos embedded, plus the original file names."""
import io, sys, zipfile
from collections import OrderedDict
import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

master, zpath, out = sys.argv[1], sys.argv[2], sys.argv[3]
wb = openpyxl.load_workbook(master)
ws = wb["Master Data"]
h = [c.value for c in ws[1]]
ix = {k: i for i, k in enumerate(h)}
groups = OrderedDict()
for r in ws.iter_rows(min_row=2, values_only=True):
    g = groups.setdefault((r[ix["StyleCode"]], r[ix["Color"]]), {"r": r, "sizes": []})
    g["sizes"].append(f"{r[ix['Size']]} x{r[ix['Ordered_Qty']]}")
files = {}
for r in wb["Images"].iter_rows(min_row=2, values_only=True):
    files.setdefault((r[0], r[1]), {})[r[2]] = (r[7], r[3])
zf = zipfile.ZipFile(zpath)

nb = openpyxl.Workbook()
s = nb.active
s.title = "Bundle Design"
cols = ["Style ID", "Style name", "Colour", "Category", "Material", "Sizes x qty ordered", "Pieces", "RRP EUR",
        "Bundle ID (design team fills in)", "Photo 1 (front)", "Photo 2 (back)", "Photo 3 (side)",
        "File name photo 1", "File name photo 2", "File name photo 3", "Original-resolution link photo 1", "Product link"]
s.append(cols)
W, H = 170, 245  # displayed pixels
for n, ((style, colour), g) in enumerate(groups.items(), start=2):
    r = g["r"]
    ph = files.get((style, colour), {})
    s.append([style, r[ix["StyleName"]], colour, r[ix["ItemMainCategory"]], r[ix["ProductsComposition"]], ", ".join(g["sizes"]),
              sum(int(x.split("x")[1]) for x in g["sizes"]), r[ix["Retail_Price_Euro"]], "",
              None, None, None, ph.get(1, ("",))[0], ph.get(2, ("",))[0], ph.get(3, ("",))[0], ph.get(1, ("", ""))[1], r[ix["ProductLink"]]])
    s.row_dimensions[n].height = H * 0.75
    for k in (1, 2, 3):
        if k in ph:
            im = Image.open(io.BytesIO(zf.read(ph[k][0]))).convert("RGB")
            im.thumbnail((510, 735))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=82); buf.seek(0)
            xi = XLImage(buf); xi.width, xi.height = W, H
            s.add_image(xi, f"{'JKL'[k - 1]}{n}")
fill = PatternFill("solid", fgColor="1F6B57")
for c in s[1]:
    c.font = Font(bold=True, color="FFFFFF"); c.fill = fill; c.alignment = Alignment(wrap_text=True, vertical="center")
for col, w in zip("ABCDEFGHI", [18, 22, 22, 13, 20, 26, 8, 9, 22]):
    s.column_dimensions[col].width = w
for col in "JKL":
    s.column_dimensions[col].width = 25
for col in "MNOPQ":
    s.column_dimensions[col].width = 34
for row in s.iter_rows(min_row=2, max_col=9):
    for c in row:
        c.alignment = Alignment(wrap_text=True, vertical="top")
s.freeze_panes = "D2"
nb.save(out)
print(len(groups), "rows saved", out)
