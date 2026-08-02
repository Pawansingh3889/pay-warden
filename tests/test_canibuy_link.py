"""Reading a sibling product's database without depending on it, or breaking it.

The theme of every test here: this panel is allowed to be absent, stale or
empty, and none of those may reach the reader as an exception.
"""

import sqlite3

import pytest

from pay_warden import canibuy_link

CANIBUY_DDL = """
CREATE TABLE merchants (
    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL
);
CREATE TABLE probes (
    id INTEGER PRIMARY KEY, merchant_id INTEGER NOT NULL REFERENCES merchants(id),
    ts TEXT NOT NULL, grade TEXT NOT NULL, automation_hostile INTEGER NOT NULL DEFAULT 0,
    route TEXT NOT NULL DEFAULT 'none', stages TEXT NOT NULL
);
"""


@pytest.fixture(autouse=True)
def no_ambient_config(monkeypatch):
    """A developer's own CANIBUY_DB must not decide what these tests see."""
    for name in canibuy_link.ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def registry(tmp_path, rows, name="canibuy.sqlite3"):
    """Build a registry the way canibuy would have written it."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(CANIBUY_DDL)
    for i, (url, probes) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO merchants VALUES (?,?,?,?)", (i, url, url, "2026-08-01T00:00:00+00:00")
        )
        for grade, ts, stages in probes:
            conn.execute(
                "INSERT INTO probes (merchant_id, ts, grade, stages) VALUES (?,?,?,?)",
                (i, ts, grade, stages),
            )
    conn.commit()
    conn.close()
    return path


def test_absent_configuration_is_a_sentence_not_an_error():
    found = canibuy_link.read()

    assert found["available"] is False
    assert found["reason"] == "not configured"
    assert canibuy_link.ENV_VARS[0] in found["how"]


def test_a_missing_file_names_the_path_it_looked_for(tmp_path, monkeypatch):
    """So the reader can fix it without reading the source."""
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(tmp_path / "nowhere.sqlite3"))

    found = canibuy_link.read()

    assert found["available"] is False
    assert "nowhere.sqlite3" in found["reason"]


def test_a_database_that_is_not_a_registry_degrades(tmp_path, monkeypatch):
    path = tmp_path / "wrong.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE something_else (id INTEGER)")
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    found = canibuy_link.read()

    assert found["available"] is False
    assert "not a canibuy registry" in found["reason"]


def test_the_connection_cannot_write(tmp_path, monkeypatch):
    """Proving mode=ro rather than asserting it in a docstring. This process has
    no business writing to another product's database."""
    path = registry(tmp_path, [("https://a.example", [("B", "2026-08-01T00:00:00+00:00", "[]")])])

    conn = canibuy_link._connect(str(path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO merchants VALUES (99,'https://x.example','x','now')")
    finally:
        conn.close()


def test_only_the_latest_probe_of_each_merchant_counts(tmp_path, monkeypatch):
    path = registry(
        tmp_path,
        [
            (
                "https://a.example",
                [("F", "2026-07-01T00:00:00+00:00", "[]"), ("B", "2026-08-01T00:00:00+00:00", "[]")],
            )
        ],
    )
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    found = canibuy_link.read()

    assert found["by_grade"] == [{"grade": "B", "merchants": 1, "attempts": 0}]


def test_a_regrade_is_reported_as_drift(tmp_path, monkeypatch):
    """A grade is a probe result at a point in time, not a contract — this
    registry has moved a merchant from C to F inside a day."""
    path = registry(
        tmp_path,
        [
            (
                "https://a.example",
                [("C", "2026-07-31T00:00:00+00:00", "[]"), ("F", "2026-08-01T00:00:00+00:00", "[]")],
            )
        ],
    )
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    drift = canibuy_link.read()["drift"]

    assert drift == [
        {
            "url": "https://a.example",
            "from_grade": "C",
            "to_grade": "F",
            "to_ts": "2026-08-01T00:00:00+00:00",
        }
    ]


def test_the_join_is_on_host_not_url(tmp_path, monkeypatch):
    """pay-warden records a full merchant URL; canibuy records a site. They meet
    on the registrable host the engine already matches merchant rules against."""
    path = registry(
        tmp_path, [("https://www.adafruit.com/", [("C", "2026-08-01T00:00:00+00:00", "[]")])]
    )
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    found = canibuy_link.read({"adafruit.com": {"attempts": 7}})

    assert found["coverage"] == {"attempted_hosts": 1, "matched": 1, "unmatched": 0}
    assert found["by_grade"] == [{"grade": "C", "merchants": 1, "attempts": 7}]


def test_both_registry_denominators_are_reported(tmp_path, monkeypatch):
    """all_latest() inner-joins probes, so an unprobed merchant is absent from
    the grades. Reporting one number would silently disagree with canibuy."""
    path = registry(
        tmp_path,
        [
            ("https://a.example", [("B", "2026-08-01T00:00:00+00:00", "[]")]),
            ("https://never-probed.example", []),
        ],
    )
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    assert canibuy_link.read()["registry"] == {"merchants": 2, "graded": 1}


def test_the_work_queue_carries_an_owner_and_a_fix(tmp_path, monkeypatch):
    """A failing merchant without an owner is a complaint; with one it is a task."""
    stages = '[{"stage": 3, "status": "fail", "failure_class": "login-wall"}]'
    path = registry(tmp_path, [("https://a.example", [("C", "2026-08-01T00:00:00+00:00", stages)])])
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    queue = canibuy_link.read({"a.example": {"attempts": 12}})["work_queue"]

    assert queue[0]["failure_class"] == "login-wall"
    assert queue[0]["owner"] == "merchant"
    assert queue[0]["unlockable"] is True
    assert queue[0]["attempts_blocked"] == 12


def test_an_unrecognised_failure_class_does_not_crash_the_queue(tmp_path, monkeypatch):
    """canibuy may add one before this mirror is updated."""
    stages = '[{"stage": 3, "status": "fail", "failure_class": "some-new-thing"}]'
    path = registry(tmp_path, [("https://a.example", [("F", "2026-08-01T00:00:00+00:00", stages)])])
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    queue = canibuy_link.read()["work_queue"]

    assert queue[0]["failure_class"] == "some-new-thing"
    assert queue[0]["owner"] == "unknown"


def test_a_buyable_merchant_contributes_no_work(tmp_path, monkeypatch):
    """The queue is what to fix, not what was probed."""
    stages = '[{"stage": 3, "status": "partial", "failure_class": "login-wall"}]'
    path = registry(tmp_path, [("https://a.example", [("B", "2026-08-01T00:00:00+00:00", stages)])])
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    assert canibuy_link.read()["work_queue"] == []


def test_malformed_stages_json_is_survivable(tmp_path, monkeypatch):
    path = registry(tmp_path, [("https://a.example", [("F", "2026-08-01T00:00:00+00:00", "{oops")])])
    monkeypatch.setenv("PAY_WARDEN_CANIBUY_DB", str(path))

    assert canibuy_link.read()["available"] is True
