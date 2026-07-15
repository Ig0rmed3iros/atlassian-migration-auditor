from auditor.textnorm import (adf_text, canon, content_fp, h16, norm_ts,
                              storage_text, wiki_text)


def adf_doc(*content):
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": list(content)}]}


def test_cross_dialect_prose_equal_fp():
    adf = adf_doc({"type": "text", "text": "Hello World,"},
                  {"type": "hardBreak"},
                  {"type": "text", "text": "line two"})
    wiki = "Hello *World*,\nline two"
    storage = "<p>Hello <strong>World</strong>,</p><p>line two</p>"
    fps = {content_fp(adf_text(adf, for_canon=True)),
           content_fp(wiki_text(wiki)),
           content_fp(storage_text(storage))}
    assert len(fps) == 1
    assert fps == {h16(canon("Hello World, line two"))}


def test_mentions_excluded_from_canon():
    adf = adf_doc({"type": "text", "text": "Thanks "},
                  {"type": "mention", "attrs": {"text": "Igor Medeiros"}},
                  {"type": "text", "text": " for the review"})
    wiki = "Thanks [~imedeiros] for the review"
    assert content_fp(adf_text(adf, for_canon=True)) == \
        content_fp(wiki_text(wiki))
    # Display text keeps the mention: only the canon path drops it.
    assert "@Igor Medeiros" in adf_text(adf)


def test_wiki_markup_stripped():
    src = ("h2. Release notes\n"
           "* {color:red}Important{color} fix for [Acme|https://acme.example]\n"
           "{code:python}\nx = 1\n{code}\n"
           "See [https://globex.example] and {{config.yml}} "
           "!screenshot.png! done")
    assert wiki_text(src) == (
        "Release notes\n"
        "Important fix for Acme\n"
        "\nx = 1\n\n"
        "See https://globex.example and config.yml  done")


def test_storage_macros_dropped_but_cdata_kept():
    s = ('<ac:structured-macro ac:name="code">'
         '<ac:parameter ac:name="language">python</ac:parameter>'
         '<ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body>'
         '</ac:structured-macro>')
    out = storage_text(s)
    assert "x = 1" in out
    assert "python" not in out          # macro parameters are config, not prose
    linked = ('<p>See <ac:link><ri:page ri:content-title="Globex Home" />'
              '</ac:link> page</p>')
    assert storage_text(linked) == "See page"


def test_storage_cdata_with_angle_brackets_survives_tag_strip():
    """CDATA bodies are authored text and may legitimately contain angle
    brackets (code!). The generic tag strip must never eat them — deleting
    authored prose is this module's declared fatal failure mode, and two
    DIFFERENT code bodies must never canonicalize to the same fingerprint
    (a false CLEAN in the firewall)."""
    s = ('<ac:structured-macro ac:name="code">'
         '<ac:plain-text-body><![CDATA[if (x < 10 && y > 5) run()]]>'
         '</ac:plain-text-body></ac:structured-macro>')
    out = storage_text(s)
    assert "if (x < 10 && y > 5) run()" in out
    a = storage_text('<ac:plain-text-body><![CDATA[a < b > c]]>'
                     '</ac:plain-text-body>')
    b = storage_text('<ac:plain-text-body><![CDATA[a < zzz > c]]>'
                     '</ac:plain-text-body>')
    assert a == "a < b > c"
    assert b == "a < zzz > c"
    assert content_fp(a) != content_fp(b)


def test_storage_cdata_stays_literal_not_unescaped():
    # CDATA content is literal in XML: &amp; inside CDATA is the four
    # characters &-a-m-p-;, while &amp; in regular element text unescapes.
    assert storage_text("<p><![CDATA[a &amp; b]]></p>") == "a &amp; b"
    assert storage_text("<p>a &amp; b</p>") == "a & b"


def test_norm_ts_variants_equal():
    variants = ["2024-01-02T03:04:05.000+0000", "2024-01-02T03:04:05+00:00",
                "2024-01-02T03:04:05Z", "2024-01-02T03:04:05.000Z"]
    assert {norm_ts(v) for v in variants} == {"1704164645"}
    assert norm_ts("2024-01-02") == "2024-01-02"
    assert norm_ts("garbage") == "garbage"
    assert norm_ts(None) is None


def test_h16_and_adf_text_reexported_from_client():
    from auditor import client, textnorm
    assert client.adf_text is textnorm.adf_text
    assert client.h16 is textnorm.h16


def test_wiki_prose_between_exclamation_marks_survives():
    """Deleting authored prose is this module's declared fatal failure mode:
    a span between two exclamation marks that does NOT look like a media ref
    (no dot/pipe/colon) must survive, or the firewall manufactures a
    cross-dialect content mismatch out of ordinary punctuation."""
    assert wiki_text("deploy failed!Retry!now please") == \
        "deploy failed!Retry!now please"
    assert wiki_text("wow!!ok") == "wow!!ok"
    assert wiki_text("ship it!today!") == "ship it!today!"


def test_wiki_media_refs_still_stripped():
    # Media-looking spans (no whitespace inside, at least one dot/pipe/colon)
    # are per-platform artifacts and must keep being removed.
    assert wiki_text("see !diagram.png|width=300! here") == "see  here"
    assert wiki_text("a !image.png! b !attachment:x! c") == "a  b  c"


