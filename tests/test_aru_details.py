import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from content_factory.ingest.aru_details import (
    load_aru_details,
    make_aru_prepare,
    original_photo_url,
    utp_from_specs,
)
from content_factory.ingest.aru_site import save_snapshot
from content_factory.orchestrator.excel_pipeline import ExcelItem, ExcelStore

THUMB = ("https://aru.ooo/wa-data/public/shop/products/00/webp/69/20/52069/"
         "images/37801/37801.180.webp")
ORIGINAL = ("https://aru.ooo/wa-data/public/shop/products/69/20/52069/"
            "images/37801/37801.970.jpg")
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 6000


def row(ident="52069", name="Перчатки нейлон ALG", brand="ALG", **extra):
    base = {
        "id": ident, "article": ident, "name": name, "url": f"https://aru.ooo/p/{ident}/",
        "brand": brand, "category_path": ["perchatki"], "available": True,
        "price": "43.48", "price_basis": "account_price", "content_markup_pct": "10",
        "content_price": "47.83", "content_price_rub": 48,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "specifications": {"Бренд": "ALG", "Класс вязки": "15", "Покрытие": "ПВХ", "Пусто": ""},
        "image_urls": [THUMB], "description": name,
    }
    base.update(extra)
    return base


def snapshot(tmp_path, rows):
    save_snapshot(tmp_path / "aru-catalog.json", {
        "schema_version": 1, "source": "aru", "complete": True, "authenticated": True,
        "price_basis": "account_price", "generated_at": "2026-10-09T16:32:14+00:00",
        "content_markup_pct": "10", "items": rows})


def test_original_photo_url_drops_webp_dir_and_asks_big_jpeg():
    assert original_photo_url(THUMB) == ORIGINAL
    jpg = "https://aru.ooo/wa-data/public/shop/products/01/39/63901/images/52650/52650.180.jpg"
    assert original_photo_url(jpg) == jpg.replace("180.jpg", "970.jpg")
    assert original_photo_url(jpg.replace(".jpg", ".jpeg")) == jpg.replace("180.jpg", "970.jpg")
    assert original_photo_url("https://example.com/x.png") is None


def test_utp_from_specs_skips_brand_and_empty_values():
    assert utp_from_specs({"Бренд": "ALG", "Класс вязки": "15", "Покрытие": "ПВХ", "Пусто": " "}) \
        == "✓ Класс вязки: 15\n✓ Покрытие: ПВХ"
    assert utp_from_specs({}) == ""
    # реальные формы каталога: общий ключ, флаги «Да», служебные UUID-ключи
    assert utp_from_specs({"характеристика": "Желтая  15 кл.", "Бренд": "ALG"}) == "✓ Желтая 15 кл."
    assert utp_from_specs({"2 форма": "Да", "Кратность (упаковка)": "10"}) \
        == "✓ 2 форма\n✓ Кратность (упаковка): 10"
    assert utp_from_specs({"a22a62b6_94b3_11f1_92f3_c84bd6f31391": "Да"}) == ""
    many = {f"К{n}": str(n) for n in range(20)}
    assert len(utp_from_specs(many).splitlines()) == 8


def test_details_are_keyed_by_brand_and_name_and_carry_supplier_date(tmp_path):
    snapshot(tmp_path, [row()])
    detail = load_aru_details(tmp_path)[("alg", "Перчатки нейлон ALG")]
    assert detail.id == "52069"
    assert detail.photo_url == ORIGINAL
    assert detail.supplier_updated_at == "2026-10-09T16:32:14+00:00"
    assert detail.specs["Покрытие"] == "ПВХ"


def test_ambiguous_rows_are_not_offered(tmp_path):
    # одинаковые бренд+название и общий ключ кэша бренд|модель → фото подмешать нельзя
    snapshot(tmp_path, [row("1"), row("2"),
                        row("3", name="150 перчатка нейлон жёлтая (8р) ALG 10шт. (300шт.уп.)"),
                        row("4", name="152 перчатка нейлон зелёная (9р) ALG 10шт. (300шт.уп.)")])
    assert load_aru_details(tmp_path) == {}


def test_missing_or_invalid_snapshot_gives_nothing(tmp_path):
    assert load_aru_details(tmp_path) == {}
    (tmp_path / "aru-catalog.json").write_text(json.dumps({"source": "other"}), encoding="utf-8")
    assert load_aru_details(tmp_path) == {}


def item(name="Перчатки нейлон ALG", brand="ALG", model="Перчатки нейлон ALG"):
    return ExcelItem(key=f"excel|{brand.lower()}|{model.lower()}", brand=brand, model=model,
                     name=name, price=48, status="new", research_job=None, card_job=None,
                     tries=0, error=None)


