"""MCP server exposing a Sakai LMS account: courses, assignments, files, notifications, tasks."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import Sakai, SakaiError, text

mcp = MCPServer(
    "sakai",
    instructions="Read and act on a student's Sakai LMS account. Start with list_sites or list_assignments; "
    "ids returned by one tool are the inputs of the others. Tools marked as writing change the account.",
)

_client: Sakai | None = None

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def sakai() -> Sakai:
    global _client
    if _client is None:
        _client = Sakai.from_env()
    return _client


# helpers

def iso(ts: float | int | None) -> str | None:
    """Epoch milliseconds (or seconds, when the value is too small to be milliseconds) to ISO-8601 UTC."""
    if ts is None:
        return None
    seconds = ts / 1000 if ts > 1e11 else ts
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def parse_due(value: str) -> int:
    """ISO-8601 date or datetime to epoch milliseconds; naive values are taken as UTC."""
    dt = datetime.fromisoformat(value)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def content_date(value: str | None) -> str | None:
    """Sakai content timestamps look like 20260917112402875 (UTC, yyyymmddHHMMSSfff)."""
    if not value or len(value) < 14:
        return None
    return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def submission_state(assignment: dict) -> dict:
    """Summarise the current user's submission. Sakai marks `submitted` true even for untouched
    assignments, so a submit timestamp is the real signal."""
    subs = assignment.get("submissions") or []
    if not subs:
        return {"state": "none"}
    sb = subs[0]
    # A resubmission in progress is a draft that still carries the earlier submit date.
    if sb.get("draft"):
        state = "draft"
    elif sb.get("dateSubmittedEpochSeconds"):
        state = "submitted"
    elif sb.get("submittedText") or sb.get("submittedAttachments"):
        state = "draft"
    else:
        state = "not_started"
    out = {"state": state, "submissionId": sb.get("id"), "submittedAt": iso(sb.get("dateSubmittedEpochSeconds")),
           "late": bool(sb.get("late")), "graded": bool(sb.get("graded"))}
    if sb.get("grade"):
        out["grade"] = sb["grade"]
    if feedback := text(sb.get("feedbackText") or sb.get("feedbackComment")):
        out["feedback"] = feedback
    if files := [{"name": a["name"], "url": a["url"]} for a in sb.get("submittedAttachments") or []]:
        out["files"] = files
    if sb.get("submittedText"):
        out["submittedText"] = text(sb["submittedText"])
    return out


def assignment_summary(a: dict) -> dict:
    return {"id": a["id"], "title": a["title"], "siteId": a["context"], "status": a["status"],
            "open": a.get("openTimeString"), "due": a.get("dueTimeString"), "close": a.get("closeTimeString"),
            "type": a.get("submissionType"), "resubmit": a.get("allowResubmission"),
            "grading": f"{a.get('gradeScale')} max {a.get('maxGradePoint')}",
            "submission": submission_state(a)}


def same_host(base_url: str, url: str) -> str:
    """Resolve url against base_url and return it, or raise if it points at another origin."""
    full = urljoin(base_url + "/", url)
    a, b = urlsplit(full), urlsplit(base_url)
    if (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()):
        raise SakaiError(f"refusing to fetch {url}: not on {base_url}")
    return full


def unique_path(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def filename_from(content_disposition: str | None, url: str) -> str:
    name = None
    if content_disposition:
        msg = Message()
        msg["content-disposition"] = content_disposition
        name = msg.get_filename()
    name = name or unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return Path(name).name or "download"


def trim_task(t: dict) -> dict:
    # Sakai stores assignment-generated task titles HTML-escaped ("Booting &amp; systems").
    unescape = lambda v: html.unescape(v) if v else v
    return {"userTaskId": t["userTaskId"], "description": unescape(t.get("description")),
            "due": iso(t.get("due")), "priority": t.get("priority"), "complete": bool(t.get("complete")),
            "notes": unescape(t.get("notes")),
            "siteId": t.get("siteId"), "siteTitle": t.get("siteTitle"), "system": bool(t.get("system")),
            "url": t.get("url")}


def _tasks() -> list[dict]:
    return sakai().get_json("/api/users/me/tasks")["tasks"]


# read tools

@mcp.tool(annotations=READ)
def whoami() -> dict:
    """Return the logged-in user's id, eid and display name, the Sakai server URL and its version."""
    s = sakai()
    u = s.get_json("/direct/user/current.json")
    return {"id": u["id"], "eid": u["eid"], "displayName": u["displayName"], "server": s.base_url,
            "version": s.version()}


