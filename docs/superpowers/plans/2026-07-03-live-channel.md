# «Живой канал» (волна 1а) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Посты в «СплитХаб.ру» сами следят за складом: товар кончился → «⛔ ПРОДАНО»,
цена изменилась → цена в подписи обновляется, товар вернулся → пост оживает.

**Architecture:** PublishState расширяется (channel/price/status/caption), новая чистая
логика `publish/channel_sync.py::plan_sync` строит список правок, исполнитель
`edit_caption` (editMessageCaption + троттлинг) применяет их; CLI `channel_sync_run`
на systemd-таймере раз в день до утреннего окна. Excel-товары пропускаются (нет остатков).
Спека: `docs/superpowers/specs/2026-07-03-wave1-improvements-design.md` (раздел 1а).

**Tech Stack:** как весь репо — Python/pytest/httpx.MockTransport/sqlite3; деплой tar+ssh.

---

### Task 1: PublishState — колонки channel/price/status/caption + records()

**Files:** Modify: `src/content_factory/publish/telegram.py` · Test: `tests/test_publish.py`

- [ ] **Step 1: Падающие тесты** (в tests/test_publish.py)

```python
def test_publish_state_migration_and_records(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k1", 10, channel="@chan", caption="cap1")
    recs = ps.records()
    assert [(r.key, r.message_id, r.channel, r.caption, r.status, r.price)
            for r in recs] == [("k1", 10, "@chan", "cap1", "active", None)]


def test_publish_state_update_sync(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k1", 10, channel="@chan", caption="cap1")
    ps.update_sync("k1", status="sold")
    ps.update_sync("k1", price=19990, caption="cap2")
    (r,) = ps.records()
    assert (r.status, r.price, r.caption) == ("sold", 19990, "cap2")


def test_publish_state_mark_backcompat(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k0", 5)                       # старый вызов без channel/caption
    (r,) = ps.records()
    assert (r.channel, r.caption, r.status) == ("", None, "active")
```

- [ ] **Step 2: Убедиться, что падают** — `python -m pytest tests/test_publish.py -q` → FAIL
- [ ] **Step 3: Реализация** в `publish/telegram.py`:
  - dataclass `PublishedRec(key, message_id, channel, price, status, caption, ts)`;
  - в `PublishState.__init__` миграции (по образцу CardJobStore.tries):
    `ALTER TABLE published ADD COLUMN channel TEXT DEFAULT ''`, `... price INTEGER`,
    `... status TEXT DEFAULT 'active'`, `... caption TEXT` (каждый в try/except OperationalError);
  - `mark(self, key, message_id, channel="", caption=None)` — записывает новые поля,
    status='active';
  - `records(self) -> list[PublishedRec]`; `update_sync(self, key, status=None, price=None,
    caption=None)` — обновляет только переданные поля;
  - `publish_post`: вызов `state.mark(key, mid, channel=str(channel_id), caption=caption)`.
- [ ] **Step 4: Всё зелёное** — `python -m pytest -q` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(publish): PublishState — channel/price/status/caption + records/update_sync"`

### Task 2: plan_sync — чистая логика решений

**Files:** Create: `src/content_factory/publish/channel_sync.py` · Test: `tests/test_channel_sync.py`

- [ ] **Step 1: Падающие тесты** (`tests/test_channel_sync.py`; группы — через
  `group_by_series([_offer(...)])`, как в tests/test_cards.py)

```python
from content_factory.publish.channel_sync import plan_sync
from content_factory.publish.telegram import PublishedRec

def _rec(key, price=None, status="active", caption="old", mid=10):
    return PublishedRec(key=key, message_id=mid, channel="@c", price=price,
                        status=status, caption=caption, ts=0)

# группы: helper _groups(stock=...) — Offer c breeze|b|s ключом
# price_fn: lambda g: 20000; caption_fn: lambda g, price: f"cap {price}"

def test_sold_when_out_of_stock(): ...      # активный + stock 0 → sold, caption "⛔ ПРОДАНО\n\nold"
def test_sold_when_series_gone(): ...       # активный, серии нет в каталоге → sold
def test_sold_without_saved_caption(): ...  # caption None → caption "⛔ ПРОДАНО"
def test_reprice_when_delta_reached(): ...  # price 20000 vs хранимая 18000, delta 100 → reprice cap "cap 20000"
def test_no_reprice_below_delta(): ...      # 20000 vs 19950, delta 100 → нет действий
def test_baseline_price_written_without_edit(): ...  # хранимая None → updates=[(key, 20000)], actions=[]
def test_revive_when_back_in_stock(): ...   # status sold + stock>0 → revive со свежей подписью
def test_excel_keys_skipped(): ...          # excel|... → игнор
def test_sold_stays_sold(): ...             # status sold + stock 0 → нет действий
```

