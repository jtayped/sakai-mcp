"""Student submissions through the assignment tool's Velocity forms (AssignmentAction).

Sakai has no REST endpoint for a student submitting, so this replays the browser's requests:
GET doView_submission, the honor pledge form when shown, one attach-upload post per file, then
option=save for a draft, or option=confirm (which saves a draft and shows the confirmation page)
followed by option=post for a final submission.

The tool answers a rejected CSRF token or a lost tool session by re-rendering its current screen
with HTTP 200 and no alert, so every post checks which template came back, not just the status.
"""
from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .client import Sakai, SakaiError

TOOL_ID = "sakai.assignment.grades"
VIEW = "chef_assignments_student_view_submission.vm"
PLEDGE = "chef_assignments_student_view_assignment_honor_pledge.vm"
CONFIRM = "chef_assignments_student_confirm_submission.vm"
DONE = "chef_assignments_student_view_submission_confirmation.vm"
TEXT = "Assignment.view_submission_text"
EULA = "review_service_eula_agreement"
# submissionType: (accepts text, max files; None = no limit)
TYPES = {
    "TEXT_ONLY_ASSIGNMENT_SUBMISSION": (True, 0),
    "ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION": (False, None),
    "TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION": (True, None),
    "SINGLE_ATTACHMENT_SUBMISSION": (False, 1),
}


class Page(HTMLParser):
    """The bits of an assignment tool page this module acts on: rendered templates, forms, and
    the text of alert banners, the honor pledge, confirmation statements and attachment names."""

    def __init__(self, html: str):
        super().__init__()
        self.templates: list[str] = []
        self.forms: dict[str, dict] = {}
        self.texts: dict[str, list[str]] = defaultdict(list)
        self._form = self._textarea = None
        self._caps: list[list] = []  # [key, tag, open same-name tags, text chunks]
        self.feed(html)
        self.close()

    def _key(self, tag, a):
        cls, id_ = a.get("class", "").split(), a.get("id")
        if tag == "div" and "sak-banner-error" in cls:
            return "alert"
        if tag == "div" and "sak-banner-success" in cls:
            return "success"
        if tag == "div" and id_ == "honor-pledge-agreement":
            return "pledge"
        if tag == "ul" and id_ == "submissionConfirmList":
            return "statement"
        if tag == "a" and "'removeAttachment'" in a.get("onclick", ""):
            return "remove-link"
        if tag == "span" and "skip" in cls and any(c[0] == "remove-link" for c in self._caps):
            return "attachment"

    def handle_comment(self, data):
        if m := re.match(r"\s*start:\s*(\S+\.vm)", data):
            self.templates.append(m[1])

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        for c in self._caps:
            c[2] += c[1] == tag
        if key := self._key(tag, a):
            self._caps.append([key, tag, 1, []])
        if tag == "form":
            self._form = {"action": a.get("action", ""), "fields": {}, "buttons": {}, "upload_url": None}
            self.forms[a.get("name") or a.get("id", "")] = self._form
        elif self._form is None or not a.get("name"):
            return
        elif tag == "textarea":
            self._textarea = [a["name"], []]
        elif tag == "input":
            kind = a.get("type", "text").lower()
            if kind == "file":
                if m := re.search(r"\.action='([^']*)'", a.get("onchange", "")):
                    self._form["upload_url"] = m[1]
            elif kind == "submit":
                self._form["buttons"][a["name"]] = a.get("value", "")
            elif kind in ("hidden", "text") or (kind in ("checkbox", "radio") and "checked" in a):
                self._form["fields"][a["name"]] = a.get("value", "")
            elif kind == "checkbox":
                self._form["fields"].setdefault(a["name"], None)  # unchecked: present, not sent

    def handle_endtag(self, tag):
        if tag == "textarea" and self._textarea and self._form is not None:
            self._form["fields"][self._textarea[0]] = "".join(self._textarea[1])
            self._textarea = None
        if tag == "form":
            self._form = None
        for c in self._caps:
            c[2] -= c[1] == tag
        for c in [c for c in self._caps if c[2] == 0]:
            self.texts[c[0]].append(" ".join("".join(c[3]).split()))
            self._caps.remove(c)

    def handle_data(self, data):
        if self._textarea:
            self._textarea[1].append(data)
        for c in self._caps:
            c[3].append(data)


def save_draft(s: Sakai, assignment_id: str, text: str | None = None, files: list[str] | None = None) -> dict:
    """Upload the files and save the text as a draft without submitting it. `text` is HTML as the
    Sakai editor would produce it; None keeps the draft's current text. Refuses an assignment that
    is already submitted, because saving a draft there would withdraw the submission."""
    return _run(s, assignment_id, text, files, final=False, send=True)


