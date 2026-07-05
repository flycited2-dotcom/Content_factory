"""Опросник заказа клиента (задача 3 + follow-up 2026-07-05). Кнопка «Заказать»
под постом ведёт в бота: клиент проходит кол-во → комментарий → телефон, и готовая
заявка (наименование, цена, кол-во, телефон, комментарий, @клиент) улетает в
отдельный чат лидов, а не смешивается с управляющим ботом владельца. Чистая логика
без Telegram (отправку инъектит bot/run.py); состояние — OrderDialogStore."""
from __future__ import annotations
import re
from dataclasses import dataclass

from content_factory.publish.orders import item_summary

_QTY_RE = re.compile(r"\d+")

QTY_KB = {"inline_keyboard": [
    [{"text": "1", "callback_data": "order:qty:1"},
     {"text": "2", "callback_data": "order:qty:2"},
     {"text": "3", "callback_data": "order:qty:3"}],
    [{"text": "✏️ Другое количество", "callback_data": "order:qty:custom"}]]}
SKIP_COMMENT_KB = {"inline_keyboard": [
    [{"text": "⏭ Пропустить", "callback_data": "order:skip_comment"}]]}
# Телефон запрашиваем reply-клавиатурой (request_contact работает только в ней).
CONTACT_KB = {"keyboard": [[{"text": "📱 Отправить телефон", "request_contact": True}],
                          [{"text": "Пропустить"}]],
              "resize_keyboard": True, "one_time_keyboard": True}
REMOVE_KB = {"remove_keyboard": True}

_COMMENT_PROMPT = ("Комментарий к заказу? (адрес, вопрос, удобное время) — "
                   "напишите или пропустите.")
_PHONE_PROMPT = ("📱 Оставьте телефон, чтобы менеджер перезвонил: нажмите кнопку ниже, "
                 "впишите номер или «Пропустить».")


@dataclass
class OrderReply:
    text: str
    markup: dict | None = None         # inline-клавиатура
    force_reply: bool = False
    placeholder: str | None = None
    keyboard: dict | None = None       # reply-клавиатура (телефон / убрать)
    lead: str | None = None            # текст заявки в чат лидов (когда заказ готов)


def _parse_qty(s: str) -> int | None:
    m = _QTY_RE.search(s or "")
    if not m:
        return None
    n = int(m.group())
    return n if 1 <= n <= 9999 else None


def _who(user: dict) -> str:
    uname = user.get("username") or ""
    return (f"@{uname}" if uname else "") or user.get("first_name") or str(user.get("id"))


def make_order_flow(store, links, pub_state):
    """start(chat, code, user) — по /start ord_<code>; callback(chat, data, user) —
    кнопки order:*; text(chat, msg, user) — число/комментарий/телефон-текст;
    contact(chat, phone, user) — клиент поделился контактом. lead != None у ответа,
    когда заявку пора слать в чат лидов."""

    def _comment_reply() -> OrderReply:
        return OrderReply(_COMMENT_PROMPT, SKIP_COMMENT_KB)

    def _phone_reply() -> OrderReply:
        return OrderReply(_PHONE_PROMPT, keyboard=CONTACT_KB)

    def _finalize(chat_id, st, user, phone) -> OrderReply:
        qty = st.qty or 1
        comment = st.comment or ""
        links.add_lead(int(user.get("id") or 0), user.get("username") or "",
                       st.key, qty=qty, comment=comment, phone=phone)
        summary = item_summary(pub_state, st.key)
        store.cancel(chat_id)
        r = OrderReply(f"✅ Заявка принята! {summary.splitlines()[0]} — {qty} шт.\n"
                       f"Менеджер свяжется с вами в ближайшее время.", keyboard=REMOVE_KB)
        lead = ["📩 Новая заявка", summary, f"🔢 Количество: {qty} шт."]
        if phone:
            lead.append(f"📱 Телефон: {phone}")
        if comment:
            lead.append(f"💬 Комментарий: {comment}")
        lead.append(f"👤 Клиент: {_who(user)} (id {user.get('id')})")
        r.lead = "\n".join(lead)
        return r

    def start(chat_id, code, user) -> OrderReply:
        key = links.key_for(code)
        if not key:
            return OrderReply("К сожалению, товар не найден (возможно, пост устарел). "
                              "Напишите нам!")
        store.start(chat_id, key)
        return OrderReply(f"Вы выбрали:\n{item_summary(pub_state, key)}\n\n"
                          f"Сколько штук заказываете?", QTY_KB)

    def callback(chat_id, data, user) -> OrderReply:
        st = store.snapshot(chat_id)
        if st is None:
            return OrderReply("Заявка устарела. Нажмите «Заказать» под постом ещё раз.")
        if data.startswith("order:qty:"):
            val = data.split(":", 2)[2]
            if val == "custom":
                store.set_step(chat_id, "awaiting_qty_custom")
                return OrderReply("Введите количество числом:", force_reply=True,
                                  placeholder="напр.: 5")
            if st.step != "awaiting_qty":
                return OrderReply("Количество уже выбрано — напишите комментарий или пропустите.")
            n = _parse_qty(val)                    # подделанный callback_data → не падаем
            if n is None:
                return OrderReply("Кнопка устарела.")
            store.set_qty(chat_id, n)
            return _comment_reply()
        if data == "order:skip_comment":
            if st.step != "awaiting_comment":
                return OrderReply("Сначала выберите количество.")
            store.set_comment(chat_id, "")
            return _phone_reply()
        return OrderReply("Кнопка устарела.")

    def text(chat_id, msg_text, user) -> OrderReply | None:
        st = store.snapshot(chat_id)
        if st is None:
            return None                                # не в диалоге заказа
        if (msg_text or "").strip().startswith("/"):
            return None                                # команду не глотаем (можно выйти)
        if st.step in ("awaiting_qty", "awaiting_qty_custom"):
            n = _parse_qty(msg_text)
            if n is None:
                if st.step == "awaiting_qty_custom":
                    return OrderReply("Нужно число больше 0. Введите количество:",
                                      force_reply=True, placeholder="напр.: 5")
                return None                            # на шаге кнопок игнорируем болтовню
            store.set_qty(chat_id, n)
            return _comment_reply()
        if st.step == "awaiting_comment":
            store.set_comment(chat_id, (msg_text or "").strip())
            return _phone_reply()
        if st.step == "awaiting_phone":
            t = (msg_text or "").strip()
            phone = "" if t == "Пропустить" else t
            return _finalize(chat_id, st, user, phone=phone)
        return None

    def contact(chat_id, phone, user) -> OrderReply | None:
        st = store.snapshot(chat_id)
        if st is None or st.step != "awaiting_phone":
            return None
        return _finalize(chat_id, st, user, phone=(phone or "").strip())

    return start, callback, text, contact
