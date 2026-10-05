"""Конфиг Контент-завода. Реальные секреты — в .env (gitignored), НЕ в yaml.
Структура файла — см. examples/config.example.yaml."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from content_factory.pricing.pricing import PricingConfig
from content_factory.content.cards import CardConfig
from content_factory.content.descriptions import load_descriptions
from content_factory.content.sizing import load_power_map, set_power_map
from content_factory.ingest.normalize import CatalogFilter


@dataclass
class SourceConfig:
    """Источник контента: БД oasis (по умолчанию) либо read-only API витрины."""
    kind: str = "oasis"
    warehouse: str = "Симферополь"
    api_url: str = ""
    token_env: str = "TENDER_SUPPLIER_API_TOKEN"
    queries: list[str] = field(default_factory=list)
    limit_per_query: int = 20
    category_id: int | None = None
    available_only: bool = True
    enrich_product_pages: bool = True
    timeout_seconds: float = 30.0
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
    review_channel_id: str = ""       # закрытый ревью-канал (превью ✅/❌); пусто = личка владельца
    order_bot: str = ""               # username бота для кнопки «📩 Заказать» (пусто = без кнопки)
    min_seconds_between_posts: int = 180
    parse_mode: str = "HTML"


@dataclass
class VkConfig:
    """VK Wall API. Токен берётся только из переменной token_env."""
    enabled: bool = False
    app_id: int = 0
    owner_id: int = 0
    token_env: str = "VK_ACCESS_TOKEN"
    redirect_uri: str = ""
    token_store: str = "state/vk-tokens.json"
    share_url: str = ""
    public_image_base_url: str = ""
    api_version: str = "5.199"
    dry_run: bool = True


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
    cards_modes_by_category: dict     # {category_id(int): mode} — авто-выбор стиля по категории
    fotogen: FotogenConfigYaml
    telegram: TelegramConfig
    vk: VkConfig
    review: ReviewConfig
    state: StateConfig
    auto_tasks: list = field(default_factory=list)   # постоянные авто-задачи (сырые dict из yaml;
                                                     # разбор — orchestrator/auto.py)
    channel_sync: dict = field(default_factory=dict) # «живой канал» (publish/channel_sync_run)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    s = d.get("source", {})
    source = SourceConfig(
        kind=str(s.get("kind", "oasis")),
        warehouse=s.get("warehouse", "Симферополь"),
        api_url=str(s.get("api_url", "") or ""),
        token_env=str(s.get("token_env", "TENDER_SUPPLIER_API_TOKEN")),
        queries=[str(x) for x in (s.get("queries", []) or [])],
        limit_per_query=max(1, min(int(s.get("limit_per_query", 20)), 20)),
        category_id=(int(s["category_id"]) if s.get("category_id") is not None else None),
        available_only=bool(s.get("available_only", True)),
        enrich_product_pages=bool(s.get("enrich_product_pages", True)),
        timeout_seconds=float(s.get("timeout_seconds", 30.0)),
        catalog=CatalogFilter(
            report_category_ids=s.get("categories", [2, 6, 7]),
            exclude_title_patterns=s.get("exclude_title_patterns", []) or []))

    p = d.get("pricing", {})
    pricing = PricingConfig(default_markup_pct=p.get("default_markup_pct", 5),
                            min_margin_abs=p.get("min_margin_abs", 0),
                            rounding=p.get("rounding", "up_to_90"),
                            prefer_retail_ref=bool(p.get("prefer_retail_ref", False)),
                            rules=p.get("rules", []) or [])

    cc = d.get("content", {})
    manifest = cc.get("descriptions_manifest", "")
    # путь к манифесту — относительно директории конфига
    descriptions = load_descriptions(path.parent / manifest) if manifest else {}

    # power_map: ручной маппинг «код номенклатуры → типоразмер» (см. sizing.size_for).
    # Ставим в реестр процесса здесь — все раннеры проходят через load_config.
    pm_name = cc.get("power_map", "power_map.yaml")
    pm_path = path.parent / pm_name
    set_power_map(load_power_map(pm_path) if pm_path.exists() else {})
    content = ContentConfig(caption_max=cc.get("caption_max", 1024),
                            stop_words=cc.get("stop_words", []) or [],
                            descriptions=descriptions)

    cd = d.get("cards", {})
    cards = CardConfig(enabled=True, dir=cd.get("dir", ""),
                       base_url=cd.get("base_url", ""),
                       exts=cd.get("exts", [".jpg", ".jpeg", ".png"]),
                       require_for_publish=bool(cd.get("require_for_publish", True)),
                       reference_dir=str(cd.get("reference_dir", "state/product-references")),
                       trusted_image_domains=[str(x).lower() for x in
                                              (cd.get("trusted_image_domains", []) or [])])
    default_card_mode = cd.get("default_mode", "mcp")
    # карта category_id→mode: ключи приводим к int (category_id из oasis — целые)
    modes_by_category = {int(k): str(v) for k, v in (cd.get("modes_by_category") or {}).items()}

    fg = d.get("fotogen", {})
    fotogen = FotogenConfigYaml(api_url=fg.get("api_url", "http://127.0.0.1:8765"),
                                per_run=fg.get("per_run", 10),
                                max_pending=fg.get("max_pending", 12),
                                max_total=fg.get("max_total", 100000))

    tg = d.get("telegram", {})
    telegram = TelegramConfig(channel_id=tg.get("channel_id", "") or "",
                              test_channel_id=tg.get("test_channel_id", "") or "",
                              review_channel_id=str(tg.get("review_channel_id", "") or ""),
                              order_bot=str(tg.get("order_bot", "") or "").lstrip("@"),
                              min_seconds_between_posts=tg.get("min_seconds_between_posts", 180),
                              parse_mode=tg.get("parse_mode", "HTML"))

    vk_raw = d.get("vk", {}) or {}
    vk = VkConfig(enabled=bool(vk_raw.get("enabled", False)),
                  app_id=int(vk_raw.get("app_id", 0) or 0),
                  owner_id=int(vk_raw.get("owner_id", 0) or 0),
                  token_env=str(vk_raw.get("token_env", "VK_ACCESS_TOKEN")),
                  redirect_uri=str(vk_raw.get("redirect_uri", "") or ""),
                  token_store=str(vk_raw.get("token_store", "state/vk-tokens.json")),
                  share_url=str(vk_raw.get("share_url", "") or ""),
                  public_image_base_url=str(vk_raw.get("public_image_base_url", "") or ""),
                  api_version=str(vk_raw.get("api_version", "5.199")),
                  dry_run=bool(vk_raw.get("dry_run", True)))

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
                     default_card_mode=default_card_mode,
                     cards_modes_by_category=modes_by_category, fotogen=fotogen,
                     telegram=telegram, vk=vk, review=review, state=state,
                     auto_tasks=d.get("auto_tasks", []) or [],
                     channel_sync=d.get("channel_sync", {}) or {})
