"""Импорт готовых карточек Avito в СУЩЕСТВУЮЩУЮ очередь подтверждения Content Factory.

Что делает: проверенный манифест (`avito_manifest`) → копия карточки в стабильный каталог →
подпись (`avito_caption`) → запись в `ConfirmStore` (pending, целевой канал публикации) →
короткий код `OrderLinks` → отправка превью ТОЛЬКО в review-чат с кнопками approve/reject.

Чего НЕ делает принципиально: не публикует в канал, не нажимает approve, не пишет
`PublishState`, не трогает строки в статусах pending/rejected/published, не поллит бота,
не считает наценку (цена берётся из манифеста как конечная).

Почему свой отправитель, а не `publish/telegram.py::publish_post`: тому нужен ответ на
два вопроса, которых он не даёт — точное `parameters.retry_after` при 429 и различие
«точно не доставлено» (ConnectError) против «неизвестно» (таймаут после отправки).
`publish_post` отдаёт только строку ошибки и ретраит транспортные сбои, что для review-
отправки недопустимо. Существующий публикатор при этом не меняется: его использует
approve-путь бота, который должен остаться нетронутым.

Разделение статусов (важно для approve): при НЕОДНОЗНАЧНОЙ отправке строка ConfirmStore
остаётся `pending` — сообщение могло дойти, и кнопка обязана работать; неоднозначность
фиксируется в журнале доставок и блокирует любую автоматическую переотправку."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from content_factory.avito_caption import REVIEW_NOTE, build_caption
from content_factory.ingest.avito_manifest import (
    ManifestError, image_kind, load_manifest, sha256_bytes,
)
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import (
    TG_API, PublishState, edit_caption, edit_post_media,
)

DEFAULT_LIMIT = 12                 # партиями по-человечески: review-лента читаема
DEFAULT_MIN_INTERVAL = 3.0         # анти-флуд Telegram
MAX_CONSECUTIVE_FAILURES = 3       # «ломается всё» → останов, а не 175 одинаковых ошибок
MAX_RETRY_AFTER = 60.0
BLOCKING_CONFIRM_STATUSES = {"pending", "published", "rejected"}
BLOCKING_DELIVERY_STATUSES = {"reserved", "sending", "sent", "send_unverified"}
# Статусы, требующие решения оператора: неоднозначная доставка и следы прерванного
# прогона (иначе `reserved`/`sending` блокировали бы товар молча и навсегда).
NEEDS_ATTENTION_STATUSES = ("send_unverified", "sending", "reserved")


class PreflightError(RuntimeError):
    """Конфигурация небезопасна — до сети дело не доходит."""


@dataclass
class Delivery:
    import_key: str
    product_key: str
    batch_id: str
    status: str
    review_chat: str = ""
    review_message_id: int | None = None
    error: str = ""
    created_ts: float = 0.0
    updated_ts: float = 0.0


class DeliveryJournal:
    """Журнал доставок review в общей state-БД.

    Нужен потому, что `ConfirmStore` не хранит review `message_id` и не различает ревизии.
    Уникальный `import_key` — авторитетная защита от повторной отправки (в т.ч. при
    конкурентных запусках): резервирование делается одним INSERT OR IGNORE."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS avito_review_deliveries ("
                      "import_key TEXT PRIMARY KEY, product_key TEXT, batch_id TEXT, "
                      "status TEXT, review_chat TEXT DEFAULT '', "
                      "review_message_id INTEGER, error TEXT DEFAULT '', "
                      "created_ts REAL, updated_ts REAL)")
            c.execute("CREATE INDEX IF NOT EXISTS avito_deliveries_product "
                      "ON avito_review_deliveries(product_key)")

    def _c(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _row(r) -> Delivery:
        return Delivery(import_key=r[0], product_key=r[1], batch_id=r[2] or "",
                        status=r[3] or "", review_chat=r[4] or "", review_message_id=r[5],
                        error=r[6] or "", created_ts=r[7] or 0.0, updated_ts=r[8] or 0.0)

    _SELECT = ("SELECT import_key, product_key, batch_id, status, review_chat, "
               "review_message_id, error, created_ts, updated_ts "
               "FROM avito_review_deliveries")

    def get(self, import_key: str) -> Delivery | None:
        with self._c() as c:
            row = c.execute(f"{self._SELECT} WHERE import_key=?", (import_key,)).fetchone()
        return self._row(row) if row else None

    def for_product(self, product_key: str) -> list[Delivery]:
        with self._c() as c:
            rows = c.execute(f"{self._SELECT} WHERE product_key=? ORDER BY created_ts",
                             (product_key,)).fetchall()
        return [self._row(r) for r in rows]

    def needs_attention(self) -> list[Delivery]:
        placeholders = ",".join("?" * len(NEEDS_ATTENTION_STATUSES))
        with self._c() as c:
            rows = c.execute(f"{self._SELECT} WHERE status IN ({placeholders}) "
                             "ORDER BY created_ts", NEEDS_ATTENTION_STATUSES).fetchall()
        return [self._row(r) for r in rows]

    def reserve(self, import_key: str, product_key: str, batch_id: str) -> tuple[bool, str]:
        """Занять ревизию. → (получилось, статус-помеха). Повтор разрешён только после
        ОДНОЗНАЧНОГО отказа Telegram (`send_failed`)."""
        now = time.time()
        with self._c() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO avito_review_deliveries"
                "(import_key, product_key, batch_id, status, created_ts, updated_ts) "
                "VALUES(?,?,?,'reserved',?,?)",
                (import_key, product_key, batch_id, now, now))
            if cur.rowcount == 1:
                return True, ""
            cur = c.execute(
                "UPDATE avito_review_deliveries SET status='reserved', error='', "
                "updated_ts=? WHERE import_key=? AND status='send_failed'",
                (now, import_key))
            if cur.rowcount == 1:
                return True, ""
            row = c.execute("SELECT status FROM avito_review_deliveries WHERE import_key=?",
                            (import_key,)).fetchone()
        return False, (row[0] if row else "unknown")

    def mark(self, import_key: str, status: str, *, message_id: int | None = None,
             error: str = "", review_chat: str | None = None) -> None:
        with self._c() as c:
            c.execute("UPDATE avito_review_deliveries SET status=?, "
                      "review_message_id=COALESCE(?, review_message_id), error=?, "
                      "review_chat=COALESCE(?, review_chat), updated_ts=? "
                      "WHERE import_key=?",
                      (status, message_id, error or "", review_chat, time.time(),
                       import_key))


