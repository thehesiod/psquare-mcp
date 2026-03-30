# ParentSquare MCP Server

[![MCP Registry](https://img.shields.io/badge/MCP-Registry-blue)](https://registry.modelcontextprotocol.io) [![PyPI](https://img.shields.io/pypi/v/parentsquare-mcp)](https://pypi.org/project/parentsquare-mcp/)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that gives Claude access to [ParentSquare](https://www.parentsquare.com), a school-parent communication platform. Since ParentSquare has no public API, this server scrapes the web interface using saved session cookies.

Available on the [MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.thehesiod/psquare` and on [PyPI](https://pypi.org/project/parentsquare-mcp/) as `parentsquare-mcp`.

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

### Authentication
- **`submit_mfa_code`** — Complete MFA verification with a 6-digit code
- Supports MCP elicitation for inline MFA prompts
- Session cookies persisted to `~/.parentsquare_cookies.json`
- Credentials loaded from 1Password CLI on session expiry

## Setup

### Prerequisites
- [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) with a "Parentsquare" item containing `username` and `password` fields

### Install in Claude Code

```bash
claude mcp add --transport stdio parentsquare -- uvx --from "parentsquare-mcp @ git+https://github.com/thehesiod/psquare-mcp" parentsquare-mcp
```

To enable PDF text extraction for post attachments (optional, AGPL-3.0 licensed):

```bash
claude mcp add --transport stdio parentsquare -- uvx --from "parentsquare-mcp[pdf] @ git+https://github.com/thehesiod/psquare-mcp" parentsquare-mcp
```

### That's It

No further configuration needed. The server **auto-discovers** your schools, students, and user ID from ParentSquare on first use. Authentication is handled automatically via 1Password CLI — when the session expires, the server loads your credentials from 1Password and re-authenticates (including MFA if needed).

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

## Disclaimer

This project is an independent, unofficial tool and is **not affiliated with, endorsed by, or associated with ParentSquare, Inc.** in any way. ParentSquare is a trademark of ParentSquare, Inc.

This server works by scraping ParentSquare's web interface, which has no public API. Use at your own risk. The authors take no responsibility for account suspension, data loss, or any other consequences arising from its use. ParentSquare may change their website at any time, which could break functionality or violate their terms of service.

## License

MIT — see [LICENSE](LICENSE). Note: the optional `pymupdf` dependency is AGPL-3.0 licensed.

mcp-name: io.github.thehesiod/psquare