@pytest.fixture
def env(tmp_path):
    snapshot(tmp_path, [row()])
    fetched = []

    def fetch(url):
        fetched.append(url)
        return JPEG
    store = ExcelStore(tmp_path / "s.db")
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos", fetch)
    return store, prepare, fetched, tmp_path


def test_prepare_seeds_cache_with_original_photo_and_specs(env):
    store, prepare, fetched, tmp = env
    prepare(item())
    utp, photo = store.cache_get("alg|перчатки нейлон alg")
    assert utp == "✓ Класс вязки: 15\n✓ Покрытие: ПВХ"
    assert photo == str(tmp / "photos" / "52069.jpg")
    assert (tmp / "photos" / "52069.jpg").read_bytes() == JPEG
    assert fetched == [ORIGINAL]
    evidence = store.cache_evidence("alg|перчатки нейлон alg")
    assert evidence["aru_id"] == "52069" and evidence["source"] == "aru"
    assert evidence["supplier_updated_at"] == "2026-10-09T16:32:14+00:00"


def test_prepare_ignores_foreign_item_and_keeps_existing_cache(env):
    store, prepare, fetched, _ = env
    prepare(item(name="Чужой товар", model="чужой"))
    assert fetched == [] and store.cache_get("alg|чужой") is None
    store.cache_put("alg|перчатки нейлон alg", "✓ ручное", "/manual.png", source="manual")
    prepare(item())
    assert store.cache_get("alg|перчатки нейлон alg") == ("✓ ручное", "/manual.png")
    assert fetched == []


def test_prepare_reuses_downloaded_photo(env):
    store, prepare, fetched, _ = env
    prepare(item())
    with store._c() as c:
        c.execute('DELETE FROM research_cache')   # кэш стёрт, фото на диске осталось
    prepare(item())
    assert fetched == [ORIGINAL]


@pytest.mark.parametrize("bad", [b"", b"<html>captcha</html>" * 400, b"\xff\xd8\xff"])
def test_prepare_does_not_seed_when_photo_is_not_a_real_jpeg(tmp_path, bad):
    snapshot(tmp_path, [row()])
    store = ExcelStore(tmp_path / "s.db")
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos",
                               lambda url: bad, retry_delay=0)
    prepare(item())
    assert store.cache_get("alg|перчатки нейлон alg") is None
    assert not (tmp_path / "photos" / "52069.jpg").exists()


def test_prepare_survives_network_error(tmp_path):
    snapshot(tmp_path, [row()])
    store = ExcelStore(tmp_path / "s.db")

    def boom(url):
        raise OSError("timeout")
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos", boom,
                               retry_delay=0)
    prepare(item())
    assert store.cache_get("alg|перчатки нейлон alg") is None


def test_prepare_falls_back_to_page_og_image(tmp_path):
    snapshot(tmp_path, [row()])
    store = ExcelStore(tmp_path / "s.db")
    og = "https://aru.ooo/wa-data/public/shop/products/69/20/52069/images/37801/37801.750x0.jpg"
    calls = []

    def fetch(url):
        calls.append(url)
        if url == ORIGINAL:
            raise OSError("404")
        if url.endswith("/p/52069/"):
            return f'<meta property="og:image" content="{og}">'.encode()
        return JPEG
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos", fetch,
                               retry_delay=0)
    prepare(item())
    assert calls[-1] == og
    assert store.cache_get("alg|перчатки нейлон alg")[1].endswith("52069.jpg")


def test_item_without_specs_is_left_to_research(tmp_path):
    snapshot(tmp_path, [row(specifications={})])
    store = ExcelStore(tmp_path / "s.db")
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos",
                               lambda url: JPEG)
    prepare(item())
    assert store.cache_get("alg|перчатки нейлон alg") is None


def test_prepare_retries_transient_download_errors(tmp_path):
    snapshot(tmp_path, [row()])
    store = ExcelStore(tmp_path / "s.db")
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) < 3:
            raise TimeoutError("site stalled")
        return JPEG
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), tmp_path / "photos", flaky,
                               retry_delay=0)
    prepare(item())
    assert attempts == [ORIGINAL] * 3
    assert store.cache_get("alg|перчатки нейлон alg") is not None


def test_cached_photo_path_is_absolute_even_for_relative_photos_dir(tmp_path, monkeypatch):
    # на сервере prices_dir относительный; resolve_photo/submit_card относительный путь
    # трактуют от output_dir фотоагента и не находят файл → позиция уходила в research
    snapshot(tmp_path, [row()])
    monkeypatch.chdir(tmp_path)
    store = ExcelStore(tmp_path / "s.db")
    prepare = make_aru_prepare(store, load_aru_details(tmp_path), Path("state/aru-photos"),
                               lambda url: JPEG)
    prepare(item())
    photo = store.cache_get("alg|перчатки нейлон alg")[1]
    assert Path(photo).is_absolute() and Path(photo).is_file()
