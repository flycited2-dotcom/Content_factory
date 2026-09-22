"""VK Wall API: адаптация поста, preview/dry-run и публикация фото на стену."""
from __future__ import annotations

import html
import re
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
                 dry_run: bool = True, http: httpx.Client | None = None):
        self.token = token
        self.owner_id = int(owner_id)
        self.api_version = api_version
        self.dry_run = dry_run
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
            upload = self._api("photos.getWallUploadServer", target)
            with path.open("rb") as fh:
                upload_response = self.http.post(
                    upload["upload_url"], files={"photo": (path.name, fh.read(), "image/png")})
            upload_response.raise_for_status()
            uploaded = upload_response.json()
            save_params = {k: uploaded[k] for k in ("server", "photo", "hash")}
            save_params.update(target)
            saved = self._api("photos.saveWallPhoto", save_params)
            photo = saved[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            if photo.get("access_key"):
                attachment += f"_{photo['access_key']}"
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

    def edit_text(self, post_id: int, caption: str,
                  *, publish_at: int | None = None) -> VkPublishResult:
        """Обновить текст существующей или отложенной записи без создания дубля.

        ``attachments`` намеренно не передаётся: уже прикреплённая вручную или
        автоматически фотография остаётся частью существующей записи.
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
            self._api("wall.edit", payload)
            return VkPublishResult(ok=True, post_id=int(post_id), payload=payload)
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            return VkPublishResult(ok=False, error=str(exc), payload=payload)
