import copy
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from sakai_mcp.client import Sakai, SakaiError
from sakai_mcp.submit import Page, save_draft, submit

FIXTURES = Path(__file__).parent / "fixtures"
FORM = (FIXTURES / "submission_form.html").read_text()
PLEDGE = (FIXTURES / "honor_pledge.html").read_text()
SITE, AID, TOOL = "course-101", "38f72308-a093-4c77-a16c-f67e591bbc22", "21caee61-724e-4adb-95ef-8f7e6419d9d9"
PLEDGE_TOOL = "e42cdf0c-1764-4114-a4c1-00fe9fa2907e"
ALERT = '<div class="sak-banner-error"><strong>Alerta:</strong> Heu d’adjuntar almenys un fitxer.</div>'

ASSIGNMENT = {
    "id": AID, "context": SITE, "title": "Práctica 2", "status": "OPEN",
    "submissionType": "TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION", "allowResubmission": True,
    "dueTimeString": "2099-12-20T22:59:00Z", "closeTimeString": "2099-12-20T22:59:00Z",
    "submissions": [{"submitted": True, "userSubmission": False, "dateSubmitted": None,
                     "dateSubmittedEpochSeconds": None, "canSubmit": True, "status": "No s’ha iniciat",
                     "properties": {"allow_resubmit_number": "3"}, "submittedAttachments": [],
                     "submittedText": None}],
}


def view(attachments=()):
    rows = "".join(f'<tr><td><a href="#" target="_blank">{n}</a></td><td class="itemAction"><a href="javascript:void(0)" '
                   f'onclick="ASN.doStudentViewSubmissionAction( \'addSubmissionForm\', \'removeAttachment\', \'/x/{n}\', '
                   f'\'attachmentspanel\' ); return false;">Elimina<span class="skip"> {n}</span></a></td></tr>'
                   for n in attachments)
    return FORM.replace('<div id="attachmentspanel" class="highlightPanel">',
                        f'<div id="attachmentspanel" class="highlightPanel"><table class="attachList">{rows}</table>')


CONFIRM = f"""<html><body><!-- start: chef_assignments_student_confirm_submission.vm -->
<ul id="submissionConfirmList"><li>Aquest treball és meu</li></ul>
<form id="addSubmissionForm" name="addSubmissionForm" action="https://sakai.example.edu/portal/tool/{TOOL}?panel=Main" method="post">
<input type="hidden" name="assignmentId" value="/assignment/a/{SITE}/{AID}"/>
<input type="hidden" name="eventSubmit_doRead_add_submission_form" value="x"/>
<input type="hidden" name="option" id="option" value="cancel"/>
<input type="button" name="post" id="post" value="Envia"/>
<input type="hidden" name="sakai_csrf_token" value="CSRF-TOKEN"/>
</form></body></html>"""


def confirmation(final):
    banner = '<div class="sak-banner-success">Lliurat</div>' if final else '<div class="sak-banner-warn">Desat</div>'
    return f"""<html><body><!-- start:  chef_assignments_student_view_submission_confirmation.vm  -->{banner}
<p class="sak-banner-error">No s'ha pogut enviar el correu</p>
<form name="addSubmissionForm" action="https://sakai.example.edu/portal/tool/{TOOL}?panel=Main" method="post">
<input type="submit" name="eventSubmit_doConfirm_assignment_submission" value="Torna"/>
<input type="hidden" name="sakai_csrf_token" value="CSRF-TOKEN"/></form></body></html>"""


def form_fields(request: httpx.Request) -> dict:
    body, ctype = request.read(), request.headers.get("content-type", "")
    if not ctype.startswith("multipart/"):
        return dict(parse_qsl(body.decode(), keep_blank_values=True))
    out = {}
    for chunk in body.split(b"--" + ctype.split("boundary=")[1].encode())[1:-1]:
        head, _, payload = chunk[2:-2].partition(b"\r\n\r\n")
        name, filename = re.search(r'\bname="([^"]*)"', head.decode()), re.search(r'filename="([^"]*)"', head.decode())
        out[name[1]] = (filename[1], payload) if filename else payload.decode()
    return out


