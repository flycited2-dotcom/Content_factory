from content_factory.dedupe import canonical_product_identity


def _caption(hook: str, title: str) -> str:
    return f"{hook}\n\n{title}\n💎 9 990 ₽ · в наличии в Симферополе\n\nОписание."


def test_same_hook_different_models_are_different_products():
    hook = "Нужно быстро согреть комнату, гараж или дачу? Вот вариант в наличии, цена открытая."
    a = canonical_product_identity(source_key="catalog:rusklimat:NS-1", category="heaters",
                                   brand="BALLU", caption=_caption(hook, "Обогреватель Ballu BIGH-55"))
    b = canonical_product_identity(source_key="catalog:rusklimat:NS-2", category="heaters",
                                   brand="BALLU", caption=_caption(hook, "Обогреватель Ballu BHP-M-09"))
    assert a != b


def test_same_model_with_different_hooks_is_one_product():
    a = canonical_product_identity(source_key="catalog:rusklimat:NS-1", category="heaters", brand="BALLU",
                                   caption=_caption("Холодно на даче?", "Обогреватель Ballu BIGH-55"))
    b = canonical_product_identity(source_key="catalog:breeze:X-9", category="heaters", brand="BALLU",
                                   caption=_caption("Гараж не прогреть?", "Обогреватель Ballu BIGH-55"))
    assert a == b


def test_caption_without_price_line_uses_first_line():
    a = canonical_product_identity(source_key="k1", category="ac", brand="DAICHI",
                                   caption="Daichi AIR20AVQ1\nописание")
    b = canonical_product_identity(source_key="k2", category="ac", brand="DAICHI",
                                   caption="Daichi AIR25AVQ1\nописание")
    assert a != b