# ── отправка в review ────────────────────────────────────────────────────────────
@dataclass
class SendOutcome:
    status: str                    # sent | send_failed | send_unverified
    message_id: int | None = None
    error: str = ""
    waited: float = 0.0


def review_markup(code: str) -> str:
    """Те же callback-коды, что у существующего пульта: approve:<code> / reject:<code>."""
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"approve:{code}"},
        {"text": "❌ Отклонить", "callback_data": f"reject:{code}"},
    ]]}, ensure_ascii=False)


def _mime(path: Path) -> str:
    kind = image_kind(path.read_bytes()[:16])
    return "image/png" if kind == "png" else "image/jpeg"


def send_review_photo(http, token: str, chat_id: str, image: Path, caption: str, *,
                      reply_markup: str, parse_mode: str = "HTML",
                      sleep=time.sleep, max_retry_after: float = MAX_RETRY_AFTER
                      ) -> SendOutcome:
    """Одна попытка sendPhoto; повтор — ТОЛЬКО при явном 429 (Telegram отверг запрос).

    Классификация транспорта: не установили соединение → точно не доставлено (позиция
    остаётся retryable); таймаут/обрыв после отправки и 5xx → неизвестно (send_unverified,
    ручная сверка)."""
    url = f"{TG_API}/bot{token}/sendPhoto"
    waited = 0.0
    for attempt in (0, 1):
        try:
            blob = image.read_bytes()
        except OSError as exc:
            return SendOutcome("send_failed", error=f"io: {exc}", waited=waited)
        data = {"chat_id": str(chat_id), "caption": caption, "reply_markup": reply_markup}
        if parse_mode:
            data["parse_mode"] = parse_mode
        files = {"photo": (image.name, blob, _mime(image))}
        try:
            response = http.post(url, data=data, files=files)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return SendOutcome("send_failed", error=f"connect: {exc}", waited=waited)
        except (httpx.TimeoutException, httpx.RemoteProtocolError,
                httpx.WriteError, httpx.ReadError) as exc:
            return SendOutcome("send_unverified", error=f"transport: {exc}", waited=waited)
        except httpx.HTTPError as exc:
            return SendOutcome("send_unverified", error=f"http: {exc}", waited=waited)

        try:
            body = response.json() or {}
        except ValueError:
            body = {}
        status = response.status_code
        if status == 429:
            retry_after = float((body.get("parameters") or {}).get("retry_after") or 0)
            if attempt == 0 and 0 < retry_after <= max_retry_after:
                sleep(retry_after)
                waited += retry_after
                continue
            return SendOutcome("send_failed", error=f"429 retry_after={retry_after}",
                               waited=waited)
        if status >= 500:
            return SendOutcome("send_unverified", error=f"http {status}", waited=waited)
        if status != 200 or not body.get("ok"):
            return SendOutcome("send_failed",
                               error=str(body.get("description") or f"http {status}"),
                               waited=waited)
        message_id = (body.get("result") or {}).get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return SendOutcome("send_unverified", error="ответ без message_id", waited=waited)
        return SendOutcome("sent", message_id=message_id, waited=waited)
    return SendOutcome("send_unverified", error="исчерпаны попытки", waited=waited)


