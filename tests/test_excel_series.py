"""Серийная группировка позиций прайса (запрос владельца 2026-07-07, кейс
EUROHOFF/Dantex): 3-5 мощностей одной серии → ОДИН пост (одна карточка, одни УТП,
цены всех мощностей линейкой). Кейсы — из реального прайса БытТехОпт:
размер зашит по-разному (AVE-07M, KSGHA21HFRN1, XG-TXE21RHA, TL-RWB100-NR),
но во всех именах кондиционеров есть явный токен «NN BTU · до NN м²»."""
from content_factory.ingest.excel_series import group_rows, parse_btu, series_sig


def _row(brand, model, name, price):
    """Кортеж как в bot make/pick: (key, brand, model, name, price)."""
    return (f"excel|{brand.lower()}|{model.lower()}", brand, model, name, price)


# ── parse_btu: размер и площадь из наименования ────────────────────────────────
def test_parse_btu_two_digits_and_area():
    assert parse_btu("DANTEX RK-07SCDG On/Off 07 BTU · до 20 м² (GREE)") == (7, 20)


def test_parse_btu_single_digit_xigma():
    assert parse_btu("XIGMA XG-TXE21RHA TXE On/Off 7 BTU · до 20 м²") == (7, 20)


def test_parse_btu_absent_for_fridge():
    assert parse_btu("Холодильник Stinol STS 167 (167*60*62)") == (None, None)


# ── series_sig: сигнатура серии (она же отображаемое имя) ──────────────────────
def test_sig_blanks_size_digits():
    assert series_sig("EUROHOFF AVE-07M On/Off 07 BTU · до 20 м²") == \
        series_sig("EUROHOFF AVE-24M On/Off 24 BTU · до 70 м²") == "EUROHOFF AVE-XXM On/Off"


def test_sig_keeps_oem_parens():
    assert series_sig("DANTEX RK-07SCDG On/Off 07 BTU · до 20 м² (GREE)") == \
        "DANTEX RK-XXSCDG On/Off (GREE)"


def test_sig_mid_code_digits_kentatsu():
    # размер зашит в середину кода (21/26/35/50/70) — серия одна
    assert series_sig("KENTATSU KSGHA21HFRN1 On/Off 07 BTU · до 20 м²") == \
        series_sig("KENTATSU KSGHA70HFRN1 On/Off 24 BTU · до 70 м²")


def test_sig_generation_digit_distinguishes_series():
    # MSAG1/MSAG2/MSAG3 — РАЗНЫЕ серии: одиночную цифру поколения не гасим
    s1 = series_sig("MIDEA MSAG1-07HRN8-I On/Off 07 BTU · до 20 м²")
    s2 = series_sig("MIDEA MSAG2-07HRN8-I On/Off 07 BTU · до 20 м²")
    assert s1 != s2 and "MSAG1" in s1 and "MSAG2" in s2


def test_sig_three_digit_code_thaicon():
    # TL-RWB100-NR (24 BTU · до 100 м²) — та же линейка, что RWB20/70
    assert series_sig("THAICON TL-RWB100-NR On/Off 24 BTU · до 100 м²") == \
        series_sig("THAICON TL-RWB20-NR On/Off 07 BTU · до 20 м²")


def test_sig_none_without_btu_token():
    assert series_sig("Холодильник Beko X100") is None


# ── group_rows: группировка перед постановкой в excel_items ────────────────────
_AVE = [_row("EUROHOFF", f"AVE-{s:02d}M On/Off {s:02d} BTU · до {a} м²",
             f"EUROHOFF AVE-{s:02d}M On/Off {s:02d} BTU · до {a} м²", p)
        for s, a, p in [(9, 25, 18390), (7, 20, 16590), (12, 35, 23990)]]
_FRIDGE = _row("Beko", "X100", "Холодильник Beko X100", 30000)


def test_group_rows_merges_series_into_one_row():
    rows, members = group_rows(_AVE + [_FRIDGE])
    assert len(rows) == 2                                  # серия + холодильник
    key, brand, model, name, price = rows[0]
    assert name == "EUROHOFF AVE-XXM On/Off"
    assert key == "excel|eurohoff|eurohoff ave-xxm on/off"
    assert model.startswith("AVE-07M")                     # представитель — младший
    assert price == 16590                                  # цена «от»
    assert rows[1] == _FRIDGE                              # одиночка не тронута


def test_group_rows_members_sorted_with_sizes():
    rows, members = group_rows(_AVE)
    (mlist,) = members.values()
    assert [m["size"] for m in mlist] == [7, 9, 12]
    assert [m["area"] for m in mlist] == [20, 25, 35]
    assert [m["price"] for m in mlist] == [16590, 18390, 23990]
    assert mlist[0]["key"] == _AVE[1][0]                   # ключи позиций сохранены


def test_single_conditioner_stays_intact():
    # один размер серии — обычная позиция с исходным ключом (не «серия из 1»)
    rows, members = group_rows([_AVE[0], _FRIDGE])
    assert rows == [_AVE[0], _FRIDGE] and members == {}


def test_different_brands_do_not_merge():
    other = _row("DANTEX", "RK-07SCDG On/Off 07 BTU · до 20 м²",
                 "DANTEX RK-07SCDG On/Off 07 BTU · до 20 м²", 19990)
    rows, members = group_rows([_AVE[0], other])
    assert len(rows) == 2 and members == {}
