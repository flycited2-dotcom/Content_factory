"""Подпись поста для готовой карточки Avito.

Своя вёрстка вместо `content/render.py::render_caption`: там переполнение режется срезом
`text[:cap_max]`, а здесь нельзя молча обрезать число, единицу или цену. Сокращаем только
ЦЕЛЫМИ блоками в фиксированном порядке; если даже шапка с ценой не влезает — позиция
отклоняется, а не режется.

Два жёстких контракта существующего бота:
1. Первая строка — название, вторая — цена: `publish/orders.py::item_summary` берёт ровно
   первые две строки сохранённой подписи для карточки клиенту и для лида.
2. В ConfirmStore хранится ЧИСТАЯ подпись (её потом публикует approve), а в review уходит
   она же плюс пометка ревью. В лимит 1024 должны влезть обе.

Лимит Telegram считается в кодовых единицах UTF-16 по ВИДИМОМУ тексту (после разбора
HTML-сущностей и тегов), иначе эмодзи делают проверку оптимистичной."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

TG_CAPTION_LIMIT = 1024
DIVIDER = "═" * 26
DEFAULT_CTA = "Заказ и консультация — напишите нам."
REVIEW_NOTE = "— закрытая проверка: требуется решение владельца —"
MIN_TABLE_ROWS = 2

_TAG_RE = re.compile(r"<[^>]+>")


def tg_len(text: str) -> int:
    """Длина видимого текста в кодовых единицах UTF-16 (как считает Telegram)."""
    visible = html.unescape(_TAG_RE.sub("", text or ""))
    return len(visible.encode("utf-16-le")) // 2


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


@dataclass
class CaptionResult:
    ok: bool
    caption: str = ""               # то, что сохраняем и что уйдёт в канал при approve
    review_caption: str = ""        # то, что уходит в review (подпись + пометка)
    models_shown: int = 0
    dropped: list[str] = field(default_factory=list)
    reason: str | None = None


def _header_lines(item) -> tuple[str, str]:
    """Строка 1 — название, строка 2 — цена (контракт item_summary)."""
    name = _esc(item.display_name)
    prefix = "от " if item.price_from else ""
    price = (f"<blockquote>💎 <b>{prefix}{money(item.price_final)}</b>"
             f"</blockquote>")
    return name, price


def _table_lines(rows, total: int) -> list[str]:
    if not rows:
        return []
    lines = ["Модели и цены:"]
    lines += [f"▫️ {_esc(m.model)} · {money(m.price)}" for m in rows]
    if len(rows) < total:
        lines.append(f"▫️ и ещё {total - len(rows)} модел{_plural(total - len(rows))}")
    return lines


def _model_line(item) -> list[str]:
    """Одна строка с названием модели — для позиции с точной ценой, где таблицы нет.

    В названии лежат артикул и объём бака («BWH/S 80»). Раньше оно приезжало внутри
    описания из фида; описание больше не содержит блок цен, поэтому без этой строки
    покупатель не увидит, 50 перед ним литров или 100."""
    if item.price_from or not item.models:
        return []
    model = (item.models[0].model or "").strip()
    # У части товаров название модели и есть заголовок поста — не печатаем дважды.
    if not model or model.casefold() in (item.display_name or "").casefold():
        return []
    return [f"Модель: {_esc(model)}"]


def _plural(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "ей"
    return {1: "ь", 2: "и", 3: "и", 4: "и"}.get(n % 10, "ей")


def _usp_blocks(item) -> list[str]:
    return [_esc(block) for block in (item.usp_text or "").split("\n\n") if block.strip()]


def _assemble(header: str, price: str, table: list[str], usp: list[str],
              cta: str | None) -> str:
    body: list[str] = []
    if table:
        body.append("\n".join(table))
    body.extend(usp)
    if cta:
        body.append(cta)
    text = f"{header}\n{price}"
    if body:
        text += f"\n{DIVIDER}\n" + "\n\n".join(body)
    return text


def build_caption(item, *, limit: int = TG_CAPTION_LIMIT, review_note: str = REVIEW_NOTE,
                  cta: str = DEFAULT_CTA) -> CaptionResult:
    """Собрать подпись. Порядок отказа от блоков: хвост УТП → строки таблицы (до 2) →
    CTA → таблица целиком. Шапка, цена и название модели неприкосновенны."""
    header, price = _header_lines(item)
    all_rows = list(item.models) if item.price_from else []
    rows = list(all_rows)
    model_line = _model_line(item)          # у точной цены вместо таблицы — одна строка
    usp = _usp_blocks(item)
    cta_on = bool(cta)
    dropped: list[str] = []
    suffix = f"\n\n{review_note}" if review_note else ""

    while True:
        caption = _assemble(header, price,
                            _table_lines(rows, len(all_rows)) or model_line, usp,
                            cta if cta_on else None)
        if tg_len(caption) <= limit and tg_len(caption + suffix) <= limit:
            return CaptionResult(ok=True, caption=caption, review_caption=caption + suffix,
                                 models_shown=len(rows), dropped=dropped)
        if usp:
            usp.pop()
            dropped.append("usp_block")
            continue
        if len(rows) > MIN_TABLE_ROWS:
            rows.pop()
            dropped.append("model_row")
            continue
        if cta_on:
            cta_on = False
            dropped.append("cta")
            continue
        if rows:
            rows = []
            dropped.append("model_table")
            continue
        return CaptionResult(ok=False, dropped=dropped, reason="caption_too_long")
