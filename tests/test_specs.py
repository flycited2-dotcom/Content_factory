from content_factory.content.specs import build_specs_for_card


def _rows(*pairs):
    return [{"title": t, "value": v} for t, v in pairs]


def test_energy_class_cooling_only():
    rows = _rows(("Класс энергоэффективности (охлаждение)", "A++"),
                 ("Класс энергоэффективности при нагреве", "B"))
    lines = build_specs_for_card(rows, "X", "S", "breeze")
    assert "⚡ Класс энергоэффективности A++" in lines


def test_inverter_from_value_and_fallback():
    yes = build_specs_for_card(_rows(("Технология работы", "DC Inverter")), "X", "S", "breeze")
    assert "❄️ Инверторная технология" in yes
    # явное «нет» не перебивается названием
    no = build_specs_for_card(_rows(("Инверторная технология", "нет")), "X", "S Inverter",
                              "breeze", titles=["Сплит Inverter"])
    assert "❄️ Инверторная технология" not in no
    # нет tech-поля → определяем по названию позиции
    by_title = build_specs_for_card(_rows(("Тип", "сплит")), "X", "S", "breeze",
                                    titles=["Сплит-система Inverter"])
    assert "❄️ Инверторная технология" in by_title


def test_compressor_warranty_wifi_noise_heat():
    rows = _rows(
        ("Марка компрессора", "RECHI"),
        ("Гарантия", "24"),
        ("Wi-Fi", "ready"),
        ("Уровень шума внутреннего блока", "27/32/38"),
        ("Границы рабочих температур (нагрев)", "-7 ~ +24"),
    )
    lines = build_specs_for_card(rows, "X", "S", "breeze")
    assert "⚙️ Компрессор RECHI" in lines
    assert "🛡 Гарантия 24 мес" in lines
    assert "📶 Wi-Fi (опция)" in lines
    assert "🔇 Уровень шума от 27 дБ" in lines
    assert "🌡 Работа на обогрев до −7 °C" in lines


def test_empty_when_no_known_specs():
    assert build_specs_for_card(_rows(("Цвет", "белый")), "X", "S", "breeze") == []


def test_order_class_first():
    rows = _rows(("Марка компрессора", "GMCC"),
                 ("Класс энергоэффективности", "A"))
    lines = build_specs_for_card(rows, "X", "S", "breeze")
    assert lines[0].startswith("⚡") and any(s.startswith("⚙️") for s in lines)