def test_wiki_emoticons_excluded_from_canon():
    """Letter-bearing wiki emoticons -- (y), (i), (x), (on), (off), :D, :p --
    must not leave alphanumeric residue in canon: the ADF side stores them as
    emoji NODES which for_canon skips, so residue manufactures a false
    content_mismatch on every DC->Cloud issue using them (audit finding 2)."""
    adf = adf_doc({"type": "text", "text": "Looks good "},
                  {"type": "emoji", "attrs": {"shortName": ":thumbsup:"}},
                  {"type": "text", "text": " ship it"})
    wiki = "Looks good (y) ship it"
    assert content_fp(adf_text(adf, for_canon=True)) == content_fp(wiki_text(wiki))


def test_wiki_letter_emoticons_all_stripped():
    s = "a (y) b (n) c (i) d (x) e (on) f (off) g (*r) h (*y) i :D j :p k"
    assert canon(wiki_text(s)) == "abcdefghijk"


def test_wiki_colon_emoticons_not_stripped_mid_word():
    """:P/:D only count as emoticons when not glued to alphanumerics on both
    sides; authored prose like host:Port must keep its letters."""
    assert canon(wiki_text("stop:Police at 9:30")) == "stoppoliceat930"


# --- macro_signature: macro TARGET config that storage_text() strips ---------
from auditor.textnorm import macro_signature, content_fp


def _macro(name, params=(), body=""):
    ps = "".join(f'<ac:parameter ac:name="{k}">{v}</ac:parameter>' for k, v in params)
    return (f'<ac:structured-macro ac:name="{name}" ac:macro-id="ID-X">'
            f'{ps}{body}</ac:structured-macro>')


def test_macro_signature_no_macros_is_stable_empty():
    assert macro_signature("<p>just prose</p>") == content_fp("")
    assert macro_signature("") == content_fp("")


def test_macro_signature_differs_on_changed_jql():
    a = _macro("jira", [("jqlQuery", "project = ACME")])
    b = _macro("jira", [("jqlQuery", "project = GLOBEX")])
    assert macro_signature(a) != macro_signature(b)


def test_macro_signature_ignores_volatile_macro_id():
    a = '<ac:structured-macro ac:name="info" ac:macro-id="AAA"><ac:parameter ac:name="title">Note</ac:parameter></ac:structured-macro>'
    b = '<ac:structured-macro ac:name="info" ac:macro-id="ZZZ"><ac:parameter ac:name="title">Note</ac:parameter></ac:structured-macro>'
    assert macro_signature(a) == macro_signature(b)


def test_macro_signature_catches_excerpt_include_target_change():
    a = '<ac:structured-macro ac:name="excerpt-include"><ac:parameter ac:name="">' \
        '<ac:link><ri:page ri:content-title="Onboarding"/></ac:link></ac:parameter></ac:structured-macro>'
    b = a.replace("Onboarding", "Offboarding")
    assert macro_signature(a) != macro_signature(b)


def test_macro_signature_same_target_matches():
    a = _macro("jira", [("jqlQuery", "project = ACME"), ("columns", "key,summary")])
    b = _macro("jira", [("columns", "key,summary"), ("jqlQuery", "project = ACME")])
    assert macro_signature(a) == macro_signature(b)   # param order-independent


def test_macro_signature_user_mention_serialization_is_not_a_mismatch():
    # CCMA rewrites ri:username (DC) -> ri:account-id (Cloud) for the SAME user
    # on a faithful migration. User identity is migration-volatile and not a
    # macro target, so the signature must NOT change — else every page with an
    # @mention false-mismatches and deflates fidelity.
    dc = '<p>By <ac:link><ri:user ri:username="igor.medeiros"/></ac:link></p>'
    cloud = '<p>By <ac:link><ri:user ri:account-id="557058:9f-ab"/></ac:link></p>'
    assert macro_signature(dc) == macro_signature(cloud)


def test_macro_signature_catches_cdata_param_change():
    # A target inside a CDATA plain-text body must still be compared (a naive
    # tag-strip would empty it -> false clean).
    a = ('<ac:structured-macro ac:name="code"><ac:parameter ac:name="t">'
         '<![CDATA[if (x<10)]]></ac:parameter></ac:structured-macro>')
    b = a.replace("x<10", "x<99")
    assert macro_signature(a) != macro_signature(b)


def test_macro_signature_self_closing_param_is_captured():
    a = ('<ac:structured-macro ac:name="m">'
         '<ac:parameter ac:name="showSubpages"/></ac:structured-macro>')
    none = '<ac:structured-macro ac:name="m"/>'
    assert macro_signature(a) != macro_signature(none)


def test_macro_signature_no_param_boundary_collision():
    # canon strips separators; two different param sets must not collapse.
    a = _macro("m", [("ab", "cd")])
    b = _macro("m", [("abc", "d")])
    assert macro_signature(a) != macro_signature(b)


def test_macro_signature_entity_encoding_normalized():
    # &amp; vs & inside a param value is a serialization artifact, not intent.
    a = _macro("jira", [("jqlQuery", "a &amp; b")])
    b = _macro("jira", [("jqlQuery", "a & b")])
    assert macro_signature(a) == macro_signature(b)