class FakeSakai:
    """Serves the assignment tool's screens the way AssignmentAction moves between them."""

    def __init__(self, assignment=ASSIGNMENT, pledge=False, override=None):
        self.assignment, self.pledge, self.override = copy.deepcopy(assignment), pledge, override or {}
        self.attachments, self.requests = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        fields = form_fields(request) if request.method == "POST" else {}
        self.requests.append((request.method, request.url.raw_path.decode(), fields))
        path = request.url.path
        if path == f"/direct/assignment/item/{AID}.json":
            return httpx.Response(200, json=self.assignment)
        if path == f"/direct/site/{SITE}/pages.json":
            return httpx.Response(200, json=[{"tools": [{"toolId": "sakai.iframe", "placementId": "nope"},
                                                        {"toolId": "sakai.assignment.grades", "placementId": TOOL}]}])
        if request.method == "GET" and params.get("sakai_action") == "doView_submission":
            return self.page("view", PLEDGE if self.pledge else view(self.attachments))
        if "eventSubmit_doAccept_assignment_honor_pledge" in fields:
            return self.page("pledge", view(self.attachments))
        if params.get("sakai_action") == "doAttachUpload":
            self.attachments.append(fields["upload"][0])
            return self.page("upload", view(self.attachments))
        sub = self.assignment["submissions"][0]
        names = [{"name": n} for n in self.attachments]
        match fields.get("option"):
            case "save":
                sub.update(draft=True, submitted=False, userSubmission=True, submittedAttachments=names)
                return self.page("save", confirmation(False))
            case "confirm":
                sub.update(draft=True, submitted=False, userSubmission=True, submittedAttachments=names)
                return self.page("confirm", CONFIRM)
            case "post":
                sub.update(draft=False, submitted=True, dateSubmitted="ara", dateSubmittedEpochSeconds=1)
                return self.page("post", confirmation(True))
        return httpx.Response(404)

    def page(self, step, html):
        return httpx.Response(200, text=self.override.get(step, html))


@pytest.fixture
def sakai(tmp_path):
    def make(fake):
        s = Sakai("https://sakai.example.edu", "user", "pass", tmp_path)
        s.http = httpx.Client(transport=httpx.MockTransport(fake), base_url="https://sakai.example.edu", follow_redirects=True)
        s.user_id, s.checked_at = "uid", time.monotonic()
        return s
    return make


@pytest.fixture
def report(tmp_path):
    p = tmp_path / "Apellido1 Apellido2 PRA2.zip"
    p.write_bytes(b"PK\x03\x04 zip bytes")
    return p


def test_parse_submission_form():
    page = Page(FORM)
    assert page.templates == ["chef_assignments_student_view_submission.vm"]
    form = page.forms["addSubmissionForm"]
    assert form["action"] == f"https://sakai.example.edu/portal/tool/{TOOL}?panel=Main"
    assert form["upload_url"] == f"https://sakai.example.edu/portal/tool/{TOOL}?sakai_csrf_token=&panel=Main&sakai_action=doAttachUpload"
    assert form["fields"]["eventSubmit_doRead_add_submission_form"] == "x"
    assert form["fields"]["option"] == "cancel"
    assert form["fields"]["Assignment.view_submission_text"] == ""
    assert form["fields"]["submissionFileCount"] == "1"
    assert form["fields"]["sakai_csrf_token"] == "CSRF-TOKEN"
    assert form["buttons"] == {}
    assert not page.texts["alert"] and not page.texts["attachment"]


def test_parse_honor_pledge():
    page = Page(PLEDGE)
    assert page.templates == ["chef_assignments_student_view_assignment_honor_pledge.vm"]
    form = page.forms["assignmentHonorPledgeForm"]
    assert form["action"] == f"https://sakai.example.edu/portal/tool/{PLEDGE_TOOL}?panel=Main"
    assert form["fields"] == {"assignmentReference": "/assignment/a/course-102/f1bf84a6-247f-4a6e-b147-65bb209b0fbc",
                              "sakai_csrf_token": "CSRF-TOKEN"}
    assert set(form["buttons"]) == {"eventSubmit_doAccept_assignment_honor_pledge", "eventSubmit_doCancel_assignment_honor_pledge"}
    assert "No he ofert" in page.texts["pledge"][0]


