from content_factory.config import load_config


def _write_cfg(tmp_path, body):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_config_parses_all_sections(tmp_path):
    cfg = load_config(_write_cfg(tmp_path,
        "source:\n"
        "  warehouse: Симферополь\n"
        "  categories: [2, 6, 7]\n"
        "  exclude_title_patterns: ['%мульти%']\n"
        "pricing: {default_markup_pct: 5, rounding: up_to_90, min_margin_abs: 0}\n"
        "content: {caption_max: 1024, stop_words: ['звоните']}\n"
        "cards: {dir: /opt/cf-cards, base_url: 'https://x/static', default_mode: mcp, require_for_publish: true}\n"
        "fotogen: {api_url: 'http://127.0.0.1:8765', per_run: 10, max_pending: 12, max_total: 999}\n"
        "telegram: {channel_id: '@chan', test_channel_id: '@test', min_seconds_between_posts: 180, parse_mode: HTML}\n"
        "review: {price_min: 1000, price_max: 1000000, require_specs: true, require_card: true}\n"
        "state: {db: state/cf.db, card_jobs_db: state/cards.db}\n"))

    assert cfg.source.warehouse == "Симферополь"
    assert cfg.source.catalog.report_category_ids == [2, 6, 7]
    assert cfg.source.catalog.exclude_title_patterns == ["%мульти%"]
    assert cfg.pricing.default_markup_pct == 5
    assert cfg.content.caption_max == 1024
    assert "звоните" in cfg.content.stop_words
    assert cfg.cards.dir == "/opt/cf-cards"
    assert cfg.cards.require_for_publish is True
    assert cfg.default_card_mode == "mcp"
    assert cfg.fotogen.max_total == 999
    assert cfg.telegram.channel_id == "@chan"
    assert cfg.telegram.test_channel_id == "@test"
    assert cfg.review.price_min == 1000
    # review.caption_max наследует content.caption_max, если не задан явно
    assert cfg.review.caption_max == 1024
    assert cfg.state.card_jobs_db == "state/cards.db"


def test_load_config_parses_modes_by_category(tmp_path):
    cfg = load_config(_write_cfg(tmp_path,
        "source: {}\n"
        "cards: {default_mode: mcp, modes_by_category: {2: mcp, 7: kbt}}\n"))
    # ключи приводятся к int (как category_id из каталога oasis)
    assert cfg.cards_modes_by_category == {2: "mcp", 7: "kbt"}


def test_load_config_defaults_on_empty(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, "source: {}\n"))
    assert cfg.source.warehouse == "Симферополь"
    assert cfg.source.catalog.report_category_ids == [2, 6, 7]
    assert cfg.default_card_mode == "mcp"
    assert cfg.cards_modes_by_category == {}            # по умолчанию — пустая карта
    assert cfg.review.require_card is True


def test_auto_tasks_and_review_channel(tmp_path):
    cfg = load_config(_write_cfg(tmp_path,
        "telegram: {channel_id: '@chan', review_channel_id: '-100500'}\n"
        "auto_tasks:\n"
        "  - id: ac\n"
        "    filter: {categories: [2, 6, 7]}\n"
        "    count: 2\n"
        "    times: ['10:00', '14:00']\n"))
    assert cfg.telegram.review_channel_id == "-100500"
    assert cfg.auto_tasks == [{"id": "ac", "filter": {"categories": [2, 6, 7]},
                               "count": 2, "times": ["10:00", "14:00"]}]


def test_auto_tasks_default_empty(tmp_path):
    cfg = load_config(_write_cfg(tmp_path, "telegram: {channel_id: '@chan'}\n"))
    assert cfg.auto_tasks == []
    assert cfg.telegram.review_channel_id == ""


def test_example_config_loads(tmp_path):
    # реальный пример из репозитория должен парситься без ошибок
    from pathlib import Path
    example = Path(__file__).parent.parent / "examples" / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.content.caption_max == 1024
    assert cfg.cards.require_for_publish is True
    assert cfg.default_card_mode == "mcp"
