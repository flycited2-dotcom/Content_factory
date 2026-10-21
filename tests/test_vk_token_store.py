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
