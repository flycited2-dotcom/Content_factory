"""Безопасный VK ID OAuth 2.1 / PKCE для серверного издателя.

Секретные токены сохраняются только в ``state/`` (каталог исключён из git).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

VK_ID_AUTHORIZE = "https://id.vk.ru/authorize"
VK_ID_TOKEN = "https://id.vk.ru/oauth2/auth"
# Новые пользовательские Access token выдаются через OAuth VK ID. Права VK API
# запрашиваются в том же PKCE-потоке; VK может отфильтровать расширенные права,
# если они ещё не согласованы для приложения.
DEFAULT_SCOPES = ("vkid.personal_info", "offline", "wall", "photos", "groups")
# Без этих прав пользовательский токен не откроет сервер загрузки фотографий.
PHOTO_PUBLISH_SCOPES = ("photos", "wall", "groups")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_code_verifier() -> str:
    """RFC 7636 verifier, 43–128 URL-safe characters."""
    return _b64url(secrets.token_bytes(64))


def code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True)
class VkOAuthSettings:
    app_id: int
    redirect_uri: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES


@dataclass
class PendingAuthorization:
    state: str
    code_verifier: str
    created_at: int


class VkTokenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["saved_at"] = int(time.time())
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def access_token(self, client: "VkOAuthClient", *, leeway_seconds: int = 120) -> str:
        """Вернуть действующий токен, обновив его заранее при необходимости."""
        current = self.load()
        expires_at = int(current.get("saved_at", 0)) + int(current.get("expires_in", 0))
        if current.get("access_token") and expires_at > int(time.time()) + leeway_seconds:
            return str(current["access_token"])
        if not current.get("refresh_token") or not current.get("device_id"):
            raise RuntimeError("VK OAuth ещё не завершён: refresh_token/device_id отсутствуют")
        refreshed = client.refresh(str(current["refresh_token"]), str(current["device_id"]))
        self.save(refreshed)
        return str(refreshed["access_token"])


class VkOAuthClient:
    def __init__(self, settings: VkOAuthSettings, *, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)

    def begin(self) -> tuple[str, PendingAuthorization]:
        verifier = make_code_verifier()
        pending = PendingAuthorization(
            state=secrets.token_urlsafe(32),
            code_verifier=verifier,
            created_at=int(time.time()),
        )
        params = {
            "client_id": str(self.settings.app_id),
            "app_id": str(self.settings.app_id),
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "scope": " ".join(self.settings.scopes),
            "state": pending.state,
            "prompt": "consent",
            "sdk_type": "vkid",
        }
        return f"{VK_ID_AUTHORIZE}?{urlencode(params)}", pending

    @staticmethod
    def parse_callback(callback_url: str, pending: PendingAuthorization) -> dict[str, str]:
        values = parse_qs(urlparse(callback_url).query)
        if values.get("error"):
            raise ValueError(values.get("error_description", values["error"])[0])
        required = {name: values.get(name, [""])[0] for name in ("code", "state", "device_id")}
        if not all(required.values()):
            raise ValueError("В callback отсутствуют code, state или device_id")
        if not secrets.compare_digest(required["state"], pending.state):
            raise ValueError("OAuth state не совпадает; авторизацию нужно начать заново")
        return required

    def exchange(self, callback_url: str, pending: PendingAuthorization) -> dict:
        callback = self.parse_callback(callback_url, pending)
        params = {
            "grant_type": "authorization_code",
            "redirect_uri": self.settings.redirect_uri,
            "client_id": str(self.settings.app_id),
            "code_verifier": pending.code_verifier,
            "state": pending.state,
            "device_id": callback["device_id"],
        }
        response = self.http.post(VK_ID_TOKEN, params=params, data={"code": callback["code"]})
        response.raise_for_status()
        result = response.json() or {}
        if result.get("error"):
            raise RuntimeError(result.get("error_description") or result["error"])
        if result.get("state") and result["state"] != pending.state:
            raise RuntimeError("VK вернул другой OAuth state")
        result["device_id"] = callback["device_id"]
        return result

    def refresh(self, refresh_token: str, device_id: str) -> dict:
        state = secrets.token_urlsafe(32)
        params = {
            "grant_type": "refresh_token",
            "redirect_uri": self.settings.redirect_uri,
            "client_id": str(self.settings.app_id),
            "device_id": device_id,
            "state": state,
        }
        response = self.http.post(VK_ID_TOKEN, params=params,
                                  data={"refresh_token": refresh_token})
        response.raise_for_status()
        result = response.json() or {}
        if result.get("error"):
            raise RuntimeError(result.get("error_description") or result["error"])
        if result.get("state") and result["state"] != state:
            raise RuntimeError("VK вернул другой OAuth state")
        result["device_id"] = device_id
        return result


def missing_photo_scopes(granted_scope: str | None) -> tuple[str, ...]:
    """Каких прав не хватает выданному токену для нативной загрузки фото.

    VK ID не отказывает в авторизации, а молча сужает ``scope`` до согласованного
    для приложения. Поэтому фактический список прав нужно сверять после обмена, а
    не полагаться на то, что было запрошено.
    """
    granted = set((granted_scope or "").replace(",", " ").split())
    return tuple(name for name in PHOTO_PUBLISH_SCOPES if name not in granted)


def resolve_publisher_token(*, store_path: str | Path, env_token: str = "",
                            app_id: int = 0, redirect_uri: str = "",
                            client: "VkOAuthClient | None" = None) -> str:
    """Токен для публикации: пользовательский из ``store_path``, иначе ключ сообщества.

    Ключ сообщества физически не может вызывать ``photos.getWallUploadServer`` —
    VK отвечает ошибкой 27 для всей группы методов загрузки. Нативное фото требует
    пользовательского токена, а он живёт час, поэтому его нельзя держать статично в
    ``.env``: значение берётся из хранилища и обновляется по refresh_token.
    Ключ сообщества остаётся резервом — текстовые записи он публикует.
    """
    store = VkTokenStore(store_path)
    if not store.load().get("access_token"):
        return env_token
    oauth = client or VkOAuthClient(VkOAuthSettings(app_id, redirect_uri))
    try:
        return store.access_token(oauth)
    except (RuntimeError, KeyError, ValueError, httpx.HTTPError):
        # Протухший refresh_token не должен останавливать текстовую публикацию.
        return env_token


def _pending_from(path: Path) -> PendingAuthorization:
    return PendingAuthorization(**json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VK ID OAuth 2.1 с PKCE")
    parser.add_argument("command", choices=("start", "exchange", "refresh"))
    parser.add_argument("--app-id", type=int, default=54732587)
    parser.add_argument("--redirect-uri", default="https://climat-simf.ru/")
    parser.add_argument("--pending", default="state/vk-oauth-pending.json")
    parser.add_argument("--tokens", default="state/vk-tokens.json")
    parser.add_argument("--callback-url")
    args = parser.parse_args(argv)

    client = VkOAuthClient(VkOAuthSettings(args.app_id, args.redirect_uri))
    pending_path = Path(args.pending)
    store = VkTokenStore(args.tokens)
    if args.command == "start":
        url, pending = client.begin()
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
        try:
            pending_path.chmod(0o600)
        except OSError:
            pass
        print(url)
        return 0
    if args.command == "exchange":
        if not args.callback_url:
            parser.error("exchange требует --callback-url")
        tokens = client.exchange(args.callback_url, _pending_from(pending_path))
        store.save(tokens)
        print("VK tokens saved securely")
        return 0
    current = store.load()
    tokens = client.refresh(current["refresh_token"], current["device_id"])
    store.save(tokens)
    print("VK access token refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