# ── предполётные проверки ────────────────────────────────────────────────────────
def normalize_chat(value) -> tuple[str, str]:
    """→ (форма, нормализованное значение). Форма: numeric | alias | empty."""
    raw = str(value or "").strip()
    if not raw:
        return "empty", ""
    if re.fullmatch(r"-?\d+", raw):
        return "numeric", raw
    alias = raw.casefold()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if alias.startswith(prefix):
            alias = alias[len(prefix):]
            break
    return "alias", alias.strip("/")


def channels_distinct(review, publish) -> tuple[bool, str]:
    """Различие review и боевого канала должно быть ДОКАЗУЕМО, а не предположено."""
    kind_r, norm_r = normalize_chat(review)
    kind_p, norm_p = normalize_chat(publish)
    if kind_r == "empty":
        return False, "review-канал не задан"
    if kind_p == "empty":
        return False, "канал публикации не задан (его надо записать в очередь подтверждения)"
    if (kind_r, norm_r) == (kind_p, norm_p):
        return False, "review-канал совпадает с каналом публикации"
    if kind_r != kind_p:
        return False, ("review и канал публикации заданы в разных формах (id против @alias) — "
                       "различие не доказуемо, приведите оба к одному виду")
    return True, ""


def preflight(*, token: str, review_chat: str, publish_channel: str, owner_chat: str,
              state_db, media_dir) -> None:
    if not str(token or "").strip():
        raise PreflightError("не задан TELEGRAM_BOT_TOKEN")
    ok, why = channels_distinct(review_chat, publish_channel)
    if not ok:
        raise PreflightError(why)
    if not str(owner_chat or "").strip():
        # bot/run.py: `if owner and frm != owner` — при пустом owner кнопку approve
        # может нажать любой участник review-канала.
        raise PreflightError("не задан TELEGRAM_OWNER_CHAT_ID/FOTOGEN_CHAT_ID: "
                             "owner-гейт кнопок в боте отключён")
    if not state_db:
        raise PreflightError("не задан --state-db")
    if not media_dir:
        raise PreflightError("не задан --media-dir")


def _mask(chat) -> str:
    raw = str(chat or "")
    return ("…" + raw[-4:]) if len(raw) > 4 else raw


# ── стабильная копия карточки ───────────────────────────────────────────────────
def stage_card_atomic(item, media_dir, batch_id: str) -> tuple[Path, Path]:
    """Готовая копия рядом с боевой, но НЕ на её месте. → (временный файл, боевой путь).

    Боевой файл читает бот при approve. Перезаписать его до подтверждения Telegram —
    значит получить review со старой картинкой и публикацию с новой."""
    final = staged_path(item, media_dir, batch_id)
    tmp = final.with_suffix(final.suffix + ".new")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(item.card_path, tmp)
    if sha256_bytes(tmp.read_bytes()) != item.card_sha256:
        tmp.unlink(missing_ok=True)
        raise OSError(f"копия карточки не совпала по хешу: {tmp}")
    return tmp, final


