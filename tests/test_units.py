import pytest

from sp_scraper.exporters import rows_to_shopify
from sp_scraper.extract import extract_product, parse_price, variants_from_json


@pytest.mark.parametrize("text,value", [
    ("€ 12,95", 12.95), ("1.234,50 EUR", 1234.5), ("1,234.50", 1234.5), ("DKK 499", 499.0),
    ("12.95", 12.95), ("1.234", 1234.0), (7, 7.0), ("n/a", None), (None, None),
])
def test_parse_price(text, value):
    assert parse_price(text) == value


def test_danish_spec_labels_and_size_buttons():
    html = """<h1>Alma Kjole</h1><table>
      <tr><th>Varenummer</th><td>SP900</td></tr><tr><th>Farve</th><td>Rosa</td></tr>
      <tr><th>Materiale</th><td>100% Bomuld</td></tr><tr><th>Vejl. udsalgspris</th><td>399,95 kr.</td></tr></table>
      <div class="sizes"><button>XS</button><button>S</button></div>"""
    p = extract_product(html, "https://x/p", category="Kjoler")
    assert (p.style_code, p.color, p.composition, p.retail_price) == ("SP900", "Rosa", "100% Bomuld", 399.95)
    assert [(v.size, v.color) for v in p.variants] == [("XS", "Rosa"), ("S", "Rosa")]


def test_configured_selectors_win():
    html = "<h1>Wrong</h1><div class='t'>Right name</div><div class='rows'><div class='r' data-x='1'><b>M</b><i>5700000000017</i><u>3 pcs</u></div></div>"
    sel = {"name": ".t", "variant_row": ".r", "variant_size": "b", "variant_ean": "i", "variant_stock": "u"}
    p = extract_product(html, "https://x/p", sel)
    assert p.style_name == "Right name"
    assert [(v.size, v.ean, v.stock) for v in p.variants] == [("M", "5700000000017", 3)]


def test_variants_from_nested_json():
    payload = {"product": {"skus": [{"EAN": "5700000000017", "size": {"name": "S"}, "stock": 4}, {"ean": "bad"}]}}
    vs = variants_from_json([payload])
    assert [(v.ean, v.size, v.stock) for v in vs] == [("5700000000017", "S", 4)]


def test_shopify_grouping_single_size_and_markup():
    rows = [
        {"StyleCode": "A1", "StyleName": "Cap", "Color": "red", "Size": "one size", "EAN": 1, "Wholesale_Price_Euro": "5,00", "ImageProductP1": "u1"},
        {"StyleCode": "A1", "StyleName": "Cap", "Color": "blue", "Size": "one size", "EAN": 2, "Wholesale_Price_Euro": "5,00", "ImageProductP1": "u2", "ImageProductP2": "u3"},
    ]
    out = rows_to_shopify(rows, {"shopify": {"markup_on_wholesale": 2, "price_multiplier": 10}})
    assert [r["Option1 Value"] for r in out if r["Variant Barcode"]] == ["red", "blue"]
    assert out[0]["Option1 Name"] == "Color" and out[0]["Option2 Name"] == ""
    assert out[0]["Variant Price"] == "100.00" and out[0]["Cost per item"] == "50.00"
    assert [r["Image Src"] for r in out] == ["u1", "u2", "u3"] and out[2]["Variant SKU"] == ""
