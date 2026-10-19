"""Рисует типографские карточки для тем, у которых задана крупная цифра.

Карточки не тратят квоту генератора изображений: рисуются локально и сразу
кладутся туда, где их ищет планировщик. Скрипт идемпотентен — существующие
файлы не перезаписываются без флага --force.

    python scripts/render_text_cards.py [--root /opt/content-factory-vk] [--force]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from content_factory.agents.editorial import load_ideas  # noqa: E402
from content_factory.content.text_card import render_text_card  # noqa: E402

KICKERS = {"number": "Цифра дня", "myth": "Миф или правда", "poll": "Вопрос недели",
           "qa": "Вопрос из чата"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).parents[1]))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    ideas, _ = load_ideas(root / "config" / "vk-editorial-sources.yaml")
    made = skipped = 0
    for idea in ideas:
        if not idea.card_big:
            continue
        target = root / "assets" / "generated" / "editorial" / f"{idea.id}.png"
        if target.exists() and not args.force:
            skipped += 1
            continue
        render_text_card(idea.card_big, idea.card_small, target,
                         kicker=KICKERS.get(idea.format, ""))
        print("нарисована:", target.name)
        made += 1
    print(f"итого: нарисовано {made}, уже были {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
