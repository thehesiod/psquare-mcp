# ParentSquare MCP Server

## Architecture

MCP server that scrapes ParentSquare's web UI. Runs as stdio transport. While there's no documented public API, ParentSquare has an internal JSON:API at `/api/v2/` that some tools use (e.g. directory).

```
server.py          — MCP tool definitions, inline image/PDF fetching
client.py          — HTTP client with auto-relogin on session expiry
auth.py            — Cookie persistence (~/.parentsquare_cookies.json), credential loading (env vars → 1Password/LastPass), MFA flow
audit.py           — Write-gate (PS_ENABLE_WRITES) + JSONL audit log for admin write tools
config.py          — URL templates and constants (no personal data — auto-discovered at runtime)
models.py          — Dataclasses for all parsed entities
download.py        — File download with conflict handling
parsers/           — One module per page type (feeds, calendar, media, messages, etc.); parsers/admin.py holds roster/edit-form parsing + write-body builders
export_cookies.py  — CLI helper to bootstrap cookies from browser DevTools
```

## Key Patterns

### Authentication
- Cookies are lazy-loaded from `~/.parentsquare_cookies.json` on startup (no network call)
- On session expiry (detected by redirect to `/signin` **or** missing `gon.user_id` on the root page), credentials are loaded via `load_credentials()`: first from `PS_USERNAME`/`PS_PASSWORD` env vars, then from the provider named by `PS_CREDENTIAL_PROVIDER` (default `1password`). Options:
  - `1password` — `op item get Parentsquare` (item named "Parentsquare" with fields labeled `username` and `password`)
  - `lastpass` — `lpass show --json <item>` where the item defaults to `parentsquare.com` and is overridable with `PS_LASTPASS_ITEM` (exact name or entry ID). Requires a prior `lpass login <email>`; `lpass status --quiet` is checked first (short timeout) so requests never hang on an interactive prompt. Credential values are never logged or included in error messages.
- **Important**: ParentSquare's root page (`/`) returns HTTP 200 even for unauthenticated users, so `/signin` redirect alone is not sufficient to detect expired sessions. `discover_account()` and `is_session_valid()` also check for `gon.user_id` in the page content.
- MFA code submission verifies the session is actually authenticated after the code is accepted
- MFA state persists to disk (`.parentsquare_mfa_state.json`) so it survives server restarts
- The server supports MCP elicitation for inline MFA code entry
- **User-Agent must include "Chrome"** — ParentSquare returns 403 `browser_unsupported` otherwise, and (worse) can serve *unauthenticated* content to a script-looking request that carries valid cookies, which ends in placeholder data rather than an error. `make_session()` in `client.py` owns this and is `PSClient`'s default session, so a bare `PSClient()` is browser-like too; `app_lifespan` just calls it. Do not re-apply the header at a call site.
- The `ps_s` session cookie is **httpOnly** — it can't be read via `document.cookie`, which is why `export_cookies` requires the Network tab in DevTools
- `ps_s` rotates on every request. `PSClient` calls `_save_cookies_if_changed()` after each successful request to persist the latest value.
- GraphQL requests (used by `list_groups`) require a CSRF token extracted from a page's `<meta name="csrf-token">` tag. MFA submit also requires a CSRF token from the MFA page.
- **The CSRF token is cached on `PSClient` for the life of the session.** Rails derives it from a per-session secret, so it stays valid across requests even though `ps_s` rotates — verified live. Without the cache every single write paid an extra `GET /`. `_with_csrf()` wraps each write and retries **once** with a force-refreshed token if the response looks like a rejected token or a dead session (`_is_csrf_rejection`: a bounce to `/signin`, a 401/419, or a 403/422 whose body names the token). That detection is deliberately narrow — retrying a plain 422 validation error could re-apply a write that had actually landed. Because caching removed the implicit `GET /` (and its session-expiry check) from every write, this retry path is now the thing that recovers an expired session mid-write. `_relogin()` and MFA completion both call `invalidate_csrf_token()`.

