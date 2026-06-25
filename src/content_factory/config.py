"""Конфиг Контент-завода. Реальные секреты — в .env (gitignored), НЕ в yaml.
Структура файла — см. examples/config.example.yaml."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from content_factory.pricing.pricing import PricingConfig
from content_factory.content.cards import CardConfig
from content_factory.content.descriptions import load_descriptions
from content_factory.ingest.normalize import CatalogFilter


@dataclass
class SourceConfig:
    """Источник контента — БД oasis: склад + фильтр категорий/исключений."""
    warehouse: str = "Симферополь"
    catalog: CatalogFilter = field(
        default_factory=lambda: CatalogFilter(report_category_ids=[2, 6, 7],
                                              exclude_title_patterns=[]))


@dataclass
class ContentConfig:
    """Параметры краткого описания для Telegram-подписи."""
    caption_max: int = 1024
    stop_words: list = field(default_factory=list)
    descriptions: dict = field(default_factory=dict)   # {series_key: ручной текст}


@dataclass
class FotogenConfigYaml:
    """Параметры очереди фотоагента из yaml; токен/пути/chat_id — из .env (см. cards_run)."""
    api_url: str = "http://127.0.0.1:8765"
    per_run: int = 10
    max_pending: int = 12
    max_total: int = 100000


@dataclass
class TelegramConfig:
    channel_id: str = ""              # боевой канал (бот — админ); токен в .env
    test_channel_id: str = ""         # тестовый канал/личка для прогона
    min_seconds_between_posts: int = 180
    parse_mode: str = "HTML"


@dataclass
class ReviewConfig:
    """Границы детерминированной ревизии (без LLM)."""
    price_min: int = 0
    price_max: int = 1_000_000_000
    require_specs: bool = True
    require_card: bool = True
    caption_max: int = 1024


@dataclass
class StateConfig:
    db: str = "state/content_factory.db"
    card_jobs_db: str = "state/card_jobs.db"


@dataclass
class AppConfig:
    source: SourceConfig
    pricing: PricingConfig
    content: ContentConfig
    cards: CardConfig                 # переиспользуем тип движка (resolve_photos/has_card)
    default_card_mode: str            # стиль карточки по умолчанию (cards.default_mode)
    fotogen: FotogenConfigYaml
    telegram: TelegramConfig
    review: ReviewConfig
    state: StateConfig


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    s = d.get("source", {})
    source = SourceConfig(
        warehouse=s.get("warehouse", "Симферополь"),
        catalog=CatalogFilter(
            report_category_ids=s.get("categories", [2, 6, 7]),
            exclude_title_patterns=s.get("exclude_title_patterns", []) or []))

    p = d.get("pricing", {})
    pricing = PricingConfig(default_markup_pct=p.get("default_markup_pct", 5),
                            min_margin_abs=p.get("min_margin_abs", 0),
                            rounding=p.get("rounding", "up_to_90"),
                            rules=p.get("rules", []) or [])

    cc = d.get("content", {})
    manifest = cc.get("descriptions_manifest", "")
    # путь к манифесту — относительно директории конфига
    descriptions = load_descriptions(path.parent / manifest) if manifest else {}
    content = ContentConfig(caption_max=cc.get("caption_max", 1024),
                            stop_words=cc.get("stop_words", []) or [],
                            descriptions=descriptions)

    cd = d.get("cards", {})
    cards = CardConfig(enabled=True, dir=cd.get("dir", ""),
                       base_url=cd.get("base_url", ""),
                       exts=cd.get("exts", [".jpg", ".jpeg", ".png"]),
                       require_for_publish=bool(cd.get("require_for_publish", True)))
    default_card_mode = cd.get("default_mode", "mcp")

    fg = d.get("fotogen", {})
    fotogen = FotogenConfigYaml(api_url=fg.get("api_url", "http://127.0.0.1:8765"),
                                per_run=fg.get("per_run", 10),
                                max_pending=fg.get("max_pending", 12),
                                max_total=fg.get("max_total", 100000))

    tg = d.get("telegram", {})
    telegram = TelegramConfig(channel_id=tg.get("channel_id", "") or "",
                              test_channel_id=tg.get("test_channel_id", "") or "",
                              min_seconds_between_posts=tg.get("min_seconds_between_posts", 180),
                              parse_mode=tg.get("parse_mode", "HTML"))

    rv = d.get("review", {})
    review = ReviewConfig(price_min=rv.get("price_min", 0),
                          price_max=rv.get("price_max", 1_000_000_000),
                          require_specs=bool(rv.get("require_specs", True)),
                          require_card=bool(rv.get("require_card", True)),
                          caption_max=rv.get("caption_max", content.caption_max))

    st = d.get("state", {})
    state = StateConfig(db=st.get("db", "state/content_factory.db"),
                        card_jobs_db=st.get("card_jobs_db", "state/card_jobs.db"))

    return AppConfig(source=source, pricing=pricing, content=content, cards=cards,
                     default_card_mode=default_card_mode, fotogen=fotogen,
                     telegram=telegram, review=review, state=state)
