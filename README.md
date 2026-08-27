# ParentSquare MCP Server

[![MCP Registry](https://img.shields.io/badge/MCP-Registry-blue)](https://registry.modelcontextprotocol.io) [![PyPI](https://img.shields.io/pypi/v/parentsquare-mcp)](https://pypi.org/project/parentsquare-mcp/)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that gives Claude access to [ParentSquare](https://www.parentsquare.com), a school-parent communication platform. Since ParentSquare has no public API, this server scrapes the web interface using saved session cookies.

Covers both the **parent/guardian experience** (feeds, posts, calendars, messages, directories, sign-ups, forms, payments) and **school admin roster management** — reading student and guardian rosters, and creating/editing students and guardians plus sending registration invitations. Admin write tools are **off by default**, gated behind `PS_ENABLE_WRITES`, and every write is recorded to a local audit log.

Available on the [MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.thehesiod/psquare` and on [PyPI](https://pypi.org/project/parentsquare-mcp/) as `parentsquare-mcp`.

## Disclaimer

> **This project is not affiliated with, endorsed by, or sponsored by ParentSquare, Inc.** "ParentSquare" and all related names, logos, and trademarks are the property of ParentSquare, Inc.
>
> This server communicates with ParentSquare's **undocumented internal APIs** (scraping the web UI and calling its non-public `/api/v2/` JSON endpoints) — these are not published, not guaranteed to be stable, and may change or be blocked at any time without notice. Use of those interfaces may violate ParentSquare's Terms of Service; you are responsible for reviewing the ToS and deciding whether your use is acceptable.
>
> **Use at your own risk.** The authors and contributors accept no responsibility for any consequences of using this software, including but not limited to: account suspension or termination, data loss or corruption, missed or incorrect notifications, MFA lockouts, leaked session cookies, IP blocks, or any other direct or indirect damages. No warranty is provided — see [LICENSE](LICENSE) for the full MIT no-warranty clause.
>
> If ParentSquare publishes an official API, this project should be considered deprecated in favor of that.

## Features

### Feed & Posts
- **`get_feeds`** — Browse paginated school feed with titles, authors, summaries, and attachment names
- **`get_post`** — Full post details with body text, comments, poll results, signup items, and **inline image/PDF content** (Claude can "see" attached calendars, flyers, etc.)
- **`get_group_feed`** — Posts from a specific group

### Calendar
- **`get_calendar_events`** — Events from ICS calendar as structured JSON (title, start/end, location, description)
- Falls back to guiding Claude to search feed posts for image/PDF calendars when ICS is empty

### Communication
- **`list_conversations`** / **`get_conversation`** — Read message threads
- **`get_directory`** — Staff directory as structured JSON (name, role, phone, user_id)
- **`get_staff_member`** — Full staff details with email, office hours, and **inline profile photo**

### Media & Files
- **`list_photos`** — Photo gallery with URLs
- **`list_files`** — Document files
- **`download_file`** — Download any attachment to local disk

### Participate
- **`list_signups`** — Sign-up and RSVP posts with progress tracking (e.g. "53/103 Items")
- **`list_notices`** — Alerts and secure documents
- **`list_polls`** — Polls with vote counts and winning options
- **`list_forms`** — Permission slips and signable forms
- **`list_payments`** — Payment items with prices and summary stats
- **`list_volunteer_hours`** — Logged volunteer hours with totals

### Groups & Discovery
- **`list_schools`** — Schools and students as structured JSON
- **`list_school_features`** — Available sections per school (parsed from sidebar)
- **`list_groups`** — Groups with member counts, descriptions, and membership status
- **`list_links`** — Quick-access links (Google Drive, external sites)

### Student
- **`get_student_dashboard`** — School, grade, classes, and teachers as structured JSON

### Admin
Read tools are always available; the tools marked *(write)* below are **disabled by default** and only run when `PS_ENABLE_WRITES` is set (see [Enabling admin write tools](#enabling-admin-write-tools)). Every write is recorded to a local audit log. Writes are create/edit only — there is no destructive operation (no deletion of students, guardians, classes, or staff).

#### Roster: students & guardians
- **`list_students`** — School roster (id, name, grade, SIS id, guardians) as structured JSON, with optional `grade` / `name_contains` filters
- **`list_parents`** — Guardian roster (user_id, name, email, phone, linked students) as structured JSON, with optional `name_contains` / `student_name_contains` filters; provides the `user_id` needed by `edit_parent` / `link_guardian_to_student`
- **`list_grades`** — A school's grades and their `grade_id` values (needed for add/edit)
- **`get_student`** — Admin detail for one student (name, grade, SIS id, linked guardians, classes)
- **`add_student`** *(write)* — Create a student in a grade
- **`edit_student`** *(write)* — Update a student's name, SIS id, or grade (unchanged fields preserved)
- **`add_parent`** *(write)* — Create a guardian linked to a student
- **`edit_parent`** *(write)* — Update a guardian's name, email, or phone (existing links preserved)
- **`link_guardian_to_student`** *(write)* — Link an existing guardian to an additional student
- **`invite_parent`** *(write)* — Send (or resend) a ParentSquare registration invitation to one guardian
- **`bulk_invite_parents`** *(write)* — Invite many guardians at once; already-registered guardians are skipped automatically

#### Classes, staff & enrollment
- **`list_classes`** / **`get_class`** — A school's classes, and one class with its full staff list (teachers, assistants, room parents)
- **`add_class`** *(write)* — Create a class; new classes start **hidden** until `set_class_visibility`
- **`edit_class`** *(write)* — Rename a class or change its grades
- **`set_class_visibility`** *(write)* — Show or hide classes (date-driven, defaults to today)
- **`list_staff`** — Staff and admin roster (user_id, name, email, phone, role/title) as structured JSON, with an optional `name_contains` filter; provides the `user_id` needed by `edit_staff` / `add_class_staff`
- **`add_staff`** *(write)* — Add a teacher, staff member, or admin, optionally assigning them to classes
- **`edit_staff`** *(write)* — Update a staff member's name, email, phone, title, or staff ID (class assignments and STAFF/ADMIN access preserved; guardians are rejected — use `edit_parent`)
- **`add_class_staff`** / **`remove_class_staff`** *(write)* — Assign or unassign teachers, assistants, and room parents for a class. Section-membership writes are serialized inside one server process; never issue them in parallel, and verify each result with a fresh read.
- **`list_class_students`** — The students enrolled in a class
- **`add_class_students`** / **`remove_class_students`** / **`move_student_to_class`** *(write)* — Manage which students are enrolled in which classes. These share the same global serialization lock as class-staff writes because student-section updates can replace a student's full enrollment list.

### Authentication
- **`submit_mfa_code`** — Complete MFA verification with a 6-digit code
- Supports MCP elicitation for inline MFA prompts (set `PS_NO_ELICIT` to disable for unattended callers)
- Session cookies persisted to `~/.parentsquare_cookies.json`
- Credentials loaded from environment variables, 1Password, or LastPass CLI on session expiry

## Setup

### Enabling admin write tools

The admin write tools — every tool marked *(write)* under [Admin](#admin), covering
the student/guardian roster, classes, staff, and class enrollment — modify live
school data, so they are **off by default**. To enable them, set `PS_ENABLE_WRITES=1`
(or `true`/`yes`/`on`) in the server's environment and restart. Every write attempt
(including blocked ones) is appended as JSONL to `PS_AUDIT_LOG` (default
`~/.parentsquare_audit.log`). The admin read tools work regardless.

ParentSquare's form endpoints answer every accepted POST with the same generic
`200` "reload" response, which some silent failures also return. To avoid
false-positive successes, the create/link tools (`add_student`, `add_parent`,
`link_guardian_to_student`) **read back authoritative state after the write** and
only report `✅ Success (verified)` once the new record is actually found. If the
POST is accepted but the read-back can't find the change, they return a `⚠️`
warning that it likely did not persist; if the read-back itself can't run, they
report the write as submitted-but-unverified.

The read-back also overrules a `5xx`. ParentSquare renders its error page *after*
the transaction commits, so a server error can hide a write that actually landed
— `add_student` did exactly that on every create until a missing
`student[section_ids][]` form param was tracked down. Reporting those as failures
invited retries, and each retry duplicated a real student with no API route to
delete one. So when a write returns a `5xx` but the record is found on read-back,
the tool reports `✅ Success (verified)` with a note not to retry. An explicit
rejection (a `4xx`, or a `200` carrying an `alert-danger` flash) is still
reported as a failure regardless of read-back.

### Prerequisites

Credentials can be provided in either of two ways (checked in this order):

1. **Environment variables** — set `PS_USERNAME` and `PS_PASSWORD`
2. **A credential manager** selected by `PS_CREDENTIAL_PROVIDER` (default `1password`):
   - **[1Password CLI](https://developer.1password.com/docs/cli/)** (`op`) — with a "Parentsquare" item containing `username` and `password` fields
   - **[LastPass CLI](https://github.com/LastPass/lastpass-cli)** (`lpass`) — set `PS_CREDENTIAL_PROVIDER=lastpass`. Run `lpass login <your-lastpass-email>` in a terminal first (may prompt for MFA). The item read defaults to `parentsquare.com` and can be overridden with `PS_LASTPASS_ITEM` (an exact entry name or entry ID).

### Install in Claude Code

```bash
claude mcp add --transport stdio parentsquare -- uvx --from "parentsquare-mcp @ git+https://github.com/thehesiod/psquare-mcp" parentsquare-mcp
```

To enable PDF text extraction for post attachments (optional, AGPL-3.0 licensed):

```bash
claude mcp add --transport stdio parentsquare -- uvx --from "parentsquare-mcp[pdf] @ git+https://github.com/thehesiod/psquare-mcp" parentsquare-mcp
```

### That's It

No further configuration needed. The server **auto-discovers** your schools, students, and user ID from ParentSquare on first use. Authentication is handled automatically — when the session expires, the server loads your credentials from environment variables (or 1Password CLI) and re-authenticates (including MFA if needed).

To use environment variables with Claude Code, add an `env` block to your MCP config:

```json
{
  "mcpServers": {
    "parentsquare": {
      "command": "uvx",
      "args": ["parentsquare-mcp"],
      "env": {
        "PS_USERNAME": "your@email.com",
        "PS_PASSWORD": "your-password"
      }
    }
  }
}
```

> **Security note:** environment variables place your password in plaintext inside your MCP config file. If you chose a password manager specifically to avoid that, prefer the 1Password or LastPass CLI path.

To use the LastPass CLI instead of 1Password, log in once (`lpass login <your-lastpass-email>`) and set the provider in your MCP config:

```json
{
  "mcpServers": {
    "parentsquare": {
      "command": "uvx",
      "args": ["parentsquare-mcp"],
      "env": {
        "PS_CREDENTIAL_PROVIDER": "lastpass",
        "PS_LASTPASS_ITEM": "parentsquare.com"
      }
    }
  }
}
```

`PS_LASTPASS_ITEM` is optional (defaults to `parentsquare.com`).

### Unattended use (`PS_NO_ELICIT`)

When MFA is required, the server prompts for the code inline via MCP elicitation.
That prompt waits for a human, so an unattended caller — a claude.ai routine, a
scheduled job — has nobody to answer it and the tool call simply blocks until the
elicitation times out.

Set `PS_NO_ELICIT=1` to skip the prompt. The tool returns the "MFA verification
required" message immediately, and the caller can retrieve the code out of band
(via a Gmail or Microsoft 365 MCP, for example) and pass it to `submit_mfa_code`:

```json
{
  "mcpServers": {
    "parentsquare": {
      "command": "uvx",
      "args": ["parentsquare-mcp"],
      "env": {
        "PS_NO_ELICIT": "1"
      }
    }
  }
}
```

The check is presence-based: **any** non-empty value disables elicitation, so
`PS_NO_ELICIT=0` and `PS_NO_ELICIT=false` also disable it. To re-enable inline
prompting, unset the variable entirely.

## How It Works

The server uses `requests` + `BeautifulSoup` to scrape ParentSquare's server-rendered HTML pages. Each tool follows the pattern:

1. **Fetch** the HTML page via `PSClient.get_page()` or JSON via `PSClient.get_json()` (auto-relogins on session expiry)
2. **Parse** with a dedicated parser in `parsers/` that extracts structured data into dataclasses
3. **Return** results as either structured JSON dicts (for data-lookup tools) or markdown text (for content-rich tools)

Data-lookup tools (`list_schools`, `get_directory`, `get_calendar_events`, `get_student_dashboard`, `get_staff_member`) return structured JSON for easy programmatic access. Content tools (`get_post`, `get_feeds`, `get_conversation`) return markdown.

On first use, the server auto-discovers your schools, students, and user ID from ParentSquare (no config file needed).

For `get_post`, image attachments are downloaded and returned as MCP `Image` objects (so Claude can see them), and PDF attachments have their text extracted via pymupdf. `get_staff_member` also returns inline profile photos.

Groups use a GraphQL endpoint (`/graphql`) instead of HTML scraping. The directory and staff details use the internal `/api/v2/` JSON:API.

## Dependencies

| Package | Purpose | License |
|---------|---------|---------|
| `mcp` | Model Context Protocol SDK | MIT |
| `requests` | HTTP client | Apache 2.0 |
| `beautifulsoup4` | HTML parsing | MIT |
| `icalendar` | ICS calendar parsing | BSD |
| `pymupdf` | PDF text extraction (optional) | AGPL-3.0 |

## License

MIT — see [LICENSE](LICENSE). Note: the optional `pymupdf` dependency is AGPL-3.0 licensed.

mcp-name: io.github.thehesiod/psquare