### JSON:API (`/api/v2/`)
ParentSquare has an internal JSON:API (not publicly documented). Discovered by inspecting JS bundle XHR calls:
- **`/api/v2/schools/{id}`** — school info (name, phone, address, timezone). Used by `get_directory`.
- **`/api/v2/schools/{id}/directory`** — staff directory (JSON:API format with `included` array containing staff records). Used by `get_directory`.
- **`/api/v2/schools/{id}/users/{user_id}`** — individual staff details (email, photo, virtual phone, office hours). Used by `get_staff_member`.
- **`/api/v2/users/{id}/virtual_phone_search`** — POST with `{"staff_ids": [...]}` to batch-fetch virtual phone numbers. Used by `get_directory`.
- **`/api/v2/sections/{id}/staff`** and **`/api/v2/sections/{id}/students`** — per-section (class) directory lookups.
- Use `client.get_json()` for GET and `client.post_json()` for POST (handles CSRF tokens automatically).
- Many pages that appear empty in HTML are actually shell pages that load data via this API. If an HTML parser returns no data, check the JS bundle for `/api/v2/` XHR calls.

### HTML Parsing
- All parsing uses BeautifulSoup with `html.parser`
- Two distinct image patterns exist in the DOM:
  - `img.feed-image-thumbnail` — gallery/attached images (outside description div)
  - `<img>` inside `.description` div — inline embedded images
- S3/CloudFront download links carry original filenames in `response-content-disposition` query params
- Attachment links are recognised by `urls.is_attachment_href()`, which matches the **parsed host** (plus signed-CloudFront query params). Never substring-test a URL here: these hrefs come from school-authored HTML and `get_post` fetches them with the caller's authenticated session
- URL deduplication via `_url_path_key()` prevents returning the same image as both thumbnail and full-size

### Response Formats
- **Structured JSON** (`-> dict`): `list_schools`, `get_calendar_events`, `get_directory`, `get_student_dashboard` — return dicts that FastMCP serializes as JSON. Better for data-lookup where Claude filters/extracts fields.
- **Mixed list** (`-> list`): `get_post`, `get_staff_member` — return a list of text + MCP `Image` objects for inline media.
- **Markdown text** (`-> str`): all other tools — formatted markdown for content-rich responses.

### Inline Content
- `get_post`: images downloaded as MCP `Image` objects (5 MB per image, 10 MB total cap), PDFs text-extracted via pymupdf
- `get_staff_member`: profile photo returned as inline `Image`
- This lets Claude "see" attached calendars, flyers, staff photos etc. without extra tool calls

