from datetime import datetime

import pytest

from m7manager import scheduler
from m7manager.services import Manager

from .fakes import FakeRuntime


def ts(text):
    return datetime.fromisoformat(text + "+08:00").timestamp()


@pytest.mark.parametrize(
    "late,state",
    [
        ("2026-09-08T04:16:00", "QUEUED"),
        ("2026-09-08T07:00:00", "MISSED"),
        ("2026-09-12T04:30:00", "QUEUED"),
    ],
)
def test_misfire_coalesces_and_dedupes(tmp_path, late, state):
    now = ts("2026-09-08T00:00:00")
    m = Manager(tmp_path, FakeRuntime(lambda: now), lambda: now)
    a = m.add_account("A")
    m.edit_account(a, "A", True, "04:15", True, 3600, {})
    scheduler.tick(m.store, ts(late))
    scheduler.tick(m.store, ts(late))
    rows = m.store.rows("SELECT * FROM triggers")
    assert len(rows) == 1 and rows[0]["state"] == state
    assert m.store.one("SELECT next_run_at FROM schedules")["next_run_at"] > ts(late)
    m.close()


def test_schedule_edit_cancels_old_pending(tmp_path):
    now = ts("2026-09-08T00:00:00")
    m = Manager(tmp_path, FakeRuntime(lambda: now), lambda: now)
    a = m.add_account("A")
    m.edit_account(a, "A", True, "04:15", True, 3600, {})
    scheduler.tick(m.store, ts("2026-09-08T04:20:00"))
    m.edit_account(a, "A", True, "08:00", True, 3600, {})
    assert m.store.one("SELECT state FROM triggers")["state"] == "CANCELLED"
    m.close()


@pytest.mark.parametrize("text", ["24:00", "4:00", "oops", "12:60"])
def test_reject_invalid_time(text):
    with pytest.raises(ValueError):
        scheduler.validate_time(text)
