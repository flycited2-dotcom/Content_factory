from __future__ import annotations
from decimal import Decimal
from pydantic import BaseModel, Field


class RawProduct(BaseModel):
    source: str
    nc_code: str | None = None
    brand: str | None = None
    title: str = ""
    series: str | None = None
    category_id: int | None = None
    btu_calc: float | None = None
    price_wholesale: Decimal | None = None
    price_base: Decimal | None = None
    stock_qty: int = 0
    image_urls: list[str] = Field(default_factory=list)
    tech: dict[str, str] = Field(default_factory=dict)


class Offer(BaseModel):
    supplier_sku: str
    source: str
    brand: str = ""
    model: str = ""
    category_id: int | None = None
    btu_calc: float | None = None
    attrs: dict[str, str] = Field(default_factory=dict)
    cost: Decimal | None = None
    retail_ref: Decimal | None = None
    stock: int = 0
    photos: list[str] = Field(default_factory=list)
    series: str | None = None
    content_hash: str = ""


class PriceResult(BaseModel):
    ok: bool
    price: int | None = None
    markup_pct: float = 0
    min_margin_applied: bool = False
    reason: str | None = None


class Content(BaseModel):
    title: str
    description: str
    from_cache: bool = False


class AdRecord(BaseModel):
    ad_id: str
    supplier_sku: str
    city_id: str
    title: str
    description: str
    price: int
    address: str = ""
    product_type: str = ""
    vendor: str = ""
    ac_type: str = ""
    ac_subtype: str = ""
    images: list[str] = Field(default_factory=list)
    status: str = "pending"


class City(BaseModel):
    id: str
    name: str
    avito_location: str
