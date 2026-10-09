"""Validate browser-captured ARU account pages without storing login credentials."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .aru_site import _price, save_snapshot


def build_account_snapshot(
    pages: list[dict], *, allow_partial=False, reconcile_moved=False
) -> dict:
    if not pages:
        raise ValueError("aru_account: no pages")
    expected = pages[0]["pages"]
    numbers = [p["page"] for p in pages]
    complete = sorted(numbers) == list(range(1, expected + 1))
    if len(set(numbers)) != len(numbers) or not 1 <= expected <= 1000:
        raise ValueError("aru_account: invalid page coverage")
    if not complete and not allow_partial:
        raise ValueError("aru_account: incomplete page coverage")
    seen, eligible, excluded, stamps = set(), [], 0, []
    observations, moved = {}, []
    for page in pages:
        url = urlparse(page["url"])
        actual_page = int(parse_qs(url.query).get("page", ["1"])[0])
        if (
            page.get("authenticated") is not True
            or page["pages"] != expected
            or url.scheme != "https"
            or url.netloc != "aru.ooo"
            or url.path != "/vse-tovary/"
            or actual_page != page["page"]
            or not page.get("items")
        ):
            raise ValueError("aru_account: unauthenticated or invalid page")
        stamp = datetime.fromisoformat(page["captured_at"])
        if stamp.tzinfo is None:
            raise ValueError("aru_account: timezone missing")
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if not -300 <= age <= 86400:
            raise ValueError("aru_account: stale capture")
        stamps.append(stamp)
        page_ids = set()
        for row in page["items"]:
            identity = str(row.get("id", ""))
            item_url = urlparse(row.get("url", ""))
            if (
                not identity.isdigit()
                or identity in page_ids
                or not row.get("name")
                or item_url.scheme != "https"
                or item_url.netloc != "aru.ooo"
            ):
                raise ValueError("aru_account: duplicate or invalid product")
            seen.add(identity)
            page_ids.add(identity)
            previous = observations.get(identity)
            if previous:
                if not reconcile_moved:
                    raise ValueError("aru_account: duplicate product across pages")
                moved.append({"id": identity, "pages": [previous[2], page["page"]]})
                if stamp < previous[1]:
                    continue
            observations[identity] = (row, stamp, page["page"])
    for identity, (row, stamp, _) in observations.items():
        item_url = urlparse(row["url"])
        price = _price(row.get("price_text", ""))
        # Exact stock text avoids matching "Нет в наличии" or unknown states.
        if row.get("stock_text", "").strip().casefold() != "в наличии" or price is None:
            excluded += 1
            continue
        sale = Decimal(price) * Decimal("1.10")
        eligible.append(
            {
                "id": identity,
                "article": row.get("article", ""),
                "name": row["name"],
                "url": row["url"],
                "brand": row.get("brand", ""),
                "category_path": item_url.path.strip("/").split("/")[:-1],
                "available": True,
                "price": price,
                "price_basis": "account_price",
                "content_markup_pct": "10",
                "content_price": str(
                    sale.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                ),
                "content_price_rub": int(
                    sale.quantize(Decimal("1"), rounding=ROUND_CEILING)
                ),
                "captured_at": stamp.isoformat(),
                "specifications": row.get("specifications", {}),
                "image_urls": row.get("image_urls", []),
                "description": row.get("description", ""),
            }
        )
    return {
        "schema_version": 1,
        "source": "aru",
        "complete": complete,
        "authenticated": True,
        "price_basis": "account_price",
        "generated_at": min(stamps).isoformat(),
        "pages": expected,
        "captured_pages": len(pages),
        "scanned_products": len(seen),
        "excluded_products": excluded,
        "content_markup_pct": "10",
        "reconciled_moved_products": moved,
        "items": eligible,
    }


def load_account_items(prices_dir: Path, markup_pct=10):
    """Keep the last complete snapshot until replaced; preserve wholesale cents."""
    from .excel_price import PriceItem

    path = Path(prices_dir) / "aru-catalog.json"
    if not path.is_file():
        return []
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("aru_account: oversized snapshot")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("source") != "aru"
        or data.get("complete") is not True
        or data.get("authenticated") is not True
        or data.get("price_basis") != "account_price"
    ):
        return []
    stamp = datetime.fromisoformat(data["generated_at"])
    if (
        stamp.tzinfo is None
        or (datetime.now(timezone.utc) - stamp).total_seconds() < -300
    ):
        return []
    markup = Decimal(str(markup_pct))
    if not markup.is_finite() or markup <= -100:
        raise ValueError("aru_account: invalid markup")
    items, seen = [], set()
    for row in data["items"]:
        identity = str(row["id"])
        if identity in seen:
            raise ValueError("aru_account: duplicate product")
        seen.add(identity)
        if (
            row.get("available") is not True
            or row.get("price_basis") != "account_price"
        ):
            continue
        price = Decimal(str(row["price"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("aru_account: invalid account price")
        sale = int(
            (price * (1 + markup / 100)).quantize(Decimal("1"), rounding=ROUND_CEILING)
        )
        items.append(
            PriceItem(
                section=" / ".join(row.get("category_path", [])),
                article=row.get("article", ""),
                brand=row.get("brand", ""),
                name=row["name"],
                price=sale,
            )
        )
    return items


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument(
        "--reconcile-moved",
        action="store_true",
        help="Use latest observation for products moved between pages; record audit",
    )
    args = parser.parse_args()
    pages = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(args.pages_dir.glob("*.json"))
    ]
    data = build_account_snapshot(
        pages, allow_partial=args.preview, reconcile_moved=args.reconcile_moved
    )
    save_snapshot(args.output, data)
    print(json.dumps({k: v for k, v in data.items() if k != "items"}))


if __name__ == "__main__":
    main()
