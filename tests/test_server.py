import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sakai_mcp.client import SakaiError
from sakai_mcp import server
from sakai_mcp.server import (content_date, filename_from, iso, parse_due, same_host, submission_state,
                              text, trim_task, unique_path)

BASE = "https://sakai.example.edu"


def test_iso_handles_millis_and_seconds():
    assert iso(1791973800665) == "2026-10-14T10:30:00+00:00"
    assert iso(1791968930.041) == "2026-10-14T09:08:50+00:00"
    assert iso(None) is None


def test_parse_due_roundtrip():
    assert iso(parse_due("2026-10-14T10:30:00+00:00")) == "2026-10-14T10:30:00+00:00"
    assert parse_due("2026-10-14") == parse_due("2026-10-14T00:00:00Z")


def test_content_date():
    assert content_date("20260917112402875") == "2026-09-17T11:24:02+00:00"
    assert content_date(None) is None


def test_text_strips_html():
    html = '<p>Hello&nbsp;<b>world</b> &amp; co</p><script>x()</script><p><a href="https://a.b/c">link</a></p>'
    assert text(html) == "Hello world & co\n[https://a.b/c] link"


@pytest.mark.parametrize("url", ["https://evil.example/access/x", "http://sakai.example.edu/access/x",
                                 "//evil.example/access/x", "https://sakai.example.edu.evil.example/x"])
def test_same_host_rejects_other_origins(url):
    with pytest.raises(SakaiError):
        same_host(BASE, url)


def test_same_host_accepts_absolute_and_relative():
    assert same_host(BASE, "https://sakai.example.edu/access/a.pdf") == "https://sakai.example.edu/access/a.pdf"
    assert same_host(BASE, "/access/a.pdf") == "https://sakai.example.edu/access/a.pdf"


def test_unique_path(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "a (1).pdf").write_text("x")
    assert unique_path(tmp_path / "a.pdf").name == "a (2).pdf"
    assert unique_path(tmp_path / "b.pdf").name == "b.pdf"


def test_filename_from():
    assert filename_from('attachment; filename="r.pdf"', "https://x/y") == "r.pdf"
    assert filename_from('attachment; filename="../../etc/passwd"', "https://x/y") == "passwd"
    assert filename_from(None, "https://x/access/My%20File.txt") == "My File.txt"


def test_submission_state():
    assert submission_state({"submissions": None}) == {"state": "none"}
    untouched = {"submissions": [{"submitted": True, "dateSubmittedEpochSeconds": None, "draft": False}]}
    assert submission_state(untouched)["state"] == "not_started"
    done = {"submissions": [{"dateSubmittedEpochSeconds": 1790719247, "graded": True, "grade": "8,5",
                             "feedbackText": "<p>good</p>",
                             "submittedAttachments": [{"name": "a.pdf", "url": "u"}]}]}
    out = submission_state(done)
    assert (out["state"], out["grade"], out["feedback"], out["files"]) == ("submitted", "8,5", "good",
                                                                          [{"name": "a.pdf", "url": "u"}])
    assert submission_state({"submissions": [{"draft": True}]})["state"] == "draft"


def test_trim_task_decodes_entities():
    t = trim_task({"userTaskId": 1, "description": "Metodolog&iacute;as &amp; co", "notes": None})
    assert (t["description"], t["notes"]) == ("Metodologías & co", None)


def test_sakai_error_message_reaches_the_client(monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.delenv("SAKAI_URL", raising=False)
    with pytest.raises(ToolError, match="^Error executing tool whoami: missing environment variable SAKAI_URL$"):
        asyncio.run(server.mcp.call_tool("whoami", {}))