### Admin write tools (roster: students & guardians)
Reverse-engineered from the admin roster UI and verified live (endpoints/bodies documented in Jason's vault note "ParentSquare admin API mapping"). v1 scope is **create/edit only** — no destructive ops (delete/unlink are deferred to the website).

- **Write gate:** every write tool checks `writes_enabled()` (env `PS_ENABLE_WRITES`, default off) and returns a friendly message if disabled. Reads (`list_students`, `list_parents`, `list_grades`, `get_student`) are ungated.
- **Audit:** `audit_write(tool, args, ok, detail)` appends JSONL to `PS_AUDIT_LOG` (default `~/.parentsquare_audit.log`) for every attempt, including blocked ones.
- **Writes are `application/x-www-form-urlencoded` Rails posts**, not JSON — use `PSClient.post_form(path, data)` (auto-injects `utf8=✓` + `authenticity_token`, sends `X-CSRF-Token`). It does **not** raise on 4xx/5xx; interpret the result with `write_succeeded()` (**success = HTTP 200 + `text/javascript` body: either a reload script or an `alert-success` flash**; an `alert-danger`/`alert-error` flash or an HTML error page, e.g. 404, is failure). `parse_flash_message()` pulls ParentSquare's own flash text so `_write_result` can surface it (e.g. invite counts).
- **No created id is returned** — after add_student/add_parent, re-query `list_students` / the roster to get the new id.
- **Rails method-override:** edit uses a POST with `_method=patch`.
- **Match the real form's submission param-for-param, and omit any nested/collection key you did not intend to change.** Rails distinguishes a *missing* key from a *present-but-empty* one and treats present-but-empty as "clear it", so a stray empty key silently wipes data while a missing one can blow up the action. Both have caused live incidents here (see `build_add_student_body` / `build_edit_student_body`). This governs every new write tool, so establish the rule from the real form before inventing a body.
- **Endpoints:** add_student `POST /schools/{id}/students`; edit_student `POST /schools/{id}/students/{sid}` (`_method=patch`); add_parent `POST /schools/{id}/users`; edit_parent + link_guardian `POST /schools/{id}/users/{uid}/update_institute_user` (`_method=patch`).
- **Parent invitations** (verified live; see vault note "ParentSquare invitations API mapping"): `invite_parent` = `POST /schools/{id}/users/{user_id}/invite` (CSRF-only form body, via `post_form(path, {})`) — emails one guardian, resend uses the same endpoint. `bulk_invite_parents` = `POST /schools/{id}/users/invite` with a **JSON** body `{"ids": "<comma-joined user_ids>", "role": "PARENT", "selected": N}` (via `post_form`-style CSRF but JSON) — sends email/text, auto-skips already-registered ids, and the response is UJS `text/javascript` (not JSON), so it uses **`PSClient.post_json_raw()`** (POST JSON, return raw `Response`). "Invite all" = `bulk_invite_parents` over every `list_parents` guardian with `registered=false` (no dedicated endpoint). Target = guardian `user_id` from `list_parents`.
- **Roster feeds** (positional-array JSON, whole roster in one call, client-side paging): `GET /schools/{id}/roster/students_data` (14 cols, surfaced by `list_students`) and `.../parents_data` (12 cols, surfaced by `list_parents` — the only tool that exposes a guardian `user_id`) — parsed in `parsers/admin.py`.
- **Edit forms** (`.../{sid}/edit`, `.../{uid}/edit_institute_user?role=PARENT`) return JS-escaped HTML; `parsers/admin.py` extracts current field values (and the parent's shared `contact_id`) so edits preserve unchanged fields. `PSClient.get_text()` fetches these.
- **Guardian links (nested attrs):** omitting `kids_attributes` on a parent PATCH leaves existing links untouched (verified) — so `edit_parent` sends none and `link_guardian_to_student` sends only the new kid under a unique numeric key. `edit_parent` email/phone are **one** contact record addressed at indices `[0]`(email)/`[2]`(phone) with the same `contact_id`.
- **grade_id** is per-school (from `list_grades` / the roster add-modal `<select name="student[grade_id]">`). add_parent/link resolve the kid's grade_id from its edit form.

### Admin write tools (classes, staff & room parents)
v3 scope, reverse-engineered from the rollover admin UI's JS bundle and verified live (see Jason's vault note "ParentSquare classes & staff API mapping"). Parsing, body builders and response interpretation live in `parsers/classes.py`. Same write gate + audit log as above. Non-destructive: no class or staff deletion. (For the record, class deletion *is* `DELETE /api/v2/schools/{id}/sections` with the ids **nested under `data`** — `{"data":{"ids":"50235549"}}` — and `Accept: application/vnd.api+json`. A flat `{"ids":…}` body or an `?ids=` query string returns 500. Not exposed as a tool: deletes are deferred to the website per the v1 destructive-op policy.)

- **These are JSON:API endpoints returning real JSON**, so the UJS helpers (`write_succeeded`, `parse_flash_message`) do **not** apply. Use `PSClient.send_json(method, path, payload)` (CSRF + XHR headers, does not raise on 4xx/5xx) and interpret with `json_write_ok()` / `json_write_error()`, which mirror the UI's `checkAjaxError`. Exception: `add_staff` and `edit_staff` are classic Rails form posts and use `post_form` + `_write_result`.
- **Endpoints:**
  - `GET /api/v2/schools/{id}/sections_mini` — the whole class list in one call (teacher names, room-parent/assistant counts, `student_count`, `section_class_status`). Backs `list_classes`.
  - `GET /api/v2/sections/{sid}?include_staff=true` — class detail. Staff must be assembled by joining `included[type=section_staff_association]` to `included[type=user]` via `relationships.user.data.id`. ParentSquare's own JS reads `data.section_staff_associations`, which **does not exist in the raw response** (its JsonApiService flattens it).
  - `POST /api/v2/schools/{id}/sections` — create. Grades are a **bare array** (`relationships.grades: [{"id": …}]`), not the JSON:API `{"data": […]}` wrapper, and `relationships` is omitted entirely when there are no grades. Unlike the v1 creates, this **returns the new id** at `data.attributes.id`.
  - `PATCH /api/v2/sections/{sid}` — edit. Note the route drops the school segment; `school_id` rides in the body.
  - `PUT /api/v2/sections/{sid}/staff` — see full-replace below.
  - `PUT /api/v2/schools/{id}/sections/bulk_update_class_visibility` — `{ids: "a,b", school_id, visibility_start_date|visibility_end_date: "YYYY-MM-DD"}`.
  - `POST /schools/{id}/users` — add staff. Same Rails endpoint as `add_parent`, differentiated by `user[school_user_associations_attributes][0][role]` = `STAFF`|`ADMIN`; body includes `no_flash=true`. **`invited_school_section_ids[]` does not create a section staff association** (verified live — presumably it only applies once the invitee registers), so `add_staff` sends it *and* then does the assignment itself: look the new user up in `staff_data` by email, then run the normal class-staff read-modify-write for each `section_ids` entry as `class_role` (default `TEACHER`).
  - `GET /schools/{id}/roster/staff_data` — 13-col positional feed, backs `list_staff`.
  - `POST /schools/{id}/users/{uid}/update_institute_user` (`_method=patch`) — edit staff, backing `edit_staff`. **Same endpoint as `edit_parent`**, read via `GET .../{uid}/edit_institute_user?role=STAFF` (`extract_staff_edit_fields`). Beyond the guardian fields it carries the school_user_association (`[0][id]` = the roster's `sua_id`, plus `[0][school_title]` = the job title) and a `staff_contacts[manual][<contact_id>][external_id]` hash holding the school's staff ID. Email/phone share **one** contact record at indices `[0]`/`[2]`, exactly as for guardians. Echo the association id back or the title is lost.
- **`edit_staff` never sends `invited_school_section_ids[]`.** Like the student form's `section_ids`, the select renders every class but marks none `selected`, so echoing it back would blank the staff member's assignments — verified live that omitting it leaves them intact. Class changes go through `add_class_staff` / `remove_class_staff`.
- **The top-level `role` param on `update_institute_user` must match the user's real role.** Posting `role=STAFF` for a user whose association is `ADMIN` — without echoing `sua_id` — is rejected with *"Cannot update user: This would create conflicting roles."* `edit_staff` therefore reads the true role from `GET /api/v2/schools/{id}/users/{uid}` (`data.attributes.role`), sends `ADMIN` or `STAFF` accordingly, and **refuses guardians outright** (they must go through `edit_parent`). That lookup also turns a bad `user_id` into a clean 404 message instead of a 500 from the edit form.
- **Class staff is a full replace.** `PUT /api/v2/sections/{sid}/staff` deletes any association omitted from the payload; `id` is the existing association id, or `null` for a new one. So `add_class_staff` / `remove_class_staff` are read-modify-write helpers (GET detail → mutate the list → PUT it back), the same way `edit_student` hides the identical `section_ids` replace semantics. Both take a **`user_ids` list** so a whole class's room parents change in one PUT (one class per call); `add_class_staff` skips people who already hold the role and moves anyone holding a different one, while `remove_class_staff` intersects `user_ids` with `role` when both are given and refuses when given neither, so it can never wipe a class. The clear-all sentinel the UI sends is `{"data": [{"attributes": {"user": {}}}]}`, **not** `{"data": []}`. The UI caps a class at 50 staff rows.
- **All section-membership writes are globally serialized within one MCP server process.** A 2026-08-18 parallel batch caused staff associations on unrelated sections to disappear, and concurrent `move_student_to_class` calls can both return success while one student's newly added class silently vanishes. `AppContext.section_membership_write_lock` therefore covers the complete read/compute/write operation for `add_class_staff`, `remove_class_staff`, `add_class_students`, `remove_class_students`, and `move_student_to_class`. For multi-student removal it covers the school-map read and the entire PUT loop, so no sibling tool can make the map stale mid-operation. Separate server processes are not coordinated; agents should still issue these tools serially and fresh-read afterward.
- **Roles** are `TEACHER`, `ASSISTANT`, `ROOM_PARENT` (`normalize_role()` also accepts "room parent"/"Room-Parent"), each with a default `class_title`.
- **New classes are created hidden** (invisible to staff, parents and students) — matching the website's two-step flow — so `add_class` must be followed by `set_class_visibility`. Visibility is date-driven, not a boolean: making a class visible submits a `visibility_start_date` and hiding it submits a `visibility_end_date`; `set_class_visibility` defaults both to today. The bundle's `clear_field` parameter means *unschedule*, not "apply now" — sending it to an already-hidden class returns 200 and silently does nothing (a false-positive success).
- **"Clear all room parents for the new school year"** is deliberately not a tool: the agent loops `list_classes` → `remove_class_staff(section_id, role="ROOM_PARENT")` per class.

### Admin write tools (student class enrollment)
v4 scope, reverse-engineered from `/schools/{id}/roster/assign_classes` and its `RostersAssignClassController`, and verified live on test fixtures. Parsing + body builders live in `parsers/enrollment.py`. JSON:API endpoints, so use `send_json` + `json_write_ok()`/`json_write_error()` (same as classes/staff), not the UJS helpers. Same write gate + audit log.

- **Endpoints:**
  - `GET /api/v2/sections/{id}/students` — class roster. Backs `list_class_students`.
  - `PUT /api/v2/sections/{id}/add_students` — **additive**, cannot wipe. Body `{"section_id": "<id>", "data": [{"type": "student", "id": "<sid>"}]}`. Returns the resulting roster. Backs `add_class_students`.
  - `PUT /api/v2/students/{id}/sections` — **full replace** of one student's class list. Body `{"student_id": "<id>", "data": [{"id": "<section_id>", "type": "section"}]}`; `data: []` clears every class. Backs removals and moves.
  - `DELETE /api/v2/sections/{id}/students` — removes **all** students from a class. **Deliberately not exposed** (v1 destructive-op policy).
  - `GET /api/v2/students/{id}/sections` **404s** — the route is PUT-only.
- **There is no cheap per-student read of current classes.** The GraphQL `StudentProfileSectionView` exposes no `id`/`sectionId`, and the student edit form is useless (it never marks a class `selected` — see `extract_student_edit_fields` in `parsers/admin.py`). Instead `/schools/{id}/roster/assign_classes` server-renders **every** student's classes as `<span class="student-sections-{student_id}" data-section-id data-section-name>` — **one HTTP call yields the whole-school map** (`parse_student_sections_map`). Students with no classes render no spans, so a **missing key means "no classes", not "unknown student"**.
- **Removals and moves are read-modify-write** over that map, because the only removal primitive is the full-replace PUT: read the student's classes, drop/swap one, PUT the rest back. This is why `remove_class_students` and `move_student_to_class` take a `school_id`.
- **`remove_class_students` refuses an empty `student_ids` list** — an empty list must never degrade into "empty the class". `add_class_students` skips ids already enrolled, so re-running is safe.
- **No multi-class bulk endpoint exists** (`bulk_update_sections` is term dates; `bulk_update_class_visibility` is visibility). Start-of-year assignment is therefore an agent loop of `add_class_students` — one call per classroom, not per student. A loop-free mega-tool was considered and rejected: it would save zero HTTP calls while removing per-classroom checkpoints.
- **CSV import is not a viable alternative**: `sections/sample_roster_csv` matches rows on **Student External ID** with no name/email fallback, and 290 of 644 students have no SIS id.


## Development

```bash
uv run parentsquare-mcp              # Run the MCP server
uv run parentsquare-export-cookies   # Bootstrap cookies from browser
uv run --group dev pytest            # Run the unit tests
```

### CI and branch protection

`ci.yml` runs the test suite (3.12 + 3.13) and the packaging checks on every pull
request and on pushes to `main`. `publish.yml` runs only on a release tag, and
runs the same matrix against that tag before anything is published — a tag push
does not trigger `ci.yml`, so without it a release would ship untested.

**All changes go through a pull request**, but `main` requires **zero approving
reviews**. That combination is deliberate rather than half-finished:

- GitHub never lets an author approve their own PR, and the repo has two people
  with write access. Requiring an approval would mean every change waits on the
  other person, and a rule that is routinely bypassed provides no safety while
  still costing friction. Requiring **status checks** instead applies to
  everyone, needs no second human, and is what actually catches regressions.
- The PR itself is still required because it is a *gate*: on a direct push CI
  reports the breakage after `main` is already broken. It also gives each change
  a revertable unit.

`main` is guarded by a **repository ruleset** named `main`
([Settings → Rules](https://github.com/thehesiod/psquare-mcp/rules), id `21636419`).
`gh api repos/thehesiod/psquare-mcp/rulesets/21636419` returns every rule and is the
authoritative source (the list endpoint omits them); the table below covers only the
rules that change how you work, and the paragraphs after it the choices the API
cannot explain.

| Rule | What it means for a contributor |
|---|---|
| `required_signatures` | every commit must carry a verified signature — see below |
| `required_linear_history` | squash or rebase; no merge commits |
| `pull_request` | every review thread must be resolved before merging |
| `copilot_code_review` | a bot review lands on every push, drafts excepted |
| `required_status_checks` | the `ci.yml` jobs, non-strict — a merge elsewhere will not force you to rebase |

Deliberate choices worth preserving: **zero approvals** for the reason given above,
**non-strict** status checks so one merge does not require rebasing every open PR,
and **no bypass actors** (`current_user_can_bypass: never`). The last is the one that
makes the rest mean anything — grant a bypass and the rules turn advisory for whoever
holds it, since GitHub offers them a "merge without waiting for requirements"
checkbox, leaving the rules binding only on people who could not bypass them anyway.

`required_signatures` is the rule most likely to block a newcomer, because nothing in
a clone hints at it and an unsigned commit is only rejected at merge time, after the
work is done. Signatures cannot be added without rewriting the commits, so configure
signing *before* committing:

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

Then add that same public key to GitHub **a second time**, as a *Signing Key* — GitHub
tracks authentication and signing keys separately, so an existing auth key does not
count. To re-sign commits already made, `git rebase --exec 'git commit --amend --no-edit -S' origin/main`.

These are recorded here rather than applied from CI because automating them would
require a PAT with `administration: write` stored as a repo secret. The release
pipeline is deliberately token-free (OIDC only), and a long-lived admin token is
a poor trade for a setting that changes almost never. That same `gh api` call prints
the full definition, so it doubles as a restorable snapshot — take one before you
need it, since a deleted ruleset cannot be read back.

**A ruleset leaves no trace in the tree**, which is the whole reason this section
exists: it is an account setting, there is no ruleset-as-code here, and so a `git pull`
can never show that the rules changed. This section has already gone stale once, by
roughly half a day. Whoever changes the rules owns updating it in the same PR.

**Because a PR description does not live in the working tree**, reasoning that
constrains future code belongs in a commit message, a docstring beside the code,
and a test that pins it. Reserve the PR body for what cannot live in the tree:
the limits of verification (e.g. "tested against fixtures, not a live district
account"), provenance of a reverse-engineered endpoint, and reviewer asks.

### Adding a New Parser
1. Create `parsers/<name>.py` with a `parse_*` function that takes `BeautifulSoup` and returns dataclass(es)
2. Add dataclass(es) to `models.py`
3. Add the tool in `server.py` using the `@mcp.tool` decorator
4. Wire through `_with_mfa_retry` for auth handling
5. Add the URL template to `config.py` if needed

### Account Discovery
Schools, students, and user ID are auto-discovered at runtime from ParentSquare pages (`gon.*` script variables, sidebar student links, and the school switcher AJAX endpoint). School names are fetched via `/api/v2/schools/{id}`. No config file needed.

**`gon.institute_id` is not always a school id.** For district-level accounts `gon.institute_type` is `"District"` and `gon.institute_id` is a *district* id, which 404s on every `/schools/{id}` route (`/schools/{id}/feeds`, `/api/v2/schools/{id}`) — the failure is silent, degrading into a placeholder `School <district_id>` entry and 404s from every school-scoped tool. `discover_account()` therefore checks `institute_type` first and, for districts, expands the id into member schools via `GET /layout_templates/district_switch_schools_list?institute_id={id}&institute_type=District`, whose HTML lists `/schools/{school_id}/feeds` links. The first member school becomes the "current" school for the student-discovery pass; the district id is never stored as a school.

## Release Process

Publishing is automated via `.github/workflows/publish.yml`. Uses **PyPI Trusted Publishers** (OIDC) and GitHub OIDC for the MCP Registry — no API tokens stored anywhere.

### One-time setup (pypi.org)

Before the first CI-driven release, add a "pending" trusted publisher on pypi.org:

1. Log into [pypi.org](https://pypi.org) as the account that owns the project.
2. Go to [Manage → Publishing → Add a new pending publisher](https://pypi.org/manage/account/publishing/).
3. Fill in:
   - **PyPI Project Name**: `parentsquare-mcp`
   - **Owner**: `thehesiod`
   - **Repository name**: `psquare-mcp`
   - **Workflow name**: `publish.yml`
   - **Environment name**: *(leave blank, or set e.g. `release` for a manual-approval gate)*
4. Save. The publisher activates on the first successful tag push that runs `publish.yml`.

*(The MCP Registry namespace `io.github.thehesiod/*` is already auto-authorized for the `thehesiod` GitHub account via OIDC — no pypi-style pending-publisher setup needed.)*

### Cutting a release

1. Bump the version in **both** `pyproject.toml` and `server.json` (the workflow fails if they don't match — both `version` and `packages[0].version` in `server.json`).
   - **`server.json`'s `description` must be <= 100 characters** — the MCP Registry rejects longer ones with a 422. The workflow validates this before building. (`pyproject.toml`'s `description`, used for the PyPI summary, has no such limit.)
   - PyPI metadata (summary, keywords, README) is **immutable per release** — updating the description requires publishing a new version.
2. Commit: `chore: bump to X.Y.Z`.
3. Tag + push (tags are bare semver — no `v` prefix):
   ```bash
   git tag X.Y.Z
   git push origin main --tags
   ```
4. The workflow:
   - Verifies versions match the tag across `pyproject.toml` + `server.json`
   - Runs the test matrix against the tag
   - Builds wheel + sdist with `uv build`
   - Publishes to PyPI via OIDC, under the `pypi` deployment environment
   - Publishes to the MCP Registry via `mcp-publisher login github-oidc`
   - Creates a GitHub Release with auto-generated notes and the built artifacts

**Re-running a release is safe, and is the way to repair a half-finished one.**
Every publishing step is idempotent: PyPI uses `skip-existing`, the registry step
treats `cannot publish duplicate version` as a no-op, and the GitHub Release step
skips a release that already exists (releases here are immutable, so its assets
cannot be replaced). Re-run by dispatching `publish.yml` with the tag as its
`tag` input — the dispatch publishes *that* tag, not the ref it was launched
from, so one version can never be published by two concurrent runs.

### Publishing identity: must match the repo owner

The canonical repository is **`thehesiod/psquare-mcp`**, and the publishing identity is pinned to
that owner on purpose:

| | value |
|---|---|
| PyPI project | `parentsquare-mcp` |
| MCP Registry name | `io.github.thehesiod/psquare` |
| `publish.yml` repo guard | `github.repository == 'thehesiod/psquare-mcp'` |

**The registry namespace is not a free choice.** `mcp-publisher login github-oidc` mints a token
whose subject is the *running repository*, and the registry only lets it publish
`io.github.<that repo's owner>/*`. A `server.json` naming `io.github.<someone-else>/…` cannot be
published from this repo at all. So the namespace, `pyproject.toml`'s `name`, `server.json`'s
`packages[0].identifier`, the README badge/`mcp-name` line, and the `publish.yml` guard all have to
move together as one unit. If the repo ever moves owners again, change all five.

The guard matters as much as the namespace: it exists so forks don't publish, and a stale owner in
it makes the release job **skip silently** — a green run that shipped nothing.

Historical note: development happened for a while in the `jasonko/psquare-mcp` fork, which
published `psquare-mcp` / `io.github.jasonko/psquare` up to 0.3.0. That fork was merged back here
at 0.3.0 and those two names are now **frozen and abandoned** — 0.4.0 is the first release under
the reunified `thehesiod` identity, versioned above both old lines so neither registry rejects it.

```bash
gh run list --workflow=publish.yml -R thehesiod/psquare-mcp
gh run view <run-id> -R thehesiod/psquare-mcp --log-failed
gh api repos/thehesiod/psquare-mcp/actions/runs --jq '.workflow_runs[0] | "\(.head_branch) \(.status) \(.conclusion)"'
```

Verify the outcome at the source rather than trusting the job list — PyPI
(`curl -s -o /dev/null -w '%{http_code}' https://pypi.org/pypi/parentsquare-mcp/X.Y.Z/json`) and the
registry (`https://registry.modelcontextprotocol.io/v0/servers?search=io.github.thehesiod/psquare`).

### If a release fails before PyPI accepts the upload

The version number is only burned once PyPI has the artifact (a PyPI 404 for `X.Y.Z` means it is
still free). If the upload never landed, prefer deleting the tag and its GitHub Release and
re-tagging the fix commit over bumping to a throwaway patch version:

```bash
gh release delete X.Y.Z -R thehesiod/psquare-mcp --yes --cleanup-tag
git tag -d X.Y.Z && git push origin :refs/tags/X.Y.Z
git tag X.Y.Z && git push origin main --tags
```

### Keep the publish action's pin current with the build metadata version

`publish.yml` pins `pypa/gh-action-pypi-publish` to an exact version, and that pin has to keep
pace with the **core metadata version hatchling emits** — the action's bundled `twine` rejects any
metadata version it predates, *before* making a network request. This broke the 0.3.0 release:
hatchling 1.32.0 (2026-08-11) bumped the default to `Metadata-Version: 2.5` per PEP 794, and the
pinned action v1.14.0 ships twine 6.1.0, which failed with
`InvalidDistribution: '2.5' is not a valid metadata version`. Fixed by moving to v1.14.2
(twine 7.0.0). **PyPI itself already accepted 2.5** — the failure is purely client-side, so the
fix is to bump the action (or, as a stopgap, `with: verify-metadata: false`), never to pin
hatchling backwards.

### Ownership proof for the MCP Registry

The PyPI package README must contain the literal line `mcp-name: io.github.thehesiod/psquare` (see the bottom of `README.md`). The registry's publisher validates this by fetching the published PyPI artifact and looking for that string. Removing the line will break future registry publishes.

## Open Improvement Areas

- **Feed search**: No keyword search/filter on `get_feeds` — Claude must paginate and scan titles/summaries manually. A search tool or keyword parameter would help.
- **CloudFront URL expiry**: S3/CloudFront signed URLs expire. Cached attachment URLs from older sessions may 403.
