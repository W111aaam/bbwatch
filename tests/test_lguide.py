from bbwatch.lguide import build_snapshot
from bbwatch.store import Store


def test_build_snapshot_exports_tasks_announcements_and_materials(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    now = "2026-11-17T02:00:00.000Z"
    store.apply_change(type("Change", (), {"seen": {
        "entity_key": "col:_c:1", "kind": "column", "course_id": "_c", "bb_id": "1",
        "due_utc": "2026-11-20T10:00:00.000Z", "grade_status": "None", "grade_score": None,
        "payload": {"name": "Homework 1", "course_code": "CSC1001", "score_possible": 100}, "scan_id": 1,
    }, "events": []})(), now)
    store.apply_change(type("Change", (), {"seen": {
        "entity_key": "ann:_c:2", "kind": "announcement", "course_id": "_c", "bb_id": "2",
        "payload": {"title": "Welcome", "body": "Hello", "created": now}, "scan_id": 1,
    }, "events": []})(), now)
    store.apply_change(type("Change", (), {"seen": {
        "entity_key": "content:_c:3", "kind": "content", "course_id": "_c", "bb_id": "3",
        "payload": {"title": "Lecture 1", "handler": "resource/x-bb-file", "modified": now}, "scan_id": 1,
    }, "events": []})(), now)
    store.close()

    snapshot = build_snapshot(db)
    assert snapshot["tasks"][0]["title"] == "Homework 1"
    assert snapshot["announcements"][0]["title"] == "Welcome"
    assert snapshot["materials"][0]["title"] == "Lecture 1"
    assert snapshot["courses"] == [{"course_id": "_c", "course_name": "CSC1001"}]
