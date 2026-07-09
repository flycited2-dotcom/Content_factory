import httpx
from content_factory.ingest.breez import (
    _extract_base, _parse_leftovers, _parse_products_utp,
    fetch_breez_base_by_nc, fetch_breez_utp_by_nc)


def test_parse_products_utp():
    data = {
        "1": {"nc": "НС-1", "utp": "Авто;Wi-Fi"},
        "2": {"nc": "НС-2", "utp": ""},          # пустой utp — пропуск
        "3": {"utp": "X"},                        # без nc — пропуск
        "4": "not a dict",                        # мусор — пропуск
    }
    assert _parse_products_utp(data) == {"НС-1": "Авто;Wi-Fi"}


def test_parse_non_dict():
    assert _parse_products_utp([1, 2]) == {}


def test_fetch_via_mock():
    def handler(req):
        assert req.url.path.endswith("/products/")
        assert req.headers.get("Authorization") == "Token x"
        return httpx.Response(200, json={"7": {"nc": "НС-7", "utp": "Тихо;Eco"}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://breez")
    res = fetch_breez_utp_by_nc(base_url="https://breez/api", auth_header="Token x", http=http)
    assert res == {"НС-7": "Тихо;Eco"}


def test_fetch_no_creds_returns_empty():
    assert fetch_breez_utp_by_nc(base_url="", auth_header="") == {}
    assert fetch_breez_utp_by_nc(base_url="https://x", auth_header="REPLACE_ME") == {}


# ── опт (base) из /leftoversnew/ ────────────────────────────────────────────

def test_extract_base():
    assert _extract_base([{"base": 100, "base_currency": "RUB"}, {"ric": 200}]) == 100
    assert _extract_base([{"ric": 200}]) is None
    assert _extract_base("мусор") is None


def test_parse_leftovers_format1_dict_by_nc():
    # текущий живой формат: ключ = NC, в записи поля nc может не быть
    data = {"НС-1": {"price": [{"base": 100}]}, "НС-2": {"price": [{"ric": 5}]}}
    assert _parse_leftovers(data) == {"НС-1": 100}


def test_parse_leftovers_format2_list_of_single_key_dicts():
    data = [{"НС-3": {"price": [{"base": 300}]}}]
    assert _parse_leftovers(data) == {"НС-3": 300}


def test_parse_leftovers_flat_list():
    data = [{"nc": "НС-4", "price": [{"base": 400}]},
            {"nc_code": "НС-5", "price": [{"base": 500}]},
            {"price": [{"base": 9}]}]                     # без ключа — пропуск
    assert _parse_leftovers(data) == {"НС-4": 400, "НС-5": 500}


def test_parse_leftovers_garbage():
    assert _parse_leftovers("не json-структура") == {}
    assert _parse_leftovers(None) == {}


def test_fetch_base_via_mock():
    def handler(req):
        assert req.url.path.endswith("/leftoversnew/")
        assert req.headers.get("Authorization") == "Token x"
        return httpx.Response(200, json={"НС-7": {"price": [{"base": 777}]}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://breez")
    res = fetch_breez_base_by_nc(base_url="https://breez/api", auth_header="Token x", http=http)
    assert res == {"НС-7": 777}


def test_fetch_base_http_error_returns_empty():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                        base_url="https://breez")
    assert fetch_breez_base_by_nc(base_url="https://breez/api", auth_header="Token x", http=http) == {}


def test_fetch_base_no_creds_returns_empty():
    assert fetch_breez_base_by_nc(base_url="", auth_header="") == {}
    assert fetch_breez_base_by_nc(base_url="https://x", auth_header="REPLACE_ME") == {}
