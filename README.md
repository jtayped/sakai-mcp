# sakai-mcp

[![ci](https://github.com/jtayped/sakai-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jtayped/sakai-mcp/actions/workflows/ci.yml)
[![license: mit](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

an [mcp](https://modelcontextprotocol.io) server for a student's account on a [sakai](https://www.sakailms.org) lms. it lets an assistant such as claude read your courses, assignments, announcements, files, grades and notifications, manage your tasks, and save or submit assignments.

it runs on your own machine and talks to your university's sakai server with your own login. nothing is sent anywhere else.

this is an independent project, not affiliated with apereo or the sakai project. tested against sakai 25. nothing in the code is specific to one university.

## tools

| tool | what it does |
| --- | --- |
| `whoami` | logged-in user and server version |
| `list_sites`, `site_tools` | your courses and other sites, and the tools each one has |
| `list_assignments`, `get_assignment` | assignments with due dates, your submission state, grade and feedback; one assignment's instructions and attachments |
| `list_announcements` | recent announcements as plain text |
| `list_resources`, `download` | files in a site's resources, and downloading one into a local folder |
| `calendar`, `grades`, `forums_summary`, `list_quizzes`, `site_roster` | calendar events, gradebook scores, forum counts, published quizzes, site members |
| `list_notifications`, `clear_notification`, `clear_all_notifications`, `mark_notifications_viewed` | notifications and clearing them |
| `list_tasks`, `create_task`, `update_task`, `delete_task` | your personal and assignment tasks |
| `assignment_save_draft`, `assignment_submit` | save a draft, or submit text and files to an assignment |

every tool that isn't a reading tool changes your account, except `download`, which only writes a local file. the mcp annotations say which tools write. `assignment_submit` only previews what it would send unless it's called with `confirm=true`. the preview includes how many resubmissions you'd have left.

the writing tools are new and have been tested mostly against captured pages, not live servers. check the result in your browser the first few times you use them.

## install

you need [uv](https://docs.astral.sh/uv/). there's no pypi release yet, so run it from github:

```sh
uvx --from git+https://github.com/jtayped/sakai-mcp sakai-mcp
```

for claude code:

```sh
claude mcp add -s user sakai \
  -e SAKAI_URL=https://sakai.example.edu \
  -e SAKAI_USERNAME=your-username \
  -e SAKAI_PASSWORD=your-password \
  -- uvx --from git+https://github.com/jtayped/sakai-mcp sakai-mcp
```

for claude desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sakai": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jtayped/sakai-mcp", "sakai-mcp"],
      "env": {
        "SAKAI_URL": "https://sakai.example.edu",
        "SAKAI_USERNAME": "your-username",
        "SAKAI_PASSWORD": "your-password"
      }
    }
  }
}
```

both configs store your password in plain text. if you have a secrets manager, a small launcher script that reads the password from it and then runs `sakai-mcp` keeps it out of the config file.

## configuration

| variable | meaning |
| --- | --- |
| `SAKAI_URL` | base url of your sakai server, for example `https://sakai.example.edu` |
| `SAKAI_USERNAME`, `SAKAI_PASSWORD` | your sakai login |
| `SAKAI_COOKIE` | browser session cookie, for servers that only allow sso logins (see below) |
| `SAKAI_STATE_DIR` | where the session cookie is kept, default `~/.local/state/sakai-mcp` |

the session cookie is saved there with mode 0600, in one file per server, so a restart reuses the session instead of logging in again.

### sso-only servers

some universities only let you log in through sso, so the username and password don't work. set `SAKAI_COOKIE` instead. log in to sakai in a browser, open the developer tools, and copy the `Cookie` request header of any sakai page (it looks like `JSESSIONID=...; ...`).

the server can't log in again by itself in this mode. when the session expires, every tool returns an error asking for a new cookie, and you restart the server after updating it.

## how it talks to sakai

- **login:** posts the username and password to `/direct/session`, then to `/api/login` if that fails, and checks the result with `/direct/session/current.json`. with `SAKAI_COOKIE` set and no password, it skips login and uses the browser's session.
- **session expiry:** anonymous `/direct` requests get http 200 with empty lists, not an error. the client therefore rechecks the session every 5 minutes and logs in again when it's gone.
- **`/direct` (entitybroker):** sites, assignments, announcements, resources, quizzes and roster. present on every modern sakai.
- **`/api` (webapi):** calendar, grades, forum summary, notifications and tasks. only newer releases have it. on a server without the route, the tool returns an error that says so.
- **assignment submission:** sakai has no rest endpoint for it, so `submit.py` replays the assignment tool's form posts: the honor pledge, file uploads, then save or confirm and post. after posting, it reads the assignment back to check the result.
- **redirects** are only followed within the sakai server, and the session cookie is only sent to it. a download that redirects elsewhere, such as a web-link resource, fails instead.
- **downloads** never overwrite an existing file.

saving a draft refuses on an assignment you've already submitted, because on sakai that withdraws the submission. assignments that need a plagiarism-check agreement have to be submitted in the browser.

## development

```sh
uv sync
uv run pytest -q
```

the tests run offline. `tests/fixtures/` holds submission pages captured from a real server, with csrf tokens and personal data removed. see the [contributing guide](CONTRIBUTING.md).

## license

[mit](LICENSE)
