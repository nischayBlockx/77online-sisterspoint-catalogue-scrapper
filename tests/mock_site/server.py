"""Tiny fake B2B shop used by the end-to-end test (login, menu, pagination,
JS-rendered listing, JSON-LD / spec table / size-grid / XHR product pages)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USER, PASSWORD = "buyer@example.com", "secret"

PRODUCTS = {
    # code: (category, name, colour, sizes-with-ean, style)
    "SP100-black": ("dresses", "Gerda Dress", "Black", [("S", "5712345000011", 12), ("M", "5712345000028", 0), ("L", "5712345000035", 4)], "grid"),
    "SP100-blue": ("dresses", "Gerda Dress", "Blue", [("S", "5712345000042", 3), ("M", "5712345000059", 7)], "grid"),
    "SP200-sand": ("dresses", "Nelly Shirt Dress", "Sand", [("XS", "5712345000066", 1), ("S", "5712345000073", 2)], "jsonld"),
    "SP300-white": ("tops", "Lia Blouse", "White", [("M", "5712345000080", 9), ("L", "5712345000097", 5)], "xhr"),
}

PAGE = """<!doctype html><html><head><title>{title}</title>{head}</head><body>
<header><nav class="main-menu">
  <a href="/">Home</a> <a href="/c/dresses">Dresses</a> <a href="/c/tops">Tops</a>
  <a href="/contact">Contact</a> <a href="/logout">Log out</a>
</nav></header>
<main>{body}</main><footer><a href="/terms">Terms</a></footer></body></html>"""


def tile(code: str) -> str:
    name = PRODUCTS[code][1]
    return (f'<div class="product-tile"><a href="/p/{code}"><img src="/img/{code}_1.jpg" alt=""></a>'
            f'<a href="/p/{code}">{name}</a></div>')


def product_page(code: str) -> tuple[str, str]:
    cat, name, color, sizes, style = PRODUCTS[code]
    style_code = code.split("-")[0]
    specs = f"""<dl class="specs">
      <dt>Style no.</dt><dd>{style_code}</dd><dt>Colour</dt><dd>{color}</dd>
      <dt>Composition</dt><dd>100% Viscose</dd><dt>Care</dt><dd>Wash 30°C</dd>
      <dt>Country of origin</dt><dd>IN</dd><dt>HS code</dt><dd>6204 4400</dd>
      <dt>Season</dt><dd>FW26</dd><dt>Weight</dt><dd>0.35 kg</dd>
      <dt>Wholesale price</dt><dd>€ 24,95</dd><dt>RRP</dt><dd>€ 59,95</dd></dl>"""
    gallery = (f'<div class="product-gallery"><img src="/img/{code}_1.jpg"><img data-src="/img/{code}_2.jpg" src="data:image/gif;base64,R0lGOD">'
               f'</div><img class="logo" src="/img/logo.svg">')
    head, body = "", f"<h1>{name}</h1>{gallery}<p class='description'>A lovely {name.lower()}.</p>"
    if style == "grid":
        rows = "".join(f'<tr><th>{s}</th><td><input type="number" data-ean="{e}" data-size="{s}" data-stock="{q}"></td></tr>'
                       for s, e, q in sizes)
        body += specs + f"<table class='order-grid'>{rows}</table>"
    elif style == "jsonld":
        ld = {
            "@context": "https://schema.org", "@type": "ProductGroup", "name": name, "productGroupID": style_code,
            "brand": {"@type": "Brand", "name": "Sisters Point"}, "description": f"Shirt dress in {color}.",
            "image": [f"/img/{code}_1.jpg"],
            "hasVariant": [{"@type": "Product", "sku": f"{style_code}-{s}", "gtin13": e, "size": s, "color": color,
                            "offers": {"@type": "Offer", "price": "29.95", "priceCurrency": "EUR", "inventoryLevel": {"value": q}}}
                           for s, e, q in sizes],
        }
        head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        body += specs.replace("€ 24,95", "€ 29,95")
    else:  # xhr: sizes rendered by JS from a JSON API
        body += specs + f"""<div id="grid">loading...</div>
        <script>fetch('/api/variants/{code}').then(r => r.json()).then(d => {{
          document.getElementById('grid').innerHTML = d.data.items.map(v => '<span class="size">' + v.sizeName + '</span>').join(' ');
        }});</script>"""
    return name, PAGE.format(title=name, head=head, body=body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return "session=ok" in (self.headers.get("Cookie") or "")

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode())
            if form.get("email", [""])[0] == USER and form.get("pass", [""])[0] == PASSWORD:
                return self._send(302, "", headers={"Location": "/", "Set-Cookie": "session=ok; Path=/"})
            return self._send(200, PAGE.format(title="Login", head="", body="<p>Wrong password</p>" + LOGIN_FORM))
        self._send(404, "nope")

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path.startswith("/img/"):
            return self._send(200, "GIF89a", "image/gif")
        if path == "/login":
            return self._send(200, PAGE.format(title="Login", head="", body=LOGIN_FORM))
        if not self._authed():
            return self._send(302, "", headers={"Location": "/login"})
        if path == "/":
            return self._send(200, PAGE.format(title="Home", head="", body="<h1>Welcome</h1>"))
        if path == "/c/dresses":
            page = int(qs.get("page", ["1"])[0])
            codes = [c for c, v in PRODUCTS.items() if v[0] == "dresses"]
            chunk = codes[:2] if page == 1 else codes[2:]
            nav = '<div class="pagination"><a rel="next" href="/c/dresses?page=2">Next</a></div>' if page == 1 else ""
            return self._send(200, PAGE.format(title="Dresses", head="", body="".join(tile(c) for c in chunk) + nav))
        if path == "/c/tops":
            # listing rendered client-side
            codes = [c for c, v in PRODUCTS.items() if v[0] == "tops"]
            script = f"<div id='list'></div><script>setTimeout(() => {{document.getElementById('list').innerHTML = {json.dumps(''.join(tile(c) for c in codes))};}}, 300);</script>"
            return self._send(200, PAGE.format(title="Tops", head="", body=script))
        if path.startswith("/api/variants/"):
            code = path.rsplit("/", 1)[1]
            items = [{"sizeName": s, "ean": e, "availableStock": q, "itemCode": f"{code}-{s}"} for s, e, q in PRODUCTS[code][3]]
            return self._send(200, json.dumps({"data": {"items": items}}), "application/json")
        if path.startswith("/p/"):
            code = path.rsplit("/", 1)[1]
            if code in PRODUCTS:
                return self._send(200, product_page(code)[1])
        self._send(404, PAGE.format(title="404", head="", body="not found"))


LOGIN_FORM = """<form method="post" action="/login" class="login">
<label>E-mail <input name="email" type="text"></label>
<label>Password <input name="pass" type="password"></label>
<button type="submit">Sign in</button></form>"""


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


if __name__ == "__main__":
    s, url = start(8765)
    print("mock site on", url)
    threading.Event().wait()
