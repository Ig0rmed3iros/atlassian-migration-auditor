import gzip
import json

import pytest

from auditor.confluence.extract import extract_space, slim_page
from auditor.textnorm import content_fp, macro_signature, norm_ts, storage_text

BODY = (
    "<h1>Ops guide</h1><p>Restart the Globex feed nightly.</p>"
    '<ac:structured-macro ac:name="toc" ac:schema-version="1"/>'
    '<ac:structured-macro ac:name="jira">'
    '<ac:parameter ac:name="key">AC-1</ac:parameter></ac:structured-macro>'
    '<ac:structured-macro ac:name="jira">'
    '<ac:parameter ac:name="key">AC-2</ac:parameter></ac:structured-macro>'
)


def test_slim_page_full_shape():
    page = {
        "id": "10001",
        "title": "Acme Runbook",
        "ancestors": [{"title": "Acme Home"}, {"title": "Acme Ops"}],
        "history": {"createdDate": "2024-01-02T03:04:05.000Z",
                    "createdBy": {"displayName": "Igor Medeiros"}},
        "version": {"number": 7},
        "metadata": {"labels": {"results": [{"name": "runbook"},
                                            {"name": "acme"}]}},
        "body": {"storage": {"value": BODY}},
        "children": {
            "attachment": {"results": [{"title": "diagram.png",
                                        "extensions": {"fileSize": 2048}}],
                           "size": 1, "_links": {}},
            "comment": {"results": [{"id": "c1"}, {"id": "c2"}],
                        "_links": {}},
        },
    }
    display = storage_text(BODY)
    assert slim_page(page) == {
        "key": "Acme Runbook", "id": "10001", "fields": {
            "title": "Acme Runbook",
            "content_type": "page",          # default when type absent
            "parent": "Acme Ops",            # LAST ancestor = direct parent
            "created": norm_ts("2024-01-02T03:04:05.000Z"),
            "creator": "Igor Medeiros",
            "version": 7,
            "labels": ["acme", "runbook"],   # sorted
            "body": {"len": len(display), "sha": content_fp(display),
                     "head": display[:200]},
            "attachment": {"capped": False, "size": 1, "items": [
                {"filename": "diagram.png", "size": 2048}]},
            "comment": {"count": 2, "capped": False, "size": None},
            "macros": {"toc": 1, "jira": 2},
            "macro_sig": macro_signature(BODY),   # captures the AC-1/AC-2 params
        }}
    # The macro signature reflects the stripped ac:parameter targets (key=AC-1,
    # key=AC-2), so it differs from a page whose macros point elsewhere.
    assert slim_page(page)["fields"]["macro_sig"] != content_fp("")
    # created really is normalized to an epoch string, not the raw ISO form.
    assert slim_page(page)["fields"]["created"] != "2024-01-02T03:04:05.000Z"


def test_slim_page_tolerates_missing_expansions():
    # A page with no ancestors/metadata/children must slim without blowing
    # up: absent expansions read as empty, never as a crash mid-extraction.
    out = slim_page({"id": "1", "title": "Acme Home"})
    f = out["fields"]
    assert out["key"] == "Acme Home"
    assert f["parent"] is None and f["labels"] == []
    assert f["body"] == {"len": 0, "sha": content_fp(""), "head": ""}
    assert f["attachment"] == {"capped": False, "size": None, "items": []}
    assert f["comment"] == {"count": 0, "capped": False, "size": None}
    assert f["macros"] == {}


def test_slim_page_titleless_falls_back_to_id():
    # slim_page's own invariant: a missing field reads as empty, never as a
    # crash. key=None would poison compare_space downstream (sorted() can't
    # order None against str titles; the presence summary slices k[:200]) —
    # so a title-less content row keys by its id instead.
    out = slim_page({"id": "98304",
                     "body": {"storage": {"value": "<p>orphan prose</p>"}}})
    assert out["key"] == "98304"
    # id-less AND title-less (a degenerate API row) still yields a sortable,
    # sliceable string key, never None.
    bare = slim_page({"body": {"storage": {"value": "<p>x</p>"}}})
    assert isinstance(bare["key"], str)


def test_attachment_capped_detection():
    def with_attachments(catt):
        return {"id": "1", "title": "Acme Home", "children": {"attachment": catt}}
    # A next link means the inline expansion overflowed.
    capped = slim_page(with_attachments(
        {"results": [{"title": "a.png", "extensions": {"fileSize": 1}}],
         "_links": {"next": "/rest/api/content/1/child/attachment?start=25"}}))
    assert capped["fields"]["attachment"]["capped"] is True
    # So does a declared size larger than what came inline.
    short = slim_page(with_attachments(
        {"results": [{"title": "a.png", "extensions": {"fileSize": 1}}],
         "size": 3, "_links": {}}))
    assert short["fields"]["attachment"]["capped"] is True
    # Comment overflow is its own cap flag.
    com = slim_page({"id": "1", "title": "Acme Home", "children": {
        "comment": {"results": [{"id": "c1"}],
                    "_links": {"next": "/rest/api/content/1/child/comment?start=25"}}}})
    assert com["fields"]["comment"] == {"count": 1, "capped": True,
                                        "size": None}


