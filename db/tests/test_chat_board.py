"""chat_board (report server page /ai_chat): threads, validation, escaping, the API glue, concurrency."""

import io
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_test_db"))

import chat_board as cb  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "DIR", str(tmp_path))


class FakeHandler:
    def __init__(self, path, body=b""):
        self.path, self.rfile, self.headers = path, io.BytesIO(body), {"Content-Length": str(len(body))}
        self.sent = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.sent.append((code, body))

    def send_response(self, code):
        self.sent.append((code, None))

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass


def test_thread_numbers_are_not_message_ids_and_a_reply_to_a_reply_stays_in_the_thread():
    a = cb.add("git", "first", title="EPAIS task dispatch")
    b = cb.add("owner", "answer", reply_to=a["id"])
    c = cb.add("git", "answer to the answer", reply_to=b["id"])
    d = cb.add("klin", "another topic", title="Second topic")
    e = cb.add("git", "reply by number", thread=2)
    assert (a["thread"], b["thread"], c["thread"], d["thread"], e["thread"]) == (1, 1, 1, 2, 2)
    assert (a["id"], d["id"], e["id"]) == (1, 4, 5)  # thread 2 is message 4: the two numberings differ
    assert [t["title"] for t in cb.threads()] == ["EPAIS task dispatch", "Second topic"]


def test_validation_messages():
    with pytest.raises(ValueError, match="author"):
        cb.add("", "x", title="t")
    with pytest.raises(ValueError, match="text"):
        cb.add("git", "  ", title="t")
    with pytest.raises(ValueError, match="needs a title"):
        cb.add("git", "x")
    with pytest.raises(ValueError, match="thread 9 does not exist"):
        cb.add("git", "x", thread=9)
    with pytest.raises(ValueError, match="reply_to 99 does not exist"):
        cb.add("git", "x", reply_to=99)
    with pytest.raises(ValueError, match="longer"):
        cb.add("git", "x" * (cb.MAX_TEXT + 1), title="t")


def test_html_is_escaped_and_the_list_shows_every_thread_with_its_page_link():
    cb.add("<b>git</b>", "<script>alert(1)</script> " + "word " * 100, title="<i>one</i>")
    cb.add("owner", "second", title="Two")
    page = cb.board_html()
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<i>one</i>" not in page
    assert 'href="/ai_chat/1"' in page and 'href="/ai_chat/2"' in page and " ..." in page
    full = cb.thread_html(1)
    assert "word word word" in full and "..." not in full.split("<pre>")[1].split("</pre>")[0]  # the thread page shows the whole text


def test_a_thread_page_has_a_post_box_at_the_top_and_at_the_bottom_and_an_unknown_thread_is_none():
    cb.add("git", "hello", title="T")
    page = cb.thread_html(1)
    assert page.count('name="thread" value="1"') == 2 and 'id="post-top"' in page and 'id="post-bottom"' in page
    assert cb.thread_html(7) is None
    h = FakeHandler("/ai_chat/7")
    cb.handle_get(h)
    assert h.sent[0][0] == 404


def test_api_new_thread_reply_read_since_and_thread_filter():
    h = FakeHandler("/ai_chat/api/messages", json.dumps({"author": "git", "text": "hi", "title": "A"}).encode())
    cb.handle_post(h)
    assert h.sent[0][0] == 201 and json.loads(h.sent[0][1]) == {"id": 1, "thread": 1}
    cb.handle_post(FakeHandler("/ai_chat/api/messages", json.dumps({"author": "klin", "text": "other", "title": "B"}).encode()))
    r = FakeHandler("/ai_chat/api/messages", json.dumps({"author": "klin", "text": "reply", "thread": 1}).encode())
    cb.handle_post(r)
    assert json.loads(r.sent[0][1]) == {"id": 3, "thread": 1}
    g = FakeHandler("/ai_chat/api/messages?since=0&thread=1")
    cb.handle_get(g)
    assert [m["text"] for m in json.loads(g.sent[0][1])["messages"]] == ["hi", "reply"]
    g2 = FakeHandler("/ai_chat/api/messages?since=3")
    cb.handle_get(g2)
    assert json.loads(g2.sent[0][1])["messages"] == []
    t = FakeHandler("/ai_chat/api/threads")
    cb.handle_get(t)
    assert [(x["n"], x["title"], x["count"]) for x in json.loads(t.sent[0][1])["threads"]] == [(1, "A", 2), (2, "B", 1)]


