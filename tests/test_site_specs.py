"""Мощность кондиционера (холод/тепло, кВт) из ТТХ поставщика в БД сайта — для карточки и подписи."""
from content_factory.ingest.site_specs import SiteSpecs, capacity_lines, model_codes, nominal_kw
from content_factory.orchestrator.excel_run import build_preview_caption


class FakeCursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if "catalog_product p\nWHERE" in sql:
            code = params["pattern"].strip("%")
            self.rows = [(nc, title) for nc, title in self.db["products"] if code in title]
        else:
            self.rows = list(self.db["specs"].get(params["nc"], {}).items())

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def close(self):
        pass


DB = {"products": [("NC1", "Инверторная сплит-система серии AKOYA Inverter RAC-I-AK25HP.")],
      "specs": {"NC1": {"Холодопроизводительность (кВт)": "2.30 (1.17 - 2.78)",
                        "Теплопроизводительность (кВт)": "2.50 (0.91 - 2.78)"}}}


def test_capacity_from_site_db_by_model_code():
    specs = SiteSpecs(lambda: FakeConn(DB))
    assert specs.lines("Кондиционер FUNAI AKOYA RAC-I-AK25HP.D01") == "❄️ Холод: 2,3 кВт\n🔥 Тепло: 2,5 кВт"
    assert specs.lines("Холодильник LG GA-B419SLGL") == ""                  # нет в БД — строк нет


def test_db_failure_gives_no_lines():
    def broken():
        raise OSError("db down")
    assert SiteSpecs(broken).lines("Кондиционер FUNAI AKOYA RAC-I-AK25HP.D01") == ""


def test_helpers():
    assert model_codes("Кондиционер MARSA TCL RK-18MTA4/RK-18MTA4E") == ["RK-18MTA4E", "RK-18MTA4"]
    assert nominal_kw("3.6 (1 - 4)") == 3.6 and capacity_lines(2.25, None) == "❄️ Холод: 2,25 кВт"


def test_caption_puts_capacity_right_after_price():
    cap = build_preview_caption("Кондиционер X", 30000, "✓ Wi-Fi", capacity="❄️ Холод: 2,3 кВт\n🔥 Тепло: 2,5 кВт")
    assert "</blockquote>\n❄️ Холод: 2,3 кВт\n🔥 Тепло: 2,5 кВт\n═" in cap
    assert "❄️" not in build_preview_caption("Холодильник", 30000, "✓ No Frost")
