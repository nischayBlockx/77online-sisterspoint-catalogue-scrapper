"""Data model shared by the scraper and the exporters.

The catalogue is one row per sellable variant (colour + size / EAN), using
exactly the columns of the reference "TBI Catalogue" workbook so the output
can be handled the same way as the catalogues you already receive.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Column order of the reference catalogue (TBI_Catalogue__Final_...xlsx).
CATALOGUE_COLUMNS = [
    "Brand",
    "ItemMainCategory",
    "EAN",
    "ItemCode",
    "ItemDescription",
    "Color",
    "Size",
    "StyleCode",
    "StyleName",
    "Wholesale_Price_Euro",
    "Retail_Price_Euro",
    "Weight",
    "WeightUnit",
    "Season",
    "CustomTariffNo",
    "Gender",
    "OriginCountry",
    "ProductsComposition",
    "CareInstruction",
    "ProductText",
    "ImageModelMW1",
    "ImageModelMW2",
    "ImageModelMW4",
    "ImageModelMW5",
    "ImageProductP1",
    "ImageProductP2",
    "ImageProductP30",
    "CreationDate",
    "Available Stock",
]

# Image columns in the order images are assigned to them.
IMAGE_COLUMNS = [
    "ImageProductP1",
    "ImageProductP2",
    "ImageModelMW1",
    "ImageModelMW2",
    "ImageModelMW4",
    "ImageModelMW5",
    "ImageProductP30",
]


@dataclass
class Variant:
    color: str = ""
    size: str = ""
    ean: str = ""
    sku: str = ""
    stock: int | None = None
    wholesale_price: float | None = None
    retail_price: float | None = None
    images: list[str] = field(default_factory=list)


@dataclass
class Product:
    url: str = ""
    category: str = ""
    brand: str = ""
    style_code: str = ""
    style_name: str = ""
    description: str = ""
    composition: str = ""
    care: str = ""
    origin: str = ""
    tariff: str = ""
    weight: str = ""
    weight_unit: str = ""
    season: str = ""
    gender: str = ""
    color: str = ""
    wholesale_price: float | None = None
    retail_price: float | None = None
    images: list[str] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