@mcp.tool(annotations=READ)
def list_sites(type: str | None = None) -> list[dict]:
    """List the sites (courses and other workspaces) the user belongs to. `type` filters, e.g. "course"."""
    sites = sakai().get_json("/direct/site.json", _limit=500)["site_collection"]
    return [{"id": x["id"], "title": x["title"], "type": x.get("type")} for x in sites
            if type is None or x.get("type") == type]


@mcp.tool(annotations=READ)
def site_tools(site_id: str) -> list[dict]:
    """List the pages of a site and the tools on each one, to see what a course offers."""
    pages = sakai().get_json(f"/direct/site/{site_id}/pages.json")
    return [{"page": p["title"], "tools": [{"toolId": t["toolId"], "id": t["id"], "title": t["title"]}
                                           for t in p["tools"]]} for p in pages]


@mcp.tool(annotations=READ)
def list_assignments(site_id: str | None = None, status: str | None = None) -> list[dict]:
    """List assignments sorted by due date, with the user's submission state (not_started, draft, submitted), grade and feedback. Covers all sites unless site_id is given; `status` is OPEN or CLOSED."""
    path = f"/direct/assignment/site/{site_id}.json" if site_id else "/direct/assignment/my.json"
    items = sakai().get_json(path)["assignment_collection"]
    items = [a for a in items if status is None or a["status"] == status.upper()]
    items.sort(key=lambda a: (a.get("dueTime") or {}).get("epochSecond", 0))
    return [assignment_summary(a) for a in items]


@mcp.tool(annotations=READ)
def get_assignment(assignment_id: str) -> dict:
    """Get one assignment with its instructions as plain text, attachments, dates and the user's submission."""
    a = sakai().get_json(f"/direct/assignment/item/{assignment_id}.json")
    return {**assignment_summary(a), "instructions": text(a.get("instructions")),
            "attachments": [{"name": x["name"], "url": x["url"]} for x in a.get("attachments") or []]}


@mcp.tool(annotations=READ)
def list_announcements(site_id: str | None = None, days: int = 30, limit: int = 50) -> list[dict]:
    """List recent announcements as plain text, newest first, from all sites or one site, looking back `days` days."""
    path = f"/direct/announcement/site/{site_id}.json" if site_id else "/direct/announcement/user.json"
    items = sakai().get_json(path, n=limit, d=days)["announcement_collection"]
    return [{"id": x["id"], "title": x["title"], "body": text(x.get("body")), "createdOn": iso(x["createdOn"]),
             "siteId": x.get("siteId"), "siteTitle": x.get("siteTitle"),
             "attachments": [{"name": a.get("name"), "url": a.get("url")} for a in x.get("attachments") or []]}
            for x in items[:limit]]


@mcp.tool(annotations=READ)
def list_resources(site_id: str) -> list[dict]:
    """List every file and folder in a site's Resources tool, flat, with a download url for use with `download`."""
    items = sakai().get_json(f"/direct/content/site/{site_id}.json")["content_collection"]
    return [{"title": x["title"], "type": x["type"], "container": x["container"], "size": x["size"],
             "modified": content_date(x.get("modifiedDate")), "url": x["url"]} for x in items]


@mcp.tool(annotations=WRITE)
def download(url: str, dest_dir: str) -> dict:
    """Download a file from this Sakai server (an /access/... url from list_resources or an assignment) into dest_dir and return its path and size. Existing files are never overwritten."""
    s = sakai()
    r = s.request("GET", same_host(s.base_url, url))
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    path = unique_path(dest / filename_from(r.headers.get("content-disposition"), str(r.url)))
    with open(path, "xb") as f:  # "x" fails instead of overwriting a file created since unique_path
        f.write(r.content)
    return {"path": str(path), "size": len(r.content)}


@mcp.tool(annotations=READ)
def calendar(site_id: str | None = None) -> list[dict]:
    """List upcoming calendar events for the user, or for one site."""
    path = f"/api/sites/{site_id}/calendar" if site_id else "/api/users/current/calendar"
    return sakai().get_json(path)["events"]


@mcp.tool(annotations=READ)
def grades(site_id: str | None = None) -> list[dict]:
    """List gradebook scores for the user across all sites, or for one site."""
    path = f"/api/sites/{site_id}/grades" if site_id else "/api/users/me/grades"
    return [{"name": g["name"], "score": g["score"], "notGradedYet": g["notGradedYet"],
             "siteId": g.get("siteId"), "siteTitle": g.get("siteTitle")}
            for g in sakai().get_json(path)["grades"]]


@mcp.tool(annotations=READ)
def forums_summary() -> list[dict]:
    """Summarise the discussion forums of each site: forum and message counts."""
    return [{"siteId": f["siteId"], "siteTitle": f["siteTitle"], "forumCount": f["forumCount"],
             "messageCount": f["messageCount"], "url": f["messageUrl"]}
            for f in sakai().get_json("/api/users/current/forums/summary")["forums"]]


