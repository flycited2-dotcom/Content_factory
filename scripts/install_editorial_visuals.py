"""Раскладывает готовые кадры генератора по темам редакции.

Задание в очереди фотоагента несёт текст сцены в поле model, а готовый файл
лежит в output под именем research_<id>.png. Скрипт находит для каждой темы без
картинки последнее выполненное задание с её сценой и копирует файл туда, где его
ищет планировщик (в оба дерева: рабочее и соседнее). Идемпотентен.

    python scripts/install_editorial_visuals.py
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ROOTS = (Path("/opt/content-factory-vk"), Path("/opt/content-factory"))
QUEUE_DB = "/root/ritualb2b/queue.db"
OUTPUT_DIR = Path("/root/ritualb2b/output")

sys.path.insert(0, str(ROOTS[0] / "src"))
from content_factory.agents.editorial import load_ideas  # noqa: E402


def main() -> int:
    ideas, _ = load_ideas(ROOTS[0] / "config" / "vk-editorial-sources.yaml")
    connection = sqlite3.connect(f"file:{QUEUE_DB}?mode=ro", uri=True)
    installed = waiting = 0
    for idea in ideas:
        if idea.card_big or not idea.visual:
            continue
        target = ROOTS[0] / "assets" / "generated" / "editorial" / f"{idea.id}.png"
        if target.is_file():
            continue
        row = connection.execute(
            "SELECT id, output_filename FROM jobs WHERE mode='research' AND status='done' "
            "AND model=? AND output_filename IS NOT NULL ORDER BY id DESC LIMIT 1",
            (idea.visual,)).fetchone()
        source = OUTPUT_DIR / row[1] if row else None
        if not (source and source.is_file()):
            waiting += 1
            continue
        for root in ROOTS:
            destination = root / "assets" / "generated" / "editorial" / f"{idea.id}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        print(f"{idea.id}: кадр из задания {row[0]}")
        installed += 1
    print(f"итого: установлено {installed}, ждут генерации {waiting}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
