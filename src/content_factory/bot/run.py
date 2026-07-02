"""Telegram-бот (long-poll). Принимает команды ТОЛЬКО от владельца (chat_id из .env),
маршрутизирует через bot.commands.handle_command. Команда /approve публикует подтверждённый
пост в канал (confirm-пилот). Запуск как сервис `cf-bot`.

  python -m content_factory.bot.run
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.publish.telegram import publish_post, PublishState, TG_API
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.bot.commands import handle_command, handle_callback


def make_publish_fn(token: str, parse_mode: str, pub_state: PublishState, http=None):
    """publish_fn(awaiting) → публикует подтверждённый пост в его канал."""
    def publish_fn(a):
        return publish_post(token, a.channel, a.card_path, a.caption, http=http,
                            parse_mode=parse_mode, key=a.key, state=pub_state, retries=2)
    return publish_fn


def finalize_preview(http, token: str, cq: dict, verdict: str) -> None:
    """После ✅/❌ в ревью-канале: заменить кнопки превью одной «вердикт»-кнопкой
    (подпись/форматирование не трогаем — канал остаётся журналом ревью)."""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not (chat_id and message_id):
        return
    kb = json.dumps({"inline_keyboard": [[{"text": verdict[:60], "callback_data": "noop"}]]},
                    ensure_ascii=False)
    try:
        http.post(f"{TG_API}/bot{token}/editMessageReplyMarkup",
                  data={"chat_id": chat_id, "message_id": message_id, "reply_markup": kb})
    except httpx.HTTPError:
        pass


def get_updates(token: str, offset: int, timeout: int = 30, http=None) -> list:
    client = http or httpx.Client(timeout=timeout + 10)
    r = client.get(f"{TG_API}/bot{token}/getUpdates",
                   params={"offset": offset, "timeout": timeout})
    return (r.json() or {}).get("result", [])


def main():
    cfg = load_config(Path("config/config.yaml"))
    token = config("TELEGRAM_BOT_TOKEN")
    owner = str(config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", "")))
    q = TaskQueue(cfg.state.db)
    cs = ConfirmStore(cfg.state.db)
    ps = PublishState(cfg.state.db)
    publish_fn = make_publish_fn(token, cfg.telegram.parse_mode, ps)
    http = httpx.Client(timeout=40)
    offset = 0
    print("bot: long-poll запущен")
    while True:
        try:
            updates = get_updates(token, offset, http=http)
        except httpx.HTTPError:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1

            # --- Нажатие inline-кнопки (✅/❌ под превью) ---
            cq = u.get("callback_query")
            if cq:
                frm = str((cq.get("from") or {}).get("id", ""))
                if owner and frm != owner:
                    continue
                if cq.get("data") == "noop":          # «вердикт»-кнопка уже нажатого превью
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id")})
                    except httpx.HTTPError:
                        pass
                    continue
                reply = handle_callback(cq.get("data", ""), q, confirm_store=cs,
                                        publish_fn=publish_fn, publish_state=ps)
                try:
                    http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                              data={"callback_query_id": cq.get("id"), "text": reply[:180]})
                except httpx.HTTPError:
                    pass
                # вместо эха в чат — приписываем вердикт к самому превью (журнал ревью)
                finalize_preview(http, token, cq, reply)
                continue

            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            if not text or (owner and chat != owner):     # только владелец управляет ботом
                continue
            reply = handle_command(text, q, confirm_store=cs, publish_fn=publish_fn,
                                   publish_state=ps)
            try:
                http.post(f"{TG_API}/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": reply})
            except httpx.HTTPError:
                pass


if __name__ == "__main__":
    main()