def staged_path(item, media_dir, batch_id: str) -> Path:
    stem = re.sub(r"[^\w.-]+", "_", item.product_key, flags=re.UNICODE)
    return (Path(media_dir) / batch_id /
            f"{stem}.{'png' if item.card_kind == 'png' else 'jpg'}")


def stage_card(item, media_dir, batch_id: str) -> Path:
    """Скопировать карточку в собственный каталог импортёра и перепроверить хеш.

    Копия, а не оригинал, потому что `bot/run.py::make_regen_fn` при команде /regen
    делает `unlink(card_path)`: оригинал готовой карточки, который запрещено
    перегенерировать, должен остаться нетронутым."""
    dest_dir = Path(media_dir) / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]+", "_", item.product_key, flags=re.UNICODE)
    dest = dest_dir / f"{stem}.{'png' if item.card_kind == 'png' else 'jpg'}"
    shutil.copyfile(item.card_path, dest)
    if sha256_bytes(dest.read_bytes()) != item.card_sha256:
        dest.unlink(missing_ok=True)
        raise OSError(f"копия карточки не совпала по хешу: {dest}")
    return dest.resolve()


# ── основной прогон ─────────────────────────────────────────────────────────────
def legacy_keys(item) -> tuple[str, ...]:
    """«Родные» ключи товара до этого импортёра: под series_key его кладёт в очередь
    старый конвейер, под sku — ручные сценарии. Дедуп обязан смотреть и на них: иначе
    товар, уже ждущий решения или опубликованный, получит второй пост под ключом avito|…"""
    return tuple(dict.fromkeys(k for k in (item.series_key, item.sku) if k))


def _sort_key(item):
    return (item.category, item.price_final, item.product_key)