@mcp.tool(annotations=READ)
def list_quizzes(site_id: str) -> list[dict]:
    """List the published quizzes (Tests & Quizzes) of a site."""
    return sakai().get_json(f"/direct/sam_pub/context/{site_id}.json")["sam_pub_collection"]


@mcp.tool(annotations=READ)
def site_roster(site_id: str) -> list[dict]:
    """List the members of a site with their role."""
    members = sakai().get_json(f"/direct/roster-membership/{site_id}/get-membership.json")["members"]
    return [{"userId": m["userId"], "displayName": m["displayName"], "role": m["role"]} for m in members]


# notifications

@mcp.tool(annotations=READ)
def list_notifications(unviewed_only: bool = False, limit: int = 50) -> list[dict]:
    """List recent notifications (new grades, announcements, ...) with ids usable by clear_notification."""
    items = sakai().get_json("/api/users/me/notifications")
    items = [n for n in items if not (unviewed_only and n["viewed"])][:limit]
    return [{"id": n["id"], "event": n["event"], "title": n["title"], "siteTitle": n.get("siteTitle"),
             "url": n.get("url"), "at": iso(n["eventDate"]), "viewed": n["viewed"], "tool": n.get("tool"),
             "from": n.get("fromDisplayName")} for n in items]


@mcp.tool(annotations=WRITE)
def clear_notification(id: str) -> str:
    """Dismiss one notification."""
    sakai().request("POST", f"/api/users/me/notifications/{id}/clear")
    return "cleared"


@mcp.tool(annotations=DESTRUCTIVE)
def clear_all_notifications() -> str:
    """Dismiss every notification."""
    sakai().request("POST", "/api/users/me/notifications/clear")
    return "cleared"


@mcp.tool(annotations=WRITE)
def mark_notifications_viewed() -> str:
    """Mark all notifications as viewed without dismissing them."""
    sakai().request("POST", "/api/users/me/notifications/markViewed")
    return "marked viewed"


# tasks

@mcp.tool(annotations=READ)
def list_tasks() -> list[dict]:
    """List the user's tasks (personal ones and those generated by assignments), with due dates."""
    return [trim_task(t) for t in _tasks()]


@mcp.tool(annotations=WRITE)
def create_task(description: str, due: str | None = None, priority: int = 3, notes: str | None = None,
                site_id: str | None = None) -> dict:
    """Create a personal task. `due` is an ISO-8601 date or datetime (UTC if no offset), `priority` 1 to 5."""
    s = sakai()
    s.ensure()
    body = {"userId": s.user_id, "description": description, "priority": priority, "notes": notes,
            "siteId": site_id, "due": parse_due(due) if due else None, "complete": False,
            "assignationType": "user"}
    return trim_task(s.request("POST", "/api/tasks", json=body).json())


@mcp.tool(annotations=WRITE)
def update_task(user_task_id: int, description: str | None = None, due: str | None = None,
                priority: int | None = None, notes: str | None = None, complete: bool | None = None) -> dict:
    """Change fields of an existing task by userTaskId; omitted fields stay as they are. `due` is ISO-8601."""
    task = next((t for t in _tasks() if t["userTaskId"] == user_task_id), None)
    if task is None:
        raise SakaiError(f"no task with userTaskId {user_task_id}")
    changes = {"description": description, "due": parse_due(due) if due else None, "priority": priority,
               "notes": notes, "complete": complete}
    task.update({k: v for k, v in changes.items() if v is not None})
    return trim_task(sakai().request("PUT", f"/api/tasks/{user_task_id}", json=task).json())


@mcp.tool(annotations=DESTRUCTIVE)
def delete_task(user_task_id: int) -> str:
    """Delete a task by userTaskId. This cannot be undone."""
    sakai().request("DELETE", f"/api/tasks/{user_task_id}")
    return "deleted"


# assignment submission

@mcp.tool(annotations=WRITE)
def assignment_save_draft(assignment_id: str, text: str | None = None, files: list[str] | None = None) -> dict:
    """Save a draft answer (text and/or local file paths) for an assignment without submitting it to the teacher."""
    from .submit import save_draft
    return save_draft(sakai(), assignment_id, text, files)


@mcp.tool(annotations=DESTRUCTIVE)
def assignment_submit(assignment_id: str, text: str | None = None, files: list[str] | None = None,
                      confirm: bool = False) -> dict:
    """Submit an assignment (text and/or local file paths). With confirm=false (the default) this is a dry run that only previews what would be sent. With confirm=true the submission is real, goes to the teacher and may not be undoable, so only confirm after the user agreed."""
    from .submit import submit
    return submit(sakai(), assignment_id, text, files, confirm)


def main():
    mcp.run()
