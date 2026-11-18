"""Хранилище пользовательского токена VK: пустой файл не роняет планировщик,
запись атомарна (грабля 2026-10-14: диск заполнился, файл обнулился, план стоял)."""
from content_factory.publish.vk_oauth import VkTokenStore, resolve_publisher_token


def test_empty_or_broken_store_reads_as_missing(tmp_path):
    path = tmp_path / "vk-tokens.json"
    path.write_text("", encoding="utf-8")
    assert VkTokenStore(path).load() == {}
    path.write_text("{oops", encoding="utf-8")
    assert VkTokenStore(path).load() == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert VkTokenStore(path).load() == {}


def test_empty_store_falls_back_to_community_key(tmp_path):
    path = tmp_path / "vk-tokens.json"
    path.write_text("", encoding="utf-8")
    assert resolve_publisher_token(store_path=path, env_token="community") == "community"


def test_save_replaces_file_atomically(tmp_path):
    path = tmp_path / "vk-tokens.json"
    VkTokenStore(path).save({"access_token": "a"})
    VkTokenStore(path).save({"access_token": "b"})
    assert VkTokenStore(path).load()["access_token"] == "b"
    assert [p.name for p in tmp_path.iterdir()] == ["vk-tokens.json"]


def test_refresh_happens_once_and_is_saved(tmp_path):
    import time
    from content_factory.publish.vk_oauth import VkTokenStore

    class Client:
        calls = 0

        def refresh(self, refresh_token, device_id):
            Client.calls += 1
            return {"access_token": "new", "refresh_token": "r2", "device_id": device_id,
                    "expires_in": 3600}

    store = VkTokenStore(tmp_path / "t.json")
    store.save({"access_token": "old", "refresh_token": "r1", "device_id": "d", "expires_in": 0})
    store.path.write_text(store.path.read_text().replace(
        f'"saved_at": {int(time.time())}', '"saved_at": 1'))
    assert store.access_token(Client()) == "new"
    assert store.access_token(Client()) == "new"
    assert Client.calls == 1