def run_import(manifest_path, *, cards_root=None, state_db=None, media_dir=None,
               telegram_token: str = "", review_chat: str = "", publish_channel: str = "",
               owner_chat: str = "", dry_run: bool = True, limit: int = DEFAULT_LIMIT,
               min_interval: float = DEFAULT_MIN_INTERVAL, http=None, sleep=time.sleep,
               review_note: str = REVIEW_NOTE) -> dict:
    manifest = load_manifest(manifest_path, cards_root=cards_root)
    summary = {
        "batch_id": manifest.batch_id,
        "mode": "dry-run" if dry_run else "send-review",
        "review_chat": _mask(review_chat), "publish_channel": _mask(publish_channel),
        "prepared": [], "sent": [], "skipped": [], "failed": [], "unverified": [],
        "review_required": [], "stopped": None,
        # Отсев на сборке партии (нет карточки, не та категория, нет остатка):
        # он случился до манифеста, но в отчёт владельцу попасть обязан.
        "source_excluded": dict(manifest.excluded or {}),
    }
    summary["skipped"] += [{"key": s.key, "reason": s.reason, "detail": s.detail}
                           for s in manifest.skipped]

    selected = sorted(manifest.items, key=_sort_key)
    if limit and limit > 0:
        for item in selected[limit:]:
            summary["skipped"].append({"key": item.product_key, "reason": "batch_limit",
                                       "detail": f"лимит партии {limit}"})
        selected = selected[:limit]

    if not dry_run:
        preflight(token=telegram_token, review_chat=review_chat,
                  publish_channel=publish_channel, owner_chat=owner_chat,
                  state_db=state_db, media_dir=media_dir)

    store = links = pub_state = journal = None
    if not dry_run:
        store = ConfirmStore(state_db)
        links = OrderLinks(state_db)
        pub_state = PublishState(state_db)
        journal = DeliveryJournal(state_db)
    client = http if http is not None or dry_run else httpx.Client(timeout=60)
    owns_client = (not dry_run) and http is None
    consecutive_failures = 0
    sent_any = False
    try:
        for item in selected:
            caption = build_caption(item, review_note=review_note)
            if not caption.ok:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": caption.reason, "detail": ""})
                continue
            prepared = {"key": item.product_key, "import_key": item.import_key,
                        "category": item.category, "review_required": item.review_required,
                        "price": item.price_final, "price_kind": item.price_kind,
                        "models_shown": caption.models_shown,
                        "dropped_blocks": caption.dropped}
            if dry_run:
                # Текст поста в dry-run: владелец принимает партию по содержанию,
                # а не по счётчикам. В боевом прогоне подпись не дублируем в отчёт.
                summary["prepared"].append({**prepared, "caption": caption.caption})
                if item.review_required:
                    summary["review_required"].append(item.product_key)
                continue

            existing = store.get(item.product_key)
            if existing and existing.status in BLOCKING_CONFIRM_STATUSES:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": f"confirm_state:{existing.status}",
                                           "detail": ""})
                continue
            if pub_state.is_published(item.product_key):
                summary["skipped"].append({"key": item.product_key,
                                           "reason": "already_published", "detail": ""})
                continue
            blocked_by_legacy = None
            for legacy in legacy_keys(item):
                prior = store.get(legacy)
                if prior and prior.status in BLOCKING_CONFIRM_STATUSES:
                    blocked_by_legacy = (f"legacy_confirm_state:{prior.status}", legacy)
                    break
                if pub_state.is_published(legacy):
                    blocked_by_legacy = ("legacy_published", legacy)
                    break
            if blocked_by_legacy:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": blocked_by_legacy[0],
                                           "detail": blocked_by_legacy[1]})
                continue
            blocking = [d for d in journal.for_product(item.product_key)
                        if d.status in BLOCKING_DELIVERY_STATUSES]
            if blocking:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": f"delivery:{blocking[-1].status}",
                                           "detail": blocking[-1].import_key})
                continue
            reserved, blocker = journal.reserve(item.import_key, item.product_key,
                                                manifest.batch_id)
            if not reserved:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": f"revision:{blocker}",
                                           "detail": item.import_key})
                continue

            try:
                card_copy = stage_card(item, media_dir, manifest.batch_id)
            except OSError as exc:
                journal.mark(item.import_key, "send_failed", error=str(exc))
                summary["failed"].append({"key": item.product_key, "reason": "card_copy",
                                          "detail": str(exc)})
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    summary["stopped"] = "подряд несколько отказов — партия остановлена"
                    break
                continue

            if sent_any and min_interval > 0:
                sleep(min_interval)
            # Целевой канал в очереди — боевой: approve владельца опубликует именно туда.
            store.add(item.product_key, publish_channel, str(card_copy), caption.caption)
            code = links.code_for(item.product_key)
            journal.mark(item.import_key, "sending", review_chat=str(review_chat))
            outcome = send_review_photo(client, telegram_token, review_chat, card_copy,
                                        caption.review_caption,
                                        reply_markup=review_markup(code), sleep=sleep)
            sent_any = True
            if outcome.status == "sent":
                journal.mark(item.import_key, "sent", message_id=outcome.message_id)
                summary["sent"].append({**prepared, "message_id": outcome.message_id,
                                        "card_path": str(card_copy)})
                if item.review_required:
                    summary["review_required"].append(item.product_key)
                consecutive_failures = 0
                continue
            if outcome.status == "send_failed":
                # Однозначный отказ: сообщения нет. Снимаем строку с pending, чтобы
                # позиция осталась пригодной к повтору, но не публикуемой.
                store.mark(item.product_key, "send_failed")
                journal.mark(item.import_key, "send_failed", error=outcome.error)
                summary["failed"].append({"key": item.product_key, "reason": "telegram",
                                          "detail": outcome.error})
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    summary["stopped"] = "подряд несколько отказов Telegram — партия остановлена"
                    break
                continue
            # Неоднозначно: сообщение могло дойти. ConfirmStore остаётся pending (кнопка
            # обязана работать), автоматической переотправки не будет никогда.
            journal.mark(item.import_key, "send_unverified", error=outcome.error)
            summary["unverified"].append({"key": item.product_key,
                                          "import_key": item.import_key,
                                          "detail": outcome.error})
            summary["stopped"] = ("неоднозначная доставка — партия остановлена, "
                                  "нужна ручная сверка (--reconcile)")
            break
        summary["counts"] = {
            "manifest_items": len(manifest.items), "selected": len(selected),
            "prepared": len(summary["prepared"]), "sent": len(summary["sent"]),
            "skipped": len(summary["skipped"]), "failed": len(summary["failed"]),
            "unverified": len(summary["unverified"]),
            "review_required": len(summary["review_required"]),
        }
        if not dry_run:
            # Отчёт владельцу — последний и вспомогательный шаг: партия уже отправлена,
            # и сбой отчёта не должен выглядеть как провал прогона.
            ok, error = send_owner_report(client, telegram_token, owner_chat, summary)
            summary["report_sent"] = ok
            if error:
                summary["report_error"] = error
    finally:
        if owns_client and client is not None:
            client.close()
    return summary


