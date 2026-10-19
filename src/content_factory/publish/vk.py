"""VK Wall API: адаптация поста, preview/dry-run и публикация фото на стену."""
from __future__ import annotations

import html
import mimetypes
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import httpx

VK_API = "https://api.vk.com/method"
VK_MESSAGE_MAX = 4096
VK_SHARE = "https://vk.com/share.php"


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def adapt_vk_text(caption: str, max_chars: int = VK_MESSAGE_MAX) -> str:
    """Telegram HTML → читаемый VK plain text без изменения фактов и цены."""
    parser = _PlainText()
    parser.feed(caption or "")
    # close() обязателен: парсер придерживает хвост с «&» в последних 34 символах
    # (ждёт окончания «сущности») и без него отдаёт пустой текст. С короткой
    # ссылкой `…&utm_content=breeze%3ANS-1` пропадал весь пост целиком.
    parser.close()
    text = html.unescape("".join(parser.parts))
    # Публичный каталог иногда склеивает индекс модели и следующее русское слово:
    # `SRW-12000-Dоднофазный` → `SRW-12000-D однофазный`.
    text = re.sub(r"(?<=[A-Z0-9])(?=[а-яё])", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars].rstrip()


def build_vk_share_url(*, url: str, title: str, description: str,
                       image_url: str = "") -> str:
    """Официальное окно ручной публикации VK без выдачи издательского токена."""
    if not url.startswith("https://"):
        raise ValueError("VK Share требует публичный HTTPS URL")
    # Карточку ссылки VK всегда собирает из Open Graph страницы `url`.
    # Параметр `image` ненадёжен и может быть проигнорирован, поэтому изображение,
    # заголовок и описание закрепляются на отдельной товарной share-странице.
    params = {"url": url, "comment": description}
    if image_url:
        if not image_url.startswith("https://"):
            raise ValueError("Изображение VK Share должно иметь публичный HTTPS URL")
    return f"{VK_SHARE}?{urlencode(params)}"


def build_stabilizer_vk_comment(message: str) -> str:
    """Развернуть краткую карточку стабилизатора в читаемый продающий текст VK."""
    source = adapt_vk_text(message)
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    title = lines[0] if lines else "Стабилизатор напряжения"
    price = next((line for line in lines if "₽" in line), "Цена по запросу")
    return "\r\n".join([
        f"⚡ {title}",
        "",
        price,
        "",
        "Почему стоит выбрать:",
        "✅ Защищает технику при нестабильном напряжении",
        "✅ Мощность 12 000 ВА",
        "✅ Рабочий диапазон 130–270 В",
        "✅ Настенное размещение — не занимает место на полу",
        "✅ Цифровой контроль напряжения",
        "✅ Гарантия производителя 36 месяцев",
        "✅ Подберём мощность под вашу нагрузку",
        "",
        "🚚 Доставка по Крыму, Запорожской и Херсонской областям",
        "💳 Оплата при получении после подтверждения заказа",
        "📦 Наличие и срок поставки подтвердит менеджер",
        "",
        "📞 +7 978 579-29-95",
        "Напишите или позвоните — подберём модель под параметры вашей сети.",
    ])[:VK_MESSAGE_MAX].rstrip()


@dataclass
class VkPublishResult:
    ok: bool
    dry_run: bool = False
    post_id: int | None = None
    error: str | None = None
    payload: dict | None = None


