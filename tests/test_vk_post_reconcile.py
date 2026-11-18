import sqlite3

from content_factory.analytics.vk import Metric, Publication, VkAnalyticsStore, collect


class FakeClient:
    """Стена VK: отложенные записи 175 и 188 вышли под номерами 184 и 192."""

    def __init__(self, wall):
        self.wall = wall
        self.asked = []

    def post_metric(self, publication):
        self.asked.append(publication.post_id)
        if publication.post_id not in {post["id"] for post in self.wall}:
            return None
        return Metric(publication.plan_id, 10, None, 1, 0, 0)

    def wall_posts(self, since):
        return [post for post in self.wall if post["date"] >= since]


def make_store(tmp_path, rows):
    path = tmp_path / "vk-plan.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE vk_content_plan (id INTEGER PRIMARY KEY, caption TEXT, "
                           "vk_post_id INTEGER)")
        connection.executemany("INSERT INTO vk_content_plan VALUES (?,?,?)",
                               [(plan_id, caption, post_id) for plan_id, caption, post_id, _ in rows])
    store = VkAnalyticsStore(path)
    for plan_id, _, post_id, due in rows:
        store.record_publication(Publication(plan_id, f"agent:{plan_id}", post_id, due,
                                             "", "expert", "u"), now=1)
    return store, path


def plan_ids(path):
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT id, vk_post_id FROM vk_content_plan"))


def test_moved_scheduled_posts_are_repointed_and_measured(tmp_path):
    store, path = make_store(tmp_path, [
        (160, "❄️ Тепловой насос\nтекст", 175, 1000),
        (162, "❄️ Мощность по комнате\nтекст", 188, 5000),
        (170, "❄️ Живой пост", 200, 9000),
    ])
    client = FakeClient([
        {"id": 200, "date": 9001, "text": "❄️ Живой пост"},
        {"id": 192, "date": 5002, "text": "❄️ Мощность по комнате\n…"},
        {"id": 184, "date": 1001, "text": "❄️ Тепловой насос\n…"},
    ])

    result = collect(store, client, now=10_000)

    assert result == {"collected": 3, "missing": 0, "repointed": 2, "errors": []}
    assert plan_ids(path) == {160: 184, 162: 192, 170: 200}
    assert store.known_post_ids() == {184, 192, 200}


def test_ambiguous_or_foreign_post_is_not_repointed(tmp_path):
    store, path = make_store(tmp_path, [
        (1, "❄️ Первый", 10, 1000),
        (2, "❄️ Второй", 20, 5000),
    ])
    client = FakeClient([
        {"id": 31, "date": 1001, "text": "❄️ Первый"},
        {"id": 32, "date": 1003, "text": "❄️ Первый"},       # два кандидата — не угадываем
        {"id": 33, "date": 5001, "text": "Чужая запись"},    # совпало время, но не текст
        {"id": 34, "date": 4990, "text": "❄️ Второй"},       # вышла раньше срока — не она
    ])

    result = collect(store, client, now=10_000)

    assert result["repointed"] == 0 and result["missing"] == 2
    assert plan_ids(path) == {1: 10, 2: 20}
