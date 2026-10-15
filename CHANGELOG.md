# changelog

all notable changes are listed here. the format follows [keep a changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [semantic versioning](https://semver.org/).

## [unreleased]

### fixed

- tool errors now reach the client with their message (for example "missing environment variable SAKAI_URL" or "the work is saved as a draft"), instead of only `Error executing tool <name>` (#4)

## [0.1.0] - 2026-10-14

first release.

- reading tools: profile, sites, site tools, assignments, announcements, resources, calendar, grades, forum counts, quizzes, roster, notifications and tasks
- downloading resources into a local folder
- clearing notifications and marking them viewed
- creating, editing and deleting tasks
- saving assignment drafts and submitting assignments, with a dry run by default
- password login, or a browser session cookie for sso-only servers
