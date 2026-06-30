import httpx
from content_factory.ingest.breez import _parse_products_utp, fetch_breez_utp_by_nc


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