def refresh_captions(manifest_path, *, cards_root=None, state_db=None, media_dir=None,
                     telegram_token: str = "", review_chat: str = "", dry_run: bool = True,
                     http=None, review_note: str = REVIEW_NOTE,
                     min_interval: float = DEFAULT_MIN_INTERVAL, sleep=time.sleep) -> dict:
    """Обновить уже отправленные в review карточки: подпись, а при необходимости и саму
    картинку.

    Нужно, когда исправление пришло задним числом: пост в review-чате и строка в
    `ConfirmStore` (её опубликует approve) остались бы со старым содержимым. Если
    карточку перерисовали, подписи мало — заменяем и изображение (`editMessageMedia`).
    Правится только то, по чему решение ЕЩЁ не принято: `published`/`rejected` не
    трогаем, это уже воля владельца."""
    manifest = load_manifest(manifest_path, cards_root=cards_root)
    summary = {"batch_id": manifest.batch_id,
               "mode": "dry-run" if dry_run else "refresh-captions",
               "updated": [], "unchanged": [], "skipped": [], "failed": []}
    store = ConfirmStore(state_db)
    journal = DeliveryJournal(state_db)
    links = OrderLinks(state_db)          # код кнопки детерминирован: тот же, что был
    client = http if http is not None or dry_run else httpx.Client(timeout=60)
    owns_client = (not dry_run) and http is None
    first = True
    try:
        for item in sorted(manifest.items, key=_sort_key):
            row = store.get(item.product_key)
            if row is None:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": "not_in_queue", "detail": ""})
                continue
            if row.status != "pending":
                summary["skipped"].append({"key": item.product_key,
                                           "reason": f"confirm_state:{row.status}",
                                           "detail": ""})
                continue
            caption = build_caption(item, review_note=review_note)
            if not caption.ok:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": caption.reason, "detail": ""})
                continue
            # Карточку сверяем по содержимому копии, которую видит бот: файл в каталоге
            # могли перерисовать, и тогда одной подписи мало.
            staged = Path(row.card_path) if row.card_path else None
            card_changed = True
            if staged and staged.is_file():
                card_changed = sha256_bytes(staged.read_bytes()) != item.card_sha256
            if caption.caption == row.caption and not card_changed:
                summary["unchanged"].append(item.product_key)
                continue
            delivery = next((d for d in reversed(journal.for_product(item.product_key))
                             if d.status == "sent" and d.review_message_id), None)
            if delivery is None:
                summary["skipped"].append({"key": item.product_key,
                                           "reason": "no_review_message", "detail": ""})
                continue
            if dry_run:
                summary["updated"].append({"key": item.product_key,
                                           "message_id": delivery.review_message_id,
                                           "media": card_changed,
                                           "caption": caption.caption})
                continue
            if not first and min_interval > 0:
                sleep(min_interval)
            first = False
            card_path = row.card_path
            if card_changed:
                base = media_dir or Path(row.card_path).parent.parent
                try:
                    tmp, final = stage_card_atomic(item, base, manifest.batch_id)
                except OSError as exc:
                    summary["failed"].append({"key": item.product_key,
                                              "reason": "card_copy", "detail": str(exc)})
                    continue
                result = edit_post_media(telegram_token, review_chat,
                                         delivery.review_message_id, str(tmp),
                                         caption.review_caption, parse_mode="HTML",
                                         reply_markup=review_markup(
                                             links.code_for(item.product_key)),
                                         http=client)
                ok, error, gone = result.ok, result.error, False
                if ok:
                    tmp.replace(final)          # боевой файл меняем только после успеха
                    card_path = str(final.resolve())
                else:
                    tmp.unlink(missing_ok=True)
            else:
                ok, error, gone = edit_caption(telegram_token, review_chat,
                                               delivery.review_message_id,
                                               caption.review_caption, parse_mode="HTML",
                                               http=client)
            if not ok:
                summary["failed"].append({"key": item.product_key, "reason": error or "?",
                                          "detail": "сообщение недоступно" if gone else ""})
                continue
            # Порядок важен: сначала подтверждённая правка в чате, потом состояние,
            # которое опубликует approve. Иначе владелец увидит один текст, а в канал
            # уйдёт другой.
            store.add(item.product_key, row.channel, card_path, caption.caption)
            summary["updated"].append({"key": item.product_key,
                                       "message_id": delivery.review_message_id,
                                       "media": card_changed})
    finally:
        if owns_client and client is not None:
            client.close()
    summary["counts"] = {"updated": len(summary["updated"]),
                         "unchanged": len(summary["unchanged"]),
                         "skipped": len(summary["skipped"]),
                         "failed": len(summary["failed"])}
    return summary