def test_rename_through_the_api_changes_the_list_and_the_page():
    cb.add("git", "hello", title="old name")
    h = FakeHandler("/ai_chat/api/threads/1/title", json.dumps({"title": "EPAIS task dispatch"}).encode())
    cb.handle_post(h)
    assert h.sent[0][0] == 200
    assert "EPAIS task dispatch" in cb.board_html() and "<h1>#1 EPAIS task dispatch</h1>" in cb.thread_html(1)
    bad = FakeHandler("/ai_chat/api/threads/5/title", json.dumps({"title": "x"}).encode())
    cb.handle_post(bad)
    assert bad.sent[0][0] == 400


def test_read_receipt_by_reader_name_not_by_the_author_and_only_once():
    cb.add("git", "hello", title="T")
    cb.handle_get(FakeHandler("/ai_chat/api/messages?since=0&reader=git22"))
    cb.handle_get(FakeHandler("/ai_chat/api/messages?since=0&reader=git22"))
    cb.handle_get(FakeHandler("/ai_chat/api/messages?since=0&reader=git"))
    only = FakeHandler("/ai_chat/api/messages?since=0")
    cb.handle_get(only)
    msg = json.loads(only.sent[0][1])["messages"][0]
    assert [r["reader"] for r in msg["read_by"]] == ["git22"] and msg["read_by"][0]["ts"] > 0
    assert "read by git22" in cb.thread_html(1)


def test_listing_without_a_reader_marks_nothing_and_opening_the_thread_page_marks_web():
    cb.add("git", "hello", title="T")
    cb.listing(0)
    assert cb.reads_map() == {}
    assert "read by web" in cb.thread_html(1)
    assert [r for r, _ in cb.reads_map()[1]] == ["web"]


def test_form_post_redirects_to_the_thread_page_and_a_bad_api_post_is_400():
    f = FakeHandler("/ai_chat/post", b"author=owner&text=hello&title=New+one")
    cb.handle_post(f)
    assert f.sent[0][0] == 303
    r = FakeHandler("/ai_chat/post", b"author=owner&text=again&thread=1")
    cb.handle_post(r)
    assert r.sent[0][0] == 303 and len(cb.thread_by_n(1)["messages"]) == 2
    bad = FakeHandler("/ai_chat/api/messages", b'{"author": "git", "title": "x"}')
    cb.handle_post(bad)
    assert bad.sent[0][0] == 400 and "text is required" in bad.sent[0][1]


def test_a_half_written_last_line_is_skipped_not_a_500():
    cb.add("git", "good", title="T")
    with open(cb._file(), "a", encoding="utf-8") as fh:
        fh.write('{"id": 2, "ts": 1, "author": "git", "text": "cut he')
    assert len(cb.listing(0)) == 1 and "T" in cb.board_html()


def test_the_page_uses_the_report_dark_theme_with_a_body_font_one_pixel_bigger():
    assert "--bg:#05081a" in cb._CSS and "--cyan:#00c7e6" in cb._CSS
    assert "font-size:17px" in cb._CSS  # report.py body is 16px
    assert 'href="/ai_chat"' in cb._page("t", "")  # the top bar links the board


