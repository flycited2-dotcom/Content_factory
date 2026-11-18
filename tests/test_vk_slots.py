from datetime import datetime

from content_factory.orchestrator.vk_content_plan import plan_slots, slot_kind, slot_ordinal


def _at(day: int, hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return int(datetime(2026, 11, day, hour, minute).timestamp())


def _times(slots):
    return [datetime.fromtimestamp(s).strftime("%d %H:%M") for s in slots]


def test_default_schedule_unchanged(monkeypatch):
    monkeypatch.delenv("VK_PLAN_TIMES", raising=False)
    monkeypatch.delenv("VK_EDITORIAL_TIMES", raising=False)
    slots = plan_slots(datetime(2026, 11, 4, 0, 0), horizon_days=1)
    assert _times(slots) == ["04 11:30", "04 18:30"]
    assert slot_kind(_at(4, "11:30")) == "product"
    assert slot_kind(_at(4, "18:30")) == "editorial"
    # Порядковый номер слота прежний: от него зависит ротация групп товара.
    assert slot_ordinal(_at(4, "11:30")) == datetime(2026, 11, 4).toordinal() * 2
    assert slot_ordinal(_at(4, "18:30")) == datetime(2026, 11, 4).toordinal() * 2 + 1


def test_product_heavy_schedule_from_env(monkeypatch):
    monkeypatch.setenv("VK_PLAN_TIMES", "10:00, 11:30,15:30,18:30")
    monkeypatch.setenv("VK_EDITORIAL_TIMES", "18:30")
    slots = plan_slots(datetime(2026, 11, 4, 0, 0), horizon_days=1)
    assert _times(slots) == ["04 10:00", "04 11:30", "04 15:30", "04 18:30"]
    assert [slot_kind(s) for s in slots] == ["product", "product", "product", "editorial"]
    # У каждого слота дня свой номер: иначе два товара за день попадали бы в одну группу.
    assert len({slot_ordinal(s) for s in slots}) == 4


def test_explicit_times_argument_still_wins(monkeypatch):
    monkeypatch.setenv("VK_PLAN_TIMES", "10:00,18:30")
    slots = plan_slots(datetime(2026, 11, 4, 0, 0), horizon_days=1, times=("15:00",))
    assert _times(slots) == ["04 15:00"]