def test_parse_alerts_and_attachments():
    page = Page(view(["a.pdf", "b c.zip"]).replace('<div id="StudentAssignmentCurrent">', ALERT + '<div id="StudentAssignmentCurrent">'))
    assert page.texts["alert"] == ["Alerta: Heu d’adjuntar almenys un fitxer."]
    assert page.texts["attachment"] == ["a.pdf", "b c.zip"]
    # the missing-email notice on the confirmation page is a <p>, not a failure
    assert not Page(confirmation(True)).texts["alert"]


def with_changes(**changes):
    a = copy.deepcopy(ASSIGNMENT)
    a["submissions"][0].update(changes.pop("sub", {}))
    return {**a, **changes}


@pytest.mark.parametrize("assignment, call, message", [
    (ASSIGNMENT, lambda s, f: submit(s, AID, files=[str(f.parent / "missing.pdf")]), "not a readable file"),
    (ASSIGNMENT, lambda s, f: submit(s, AID, files=[str(f.parent)]), "not a readable file"),
    (with_changes(submissionType="TEXT_ONLY_ASSIGNMENT_SUBMISSION"), lambda s, f: submit(s, AID, files=[str(f)]), "no files"),
    (with_changes(submissionType="ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION"), lambda s, f: save_draft(s, AID, text="hola"), "files only"),
    (with_changes(submissionType="SINGLE_ATTACHMENT_SUBMISSION"), lambda s, f: submit(s, AID, files=[str(f), str(f)]), "take 1 file,"),
    (with_changes(submissionType="NON_ELECTRONIC_ASSIGNMENT_SUBMISSION"), lambda s, f: submit(s, AID, text="x"), "can't be submitted"),
    (with_changes(status="CLOSED"), lambda s, f: submit(s, AID, text="x"), "is CLOSED"),
    (with_changes(status="DUE", closeTimeString="2020-01-01T00:00:00Z"), lambda s, f: submit(s, AID, text="x"), "closes 2020"),
    (with_changes(sub={"canSubmit": False}), lambda s, f: submit(s, AID, text="x"), r"can't be submitted \(again\)"),
    (with_changes(sub={"userSubmission": True, "dateSubmittedEpochSeconds": 5}), lambda s, f: save_draft(s, AID, text="x"), "withdraw"),
])
def test_preflight_rejects_before_any_post(sakai, report, assignment, call, message):
    fake = FakeSakai(assignment)
    with pytest.raises(SakaiError, match=message):
        call(sakai(fake), report)
    assert all(method == "GET" for method, _, _ in fake.requests)
    assert all("/portal/" not in url for _, url, _ in fake.requests)