- [ ] **Step 2: FAIL** — модуля нет
- [ ] **Step 3: Реализация** `publish/channel_sync.py`:

```python
"""«Живой канал»: сверка опубликованных постов с каталогом. Чистая логика — сеть/state
снаружи. Правила: нет в наличии → sold («⛔ ПРОДАНО» поверх сохранённой подписи);
sold и снова в наличии → revive; цена ушла на ≥ min_price_delta → reprice.
excel|* пропускаем (в прайсе нет остатков)."""
from __future__ import annotations
from dataclasses import dataclass

SOLD_MARK = "⛔ ПРОДАНО"


@dataclass
class SyncAction:
    key: str
    kind: str                 # sold | reprice | revive
    message_id: int
    channel: str
    caption: str              # полная новая подпись (≤1024 отрезает исполнитель)
    price: int | None = None  # новая цена для записи в state


def plan_sync(records, groups, price_fn, caption_fn, default_channel: str,
              min_price_delta: int = 100):
    """→ (actions, baseline): baseline — [(key, price)] для записи цены без правки поста
    (первый прогон после миграции)."""
    by_key = {g.key: g for g in groups}
    actions, baseline = [], []
    for r in records:
        if r.key.startswith("excel|") or not r.message_id:
            continue
        chan = r.channel or default_channel
        g = by_key.get(r.key)
        in_stock = bool(g) and any((m.stock or 0) > 0 for m in g.members)
        if r.status != "sold" and not in_stock:
            cap = f"{SOLD_MARK}\n\n{r.caption}" if r.caption else SOLD_MARK
            actions.append(SyncAction(r.key, "sold", r.message_id, chan, cap))
            continue
        if not in_stock:
            continue                              # sold и по-прежнему нет — не трогаем
        price = price_fn(g)
        if r.status == "sold":
            actions.append(SyncAction(r.key, "revive", r.message_id, chan,
                                      caption_fn(g, price), price))
        elif price and r.price is None:
            baseline.append((r.key, price))       # первая сверка: запомнить без правки
        elif price and r.price and abs(price - r.price) >= min_price_delta:
            actions.append(SyncAction(r.key, "reprice", r.message_id, chan,
                                      caption_fn(g, price), price))
    return actions, baseline
```

- [ ] **Step 4: PASS + Commit** — `git commit -m "feat(channel-sync): plan_sync — продано/цена/оживление (чистая логика)"`

### Task 3: edit_caption — правка поста в канале

**Files:** Modify: `src/content_factory/publish/telegram.py` · Test: `tests/test_publish.py`

- [ ] **Step 1: Падающие тесты**

```python
def test_edit_caption_ok(): ...        # MockTransport: путь /botTOK/editMessageCaption,
                                       # body содержит message_id и подпись → (True, None, False)
def test_edit_caption_message_gone(): ...  # ответ ok:false, description "message to edit not found"
                                           # → (False, "...", gone=True)
def test_edit_caption_transient_retries(): ...  # первый 500 → ретрай → 200 ok
```

- [ ] **Step 2: FAIL**
- [ ] **Step 3: Реализация** (рядом с publish_post, та же механика ретраев):

```python
def edit_caption(bot_token, chat_id, message_id, caption, *, parse_mode=None,
                 http=None, retries=1, backoff=1.0):
    """editMessageCaption. → (ok, error, gone): gone=True — пост удалён/недоступен
    (больше не трогать: пометить sold)."""
    client = http or httpx.Client(timeout=30)
    data = {"chat_id": str(chat_id), "message_id": message_id,
            "caption": (caption or "")[:CAPTION_MAX]}
    if parse_mode:
        data["parse_mode"] = parse_mode
    for attempt in range(max(1, retries) + 1):
        try:
            r = client.post(f"{TG_API}/bot{bot_token}/editMessageCaption", data=data)
        except httpx.HTTPError as e:
            if attempt < retries:
                time.sleep(backoff)
                continue
            return False, f"network: {e}", False
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < retries:
                time.sleep(backoff)
                continue
            return False, f"http {r.status_code}", False
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            pass
        if body.get("ok"):
            return True, None, False
        desc = (body.get("description") or f"http {r.status_code}").lower()
        gone = "not found" in desc or "message_id_invalid" in desc or "can't be edited" in desc
        return False, desc, gone
    return False, "unknown", False
```