class VkPublisher:
    def __init__(self, token: str, owner_id: int, *, api_version: str = "5.199",
                 dry_run: bool = True, http: httpx.Client | None = None,
                 upload_retry_delay: float = 1.5):
        self.token = token
        self.owner_id = int(owner_id)
        self.api_version = api_version
        self.dry_run = dry_run
        self.upload_retry_delay = upload_retry_delay
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)

    def preview(self, image: str, caption: str, *, publish_at: int | None = None) -> dict:
        payload = {"owner_id": self.owner_id, "message": adapt_vk_text(caption),
                   "image": str(image), "dry_run": self.dry_run}
        if publish_at is not None:
            payload["publish_date"] = int(publish_at)
        return payload

    def _api(self, method: str, data: dict) -> dict:
        payload = {**data, "access_token": self.token, "v": self.api_version}
        response = self.http.post(f"{VK_API}/{method}", data=payload)
        response.raise_for_status()
        body = response.json() or {}
        if body.get("error"):
            err = body["error"]
            raise RuntimeError(f"VK {err.get('error_code')}: {err.get('error_msg')}")
        return body.get("response")

    UPLOAD_ATTEMPTS = 5

    def _upload_wall_photo(self, path: Path, target: dict) -> str:
        """Загрузить файл на стену и вернуть вложение ``photo<owner>_<id>[_<key>]``.

        Сервер загрузки VK изредка принимает файл «вхолостую» (photo=[]), и тогда
        saveWallPhoto отвечает ``100: photo is undefined``, а иногда сам отвечает 504.
        Это разовые сбои: на боевой стене они выпадали на каждой второй-третьей попытке
        (однажды четыре подряд) и проходили при повторе с новым адресом загрузки, пауза
        между попытками растёт. Повторяем только их; ошибки
        прав (5, 15, 27) и 4xx сервера загрузки не повторяются.
        """
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        for attempt in range(1, self.UPLOAD_ATTEMPTS + 1):
            upload = self._api("photos.getWallUploadServer", target)
            try:
                reply = self.http.post(upload["upload_url"],
                                       files={"photo": (path.name, data, mime)})
                reply.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                # Сбой самого сервера загрузки (504, обрыв соединения) тоже разовый;
                # 4xx — ошибка запроса, повтор ничего не изменит.
                transient = (isinstance(exc, httpx.TransportError)
                             or exc.response.status_code >= 500)
                if transient and attempt < self.UPLOAD_ATTEMPTS:
                    time.sleep(self.upload_retry_delay * attempt)
                    continue
                raise
            uploaded = reply.json()
            try:
                saved = self._api("photos.saveWallPhoto", {
                    **{k: uploaded[k] for k in ("server", "photo", "hash")}, **target})
            except RuntimeError as exc:
                if "photo is undefined" in str(exc) and attempt < self.UPLOAD_ATTEMPTS:
                    time.sleep(self.upload_retry_delay * attempt)
                    continue
                raise
            photo = saved[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            if photo.get("access_key"):
                attachment += f"_{photo['access_key']}"
            return attachment
        raise RuntimeError("VK: загрузка фото не удалась")  # недостижимо: последняя попытка raise

    def publish(self, image: str, caption: str,
                *, publish_at: int | None = None) -> VkPublishResult:
        """Опубликовать карточку нативной фотографией.

        Отрицательный ``owner_id`` обозначает стену сообщества, а не тип токена.
        Поэтому право загрузки проверяет сам VK API: пользовательский токен
        администратора сможет загрузить фото в сообщество, ключ сообщества вернёт
        ошибку, и запись без изображения создана не будет.
        """
        preview = self.preview(image, caption, publish_at=publish_at)
        if self.dry_run:
            return VkPublishResult(ok=True, dry_run=True, payload=preview)
        if not self.token:
            return VkPublishResult(ok=False, error="VK_ACCESS_TOKEN не задан", payload=preview)
        path = Path(image)
        if not path.is_file():
            return VkPublishResult(ok=False, error=f"файл карточки не найден: {path}",
                                   payload=preview)
        try:
            target = ({"group_id": abs(self.owner_id)}
                      if self.owner_id < 0 else {"user_id": self.owner_id})
            attachment = self._upload_wall_photo(path, target)
            post = self._api("wall.post", {
                "owner_id": self.owner_id,
                "from_group": 1 if self.owner_id < 0 else 0,
                "message": preview["message"],
                "attachments": attachment,
                **({"publish_date": int(publish_at)} if publish_at is not None else {}),
            })
            return VkPublishResult(ok=True, post_id=int(post["post_id"]), payload=preview)
        except (OSError, KeyError, IndexError, ValueError, RuntimeError,
                httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=preview)

    def publish_text(self, caption: str, *, publish_at: int | None = None) -> VkPublishResult:
        """Явно опубликовать текст без вложения для переходного ручного фотопотока.

        Этот метод отделён от ``publish`` намеренно: штатная публикация карточки не
        должна молча терять изображение, а текстовый пост помечается как требующий
        последующего ручного прикрепления готовой карточки.
        """
        payload = {
            "owner_id": self.owner_id,
            "from_group": 1 if self.owner_id < 0 else 0,
            "message": adapt_vk_text(caption),
            "manual_photo_required": True,
        }
        if publish_at is not None:
            payload["publish_date"] = int(publish_at)
        if self.dry_run:
            return VkPublishResult(ok=True, dry_run=True, payload=payload)
        if not self.token:
            return VkPublishResult(ok=False, error="VK_ACCESS_TOKEN не задан", payload=payload)
        try:
            post_data = {
                "owner_id": payload["owner_id"],
                "from_group": payload["from_group"],
                "message": payload["message"],
            }
            if publish_at is not None:
                post_data["publish_date"] = int(publish_at)
            post = self._api("wall.post", post_data)
            return VkPublishResult(ok=True, post_id=int(post["post_id"]), payload=payload)
        except (KeyError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=payload)

    def _attachments_of(self, post_id: int) -> str:
        """Вложения записи в формате параметра ``attachments`` (через запятую)."""
        got = self._api("wall.getById", {"posts": f"{self.owner_id}_{int(post_id)}"})
        items = got.get("items", got) if isinstance(got, dict) else got
        parts: list[str] = []
        for attachment in (items[0].get("attachments") or []) if items else []:
            kind = attachment.get("type")
            body = attachment.get(kind) or {}
            if kind == "link":
                if body.get("url"):
                    parts.append(body["url"])
            elif body.get("owner_id") is not None and body.get("id") is not None:
                ref = f"{kind}{body['owner_id']}_{body['id']}"
                parts.append(ref + (f"_{body['access_key']}" if body.get("access_key") else ""))
        return ",".join(parts)

    def edit_text(self, post_id: int, caption: str,
                  *, publish_at: int | None = None) -> VkPublishResult:
        """Обновить текст существующей или отложенной записи без создания дубля.

        wall.edit без ``attachments`` стирает вложения записи (проверено на боевой стене:
        фото пропало после правки текста). Поэтому текущие вложения читаются и передаются
        явно. Не удалось прочитать — запись не правим: лучше старый текст, чем пост без фото.
        """
        payload = {
            "owner_id": self.owner_id,
            "post_id": int(post_id),
            "message": adapt_vk_text(caption),
        }
        if publish_at is not None:
            payload["publish_date"] = int(publish_at)
        if self.dry_run:
            return VkPublishResult(ok=True, dry_run=True, post_id=int(post_id), payload=payload)
        if not self.token:
            return VkPublishResult(ok=False, error="VK_ACCESS_TOKEN не задан", payload=payload)
        try:
            attachments = self._attachments_of(post_id)
            self._api("wall.edit", {**payload, **({"attachments": attachments} if attachments else {})})
            return VkPublishResult(ok=True, post_id=int(post_id), payload=payload)
        except (KeyError, IndexError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=payload)