REPORT_LIMIT = 4096                # лимит sendMessage
_REPORT_KEYS_SHOWN = 10


def _reason_lines(rows, title: str) -> list[str]:
    """Причины — сводкой по видам, а не простынёй из сотни одинаковых строк."""
    if not rows:
        return []
    counts = {}
    for row in rows:
        counts[row.get("reason", "?")] = counts.get(row.get("reason", "?"), 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [title] + [f"  • {reason} — {count}" for reason, count in ordered]


def format_owner_report(summary: dict) -> str:
    """Короткая сводка прогона для лички владельца. Полные данные уходят файлом."""
    counts = summary.get("counts") or {}
    lines = [f"Партия {summary.get('batch_id', '?')} · {summary.get('mode', '?')}",
             f"Отправлено на подтверждение: {counts.get('sent', 0)} "
             f"из {counts.get('selected', 0)}"]
    if counts.get("failed"):
        lines.append(f"Ошибок отправки: {counts['failed']}")
    if counts.get("unverified"):
        lines.append(f"Неоднозначных (нужна сверка глазами): {counts['unverified']}")
        for row in summary.get("unverified", [])[:_REPORT_KEYS_SHOWN]:
            lines.append(f"  • {row.get('key')} ({row.get('import_key')})")
    if counts.get("review_required"):
        lines.append(f"Требуют отдельного решения: {counts['review_required']}")
        lines += [f"  • {key}" for key
                  in summary.get("review_required", [])[:_REPORT_KEYS_SHOWN]]
    lines += _reason_lines(summary.get("skipped", []),
                           f"Пропущено при импорте: {counts.get('skipped', 0)}")
    source = summary.get("source_excluded") or {}
    if source.get("count"):
        lines.append(f"Не вошло в партию: {source['count']}")
        lines += [f"  • {reason} — {count}"
                  for reason, count in (source.get("reasons") or {}).items()]
    lines += _reason_lines(summary.get("failed", []), "Причины ошибок:")
    if summary.get("stopped"):
        lines.append(f"Партия остановлена: {summary['stopped']}")
    text = "\n".join(lines)
    if len(text) > REPORT_LIMIT:                   # режем по строкам, не посреди числа
        cut, out = 0, []
        for line in lines:
            if cut + len(line) + 1 > REPORT_LIMIT - 20:
                out.append("…")
                break
            out.append(line)
            cut += len(line) + 1
        text = "\n".join(out)
    return text


def send_owner_report(http, token: str, owner_chat: str, summary: dict) -> tuple[bool, str]:
    """Сводка текстом + полный отчёт JSON-файлом. → (отправлено, ошибка)."""
    if http is None or not str(owner_chat or "").strip() or not str(token or "").strip():
        return False, "нет клиента/owner_chat/токена"
    try:
        response = http.post(f"{TG_API}/bot{token}/sendMessage",
                             data={"chat_id": str(owner_chat),
                                   "text": format_owner_report(summary)})
        body = response.json() or {}
        if response.status_code != 200 or not body.get("ok"):
            return False, body.get("description") or f"http {response.status_code}"
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return False, str(exc)
    try:
        blob = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
        http.post(f"{TG_API}/bot{token}/sendDocument",
                  data={"chat_id": str(owner_chat)},
                  files={"document": (f"{summary.get('batch_id', 'batch')}-report.json",
                                      blob, "application/json")})
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return True, f"сводка ушла, файл — нет: {exc}"    # главное уже доставлено
    return True, ""


def reconcile(state_db) -> dict:
    """Доставки, требующие ручной сверки глазами в review-канале: неоднозначные плюс
    следы прерванного прогона. Автоматической сверки нет намеренно: второй поллер
    бота запрещён."""
    journal = DeliveryJournal(state_db)
    return {"unverified": [{"import_key": d.import_key, "product_key": d.product_key,
                            "batch_id": d.batch_id, "status": d.status,
                            "review_chat": _mask(d.review_chat),
                            "error": d.error, "ts": d.updated_ts}
                           for d in journal.needs_attention()]}


def resolve_unverified(state_db, import_key: str, outcome: str,
                       message_id: int | None = None) -> dict:
    """Оператор посмотрел review-канал и сообщил факт.

    delivered — записываем реальный message_id, строка подтверждения остаётся pending;
    not-delivered — снимаем pending (если он ещё pending) и разрешаем повтор."""
    journal = DeliveryJournal(state_db)
    record = journal.get(import_key)
    if record is None or record.status not in NEEDS_ATTENTION_STATUSES:
        return {"ok": False, "error": f"нет доставки, требующей сверки: {import_key}"}
    if outcome == "delivered":
        if not isinstance(message_id, int) or message_id <= 0:
            return {"ok": False, "error": "для delivered нужен реальный --message-id"}
        journal.mark(import_key, "sent", message_id=message_id)
        return {"ok": True, "status": "sent", "product_key": record.product_key}
    if outcome == "not-delivered":
        store = ConfirmStore(state_db)
        current = store.get(record.product_key)
        if current and current.status == "pending":
            store.mark(record.product_key, "send_failed")
        journal.mark(import_key, "send_failed", error="оператор: не доставлено")
        return {"ok": True, "status": "send_failed", "product_key": record.product_key}
    return {"ok": False, "error": "outcome должен быть delivered | not-delivered"}


def _env(path: str):
    from decouple import Config, RepositoryEnv
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    return Config(RepositoryEnv(str(p)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Импорт готовых карточек Avito в очередь подтверждения (review).")
    parser.add_argument("--manifest")
    parser.add_argument("--cards-root")
    parser.add_argument("--state-db")
    parser.add_argument("--media-dir")
    parser.add_argument("--telegram-env", help="файл .env с токеном и id каналов")
    parser.add_argument("--send-review", action="store_true",
                        help="отправить превью в review-чат (без него — только dry-run)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL)
    parser.add_argument("--refresh-captions", action="store_true",
                        help="обновить подписи уже отправленных карточек "
                             "(без --send-review — dry-run)")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--resolve-unverified")
    parser.add_argument("--outcome", choices=("delivered", "not-delivered"))
    parser.add_argument("--message-id", type=int)
    args = parser.parse_args(argv)

    if args.reconcile:
        print(json.dumps(reconcile(args.state_db), ensure_ascii=False))
        return
    if args.resolve_unverified:
        result = resolve_unverified(args.state_db, args.resolve_unverified,
                                    args.outcome or "", args.message_id)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result.get("ok") else 1)

    token = review = channel = owner = ""
    if args.telegram_env:
        env = _env(args.telegram_env)
        token = env("TELEGRAM_BOT_TOKEN", default="")
        review = env("TELEGRAM_REVIEW_CHANNEL_ID", default="")
        channel = env("TELEGRAM_CHANNEL_ID", default="")
        owner = env("TELEGRAM_OWNER_CHAT_ID", default="") or env("FOTOGEN_CHAT_ID", default="")
    if args.refresh_captions:
        try:
            result = refresh_captions(args.manifest, cards_root=args.cards_root,
                                      state_db=args.state_db, media_dir=args.media_dir,
                                      telegram_token=token, review_chat=review,
                                      dry_run=not args.send_review,
                                      min_interval=args.min_interval)
        except (ManifestError, PreflightError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(2) from exc
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(1 if result["failed"] else 0)

    try:
        summary = run_import(args.manifest, cards_root=args.cards_root,
                             state_db=args.state_db, media_dir=args.media_dir,
                             telegram_token=token, review_chat=review,
                             publish_channel=channel, owner_chat=owner,
                             dry_run=not args.send_review, limit=args.limit,
                             min_interval=args.min_interval)
    except (ManifestError, PreflightError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    print(json.dumps(summary, ensure_ascii=False))
    if summary["failed"] or summary["unverified"] or summary["stopped"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
