import pytest

from grok_imagine_mcp.store import TaskStore


def _task(tid, created):
    return {"task_id": tid, "kind": "video", "created_at": created}


def test_save_load_roundtrip_and_no_tmp_left(tmp_path):
    s = TaskStore(tmp_path / "deep" / "tasks")          # каталога ещё нет — создаётся сам
    s.save(_task("aaaaaaaaaaaa", "2026-09-30T10:00:00+00:00"))
    assert s.load("aaaaaaaaaaaa")["kind"] == "video"
    assert [p.name for p in (tmp_path / "deep" / "tasks").iterdir()] == ["aaaaaaaaaaaa.json"]


def test_load_missing_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        TaskStore(tmp_path).load("bbbbbbbbbbbb")


@pytest.mark.parametrize("bad", ["../x", "abc", "AAAAAAAAAAAA", "aaaaaaaaaaaa.json", "a" * 13, ""])
def test_bad_task_id_is_rejected_before_touching_disk(tmp_path, bad):
    # task_id приходит от модели: ../ не должен вывести за пределы папки
    with pytest.raises(KeyError):
        TaskStore(tmp_path).load(bad)


def test_list_newest_first_with_limit(tmp_path):
    s = TaskStore(tmp_path)
    s.save(_task("111111111111", "2026-09-30T10:00:00+00:00"))
    s.save(_task("222222222222", "2026-09-30T12:00:00+00:00"))
    s.save(_task("333333333333", "2026-09-30T11:00:00+00:00"))
    assert [t["task_id"] for t in s.list(2)] == ["222222222222", "333333333333"]


def test_list_ignores_foreign_and_broken_files(tmp_path):
    s = TaskStore(tmp_path)
    s.save(_task("111111111111", "2026-09-30T10:00:00+00:00"))
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "222222222222.json").write_text("{ не json")
    assert [t["task_id"] for t in s.list(10)] == ["111111111111"]


def test_new_id_format():
    tid = TaskStore.new_id()
    assert len(tid) == 12 and int(tid, 16) >= 0
