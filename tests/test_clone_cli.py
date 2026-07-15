from webapp.main import parse_pairs_csv, _clone_modes


def test_clone_modes():
    # default: no flags -> dry_run=True, scan_roles=False (groups-only preview)
    assert _clone_modes(False, False) == (True, False)
    # --dry-run only -> dry_run=True, scan_roles=True (full plan, no writes)
    assert _clone_modes(False, True) == (True, True)
    # --apply only -> dry_run=False, scan_roles=True (live write)
    assert _clone_modes(True, False) == (False, True)
    # --apply --dry-run -> --dry-run wins, no write (safety invariant)
    assert _clone_modes(True, True) == (True, True)


def test_parse_pairs_csv_header_and_extra_columns(tmp_path):
    p = tmp_path / "pairs.csv"
    p.write_text("main,clone,note\n"
                 "a@x.y,a@z.y,hi\n"
                 "557058:1-2,557058:3-4,\n"
                 "\n", encoding="utf-8")          # blank row ignored
    pairs = parse_pairs_csv(str(p))
    assert pairs == [("a@x.y", "a@z.y"), ("557058:1-2", "557058:3-4")]


def test_parse_pairs_csv_requires_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("user,target\na,b\n", encoding="utf-8")
    try:
        parse_pairs_csv(str(p))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "main" in str(e) and "clone" in str(e)