def test_empty_file_rejected_without_requests(sakai, tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.touch()
    fake = FakeSakai()
    with pytest.raises(SakaiError, match="empty"):
        submit(sakai(fake), AID, files=[str(empty)], confirm=True)
    assert fake.requests == []


def test_dry_run_posts_nothing(sakai, report):
    fake = FakeSakai(with_changes(allowResubmission=False, sub={"properties": {}}), pledge=True)
    result = submit(sakai(fake), AID, text="<p>hola</p>", files=[str(report)])
    assert [m for m, _, _ in fake.requests] == ["GET", "GET", "GET"]
    assert result["dry_run"] is True
    assert result["assignment"]["title"] == "Práctica 2" and result["assignment"]["site"] == SITE
    assert result["submission"]["state"] == "none"
    assert result["files"] == [{"name": report.name, "size": report.stat().st_size}]
    assert result["text_length"] == len("<p>hola</p>")
    assert "No he ofert" in result["honor_pledge"]
    assert result["warnings"][0].startswith("FINAL:")
    assert any("honor pledge" in w for w in result["warnings"])


def test_dry_run_counts_resubmissions(sakai):
    result = submit(sakai(FakeSakai()), AID, text="x")
    assert result["submission"]["resubmissions_left"] == 3
    assert "3 more resubmission(s)" in result["warnings"][0]


def test_save_draft_sequence(sakai, report):
    fake = FakeSakai()
    result = save_draft(sakai(fake), AID, text="<p>hola</p>", files=[str(report)])
    methods_urls = [(m, u) for m, u, _ in fake.requests]
    ref = f"%2Fassignment%2Fa%2F{SITE}%2F{AID}"
    assert methods_urls == [
        ("GET", f"/direct/assignment/item/{AID}.json"),
        ("GET", f"/direct/site/{SITE}/pages.json"),
        ("GET", f"/portal/tool/{TOOL}?assignmentReference={ref}&sakai_action=doView_submission"),
        ("POST", f"/portal/tool/{TOOL}?panel=Main&sakai_action=doAttachUpload"),
        ("POST", f"/portal/tool/{TOOL}?panel=Main"),
        ("GET", f"/direct/assignment/item/{AID}.json"),
    ]
    upload, save = fake.requests[3][2], fake.requests[4][2]
    assert upload["upload"] == (report.name, report.read_bytes())
    assert upload["option"] == "cancel" and upload["sakai_csrf_token"] == "CSRF-TOKEN"
    assert upload["Assignment.view_submission_text"] == "<p>hola</p>"
    assert save["option"] == "save" and save["eventSubmit_doRead_add_submission_form"] == "x"
    assert save["sakai_csrf_token"] == "CSRF-TOKEN" and save["Assignment.view_submission_text"] == "<p>hola</p>"
    assert result["done"] == "saved draft" and result["verified"] is True, result["problems"]
    assert result["submission"]["state"] == "draft" and result["submission"]["attachments"] == [report.name]


def test_submit_sequence_with_honor_pledge(sakai, report):
    fake = FakeSakai(pledge=True)
    result = submit(sakai(fake), AID, files=[str(report)], confirm=True)
    posts = [(u, f) for m, u, f in fake.requests if m == "POST"]
    assert [u for u, _ in posts] == [
        f"/portal/tool/{PLEDGE_TOOL}?panel=Main",
        f"/portal/tool/{TOOL}?panel=Main&sakai_action=doAttachUpload",
        f"/portal/tool/{TOOL}?panel=Main",
        f"/portal/tool/{TOOL}?panel=Main",
    ]
    assert posts[0][1] == {"assignmentReference": "/assignment/a/course-102/f1bf84a6-247f-4a6e-b147-65bb209b0fbc",
                           "sakai_csrf_token": "CSRF-TOKEN", "eventSubmit_doAccept_assignment_honor_pledge": "D’acord"}
    assert posts[2][1]["option"] == "confirm" and posts[2][1]["Assignment.view_submission_text"] == ""
    assert posts[3][1] == {"assignmentId": f"/assignment/a/{SITE}/{AID}", "eventSubmit_doRead_add_submission_form": "x",
                           "option": "post", "sakai_csrf_token": "CSRF-TOKEN"}
    assert fake.requests[-1][:2] == ("GET", f"/direct/assignment/item/{AID}.json")
    assert result["done"] == "submitted" and result["verified"] is True, result["problems"]
    assert result["confirm_statements"] == ["Aquest treball és meu"]


@pytest.mark.parametrize("step, html, message", [
    ("save", view().replace('<div id="StudentAssignmentCurrent">', ALERT + '<div id="StudentAssignmentCurrent">'), "Heu d’adjuntar almenys un fitxer"),
    ("save", view(), "instead of chef_assignments_student_view_submission_confirmation.vm"),
    ("upload", view(), "did not show up among the attachments"),
    ("pledge", PLEDGE, "accepting the honor pledge"),
])
def test_failures_raise(sakai, report, step, html, message):
    fake = FakeSakai(pledge=step == "pledge", override={step: html})
    with pytest.raises(SakaiError, match=message):
        save_draft(sakai(fake), AID, files=[str(report)])


def test_post_without_success_banner_raises(sakai):
    fake = FakeSakai(override={"post": confirmation(False)})
    with pytest.raises(SakaiError, match="did not confirm"):
        submit(sakai(fake), AID, text="x", confirm=True)


def test_verification_disagreement_is_reported(sakai):
    class StaysDraft(FakeSakai):
        def page(self, step, html):
            if step == "post":
                self.assignment["submissions"][0].update(draft=True, submitted=False)
            return super().page(step, html)

    result = submit(sakai(StaysDraft()), AID, text="x", confirm=True)
    assert result["verified"] is False
    assert "as draft, not submitted" in result["problems"][0]
