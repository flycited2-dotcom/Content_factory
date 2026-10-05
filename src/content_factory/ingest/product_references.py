"""Fail-closed очередь поиска изображения точной модели, когда каталог не дал фото."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse


class ReferencePending(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferenceCandidate:
    image_url: str
    page_url: str
    page_title: str
    source_type: str
    usage_allowed: bool
    corroborating_sources: int = 0


def normalize_model(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").lower().replace("ё", "е"))


def exact_model_for_offer(offer) -> str:
    """Выделить артикул модели, а не проверять против страницы всё длинное название."""
    attrs = offer.attrs or {}
    for key in ("Артикул", "Модель", "Код модели", "SKU"):
        value = str(attrs.get(key) or "").strip()
        if value:
            return value
    tokens = re.findall(r"(?<![\w-])[A-ZА-Я]{2,}[A-ZА-Я0-9]*(?:-[A-ZА-Я0-9]+)+(?=$|[^\w-])",
                        (offer.model or "").upper())
    if tokens:
        return max(tokens, key=len)
    tokens = re.findall(r"(?<![\w-])[A-ZА-Я]{2,}\d+[A-ZА-Я0-9-]*(?=$|[^\w-])",
                        (offer.model or "").upper())
    return max(tokens, key=len) if tokens else (offer.model or "").strip()


def candidate_is_verified(model: str, candidate: ReferenceCandidate,
                          trusted_domains=()) -> bool:
    wanted = normalize_model(model)
    if not wanted or wanted not in normalize_model(candidate.page_title):
        return False
    if not candidate.usage_allowed or not candidate.image_url.startswith(("http://", "https://")):
        return False
    domain = (urlparse(candidate.page_url).hostname or "").lower()
    trusted = any(domain == d or domain.endswith("." + d) for d in trusted_domains)
    authoritative = candidate.source_type in {"manufacturer", "supplier"}
    corroborated = candidate.corroborating_sources >= 2
    return trusted or authoritative or corroborated


class ProductReferenceInbox:
    """Request/result JSON — контракт с web-research агентом; неподтверждённый ответ не проходит."""
    def __init__(self, root, trusted_domains=()):
        self.root = Path(root)
        self.requests = self.root / "requests"
        self.results = self.root / "results"
        self.trusted_domains = tuple(str(x).lower() for x in trusted_domains)

    @staticmethod
    def key_for(offer) -> str:
        raw = f"{offer.brand}|{offer.model}|{offer.supplier_sku}".casefold()
        return sha256(raw.encode("utf-8")).hexdigest()[:20]

    def queue(self, offer) -> Path:
        self.requests.mkdir(parents=True, exist_ok=True)
        path = self.requests / f"{self.key_for(offer)}.json"
        if not path.exists():
            exact_model = exact_model_for_offer(offer)
            payload = {
                "supplier_sku": offer.supplier_sku,
                "brand": offer.brand,
                "model": exact_model,
                "query": f'точная модель "{offer.brand} {exact_model}" фото товара',
                "requirements": {
                    "exact_model_only": True,
                    "preserve_original_angle": True,
                    "usage_permission_required": True,
                    "minimum_corroborating_sources": 2,
                    "forbid_similar_models": True,
                },
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def resolve(self, offer) -> str | None:
        if offer.photos:
            return offer.photos[0]
        result = self.results / f"{self.key_for(offer)}.json"
        if result.is_file():
            payload = json.loads(result.read_text(encoding="utf-8"))
            candidates = [ReferenceCandidate(**x) for x in payload.get("candidates", [])]
            for candidate in candidates:
                if candidate_is_verified(exact_model_for_offer(offer), candidate,
                                         self.trusted_domains):
                    return candidate.image_url
        self.queue(offer)
        return None
