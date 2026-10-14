# contributing

bug reports, fixes and support for more sakai versions are welcome.

## setup

```sh
uv sync
uv run pytest -q
```

the tests run offline and must keep doing so. to try a change against a real server, run the server with your own account (see the readme) and call the tools from an mcp client.

## testing against a live account

your account is real. while developing, stick to reading tools and dry runs (`assignment_submit` without `confirm=true`). only save drafts or submit on an assignment where that's fine, and say in the pr which write paths you tested live.

## captured pages

the submission flow is tested against html pages captured from a real sakai server in `tests/fixtures/`. before adding or updating one, remove:

- csrf tokens (replace the value with `CSRF-TOKEN`)
- names, usernames, emails and user ids
- the server's hostname and site ids (use `sakai.example.edu` and ids like `course-101`)
- course titles and the teacher's instructions (placeholder text is fine)
- anything else that identifies you, your classmates or your teachers

assignment and tool ids are random and can stay, since the tests use them.

## pull requests

- keep each pr to one change and add a test for new behaviour.
- say which sakai version you tested on, if you tested live.
- never include passwords, cookies or session ids in code, tests, issues or prs.