def submit(s: Sakai, assignment_id: str, text: str | None = None, files: list[str] | None = None,
           confirm: bool = False) -> dict:
    """Submit the assignment for grading; with confirm=False nothing is posted and the result says
    what would be submitted, the current state, and whether the submission could be redone.
    With confirm=True it uploads the files, accepts an honor pledge if shown, and submits.
    `text` is HTML as the Sakai editor would produce it; None keeps the draft's current text."""
    return _run(s, assignment_id, text, files, final=True, send=confirm)


def _run(s, assignment_id, text, files, final, send):
    with s.lock:
        return _run_locked(s, assignment_id, text, files, final, send)


def _run_locked(s, assignment_id, text, files, final, send):
    paths = [Path(f).expanduser() for f in files or []]
    for p in paths:
        if not p.is_file() or not os.access(p, os.R_OK):
            raise SakaiError(f"{p}: not a readable file")
        if p.stat().st_size == 0:
            raise SakaiError(f"{p}: the file is empty and Sakai rejects empty uploads")
    a = s.get_json(f"/direct/assignment/item/{assignment_id}.json")
    sub = (a.get("submissions") or [None])[0]
    _validate(a, sub, text, paths, final)
    tool = next((t["placementId"] for pg in s.get_json(f"/direct/site/{a['context']}/pages.json")
                 for t in pg.get("tools", []) if t.get("toolId") == TOOL_ID), None)
    if not tool:
        raise SakaiError(f"site {a['context']} has no assignment tool")
    ref = f"/assignment/a/{a['context']}/{a['id']}"
    page = Page(s.request("GET", f"/portal/tool/{tool}",
                          params={"assignmentReference": ref, "sakai_action": "doView_submission"}).text)
    pledge = PLEDGE in page.templates
    _check(page, PLEDGE if pledge else VIEW, "opening the submission form")
    if not pledge and EULA in _form(page, "addSubmissionForm")["fields"]:
        raise SakaiError("this assignment sends work to a plagiarism-review service whose licence "
                         "has to be accepted in the browser")
    result = {"assignment": _summary(a), "submission": _state(sub, a),
              "honor_pledge": " ".join(page.texts["pledge"]) or None,
              "files": [{"name": p.name, "size": p.stat().st_size} for p in paths],
              "text_length": None if text is None else len(text),
              "warnings": _warnings(a, sub, paths, final, pledge)}
    if not send:
        return {"dry_run": True, **result}

    if pledge:
        form = _form(page, "assignmentHonorPledgeForm")
        button = "eventSubmit_doAccept_assignment_honor_pledge"
        page = _post(s, form["action"], {**form["fields"], button: form["buttons"][button]})
        _check(page, VIEW, "accepting the honor pledge")
    for p in paths:
        form = _form(page, "addSubmissionForm")
        if not form["upload_url"]:
            raise SakaiError(f"uploading {p.name}: the submission form has no file field "
                             "(a single-file assignment that already has a file attached?)")
        before = page.texts["attachment"]
        upload = (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        page = _post(s, form["upload_url"], _fields(form, text, form["fields"]["option"]), upload)
        _check(page, VIEW, f"uploading {p.name}")
        names = page.texts["attachment"]
        if len(names) != len(before) + 1 or _nfc(p.name) not in map(_nfc, names):
            raise SakaiError(f"uploading {p.name}: the file did not show up among the attachments")

    form = _form(page, "addSubmissionForm")
    if not final:
        page = _post(s, form["action"], _fields(form, text, "save"), ("", b"", "application/octet-stream"))
        _check(page, DONE, "saving the draft")
    else:
        page = _post(s, form["action"], _fields(form, text, "confirm"), ("", b"", "application/octet-stream"))
        _check(page, CONFIRM, "preparing the submission (the work is now saved as a draft)")
        result["confirm_statements"] = page.texts["statement"]
        form = _form(page, "addSubmissionForm")
        if EULA in form["fields"]:
            raise SakaiError("the confirmation page asks to accept a plagiarism-review licence, which "
                             "has to be done in the browser; the work is saved as a draft")
        page = _post(s, form["action"], _fields(form, None, "post"))
        _check(page, DONE, "submitting (the work is saved as a draft)")
        if not page.texts["success"]:
            raise SakaiError("submitting: Sakai did not confirm the submission; check it in the browser")

    after_a = s.get_json(f"/direct/assignment/item/{assignment_id}.json")
    after = (after_a.get("submissions") or [None])[0]
    result["submission"] = _state(after, after_a)
    expected = "submitted" if final else "draft"
    problems = []
    if result["submission"]["state"] != expected:
        problems.append(f"Sakai reports the submission as {result['submission']['state']}, not {expected}")
    if final and sub and after and after.get("dateSubmittedEpochSeconds") == sub.get("dateSubmittedEpochSeconds"):
        problems.append("the submission date did not change")
    if missing := [p.name for p in paths if _nfc(p.name) not in map(_nfc, result["submission"]["attachments"])]:
        problems.append(f"not among the submission's attachments: {', '.join(missing)}")
    return {"done": "submitted" if final else "saved draft", "verified": not problems,
            "problems": problems, **result}


def _validate(a, sub, text, paths, final):
    kind = a.get("submissionType")
    if kind not in TYPES:
        raise SakaiError(f"assignments of type {kind} can't be submitted from here")
    takes_text, max_files = TYPES[kind]
    if text is not None and not takes_text:
        raise SakaiError(f"{kind} assignments take files only, no text")
    if max_files is not None and len(paths) > max_files:
        raise SakaiError(f"{kind} assignments take {max_files or 'no'} file{'s' * (max_files != 1)}, got {len(paths)}")
    close = a.get("closeTimeString")
    if a.get("status") not in ("OPEN", "DUE") or (close and datetime.fromisoformat(close) <= datetime.now(UTC)):
        raise SakaiError(f"the assignment is {a.get('status')} (closes {close}); nothing can be submitted")
    if sub and sub.get("canSubmit") is False:
        raise SakaiError("Sakai says this assignment can't be submitted (again)")
    if not final and _state(sub, a)["state"] == "submitted":
        raise SakaiError("already submitted: saving a draft would withdraw the submission until it is "
                         "submitted again; use submit to resubmit")


def _summary(a) -> dict:
    return {"id": a.get("id"), "title": a.get("title"), "site": a.get("context"), "status": a.get("status"),
            "due": a.get("dueTimeString"), "close": a.get("closeTimeString"),
            "submission_type": a.get("submissionType"), "allows_resubmission": bool(a.get("allowResubmission"))}


def _state(sub, a) -> dict:
    sub = sub or {}
    if sub.get("draft"):
        state = "draft"
    elif sub.get("submitted") and sub.get("dateSubmittedEpochSeconds"):
        state = "submitted"
    else:  # also Sakai's placeholder (submitted, but not by the student and with no date)
        state = "none"
    raw = (sub.get("properties") or {}).get("allow_resubmit_number")
    left = None if raw is None and a.get("allowResubmission") else int(raw or 0)
    return {"state": state, "status": sub.get("status"), "submitted_at": sub.get("dateSubmitted"),
            "text_length": len(sub.get("submittedText") or ""),
            "attachments": [x.get("name") for x in sub.get("submittedAttachments") or []],
            # Sakai's counter: resubmissions allowed after the first submission, -1 = unlimited
            "resubmissions_left": left}


def _warnings(a, sub, paths, final, pledge) -> list[str]:
    st = _state(sub, a)
    out = []
    if final:
        left = st["resubmissions_left"]
        # A draft may follow an earlier submission, and the JSON hides that, so count it as one.
        after = None if left is None else left if left < 0 else left - (st["state"] != "none")
        if not a.get("allowResubmission") or after == 0:
            out.append("FINAL: once submitted this can't be redone or withdrawn; the assignment allows no further resubmission")
        elif after is None:
            out.append("Sakai doesn't say how many resubmissions remain; treat this submission as final")
        else:
            more = "unlimited" if after < 0 else after
            out.append(f"after this, {more} more resubmission(s) are allowed until the resubmission deadline")
        if st["state"] == "submitted":
            out.append("this is a resubmission: it replaces the text submitted before, and Sakai shows it "
                       "as a draft between the confirm and post steps")
    if a.get("status") == "DUE":
        out.append("the due date has passed; Sakai will mark this submission late")
    if paths and st["attachments"]:
        out.append(f"already attached, and staying attached: {', '.join(st['attachments'])}")
    if pledge:
        out.append("this assignment has an honor pledge, which is accepted as part of the submission")
    return out


def _fields(form, text, option) -> dict:
    fields = {k: v for k, v in form["fields"].items() if v is not None}
    if text is not None:
        fields[TEXT] = text
    return {**fields, "option": option}


def _post(s, url, data, upload=None) -> Page:
    u = urlsplit(url)
    if u.netloc and u.netloc != urlsplit(s.base_url).netloc:
        raise SakaiError(f"refusing to post to another host: {u.netloc}")
    # The upload URL renders sakai_csrf_token empty, and for multipart requests Sakai's RequestFilter
    # lets URL parameters override body ones, so keeping it would fail the CSRF check.
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "sakai_csrf_token"])
    r = s.request("POST", u.path + (f"?{query}" if query else ""), data=data,
                  files={"upload": upload} if upload else None)
    return Page(r.text)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name or "")


def _form(page: Page, name: str) -> dict:
    if name not in page.forms:
        raise SakaiError(f"Sakai's page ({', '.join(page.templates)}) has no {name}")
    return page.forms[name]


def _check(page: Page, template: str, what: str):
    if page.texts["alert"]:
        raise SakaiError(f"{what}: {' '.join(page.texts['alert'])}")
    if template not in page.templates:
        raise SakaiError(f"{what}: Sakai showed {', '.join(page.templates) or 'an unknown page'} instead of "
                         f"{template} (an expired session or rejected CSRF token looks like this)")
