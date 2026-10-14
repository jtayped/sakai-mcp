# security

sakai-mcp handles sakai passwords and session cookies, and it can submit assignments, so security reports matter.

## reporting a vulnerability

please report it privately through [github's security advisories](https://github.com/jtayped/sakai-mcp/security/advisories/new), not in a public issue. include what an attacker could do and how to reproduce it. you should get an answer within a week.

## what's in scope

- the password or session cookie leaking: in logs, error messages, tool results, or files readable by other users
- a tool reaching hosts other than the configured sakai server, or writing files outside the folder it was given
- a write action (submit, delete, clear) happening without the call that asks for it

problems in sakai itself should go to the [sakai project](https://www.sakailms.org) or your university.

## supported versions

only the latest commit on `main` gets fixes.