def test_comment_capped_by_declared_size_without_next_link():
    """Belt-and-braces parity with attachments (audit findings 4/11): a
    declared comment size larger than the inline rows means truncation even
    when the envelope omits _links.next — two sides both silently capped at
    the same inline count must read uncheckable, never verified-equal."""
    com = slim_page({"id": "1", "title": "Acme Home", "children": {
        "comment": {"size": 40, "results": [{"id": f"c{i}"} for i in range(25)],
                    "_links": {}}}})
    assert com["fields"]["comment"] == {"count": 25, "capped": True,
                                        "size": 40}


class FakeConf:
    """Just the two methods extract_space drives; space_content() is an iterator
    so streaming behavior is exercised like the real client (pages + blogs)."""

    def __init__(self, pages, count):
        self._pages, self._count = pages, count

    def space_content(self, space_key, page_size=50):
        yield from self._pages

    def count_content(self, space_key):
        return self._count


def mk_page(title, body="<p>Globex feed prose</p>", pid="1"):
    return {"id": pid, "title": title,
            "body": {"storage": {"value": body}},
            "history": {"createdDate": "2024-01-02T03:04:05.000Z",
                        "createdBy": {"displayName": "Igor Medeiros"}},
            "version": {"number": 1}}


def test_extract_space_gz_and_verification(tmp_path):
    pages = [mk_page(f"Acme Page {i}", pid=str(i)) for i in range(3)]
    out = tmp_path / "ENG.core.jsonl.gz"
    res = extract_space(FakeConf(pages, 3), "ENG", str(out))
    assert res == {"extracted": 3, "approx": 3, "verified": True}
    with gzip.open(out, "rt") as fh:
        rows = [json.loads(l) for l in fh]
    assert "_extract_format" in rows[0]          # format stamp header first
    assert [r["key"] for r in rows[1:]] == ["Acme Page 0", "Acme Page 1",
                                            "Acme Page 2"]
    # CQL count disagreeing with what was enumerated → unverified, loudly.
    res2 = extract_space(FakeConf(pages, 4), "ENG", str(tmp_path / "y.gz"))
    assert res2["verified"] is False and res2["approx"] == 4
    # ERR-string count (search endpoint down) → unverified, not a crash.
    res3 = extract_space(FakeConf(pages, "ERR500"), "ENG",
                         str(tmp_path / "z.gz"))
    assert res3["verified"] is False and res3["approx"] == "ERR500"


def test_truncated_space_extract_not_committed_but_unknown_count_is(tmp_path):
    pages = [mk_page(f"Acme Page {i}", pid=str(i)) for i in range(3)]
    # Known truncation (enumerated 3, CQL says 4) must NOT cache a short file a
    # reuse run would trust (review Bug 5).
    trunc = tmp_path / "TRUNC.core.jsonl.gz"
    extract_space(FakeConf(pages, 4), "ENG", str(trunc))
    assert not trunc.exists()
    # A prior complete extract is preserved, not clobbered by a truncated re-run.
    keep = tmp_path / "KEEP.core.jsonl.gz"
    keep.write_bytes(b"PRIOR-COMPLETE")
    extract_space(FakeConf(pages, 4), "ENG", str(keep))
    assert keep.read_bytes() == b"PRIOR-COMPLETE"
    # ac unavailable (ERR string) is NOT a known truncation -> best-effort file
    # is still committed so the caller can decide.
    err = tmp_path / "ERR.core.jsonl.gz"
    extract_space(FakeConf(pages, "ERR500"), "ENG", str(err))
    assert err.exists()


def test_extract_space_empty_body_tripwire(tmp_path):
    empty = [mk_page(f"Acme Page {i}", body="", pid=str(i)) for i in range(12)]
    out = tmp_path / "ENG.core.jsonl.gz"
    with pytest.raises(RuntimeError, match="expand"):
        extract_space(FakeConf(empty, 12), "ENG", str(out))
    # A refused extract must never land where cached-extract reuse finds it.
    assert not out.exists()
    # One real body among 12 → the expansion clearly works; no tripwire.
    mixed = empty[:11] + [mk_page("Acme Page 11", pid="11")]
    res = extract_space(FakeConf(mixed, 12), "ENG", str(out))
    assert res == {"extracted": 12, "approx": 12, "verified": True}
    # Tiny stub spaces (under the 10-page threshold) never trip.
    stubs = [mk_page(f"Stub {i}", body="", pid=str(i)) for i in range(3)]
    res2 = extract_space(FakeConf(stubs, 3), "ENG", str(tmp_path / "s.gz"))
    assert res2["extracted"] == 3


def test_slim_page_namespaces_blog_keys():
    blog = slim_page({"id": "5", "title": "Launch", "type": "blogpost",
                      "body": {"storage": {"value": "<p>hi</p>"}},
                      "history": {"createdDate": "2024-01-02T03:04:05.000Z"}})
    assert blog["key"] == "[blog] Launch"
    assert blog["fields"]["content_type"] == "blogpost"
    # a PAGE with the same title keys distinctly -> no collision into one row.
    pg = slim_page({"id": "6", "title": "Launch", "type": "page",
                    "body": {"storage": {"value": "<p>hi</p>"}}})
    assert pg["key"] == "Launch" and pg["key"] != blog["key"]
