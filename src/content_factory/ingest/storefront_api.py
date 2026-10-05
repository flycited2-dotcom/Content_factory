"""Read-only загрузчик товарной витрины через внутренний tender-products API."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from content_factory.models import Offer


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        values = {str(k).lower(): str(v or "").lower() for k, v in attrs}
        if values.get("type") == "application/ld+json":
            self.capture = True
            self.parts = []

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.capture:
            self.blocks.append("".join(self.parts))
            self.capture = False
            self.parts = []


def _decimal(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _product_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _product_nodes(item)
    elif isinstance(value, dict):
        if value.get("@type") == "Product":
            yield value
        yield from _product_nodes(value.get("@graph", []))


def parse_product_jsonld(html: str) -> tuple[list[str], Decimal | None]:
    """Вернуть изображения и розничную цену из публичной JSON-LD карточки."""
    parser = _JsonLdParser()
    parser.feed(html)
    for block in parser.blocks:
        try:
            value = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        for product in _product_nodes(value):
            raw_images = product.get("image") or []
            if isinstance(raw_images, str):
                raw_images = [raw_images]
            images = [str(x) for x in raw_images if str(x).startswith(("http://", "https://"))]
            offers = product.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            return images, _decimal(offers.get("price") if isinstance(offers, dict) else None)
    return [], None


def _attrs(product: dict) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for item in product.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("label") or item.get("key") or "").strip()
        value = str(item.get("value") or item.get("numericValue") or "").strip()
        unit = str(item.get("unit") or "").strip()
        if key and value:
            attrs[key] = " ".join(x for x in (value, unit) if x)
    specs = product.get("specifications")
    if isinstance(specs, dict):
        for key, value in specs.items():
            if value is not None:
                attrs.setdefault(str(key), str(value))
    for key, label in (("warranty", "Гарантия"), ("part", "Артикул"),
                       ("stockStatus", "Статус наличия"), ("deliveryDays", "Срок поставки, дней")):
        if product.get(key) not in (None, ""):
            attrs.setdefault(label, str(product[key]))
    if product.get("productUrl"):
        attrs["Ссылка на товар"] = str(product["productUrl"])
    return attrs


def _to_offer(product: dict, *, category_id: int | None, images: list[str],
              retail_ref: Decimal | None) -> Offer:
    sku = str(product.get("sku") or product.get("part") or "").strip()
    name = str(product.get("name") or product.get("supplierName") or "").strip()
    brand = str(product.get("vendor") or "").strip()
    identity = f"storefront:{sku or sha256(name.encode('utf-8')).hexdigest()[:16]}"
    fingerprint = json.dumps(product, ensure_ascii=False, sort_keys=True, default=str)
    return Offer(
        supplier_sku=identity,
        source="storefront",
        brand=brand,
        model=name,
        category_id=category_id,
        attrs=_attrs(product),
        cost=_decimal(product.get("purchasePriceGross")),
        retail_ref=retail_ref,
        stock=1 if product.get("isAvailable") else 0,
        photos=images,
        series=None,
        content_hash=sha256(fingerprint.encode("utf-8")).hexdigest(),
    )


def collect_storefront_offers(cfg, token: str, *, client: httpx.Client | None = None) -> list[Offer]:
    """Собрать и дедуплицировать товары. Метод ничего не изменяет на витрине."""
    if not cfg.api_url:
        raise ValueError("Для storefront_api не задан source.api_url")
    if len(token.strip()) < 32:
        raise ValueError(f"Не задан или слишком короткий токен из {cfg.token_env}")
    queries = [q.strip() for q in cfg.queries if q.strip()]
    if not queries:
        raise ValueError("Для storefront_api не заданы source.queries")
    owns_client = client is None
    http = client or httpx.Client(timeout=cfg.timeout_seconds, follow_redirects=True)
    products: dict[str, dict] = {}
    try:
        for query in queries:
            response = http.post(cfg.api_url, json={"query": query, "limit": cfg.limit_per_query},
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ValueError("Витрина вернула некорректный ответ")
            for product in payload.get("products") or []:
                if not isinstance(product, dict):
                    continue
                if cfg.available_only and not product.get("isAvailable"):
                    continue
                key = str(product.get("sku") or product.get("part") or product.get("name") or "").casefold()
                if key:
                    products[key] = product

        offers: list[Offer] = []
        for product in products.values():
            images: list[str] = []
            retail_ref = None
            page_url = str(product.get("productUrl") or "")
            if cfg.enrich_product_pages and page_url:
                page = http.get(urljoin(cfg.api_url, page_url))
                page.raise_for_status()
                images, retail_ref = parse_product_jsonld(page.text)
            offers.append(_to_offer(product, category_id=cfg.category_id,
                                    images=images, retail_ref=retail_ref))
        return offers
    finally:
        if owns_client:
            http.close()
