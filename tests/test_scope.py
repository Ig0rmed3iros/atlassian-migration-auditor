from auditor.scope import match_projects

SRC = [
    {"key": "AC", "name": "AC Support", "id": "100"},
    {"key": "MS", "name": "Managed Services", "id": "101"},
    {"key": "OLD", "name": "Legacy", "id": "102"},
]
TGT = [
    {"key": "AC", "name": "AC Support", "id": "900"},
    {"key": "MS", "name": "Managed Services", "id": "901"},
    {"key": "NEW", "name": "Greenfield", "id": "902"},
]


def test_match_by_key():
    m = match_projects(SRC, TGT)
    keys = [p["key"] for p in m["matched"]]
    assert keys == ["AC", "MS"]
    ac = m["matched"][0]
    assert ac["src_id"] == "100" and ac["tgt_id"] == "900"
    assert ac["src_count"] is None and ac["tgt_count"] is None


def test_source_and_target_only():
    m = match_projects(SRC, TGT)
    assert [p["key"] for p in m["source_only"]] == ["OLD"]
    assert [p["key"] for p in m["target_only"]] == ["NEW"]


def test_empty_sides():
    m = match_projects([], TGT)
    assert m["matched"] == [] and len(m["target_only"]) == 3