- [ ] **Step 4: PASS + Commit** — `git commit -m "feat(publish): edit_caption с ретраями и признаком gone"`

### Task 4: конфиг + CLI + systemd

**Files:** Modify: `src/content_factory/config.py`, `examples/config.example.yaml`,
`config/config.yaml` · Create: `src/content_factory/publish/channel_sync_run.py`,
`deploy/cf-channel-sync.service`, `deploy/cf-channel-sync.timer` · Test: `tests/test_config.py`

- [ ] **Step 1: Конфиг (TDD)** — тест: `channel_sync: {enabled: true, min_price_delta: 200}` →
  `cfg.channel_sync == {...}`; без секции → `{}`. Реализация: `AppConfig.channel_sync: dict =
  field(default_factory=dict)` + `d.get("channel_sync", {}) or {}` (как auto_tasks).
- [ ] **Step 2: CLI** `publish/channel_sync_run.py` (по образцу scheduler_run.main):
  load_config → `if not cfg.channel_sync.get("enabled"): print(...); return` →
  records = PublishState(cfg.state.db).records() (пусто → выход) → каталог из oasis
  (fetch_raw_products/collect_offers/group_by_series + utp_lookup Бриза как в scheduler_run) →
  `price_fn` = compute_price(...).price, `caption_fn` = render_caption(g, price, cfg.content,
  utp_raw=utp_lookup(g)) → plan_sync → baseline: `ps.update_sync(key, price=price)` →
  для actions: `edit_caption(...)`; ok → update_sync (sold → status='sold'; reprice →
  price+caption; revive → status='active', price, caption); gone → status='sold';
  пауза `edit_pause_sec` (дефолт 4) между правками; сводка print
  (`sync: продано N | цены M | ожило K | baseline B | ошибок E`).
  `sold_action: delete` (опция) — вместо правки вызвать deleteMessage (маленький helper
  рядом с edit_caption; в MVP допустимо оставить только mark и отметить TODO).
- [ ] **Step 3: Смоук** — `python -c "import content_factory.publish.channel_sync_run"` без ошибок.
- [ ] **Step 4: Unit-файлы** (по образцу cf-scheduler.*): `cf-channel-sync.service`
  (oneshot, WorkingDirectory /opt/content-factory, PYTHONPATH=src,
  ExecStartPre=update_db_host.sh, ExecStart `.venv/bin/python -m
  content_factory.publish.channel_sync_run`), `cf-channel-sync.timer`
  (`OnCalendar=*-*-* 08:30`, Persistent=true).
- [ ] **Step 5: config/config.yaml (прод)** — добавить `channel_sync: {enabled: true,
  sold_action: mark, min_price_delta: 100, edit_pause_sec: 4}`.
- [ ] **Step 6: Все тесты + Commit** — `git commit -m "feat(channel-sync): CLI + конфиг + systemd (живой канал)"`

### Task 5: Деплой + пилот (внешнее, по ОК владельца)

- [ ] Выложить код+конфиг (tar+ssh), `cp deploy/cf-channel-sync.* /etc/systemd/system/`,
  `systemctl daemon-reload && systemctl enable --now cf-channel-sync.timer`.
- [ ] Первый прогон вручную: `systemctl start cf-channel-sync.service` → в журнале
  ожидаемо «baseline B» (запоминание цен) и, если что-то распродано за сутки, — пометки
  «⛔ ПРОДАНО» в канале. Показать владельцу.
- [ ] Проверить лимиты: при >15 правок за прогон убедиться, что пауза держит (журнал без 429).

---

## Самопроверка плана
Покрытие спеки 1а: миграция ✓ (Task 1), правила sold/reprice/revive/excel-skip ✓ (Task 2),
editMessageCaption+троттлинг+gone ✓ (Task 3-4), CLI+таймер 08:30+конфиг ✓ (Task 4),
деплой/пилот ✓ (Task 5). Типы согласованы: PublishedRec (T1) ↔ plan_sync (T2) ↔
update_sync (T4); SyncAction.kind ∈ {sold, reprice, revive}.

## Следом (отдельные планы)
1б «Кнопка Заказать» — план после сдачи 1а (order_links/лиды/разрешение /start ord_* чужим).
1в «Кэш УТП» — внутри плана подпроекта 3 (Excel-источник, research_pipeline).
