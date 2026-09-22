"""Атрибуция, метрики, отчётность и уровни автономности VK."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from decouple import config

from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import send_message
from content_factory.publish.vk import VK_API, VK_MESSAGE_MAX, adapt_vk_text


LEVELS = ("L0", "L1", "L2", "L3")
LOW_RISK_TYPES = ("useful", "service", "trust")

EDITORIAL_CATALOG_DESTINATIONS = {
    "air_conditioners": ("air_conditioners", "кондиционеры"),
    "heat_pumps": ("heat_pumps", "тепловые насосы"),
    "recuperators": ("recuperators", "рекуператоры"),
    "ventilation": ("ventilation", "вентиляцию"),
    "stabilizers": ("stabilizers", "стабилизаторы"),
    "ups": ("ups", "источники бесперебойного питания"),
}


@dataclass(frozen=True)
class Publication:
    plan_id: int
    source_key: str
    post_id: int
    due_at: int
    category: str
    content_type: str
    tracked_url: str


@dataclass(frozen=True)
class Metric:
    plan_id: int
    views: int
    reach: int | None
    likes: int
    comments: int
    reposts: int


def campaign_url(plan_id: int, *, base_url: str = "https://splithome.ru/") -> str:
    query = urlencode({
        "utm_source": "vk",
        "utm_medium": "organic_social",
        "utm_campaign": "content_factory",
        "utm_content": f"vkp_{int(plan_id)}",
    })
    return f"{base_url.rstrip('/')}/?{query}"


def campaign_short_url(plan_id: int, *, base_url: str = "https://splithome.ru/",
                       intent: str = "") -> str:
    """Публичная короткая ссылка; сайт разворачивает её в UTM-адрес."""
    prefixes = {
        "service": "svc", "air_conditioners": "ac", "heat_pumps": "hp",
        "recuperators": "rc", "ventilation": "vt", "stabilizers": "st",
        "ups": "ups",
    }
    prefix = prefixes.get(intent, "vk")
    return f"{base_url.rstrip('/')}/go/{prefix}{int(plan_id)}"


def editorial_destination(category: str, content_type: str) -> str:
    """Выбрать одну клиентскую цель для нетоварного материала."""
    if content_type == "service":
        return "service"
    destination = EDITORIAL_CATALOG_DESTINATIONS.get(category)
    return destination[0] if destination else ""


def tracked_caption(caption: str, plan_id: int, *, source_key: str = "",
                    order_bot: str = "", links: OrderLinks | None = None,
                    base_url: str = "https://splithome.ru/",
                    catalog_base_url: str = "https://climat-simf.ru/",
                    editorial_destination: str = "") -> tuple[str, str]:
    """Добавить ровно один релевантный переход к реально работающей цели.

    Сервисный материал ведёт на форму обслуживания, тематический материал —
    в соответствующий раздел ассортимента, а товар — в заказ Telegram.
    """
    content_id = f"vkp_{int(plan_id)}"
    tracked_url = campaign_url(plan_id, base_url=base_url)
    lines = [line for line in adapt_vk_text(caption).splitlines()
             if not line.strip().casefold().startswith("источник:")
             and line.strip().casefold() not in {
                 "🌐 splithome.ru", "🌐 https://splithome.ru/", "splithome.ru",
             }]
    editorial = source_key.startswith("editorial:")
    body = "\n".join(lines).strip()
    footer = []
    if editorial and editorial_destination == "service":
        footer.append(
            "🛠 Записаться на обслуживание: "
            f"{campaign_short_url(plan_id, base_url=base_url, intent='service')}"
        )
    elif editorial and editorial_destination:
        label = next(
            (title for key, title in EDITORIAL_CATALOG_DESTINATIONS.values()
             if key == editorial_destination),
            "технику",
        )
        footer.append(
            f"🔎 Смотреть {label}: "
            f"{campaign_short_url(plan_id, base_url=catalog_base_url, intent=editorial_destination)}"
        )
    if source_key and order_bot and links is not None and not editorial:
        code = links.code_for_context(
            source_key, origin="vk", content_id=content_id,
        )
        footer.append(f"📩 Заказать: https://t.me/{order_bot.lstrip('@')}?start=ord_{code}")
    if not footer:
        return body[:VK_MESSAGE_MAX].rstrip(), tracked_url
    suffix = "\n".join(footer)
    room = VK_MESSAGE_MAX - len(suffix) - 2
    return f"{body[:room].rstrip()}\n\n{suffix}", tracked_url


class VkAnalyticsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_publications ("
                "plan_id INTEGER PRIMARY KEY,source_key TEXT NOT NULL,post_id INTEGER NOT NULL,"
                "due_at INTEGER NOT NULL,category TEXT NOT NULL,content_type TEXT NOT NULL,"
                "tracked_url TEXT NOT NULL,created_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_metrics ("
                "plan_id INTEGER NOT NULL,collected_at INTEGER NOT NULL,views INTEGER NOT NULL,"
                "reach INTEGER,likes INTEGER NOT NULL,comments INTEGER NOT NULL,"
                "reposts INTEGER NOT NULL,PRIMARY KEY(plan_id,collected_at))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_runtime_settings ("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO vk_runtime_settings(key,value,updated_at) "
                "VALUES('autonomy_level','L1',?)", (int(time.time()),)
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_cycle_health ("
                "ts INTEGER NOT NULL,ok INTEGER NOT NULL,error_count INTEGER NOT NULL)"
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    def level(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM vk_runtime_settings WHERE key='autonomy_level'",
            ).fetchone()
        return row[0] if row and row[0] in LEVELS else "L1"

    def set_level(self, level: str, *, now: int | None = None) -> None:
        value = str(level).upper()
        if value not in LEVELS:
            raise ValueError(f"Уровень должен быть одним из: {', '.join(LEVELS)}")
        if value == "L3" and not self.l3_eligible(now=now):
            raise ValueError("L3 недоступен: требуется минимум 14 дней без ошибок")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vk_runtime_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                ("autonomy_level", value, int(now or time.time())),
            )

    def l3_eligible(self, *, now: int | None = None, minimum_days: int = 14) -> bool:
        current = int(now or time.time())
        cutoff = current - int(minimum_days) * 86400
        with self._connect() as connection:
            oldest = connection.execute(
                "SELECT MIN(ts) FROM vk_cycle_health WHERE ok=1",
            ).fetchone()[0]
            failures = connection.execute(
                "SELECT COUNT(*) FROM vk_cycle_health WHERE ts>=? AND ok=0", (cutoff,),
            ).fetchone()[0]
        return oldest is not None and int(oldest) <= cutoff and int(failures) == 0

    def record_cycle(self, error_count: int, *, now: int | None = None) -> bool:
        """Вернуть True, если три ошибки подряд впервые принудительно включили L0."""
        ts = int(now or time.time())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vk_cycle_health(ts,ok,error_count) VALUES(?,?,?)",
                (ts, int(int(error_count) == 0), int(error_count)),
            )
            recent = connection.execute(
                "SELECT ok FROM vk_cycle_health ORDER BY ts DESC,rowid DESC LIMIT 3",
            ).fetchall()
        if len(recent) == 3 and all(int(row[0]) == 0 for row in recent) and self.level() != "L0":
            self.set_level("L0", now=ts)
            return True
        return False

    def record_publication(self, publication: Publication, *, now: int | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vk_publications(plan_id,source_key,post_id,due_at,category,"
                "content_type,tracked_url,created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET post_id=excluded.post_id,"
                "tracked_url=excluded.tracked_url",
                (*publication.__dict__.values(), int(now or time.time())),
            )

    def due_publications(self, now: int | None = None) -> list[Publication]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_id,source_key,post_id,due_at,category,content_type,tracked_url "
                "FROM vk_publications WHERE due_at<=? ORDER BY due_at",
                (int(now or time.time()),),
            ).fetchall()
        return [Publication(*row) for row in rows]

    def bootstrap_plan_publications(self, *, now: int | None = None) -> int:
        """Подхватить созданные до включения аналитики записи без создания дублей."""
        created = 0
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id,source_key,vk_post_id,due_at,category,content_type "
                    "FROM vk_content_plan WHERE vk_post_id IS NOT NULL",
                ).fetchall()
                for plan_id, source_key, post_id, due_at, category, content_type in rows:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO vk_publications(plan_id,source_key,post_id,due_at,"
                        "category,content_type,tracked_url,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (plan_id, source_key, post_id, due_at, category, content_type,
                         campaign_url(plan_id), int(now or time.time())),
                    )
                    created += int(cursor.rowcount)
        except sqlite3.OperationalError:
            return 0
        return created

    def add_metric(self, metric: Metric, *, now: int | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO vk_metrics(plan_id,collected_at,views,reach,likes,"
                "comments,reposts) VALUES(?,?,?,?,?,?,?)",
                (metric.plan_id, int(now or time.time()), metric.views, metric.reach,
                 metric.likes, metric.comments, metric.reposts),
            )

    def weekly_totals(self, start: int, end: int) -> dict[str, int | None]:
        with self._connect() as connection:
            row = connection.execute(
                "WITH latest AS (SELECT m.* FROM vk_metrics m JOIN ("
                "SELECT plan_id,MAX(collected_at) collected_at FROM vk_metrics GROUP BY plan_id"
                ") x ON x.plan_id=m.plan_id AND x.collected_at=m.collected_at) "
                "SELECT COUNT(p.plan_id),COALESCE(SUM(l.views),0),SUM(l.reach),"
                "COALESCE(SUM(l.likes),0),COALESCE(SUM(l.comments),0),"
                "COALESCE(SUM(l.reposts),0) FROM vk_publications p "
                "LEFT JOIN latest l ON l.plan_id=p.plan_id WHERE p.due_at>=? AND p.due_at<?",
                (int(start), int(end)),
            ).fetchone()
        return dict(zip(("posts", "views", "reach", "likes", "comments", "reposts"), row))


class VkMetricsClient:
    def __init__(self, token: str, owner_id: int, *, api_version: str = "5.199",
                 http: httpx.Client | None = None):
        self.token = token
        self.owner_id = int(owner_id)
        self.api_version = api_version
        self.http = http or httpx.Client(timeout=30, follow_redirects=True)

    def _api(self, method: str, data: dict) -> object:
        response = self.http.post(
            f"{VK_API}/{method}",
            data={**data, "access_token": self.token, "v": self.api_version},
        )
        response.raise_for_status()
        body = response.json() or {}
        if body.get("error"):
            error = body["error"]
            raise RuntimeError(f"VK {error.get('error_code')}: {error.get('error_msg')}")
        return body.get("response")

    def post_metric(self, publication: Publication) -> Metric | None:
        response = self._api(
            "wall.getById", {"posts": f"{self.owner_id}_{publication.post_id}"},
        )
        posts = response.get("items", []) if isinstance(response, dict) else (response or [])
        if not posts:
            return None
        post = posts[0]
        reach = None
        try:
            raw_reach = self._api(
                "stats.getPostReach",
                {"owner_id": self.owner_id, "post_ids": publication.post_id},
            )
            first = (raw_reach or [None])[0]
            if isinstance(first, dict):
                reach = int(first.get("reach_total", 0) or 0)
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError):
            pass
        return Metric(
            plan_id=publication.plan_id,
            views=int((post.get("views") or {}).get("count", 0) or 0),
            reach=reach,
            likes=int((post.get("likes") or {}).get("count", 0) or 0),
            comments=int((post.get("comments") or {}).get("count", 0) or 0),
            reposts=int((post.get("reposts") or {}).get("count", 0) or 0),
        )

    def recent_incoming_conversations(self, start: int) -> int | None:
        try:
            response = self._api(
                "messages.getConversations", {"count": 200, "filter": "all"},
            )
            items = response.get("items", []) if isinstance(response, dict) else []
            return sum(
                int((item.get("last_message") or {}).get("date", 0)) >= int(start)
                and not bool((item.get("last_message") or {}).get("out"))
                for item in items
            )
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError):
            return None


def collect(store: VkAnalyticsStore, client: VkMetricsClient,
            *, now: int | None = None) -> dict:
    result = {"collected": 0, "missing": 0, "errors": []}
    for publication in store.due_publications(now):
        try:
            metric = client.post_metric(publication)
            if metric is None:
                result["missing"] += 1
                continue
            store.add_metric(metric, now=now)
            result["collected"] += 1
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            result["errors"].append(f"post {publication.post_id}: {exc}")
    return result


def attributed_counts(source_db: str | Path, start: int, end: int) -> tuple[int, int]:
    try:
        with sqlite3.connect(source_db) as connection:
            clicks = connection.execute(
                "SELECT COUNT(*) FROM order_clicks WHERE origin='vk' AND ts>=? AND ts<?",
                (int(start), int(end)),
            ).fetchone()[0]
            leads = connection.execute(
                "SELECT COUNT(*) FROM leads WHERE origin='vk' AND ts>=? AND ts<?",
                (int(start), int(end)),
            ).fetchone()[0]
        return int(clicks), int(leads)
    except sqlite3.OperationalError:
        return 0, 0


def weekly_report(store: VkAnalyticsStore, source_db: str | Path,
                  client: VkMetricsClient | None = None,
                  *, now: datetime | None = None) -> str:
    end_dt = now or datetime.now()
    start_dt = end_dt - timedelta(days=7)
    start, end = int(start_dt.timestamp()), int(end_dt.timestamp())
    totals = store.weekly_totals(start, end)
    clicks, leads = attributed_counts(source_db, start, end)
    messages = client.recent_incoming_conversations(start) if client else None
    reach = "нет доступа" if totals["reach"] is None else str(totals["reach"])
    message_count = "нет доступа" if messages is None else str(messages)
    conversion = f"{(leads / clicks * 100):.1f}%" if clicks else "—"
    return "\n".join([
        f"📊 VK за 7 дней · {start_dt:%d.%m}–{end_dt:%d.%m}",
        f"Уровень автономности: {store.level()}",
        f"Публикации: {totals['posts']}",
        f"Просмотры: {totals['views']} · охват: {reach}",
        f"Реакции: {totals['likes']} · комментарии: {totals['comments']} · репосты: {totals['reposts']}",
        f"Входящие диалоги: {message_count}",
        f"Переходы в Telegram: {clicks} · заявки: {leads} · конверсия: {conversion}",
        "UTM: vk / organic_social / content_factory",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аналитика и автономность VK")
    parser.add_argument("action", nargs="?", choices=("collect", "report", "set-level"),
                        default="collect")
    parser.add_argument("level", nargs="?")
    parser.add_argument("--state-db", default=os.getenv(
        "VK_PLAN_STATE_DB", "/opt/content-factory-vk/state/vk-plan.db"))
    parser.add_argument("--source-db", default=os.getenv(
        "CF_SOURCE_DB", "/opt/content-factory/state/content_factory.db"))
    parser.add_argument("--owner-id", type=int,
                        default=int(os.getenv("VK_OWNER_ID", "-241020718")))
    args = parser.parse_args(argv)
    store = VkAnalyticsStore(args.state_db)
    bootstrapped = store.bootstrap_plan_publications()
    if args.action == "set-level":
        if not args.level:
            parser.error("для set-level нужен L0, L1, L2 или L3")
        store.set_level(args.level)
        print(json.dumps({"autonomy_level": store.level(), "bootstrapped": bootstrapped},
                         ensure_ascii=False))
        return 0

    client = VkMetricsClient(config("VK_ACCESS_TOKEN", default=""), args.owner_id)
    collected = collect(store, client)
    if args.action == "report":
        report = weekly_report(store, args.source_db, client)
        sent = send_message(
            config("TELEGRAM_BOT_TOKEN", default=""),
            config("TELEGRAM_REVIEW_CHANNEL_ID", default=""), report,
        )
        print(json.dumps({**collected, "bootstrapped": bootstrapped,
                          "report_sent": bool(sent)}, ensure_ascii=False))
    else:
        print(json.dumps({**collected, "bootstrapped": bootstrapped}, ensure_ascii=False))
    return 0 if not collected["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