def test_many_posts_at_once_keep_unique_gap_free_ids():
    cb.add("git", "root", title="T")

    def poster(k):
        for i in range(15):
            cb.add(f"agent{k}", f"m {k}-{i}", thread=1)

    ts = [threading.Thread(target=poster, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = [m["id"] for m in cb.listing(0, 1000)]
    assert ids == list(range(1, 122))


def test_a_moved_board_answers_410_with_the_new_address_and_takes_no_post():
    cb.add("git", "hello", title="T")
    with open(cb._file("MOVED_TO"), "w", encoding="utf-8") as fh:
        fh.write("http://10.136.218.74:8080/")
    api = FakeHandler("/ai_chat/api/messages")
    cb.handle_get(api)
    assert api.sent[0][0] == 410 and json.loads(api.sent[0][1])["moved_to"] == "http://10.136.218.74:8080/ai_chat"
    page = FakeHandler("/ai_chat")
    cb.handle_get(page)
    assert page.sent[0][0] == 410 and "http://10.136.218.74:8080/ai_chat" in page.sent[0][1]
    post = FakeHandler("/ai_chat/api/messages", json.dumps({"author": "git", "text": "x", "thread": 1}).encode())
    cb.handle_post(post)
    assert post.sent[0][0] == 410 and len(cb.listing(0)) == 1


def test_markdown_renders_headings_tables_code_lists_links_and_stays_safe():
    out = cb.md("## Title\n\n**bold** and `code` and *it*\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\n<script>x</script>\n```\n\n[go](https://x.y/z) and https://bare.example/p")
    assert "<h4>Title</h4>" in out and "<strong>bold</strong>" in out and "<code>code</code>" in out and "<em>it</em>" in out
    assert "<ul><li" in out and out.count("<li") == 2
    assert "<table class='mdt'>" in out and "<th>a</th>" in out and "<td>2</td>" in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out and "<script>" not in out
    assert 'href="https://x.y/z"' in out and 'href="https://bare.example/p"' in out


def test_markdown_never_makes_a_link_from_javascript_or_raw_html():
    out = cb.md("[bad](javascript:alert(1)) <img src=x onerror=alert(1)> <b>x</b>")
    assert "href=\"javascript" not in out and "<img" not in out and "&lt;img" in out and "<b>" not in out


def test_the_thread_page_shows_the_rendered_text_and_keeps_the_raw_text_in_a_details():
    cb.add("git", "## Plan\n\n- a\n- b", title="T")
    page = cb.thread_html(1)
    assert "<h4>Plan</h4>" in page and "<li" in page and "raw text" in page and "## Plan" in page


def test_the_thread_page_text_is_one_pixel_smaller_than_the_list_page():
    cb.add("git", "hello", title="T")
    assert "font-size:17px" in cb._CSS and ".page.thread{font-size:16px}" in cb._CSS
    assert 'class="page thread"' in cb.thread_html(1) and 'class="page thread"' not in cb.board_html()


def test_every_author_gets_a_stable_colour_and_the_post_header_is_not_smaller_than_the_text():
    a, b = cb._acolor("git"), cb._acolor("klin")
    assert a != b and a == cb._acolor("git") and cb._acolor("someone new") == cb._acolor("someone new")
    cb.add("git", "one", title="T")
    cb.add("klin", "two", thread=1)
    page = cb.thread_html(1)
    assert f'--c:{a[0]}' in page and f'--c:{b[0]}' in page
    assert ".msg .head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:1em" in cb._CSS


def test_new_messages_come_as_cards_after_the_given_id_and_mark_web_read_without_a_page_reload():
    a = cb.add("git", "first", title="live")
    cb.add("klin", "second **bold**", thread=1)
    cb.add("git", "third", thread=1)
    frag = cb.thread_new_fragment(1, a["id"])
    assert frag["last_id"] == 3 and frag["count"] == 3
    assert 'id="m2"' in frag["html"] and 'id="m3"' in frag["html"] and 'id="m1"' not in frag["html"]
    assert "<strong>bold</strong>" in frag["html"]  # same card builder as the thread page: rendered text
    assert cb.thread_new_fragment(1, 3) == {"html": "", "last_id": 3, "count": 3}  # nothing new: empty, same id
    assert cb.thread_new_fragment(9, 0) is None


def test_the_fragment_route_answers_json_and_a_poll_does_not_leave_a_reader_receipt_of_a_named_agent():
    cb.add("git", "first", title="live")
    h = FakeHandler("/ai_chat/1/new?since=0")
    cb.handle_get(h)
    code, body = h.sent[0]
    assert code == 200 and json.loads(body)["last_id"] == 1
    rm = cb.reads_map()
    assert [r for r, _ in rm.get(1, [])] == ["web"]  # only the web viewer, no agent name
    h = FakeHandler("/ai_chat/7/new?since=0")
    cb.handle_get(h)
    assert h.sent[0][0] == 404


def test_the_thread_page_polls_for_new_messages_and_no_longer_reloads_itself():
    cb.add("git", "first", title="live")
    page = cb.thread_html(1)
    assert 'id="post-bottom" data-thread="1"' in page and "/new?since=" in page
    assert "location.reload" not in page
