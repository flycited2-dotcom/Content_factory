"""Детерминированный контур: идея → исследование → VK-редактор → критик.

На первом безопасном уровне идеи и проверенные факты задаются редакционной базой знаний.
Это исключает выдуманные характеристики: редактор может использовать только точные факты,
связанные с разрешённым официальным источником, а критик блокирует иной результат.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


PROHIBITED = ("политик", "выборы", "военн", "санкци", "общественн новост")

# Сколько пунктов уходит в пост. В живой ленте более длинный список
# читался стеной и утаскивал призыв с ссылкой под «Показать ещё».
MAX_POST_FACTS = 4


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    publisher: str
    url: str
    source_type: str
    checked_at: str


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    source: Source


@dataclass(frozen=True)
class Idea:
    id: str
    category: str
    content_type: str
    title: str
    intro: str
    cta: str
    visual: str
    facts: tuple[Fact, ...]
    # Крючок и предложение — необязательные: тема без них собирается по-старому.
    hook: str = ""
    offer: str = ""


@dataclass(frozen=True)
class EditorialDraft:
    idea_id: str
    category: str
    content_type: str
    text: str
    fact_ids: tuple[str, ...]
    source_urls: tuple[str, ...]
    visual_prompt: str


@dataclass(frozen=True)
class CriticVerdict:
    ok: bool
    reasons: tuple[str, ...]


def load_ideas(path: str | Path) -> tuple[list[Idea], set[str]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    trusted = {str(value).casefold() for value in raw.get("trusted_domains", [])}
    source_map = {}
    for key, item in (raw.get("sources") or {}).items():
        source_map[str(key)] = Source(
            id=str(key), title=str(item["title"]), publisher=str(item["publisher"]),
            url=str(item["url"]), source_type=str(item.get("type", "official")),
            checked_at=str(item["checked_at"]),
        )
    ideas = []
    for item in raw.get("topics", []):
        facts = []
        for fact in item.get("facts", []):
            source_id = str(fact["source"])
            if source_id not in source_map:
                raise ValueError(f"Неизвестный источник {source_id} в теме {item.get('id')}")
            facts.append(Fact(str(fact["id"]), str(fact["text"]), source_map[source_id]))
        ideas.append(Idea(
            id=str(item["id"]), category=str(item["category"]),
            content_type=str(item["content_type"]), title=str(item["title"]),
            intro=str(item["intro"]), cta=str(item["cta"]),
            visual=str(item.get("visual", "")).strip(), facts=tuple(facts),
            hook=str(item.get("hook", "")).strip(),
            offer=str(item.get("offer", "")).strip(),
        ))
    return ideas, trusted


def select_post_facts(facts: tuple[Fact, ...]) -> tuple[Fact, ...]:
    """Отобрать пункты, которые реально уходят в пост.

    Функция общая намеренно: тексты собирает и конвейер, и разовая
    пересборка уже запланированных постов. Разойдись они — в ленте
    оказались бы посты двух разных форматов.
    """
    return tuple(facts)[:MAX_POST_FACTS]


class IdeaAgent:
    def choose(self, ideas: list[Idea], used: set[str], limit: int) -> list[Idea]:
        return [idea for idea in ideas if idea.id not in used][:max(0, int(limit))]


class ResearchAgent:
    def __init__(self, trusted_domains: set[str]):
        self.trusted_domains = {value.casefold() for value in trusted_domains}

    def verify(self, idea: Idea) -> tuple[Fact, ...]:
        if not idea.facts:
            raise ValueError(f"У темы {idea.id} нет фактов")
        for fact in idea.facts:
            host = (urlparse(fact.source.url).hostname or "").casefold()
            if fact.source.source_type not in {"official", "manufacturer", "supplier"}:
                raise ValueError(f"Недопустимый тип источника: {fact.source.source_type}")
            if not any(host == domain or host.endswith(f".{domain}")
                       for domain in self.trusted_domains):
                raise ValueError(f"Недоверенный домен источника: {host}")
            if not fact.text.strip():
                raise ValueError(f"Пустой факт {fact.id}")
        return idea.facts


class VkEditorialAgent:
    def write(self, idea: Idea, facts: tuple[Fact, ...]) -> EditorialDraft:
        headings = {
            "service": "Чек-лист:",
            "comparison": "Что важно сравнить:",
            "trust": "Как проходит профессиональный подбор:",
        }
        heading = headings.get(idea.content_type, "Что важно проверить:")
        # Пустая строка между пунктами: так список сканируется на телефоне.
        # Пустая строка между пунктами: так список сканируется на телефоне.
        bullets = "\n\n".join(
            f"{index}. {fact.text}" for index, fact in enumerate(facts, 1)
        )
        sources = []
        for fact in facts:
            if fact.source.url not in [value[1] for value in sources]:
                sources.append((fact.source.publisher, fact.source.url))
        # Полные технические URL нужны исследователю и критику, но не читателю.
        # Они сохраняются в source_urls и vk_editorial_audit; публичный текст
        # остаётся компактным и получает одну клиентскую ссылку на этапе публикации.
        # Пост читают с первой строки. Если у темы есть крючок — узнаваемая
        # проблема читателя, — он и открывает пост, а сухой заголовок темы в
        # тело не выводится: он остаётся служебным именем материала.
        opening = idea.hook.strip() or idea.title
        closing = "\n\n".join(part for part in (idea.offer.strip(), idea.cta) if part)
        text = f"{opening}\n\n{idea.intro}\n\n{heading}\n\n{bullets}\n\n{closing}"
        return EditorialDraft(
            idea_id=idea.id, category=idea.category, content_type=idea.content_type,
            text=text, fact_ids=tuple(fact.id for fact in facts),
            source_urls=tuple(url for _, url in sources),
            visual_prompt=EditorialVisualAgent().build_prompt(idea),
        )


class EditorialVisualAgent:
    """Формирует строгий промпт для фотореалистичного редакционного кадра.

    Генератор получает сцену из редакционной базы, но не может добавлять текст,
    бренды и продающую графику. Это сохраняет изображение универсальным для VK.
    """

    def build_prompt(self, idea: Idea) -> str:
        if not idea.visual:
            raise ValueError(f"У темы {idea.id} нет визуального задания")
        return "\n".join([
            "Use case: photorealistic-natural",
            "Asset type: square thematic image for a VK educational post",
            f"Primary request: {idea.visual}",
            "Style/medium: premium documentary commercial photography, true-to-life materials and proportions, not a 3D render",
            "Composition/framing: square 1:1, one clear focal point, safe margins for VK mobile and desktop cropping",
            "Lighting/mood: soft natural light, trustworthy professional mood, realistic shadows and color balance",
            "Constraints: technically plausible scene; no text; no captions; no logos; no brand marks; no watermark",
            "Avoid: glossy CGI, staged stock-photo pose, distorted hands, floating objects, sales graphics, illegible labels",
        ])


class StrictCriticAgent:
    def __init__(self, trusted_domains: set[str], max_length: int = 3500):
        self.trusted_domains = {value.casefold() for value in trusted_domains}
        self.max_length = int(max_length)

    def review(self, idea: Idea, facts: tuple[Fact, ...], draft: EditorialDraft) -> CriticVerdict:
        reasons = []
        lower = draft.text.casefold()
        if any(term in lower for term in PROHIBITED):
            reasons.append("запрещённая тема")
        if len(draft.text) > self.max_length:
            reasons.append(f"текст длиннее лимита ({len(draft.text)} > {self.max_length})")
        expected = tuple(fact.id for fact in facts)
        if not 3 <= len(facts) <= 7:
            reasons.append(
                f"для законченного поста требуется 3–7 проверенных пунктов, сейчас {len(facts)}"
            )
        if draft.fact_ids != expected:
            reasons.append("набор фактов редактора не совпадает с исследованием")
        for index, fact in enumerate(facts, 1):
            if f"{index}. {fact.text}" not in draft.text:
                reasons.append(f"факт {fact.id} изменён или потерян")
            if fact.source.url not in draft.source_urls:
                reasons.append(f"нет ссылки для факта {fact.id}")
        if not draft.source_urls:
            reasons.append("нет источников")
        if not draft.visual_prompt:
            reasons.append("нет задания для тематического изображения")
        return CriticVerdict(not reasons, tuple(reasons))


class EditorialAuditStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_editorial_audit ("
                "idea_id TEXT PRIMARY KEY,research_json TEXT NOT NULL,draft TEXT NOT NULL,"
                "verdict_json TEXT NOT NULL,created_at INTEGER NOT NULL,"
                "visual_prompt TEXT NOT NULL DEFAULT '')"
            )
            columns = {str(row[1]) for row in connection.execute(
                "PRAGMA table_info(vk_editorial_audit)"
            )}
            if "visual_prompt" not in columns:
                connection.execute(
                    "ALTER TABLE vk_editorial_audit ADD COLUMN visual_prompt TEXT NOT NULL DEFAULT ''"
                )

    def record(self, idea: Idea, facts: tuple[Fact, ...], draft: EditorialDraft,
               verdict: CriticVerdict) -> None:
        research = [{"id": fact.id, "text": fact.text, "source": asdict(fact.source)}
                    for fact in facts]
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO vk_editorial_audit "
                "(idea_id,research_json,draft,verdict_json,created_at,visual_prompt) "
                "VALUES(?,?,?,?,?,?)",
                (idea.id, json.dumps(research, ensure_ascii=False), draft.text,
                 json.dumps(asdict(verdict), ensure_ascii=False), int(time.time()),
                 draft.visual_prompt),
            )


def build_editorial_drafts(path: str | Path, used: set[str], limit: int,
                           audit_db: str | Path | None = None) -> list[EditorialDraft]:
    ideas, trusted = load_ideas(path)
    research = ResearchAgent(trusted)
    editor = VkEditorialAgent()
    critic = StrictCriticAgent(trusted)
    audit = EditorialAuditStore(audit_db) if audit_db else None
    drafts = []
    for idea in IdeaAgent().choose(ideas, used, limit):
        facts = select_post_facts(research.verify(idea))
        draft = editor.write(idea, facts)
        verdict = critic.review(idea, facts, draft)
        if audit:
            audit.record(idea, facts, draft, verdict)
        if verdict.ok:
            drafts.append(draft)
    return drafts
