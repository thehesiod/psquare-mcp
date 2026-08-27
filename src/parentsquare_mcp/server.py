from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import requests
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from parentsquare_mcp.auth import MFARequiredError, MFAState, load_cookies, submit_mfa
from parentsquare_mcp.audit import WRITES_DISABLED_MESSAGE, audit_write, writes_enabled
from parentsquare_mcp.client import PSClient, make_session
from parentsquare_mcp.config import DEFAULT_DOWNLOAD_DIR, URLS
from parentsquare_mcp.download import download_file as do_download
from parentsquare_mcp.parsers.admin import (
    STUDENT_PROFILE_QUERY,
    build_add_parent_body,
    build_add_student_body,
    build_bulk_invite_body,
    build_edit_parent_body,
    build_edit_student_body,
    build_link_guardian_body,
    extract_parent_edit_fields,
    extract_student_edit_fields,
    guardian_linked,
    guardian_present,
    parse_flash_message,
    parse_grades,
    parse_roster_parents,
    parse_roster_students,
    parse_student_profile,
    roster_has_student,
    write_succeeded,
)
from parentsquare_mcp.parsers.calendar import parse_ics_calendar
from parentsquare_mcp.parsers.classes import (
    build_add_class_body,
    build_add_staff_body,
    build_edit_class_body,
    build_edit_staff_body,
    build_staff_replace_body,
    build_visibility_body,
    default_class_title,
    extract_staff_edit_fields,
    json_write_error,
    json_write_ok,
    normalize_role,
    parse_roster_staff,
    parse_section_detail,
    parse_sections_mini,
)

from parentsquare_mcp.parsers.enrollment import (
    build_add_students_body,
    build_student_sections_body,
    parse_class_students,
    parse_student_sections_map,
)

from parentsquare_mcp.parsers.feeds import parse_feed_page, parse_post_detail
from parentsquare_mcp.models import ClassStaff, Group
from parentsquare_mcp.parsers.groups import parse_group_feed
from parentsquare_mcp.parsers.links import parse_links_page
from parentsquare_mcp.parsers.notices import parse_notices
from parentsquare_mcp.parsers.payments import parse_payments_page
from parentsquare_mcp.parsers.polls import parse_polls_page
from parentsquare_mcp.parsers.media import parse_files_page, parse_photos_page
from parentsquare_mcp.parsers.volunteer import parse_volunteer_hours
from parentsquare_mcp.parsers.messages import parse_chat_thread, parse_conversation_list
from parentsquare_mcp.parsers.schools import parse_sidebar_features
from parentsquare_mcp.parsers.students import parse_student_dashboard
from parentsquare_mcp.urls import redact_url

# Configure logging to stderr only (stdout is reserved for MCP JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    client: PSClient
    download_dir: Path
    mfa_state: MFAState | None = None
    section_membership_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Set up session and yield client. Auth is fully lazy — cookies are loaded
    if available, but 1Password and login only happen on the first actual request
    that needs authentication (via PSClient._relogin).
    """
    session = make_session()

    # Pre-load saved cookies if available (no network call, no 1Password)
    load_cookies(session)

    client = PSClient(session=session)
    download_dir = Path(os.environ.get("PS_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).expanduser()

    # Restore pending MFA state from disk (survives server restarts)
    mfa_state = MFAState.load()
    if mfa_state:
        logger.info("Restored pending MFA state from disk")

    yield AppContext(client=client, download_dir=download_dir, mfa_state=mfa_state)


MCP_INSTRUCTIONS = """
Section-membership safety: never run class-staff or student-enrollment writes in
parallel, even for different sections. ParentSquare uses full-replace endpoints,
and concurrent writes have silently lost staff associations and student classes.
This server serializes these writes within one process, but callers must still
issue them one at a time and verify with fresh reads.
"""


mcp = MCPServer("ParentSquare", lifespan=app_lifespan, instructions=MCP_INSTRUCTIONS)


def _app(ctx: Context[Any, Any]) -> AppContext:
    """Extract AppContext from tool context."""
    return ctx.request_context.lifespan_context


def _ensure_account(app: AppContext) -> None:
    """Lazily discover account info (schools, students, user_id) on first use."""
    if not app.client.account.user_id:
        try:
            app.client.discover_account()
        except MFARequiredError as exc:
            app.mfa_state = exc.mfa_state
            app.client.mfa_state = exc.mfa_state
            raise


def _school_name(app: AppContext, school_id: int) -> str:
    _ensure_account(app)
    return app.client.account.schools.get(school_id, f"School {school_id}")


# ---------------------------------------------------------------------------
# Authentication tools
# ---------------------------------------------------------------------------


@mcp.tool(name="submit_mfa_code")
def submit_mfa_code(code: str, context: Context[Any, Any] = None) -> str:
    """Submit a 6-digit MFA verification code to complete ParentSquare login.

    When a ParentSquare tool returns an MFA error, a verification code is sent
    to your email. Check your email for the code and use this tool to complete
    authentication.

    Args:
        code: The 6-digit verification code from your email
    """
    app = _app(context)
    if app.mfa_state is None:
        return "No pending MFA verification. Try calling a ParentSquare tool first to trigger login."

    try:
        submit_mfa(app.client.session, app.mfa_state, code)
        app.mfa_state = None
        app.client.mfa_state = None
        app.client.invalidate_csrf_token()
        return "✅ MFA verification successful! You can now use all ParentSquare tools."
    except Exception as e:
        return f"MFA verification failed: {e}"


class MFACodeInput(BaseModel):
    """Schema for MFA code elicitation."""
    code: str = Field(description="The 6-digit verification code from your email")


async def _handle_mfa(app: AppContext, exc: MFARequiredError, ctx: Context[Any, Any]) -> str:
    """Try inline elicitation for MFA code; fall back to text message if unsupported."""
    app.mfa_state = exc.mfa_state
    app.client.mfa_state = exc.mfa_state

    # Skip elicitation when running unattended (e.g. claude.ai routines without
    # a live user to answer). Caller should fetch the code from email and call
    # submit_mfa_code explicitly.
    if os.environ.get("PS_NO_ELICIT"):
        return str(exc)

    # Try elicitation — prompt user for MFA code inline
    try:
        result = await ctx.elicit(
            message=str(exc),
            schema=MFACodeInput,
        )
        if result.action == "accept":
            code = result.data.code.strip()
            submit_mfa(app.client.session, exc.mfa_state, code)
            app.mfa_state = None
            app.client.mfa_state = None
            app.client.invalidate_csrf_token()
            return ""  # empty string signals success — caller should retry
        elif result.action == "decline":
            return "MFA verification declined. Use submit_mfa_code tool later to complete login."
        else:  # cancel
            return "MFA verification cancelled. Use submit_mfa_code tool later to complete login."
    except MCPError:
        # Client doesn't support elicitation — fall back to text message
        return str(exc)


async def _with_mfa_retry(
    app: AppContext,
    ctx: Context[Any, Any],
    fn: Callable[[], Any],
) -> tuple[Any, str | None]:
    """Call fn(), handling MFA with inline elicitation + retry.

    Returns (result, None) on success, or (None, error_message) if MFA
    couldn't be completed inline.
    """
    try:
        return fn(), None
    except MFARequiredError as exc:
        msg = await _handle_mfa(app, exc, ctx)
        if msg:  # elicitation failed or was declined — return message to user
            return None, msg
        # Elicitation succeeded — retry the original call
        return fn(), None


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool(name="list_schools")
async def list_schools(context: Context[Any, Any]) -> dict | str:
    """List available schools and students in your ParentSquare account.

    Returns JSON with schools, students, and your user ID.
    Use the school_id and student_id values from this output in other tools.
    For school contact info (phone, address), use get_directory(school_id).
    """
    app = _app(context)
    _, err = await _with_mfa_retry(app, context, lambda: _ensure_account(app))
    if err:
        return err
    acct = app.client.account
    schools = [{"school_id": sid, "name": name} for sid, name in acct.schools.items()]
    students = [
        {
            "student_id": sid,
            "name": info["name"],
            "school": acct.schools.get(info["school_id"], "Unknown"),
            "school_id": info["school_id"],
            "grade": info.get("grade", ""),
        }
        for sid, info in acct.students.items()
    ]
    return {"schools": schools, "students": students, "user_id": acct.user_id}


@mcp.tool(name="list_school_features")
async def list_school_features(school_id: int, context: Context[Any, Any]) -> str:
    """List features available for a specific school by parsing its sidebar navigation.

    Returns a list of available sections (Feed, Messages, Calendar, Photos, etc.).
    Different schools may have different features enabled.

    Args:
        school_id: School ID (use list_schools to find available IDs)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/schools/{school_id}/feeds"))
    if err:
        return err
    features = parse_sidebar_features(soup)
    name = _school_name(app, school_id)
    lines = [f"# Features for {name}", ""]
    for f in features:
        lines.append(f"- {f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feed/Post tools
# ---------------------------------------------------------------------------


@mcp.tool(name="get_feeds")
async def get_feeds(school_id: int, page: int = 1, context: Context[Any, Any] = None) -> str:
    """Get recent posts from a school's feed with titles, authors, dates, and summaries.

    Returns a paginated list of posts. Use get_post with the feed_id to see full details.

    Args:
        school_id: School ID
        page: Page number for pagination (default: 1)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/schools/{school_id}/feeds", params={"page": str(page)}))
    if err:
        return err
    posts = parse_feed_page(soup)
    if not posts:
        return "No posts found."
    lines = [f"# Feed for {_school_name(app, school_id)} (page {page})", ""]
    for p in posts:
        lines.append(f"**{p.title}** (feed_id: {p.id})")
        lines.append(f"  By {p.author} on {p.date}")
        if p.summary:
            lines.append(f"  {p.summary}")
        extras = []
        if p.signup_progress:
            extras.append(f"📋 {p.signup_progress}")
        if p.has_attachments:
            if p.attachment_names:
                extras.append("📎 " + ", ".join(p.attachment_names[:3]))
            else:
                extras.append("📎 attachments")
        if p.comment_count:
            extras.append(f"💬 {p.comment_count} comments")
        if extras:
            lines.append(f"  [{', '.join(extras)}]")
        lines.append("")
    return "\n".join(lines)


_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # skip images larger than 5 MB
_MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024  # stop fetching after 10 MB total
_MAX_PDF_BYTES = 10 * 1024 * 1024  # skip PDFs larger than 10 MB


def _fetch_image(client: PSClient, url: str) -> tuple[Image | None, int]:
    """Download an image and return (MCP Image, byte_count).

    Returns (None, 0) if too large or failed.
    """
    try:
        resp = client.get_raw(url)
        size = len(resp.content)
        if size > _MAX_IMAGE_BYTES:
            return None, 0
        content_type = resp.headers.get("content-type", "image/png")
        fmt = content_type.split("/")[-1].split(";")[0].strip()
        return Image(data=resp.content, format=fmt), size
    except Exception:
        logger.debug(f"Failed to fetch image: {redact_url(url)}", exc_info=True)
        return None, 0


def _fetch_pdf_text(client: PSClient, url: str) -> str | None:
    """Download a PDF and extract its text content.

    Returns extracted text, or None if failed/too large.
    """
    try:
        import fitz  # pymupdf

        resp = client.get_raw(url)
        if len(resp.content) > _MAX_PDF_BYTES:
            return None
        doc = fitz.open(stream=resp.content, filetype="pdf")
        pages: list[str] = []
        for page in doc:
            text = page.get_text().strip()
            if text:
                pages.append(text)
        doc.close()
        return "\n\n---\n\n".join(pages) if pages else None
    except Exception:
        logger.debug(f"Failed to extract PDF text: {redact_url(url)}", exc_info=True)
        return None


@mcp.tool(name="get_post")
async def get_post(feed_id: int, context: Context[Any, Any] = None) -> list:
    """Get full details of a specific post including body text, comments, and attachments.

    Image attachments are returned inline so you can see their contents directly.
    PDF attachments have their text extracted and included inline.

    Args:
        feed_id: Post/feed ID (shown as feed_id in get_feeds results)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/feeds/{feed_id}"))
    if err:
        return [err]
    post = parse_post_detail(soup)
    lines = [
        f"# {post.title}",
        f"By {post.author} on {post.date}",
        "",
        post.body_text,
    ]

    # Collect image content blocks to return alongside text
    image_blocks: list[tuple[str, Image]] = []
    pdf_blocks: list[tuple[str, str]] = []
    total_image_bytes = 0
    if post.attachments:
        lines.extend(["", "## Attachments"])
        for a in post.attachments:
            is_pdf = a.name.lower().endswith(".pdf") or "pdf" in a.file_type.lower()
            icon = "📷" if a.file_type == "image" else ("📄" if is_pdf else "📎")
            lines.append(f"- {icon} **{a.name}**")

            if a.file_type == "image" and total_image_bytes < _MAX_TOTAL_IMAGE_BYTES:
                img, size = _fetch_image(app.client, a.url)
                if img:
                    image_blocks.append((a.name, img))
                    total_image_bytes += size
                else:
                    lines.append(f"  URL: {a.url}")
            elif is_pdf:
                pdf_text = _fetch_pdf_text(app.client, a.url)
                if pdf_text:
                    pdf_blocks.append((a.name, pdf_text))
                else:
                    lines.append(f"  URL: {a.url}")
            elif a.file_type != "image":
                lines.append(f"  URL: {a.url}")
            else:
                lines.append(f"  URL: {a.url}  _(image too large for inline display)_")

    # Poll results — detect poll-option elements on the detail page
    poll_options = soup.find_all("div", class_="poll-option")
    if poll_options:
        total_votes = 0
        poll_lines: list[str] = []
        for opt_div in poll_options:
            radio = opt_div.find("input", attrs={"aria-label": True})
            text = radio["aria-label"] if radio else ""
            if not text:
                prog_text = opt_div.find("div", class_="vote-progress-text")
                text = prog_text.get_text(strip=True) if prog_text else ""
            num_el = opt_div.find("span", class_="num-votes")
            votes = 0
            if num_el:
                m = re.search(r"(\d+)", num_el.get_text(strip=True))
                if m:
                    votes = int(m.group(1))
            bar = opt_div.find("span", class_="vote-progress-bar")
            winner = bar is not None and "most" in " ".join(bar.get("class", []))
            total_votes += votes
            star = " ★" if winner else ""
            poll_lines.append(f"- {text}: {votes} votes{star}")
        lines.extend(["", f"## Poll Results ({total_votes} total votes)"])
        lines.extend(poll_lines)

    if post.signup_items:
        total_filled = sum(s.filled for s in post.signup_items)
        total_total = sum(s.total for s in post.signup_items)
        total_open = total_total - total_filled
        lines.extend(["", f"## Sign-Up Items ({total_filled}/{total_total} filled, {total_open} open)"])
        for s in post.signup_items:
            open_count = s.total - s.filled
            progress = f"{s.filled}/{s.total}" if s.total else f"{s.filled} filled"
            time_str = f" [{s.time_slot}]" if s.time_slot else ""
            names = ", ".join(s.signed_up[:5])
            if len(s.signed_up) > 5:
                names += f", +{len(s.signed_up) - 5} more"
            status = f" — {names}" if names else ""
            open_note = f" ({open_count} open)" if open_count > 0 else " ✅ full"
            lines.append(f"- **{s.name}**{time_str}: {progress}{open_note}{status}")

    if post.comments:
        lines.extend(["", f"## Comments ({len(post.comments)})"])
        for c in post.comments:
            lines.append(f"- **{c.author}** ({c.date}): {c.text}")

    # Return text + inline images + PDF text so Claude can see everything
    result: list = ["\n".join(lines)]
    for caption, img in image_blocks:
        result.append(f"\n📷 {caption}:")
        result.append(img)
    for caption, text in pdf_blocks:
        result.append(f"\n📄 **{caption}** (extracted text):\n\n{text}")
    return result


# ---------------------------------------------------------------------------
# Message tools
# ---------------------------------------------------------------------------


@mcp.tool(name="list_conversations")
async def list_conversations(school_id: int, context: Context[Any, Any] = None) -> str:
    """List message conversations for a school. Returns conversation IDs, participants, and previews.

    To read a full conversation, call get_conversation with BOTH the same school_id
    used here AND the chat_id shown in the results.

    Args:
        school_id: School ID (use list_schools to find available IDs)
    """
    app = _app(context)
    def _fetch_chats():
        _ensure_account(app)
        path = URLS["chats"].format(school_id=school_id, user_id=app.client.account.user_id)
        return app.client.get_page(path)
    soup, err = await _with_mfa_retry(app, context, _fetch_chats)
    if err:
        return err
    convos = parse_conversation_list(soup)
    if not convos:
        return "No conversations found."
    lines = [f"# Conversations ({len(convos)})", ""]
    lines.append(f"_To read a conversation, call get_conversation(school_id={school_id}, chat_id=<id>)_")
    lines.append("")
    for c in convos:
        unread = " 🔴 UNREAD" if c.unread else ""
        lines.append(f"**[chat_id={c.id}] {', '.join(c.participants)}**{unread}")
        lines.append(f"  {c.last_message_preview}")
        lines.append(f"  {c.date}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="get_conversation")
async def get_conversation(school_id: int, chat_id: int, context: Context[Any, Any] = None) -> str:
    """Read a specific message conversation thread with all messages.

    IMPORTANT: Both school_id and chat_id are required.

    Args:
        school_id: School ID (same school_id used in list_conversations)
        chat_id: Conversation/chat ID (from list_conversations results)
    """
    app = _app(context)
    def _fetch_chat():
        _ensure_account(app)
        path = URLS["chat"].format(school_id=school_id, user_id=app.client.account.user_id, chat_id=chat_id)
        return app.client.get_page(path, params={"lang": "en"})
    soup, err = await _with_mfa_retry(app, context, _fetch_chat)
    if err:
        return err
    messages = parse_chat_thread(soup)
    if not messages:
        return "No messages found in this conversation."
    lines = [f"# Conversation {chat_id}", ""]
    for m in messages:
        lines.append(f"**{m.author}** ({m.date}):")
        lines.append(f"  {m.text}")
        for a in m.attachments:
            lines.append(f"  📎 [{a.name}]({a.url})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calendar tool
# ---------------------------------------------------------------------------


@mcp.tool(name="get_calendar_events")
async def get_calendar_events(school_id: int, context: Context[Any, Any] = None) -> dict:
    """Get calendar events for a school using the ICS calendar export.

    Returns JSON array of events with title, start/end times, location, and description.
    Note: Some schools post monthly calendars as images or PDFs in the feed instead
    of using the ICS calendar. If this returns no events, use get_feeds to browse
    recent posts — look for posts with "calendar" in the title or image/PDF attachments.
    Use get_post to view them (images are returned inline so you can read them).

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = URLS["calendar_ics"].format(school_id=school_id)
    ics_text, err = await _with_mfa_retry(app, context, lambda: app.client.get_ics(path))
    if err:
        return err
    events = parse_ics_calendar(ics_text)
    if not events:
        return {
            "school": _school_name(app, school_id),
            "event_count": 0,
            "events": [],
            "hint": "This school may post calendars as images/PDFs in the feed. Try get_feeds and look for calendar-related posts.",
        }
    records = []
    for e in events:
        record: dict[str, Any] = {"title": e.title, "start": e.start, "all_day": e.all_day}
        if e.end:
            record["end"] = e.end
        if e.location:
            record["location"] = e.location
        if e.description:
            record["description"] = e.description[:300]
        records.append(record)
    return {"school": _school_name(app, school_id), "event_count": len(records), "events": records}


# ---------------------------------------------------------------------------
# Media tools
# ---------------------------------------------------------------------------


@mcp.tool(name="list_photos")
async def list_photos(school_id: int, page: int = 1, context: Context[Any, Any] = None) -> str:
    """List photos posted for a school. Returns photo URLs that can be used with download_file.

    Args:
        school_id: School ID
        page: Page number (default: 1)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/schools/{school_id}/feeds/photos", params={"page": str(page)}))
    if err:
        return err
    photos = parse_photos_page(soup)
    if not photos:
        return "No photos found."
    lines = [f"# Photos for {_school_name(app, school_id)} (page {page})", ""]
    for p in photos:
        lines.append(f"**[{p.id}] {p.title}** ({p.date})")
        lines.append(f"  URL: {p.url}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_files")
async def list_files(school_id: int, context: Context[Any, Any] = None) -> str:
    """List files and documents posted for a school. Returns file URLs for download_file.

    Args:
        school_id: School ID
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/schools/{school_id}/feeds/files"))
    if err:
        return err
    files = parse_files_page(soup)
    if not files:
        return "No files found."
    lines = [f"# Files for {_school_name(app, school_id)}", ""]
    for f in files:
        lines.append(f"**[{f.id}] {f.title}** ({f.date})")
        lines.append(f"  Type: {f.file_type}")
        lines.append(f"  URL: {f.url}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="download_file")
async def download_file(url: str, filename: str | None = None, context: Context[Any, Any] = None) -> str:
    """Download a photo, video, or file to local disk from a ParentSquare URL.

    Returns the local file path where the file was saved.
    Use URLs from list_photos, list_files, or get_post attachment results.

    Args:
        url: The file URL (from list_photos, list_files, or get_post results)
        filename: Optional custom filename. If not provided, uses the original filename from the URL.
    """
    app = _app(context)
    saved_path, err = await _with_mfa_retry(app, context, lambda: do_download(app.client, url, app.download_dir, filename))
    if err:
        return err
    return f"Downloaded to: {saved_path}"


# ---------------------------------------------------------------------------
# Directory tool
# ---------------------------------------------------------------------------


def _format_phone(phone: str) -> str:
    """Format a phone number like 14082394646 -> (408) 239-4646."""
    digits = phone.lstrip("+")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


@mcp.tool(name="get_directory")
async def get_directory(school_id: int, context: Context[Any, Any] = None) -> dict:
    """List school contact info and staff directory with names, roles, and phone numbers.

    Returns JSON with school details (phone, address) and staff records.
    Use get_staff_member(school_id, user_id) for full staff details including email, photo, and office hours.

    Args:
        school_id: School ID
    """
    app = _app(context)

    # Fetch school info and directory in sequence (both needed)
    school_data, err = await _with_mfa_retry(app, context, lambda: app.client.get_json(f"/api/v2/schools/{school_id}"))
    if err:
        return err
    dir_data, err = await _with_mfa_retry(app, context, lambda: app.client.get_json(f"/api/v2/schools/{school_id}/directory"))
    if err:
        return err

    # School contact info
    school_attrs = school_data.get("data", {}).get("attributes", {})
    school_info: dict[str, Any] = {"name": school_attrs.get("name", _school_name(app, school_id))}
    if school_attrs.get("phone"):
        school_info["phone"] = _format_phone(str(school_attrs["phone"]))
    if school_attrs.get("address"):
        school_info["address"] = school_attrs["address"]

    # JSON:API format — staff details are in the "included" array
    included = dir_data.get("included", [])
    staff = [item for item in included if item.get("type") == "staff"]

    if not staff:
        return {"school": school_info, "staff_count": 0, "staff": []}

    # Fetch virtual phone numbers (separate API call)
    phone_map: dict[int, str] = {}
    staff_ids = [int(s["id"]) for s in staff]
    try:
        vp_data = app.client.post_json(
            f"/api/v2/users/{app.client.account.user_id}/virtual_phone_search",
            {"staff_ids": staff_ids},
        )
        for entry in vp_data.get("content", []):
            if entry.get("virtual_phone"):
                phone_map[entry["staff_id"]] = _format_phone(str(entry["virtual_phone"]))
    except Exception:
        pass  # phone numbers are optional — don't fail the whole tool

    records = []
    for s in staff:
        attrs = s.get("attributes", {})
        staff_id = attrs.get("id", int(s["id"]))
        first = attrs.get("first_name", "")
        last = attrs.get("last_name", "")
        role = attrs.get("role", "")
        title = attrs.get("user_title", "")
        record: dict[str, Any] = {
            "user_id": staff_id,
            "name": f"{first} {last}".strip(),
            "role": title if title else role.replace("_", " ").title(),
        }
        phone = phone_map.get(staff_id)
        if phone:
            record["phone"] = phone
        records.append(record)

    return {"school": school_info, "staff_count": len(records), "staff": records}


@mcp.tool(name="get_staff_member")
async def get_staff_member(school_id: int, user_id: int, context: Context[Any, Any] = None) -> list:
    """Get detailed info for a specific staff member including email, photo, and office hours.

    Use get_directory first to find the user_id, then call this for full details.
    Returns structured data plus an inline profile photo when available.

    Args:
        school_id: School ID
        user_id: Staff member's user ID (from get_directory results)
    """
    app = _app(context)
    data, err = await _with_mfa_retry(
        app, context,
        lambda: app.client.get_json(f"/api/v2/schools/{school_id}/users/{user_id}"),
    )
    if err:
        return [err]

    attrs = data.get("data", {}).get("attributes", {})
    name = f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip()
    title = attrs.get("user_title", "")
    role = attrs.get("role", "")
    display_role = title if title else role.replace("_", " ").title()

    info: dict[str, Any] = {"name": name, "role": display_role, "user_id": user_id}

    emails = attrs.get("emails", [])
    if emails and attrs.get("email_visible", True):
        info["email"] = emails[0] if len(emails) == 1 else emails

    phone = attrs.get("virtual_phone_number")
    if phone and attrs.get("virtual_phone_enabled"):
        info["phone"] = _format_phone(str(phone))

    # Office hours from included data
    included = data.get("included", [])
    for item in included:
        if item.get("type") == "office_hour":
            oh = item["attributes"]
            if oh.get("enabled"):
                days_map = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
                days = [days_map.get(d, str(d)) for d in oh.get("enabled_days", [])]
                info["office_hours"] = {
                    "days": days,
                    "start": oh.get("local_start_time", ""),
                    "end": oh.get("local_stop_time", ""),
                    "timezone": oh.get("time_zone_abbreviation", ""),
                }

    result: list = [info]

    # Fetch profile photo inline so Claude can see the person
    photo = attrs.get("profile_photo_thumb_url")
    if photo:
        img, _size = _fetch_image(app.client, photo)
        if img:
            result.append(img)

    return result


# ---------------------------------------------------------------------------
# Group tools
# ---------------------------------------------------------------------------


_GROUPS_QUERY = """
query GetGroups($institute: InstituteInputType!, $studentId: ID = null) {
  groupsIndex(institute: $institute, studentId: $studentId) {
    list {
      instituteName
      hasGroups
      categorizedGroups {
        name
        groups {
          id
          name
          description
          isPublic
        }
      }
    }
  }
}
"""


@mcp.tool(name="list_groups")
async def list_groups(school_id: int, context: Context[Any, Any] = None) -> str:
    """List groups at a school with active post counts and descriptions.

    To view a group's posts, call get_group_feed with BOTH the same school_id
    used here AND the group_id shown in the results.

    Args:
        school_id: School ID (use list_schools to find available IDs)
    """
    app = _app(context)

    def _fetch():
        variables = {"institute": {"type": "school", "id": school_id}, "studentId": None}
        return app.client.graphql(_GROUPS_QUERY, variables, "GetGroups")

    data, err = await _with_mfa_retry(app, context, _fetch)
    if err:
        return err

    cat_groups = (data.get("groupsIndex") or {}).get("list", {}).get("categorizedGroups", [])

    # ParentSquare removed userCount/activeFeedsCount/hasUserOrStudent (and
    # lastPostAt/feedsPath) from the GraphQL Group type, which made the old
    # query 422 and broke list_groups entirely. Re-source the active-post count
    # from the JSON:API groups endpoint (keyed by id); best-effort, falls back
    # to 0. Member count is no longer exposed on either surface.
    post_counts: dict[int, int] = {}
    try:
        jg = app.client.get_json(f"/api/v2/schools/{school_id}/groups")
        for item in jg.get("data", []):
            attrs = item.get("attributes", {})
            gid = attrs.get("id") or item.get("id")
            if gid is not None:
                post_counts[int(gid)] = attrs.get("active_posts_count", 0)
    except Exception:
        logger.debug("Could not enrich group post counts from JSON:API", exc_info=True)

    groups: list[Group] = []
    for cat in cat_groups:
        cat_name = cat.get("name", "")
        for g in cat.get("groups", []):
            gid = g["id"]
            groups.append(Group(
                id=gid,
                name=g["name"],
                member_count=0,  # userCount removed from GraphQL Group type
                description=g.get("description"),
                category=cat_name,
                post_count=post_counts.get(int(gid), 0),
                is_member=False,  # hasUserOrStudent removed from GraphQL Group type
            ))

    if not groups:
        return "No groups found."

    lines = [f"# Groups for {_school_name(app, school_id)}", ""]
    lines.append(f"_To view a group's posts, call get_group_feed(school_id={school_id}, group_id=<id>)_")
    lines.append("")
    current_cat = None
    for g in groups:
        if g.category != current_cat:
            current_cat = g.category
            lines.append(f"## {current_cat or 'Uncategorized'}")
            lines.append("")
        lines.append(f"**[group_id={g.id}] {g.name}** ({g.post_count} active posts)")
        if g.description:
            lines.append(f"  {g.description[:200]}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="get_group_feed")
async def get_group_feed(school_id: int, group_id: int, context: Context[Any, Any] = None) -> str:
    """Get posts from a specific group's feed.

    IMPORTANT: Both school_id and group_id are required.

    Args:
        school_id: School ID (same school_id used in list_groups)
        group_id: Group ID (from list_groups results)
    """
    app = _app(context)
    path = f"/schools/{school_id}/groups/{group_id}/feeds"
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    posts = parse_group_feed(soup)
    if not posts:
        return "No posts in this group."
    lines = ["# Group Feed", ""]
    for p in posts:
        lines.append(f"**[{p.id}] {p.title}**")
        lines.append(f"  By {p.author} on {p.date}")
        if p.summary:
            lines.append(f"  {p.summary}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Student tool
# ---------------------------------------------------------------------------


@mcp.tool(name="get_student_dashboard")
async def get_student_dashboard(student_id: int, context: Context[Any, Any] = None) -> dict:
    """Get student dashboard information including school, grade, classes, and teachers.

    Args:
        student_id: Student ID (use list_schools to see available students)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(f"/students/{student_id}/dashboard"))
    if err:
        return err
    dashboard = parse_student_dashboard(soup)
    info: dict[str, Any] = {
        "student": dashboard.student_name,
        "school": dashboard.school_name,
    }
    if dashboard.grade:
        info["grade"] = dashboard.grade
    if dashboard.teachers:
        info["teachers"] = dashboard.teachers
    if dashboard.classes:
        info["classes"] = dashboard.classes
    return info


# ---------------------------------------------------------------------------
# Participate tools (Sign-ups, Notices, Polls, Links)
# ---------------------------------------------------------------------------


@mcp.tool(name="list_signups")
async def list_signups(school_id: int, page: int = 1, context: Context[Any, Any] = None) -> str:
    """List sign-up and RSVP posts for a school (item donations, volunteer slots, event RSVPs).

    Shows each signup post with its title, progress (e.g. "53/103 Items"), and author.
    Use get_post with the feed_id to see full signup details including individual items.

    Args:
        school_id: School ID
        page: Page number for pagination (default: 1)
    """
    app = _app(context)
    path = f"/schools/{school_id}/sign_ups"
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path, params={"page": str(page)}))
    if err:
        return err
    posts = parse_feed_page(soup)
    if not posts:
        return f"No sign-ups or RSVP requests found for {_school_name(app, school_id)}."
    lines = [f"# Sign-Ups for {_school_name(app, school_id)} (page {page})", ""]
    for p in posts:
        lines.append(f"**{p.title}** (feed_id: {p.id})")
        progress = f"  📋 {p.signup_progress}" if p.signup_progress else ""
        lines.append(f"  By {p.author} on {p.date}")
        if progress:
            lines.append(progress)
        if p.summary:
            lines.append(f"  {p.summary}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_notices")
async def list_notices(school_id: int, context: Context[Any, Any] = None) -> str:
    """List alerts and notices for a school (urgent alerts, secure documents, consent forms).

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = f"/schools/{school_id}/notices"
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    notices = parse_notices(soup)
    if not notices:
        return f"No notices found for {_school_name(app, school_id)}."
    lines = [f"# Notices for {_school_name(app, school_id)} ({len(notices)})", ""]
    for n in notices:
        icon = "🔔" if n.notice_type == "alert" else "📄"
        lines.append(f"{icon} **{n.title}**")
        lines.append(f"  {n.date}")
        if n.school:
            lines.append(f"  From: {n.school}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_polls")
async def list_polls(school_id: int, context: Context[Any, Any] = None) -> str:
    """List polls for a school with questions, options, and vote counts.

    Shows each poll's question, answer options with vote totals, and which
    option is winning.

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = URLS["polls"].format(school_id=school_id)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    polls = parse_polls_page(soup)
    if not polls:
        return f"No polls found for {_school_name(app, school_id)}."
    lines = [f"# Polls for {_school_name(app, school_id)} ({len(polls)})", ""]
    for poll in polls:
        lines.append(f"**{poll.question}** (feed_id: {poll.id})")
        lines.append(f"  By {poll.author} on {poll.date}")
        voted = " (you voted)" if poll.user_voted else ""
        lines.append(f"  Total votes: {poll.total_votes}{voted}")
        for opt in poll.options:
            winner = " ★" if opt.is_winner else ""
            lines.append(f"  - {opt.text}: {opt.votes} votes{winner}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_links")
async def list_links(school_id: int, context: Context[Any, Any] = None) -> str:
    """List quick-access links for a school (calendars, menus, forms, etc.).

    Returns link names and URLs (typically Google Drive or external sites).

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = URLS["links"].format(school_id=school_id)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    links = parse_links_page(soup)
    if not links:
        return f"No links found for {_school_name(app, school_id)}."
    lines = [f"# Links for {_school_name(app, school_id)} ({len(links)})", ""]
    for link in links:
        lines.append(f"- **{link.name}**")
        lines.append(f"  {link.url}")
        if link.school:
            lines.append(f"  From: {link.school}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_payments")
async def list_payments(school_id: int, context: Context[Any, Any] = None) -> str:
    """List payment items for a school with summary stats and item prices.

    Shows upcoming/paid counts, total amount paid, and each payment post
    with its available items and prices.

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = URLS["payments"].format(school_id=school_id)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    summary = parse_payments_page(soup)
    lines = [f"# Payments for {_school_name(app, school_id)}", ""]
    lines.append(f"**Upcoming:** {summary.upcoming} | **Paid For:** {summary.paid_for} | **Total Paid:** {summary.total_paid}")
    lines.append("")
    if not summary.posts:
        lines.append("No payment items found.")
    for post in summary.posts:
        lines.append(f"## {post.title} (feed_id: {post.id})")
        lines.append(f"By {post.author}")
        if post.date:
            lines[-1] += f" on {post.date}"
        for section_name, items in post.sections:
            if section_name:
                lines.append(f"### {section_name}")
            for item in items:
                lines.append(f"- {item.name}: **{item.price}**")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="list_volunteer_hours")
async def list_volunteer_hours(school_id: int, context: Context[Any, Any] = None) -> str:
    """List your logged volunteer hours for a school.

    Shows each logged entry with month, activity type, notes, and hours.

    Args:
        school_id: School ID
    """
    app = _app(context)
    def _fetch_hours():
        _ensure_account(app)
        path = URLS["volunteer_hours"].format(school_id=school_id, user_id=app.client.account.user_id)
        return app.client.get_page(path)
    soup, err = await _with_mfa_retry(app, context, _fetch_hours)
    if err:
        return err
    records = parse_volunteer_hours(soup)
    if not records:
        return f"No volunteer hours logged for {_school_name(app, school_id)}."
    # Calculate total
    total_minutes = 0
    for r in records:
        m = re.match(r"(\d+):(\d+)", r.hours)
        if m:
            total_minutes += int(m.group(1)) * 60 + int(m.group(2))
    total_h, total_m = divmod(total_minutes, 60)
    lines = [f"# Volunteer Hours for {_school_name(app, school_id)}", ""]
    lines.append(f"**Total: {total_h}:{total_m:02d} hrs** ({len(records)} entries)")
    lines.append("")
    for r in records:
        note = f" — {r.note}" if r.note else ""
        lines.append(f"- **{r.month}** | {r.activity}{note} | {r.hours}")
    return "\n".join(lines)


@mcp.tool(name="list_forms")
async def list_forms(school_id: int, context: Context[Any, Any] = None) -> str:
    """List available forms and permission slips for a school.

    Shows form titles, authors, and dates. These are signable forms that may
    require parent signatures (e.g. field trip permission, driver forms).
    Use get_post with the feed_id to see the full form content.

    Args:
        school_id: School ID
    """
    app = _app(context)
    path = URLS["forms"].format(school_id=school_id)
    soup, err = await _with_mfa_retry(app, context, lambda: app.client.get_page(path))
    if err:
        return err
    # Forms page reuses the feeds-list structure
    posts = parse_feed_page(soup)
    if not posts:
        return f"No forms found for {_school_name(app, school_id)}."
    lines = [f"# Forms for {_school_name(app, school_id)} ({len(posts)})", ""]
    for p in posts:
        lines.append(f"📝 **{p.title}** (feed_id: {p.id})")
        lines.append(f"  By {p.author} on {p.date}")
        if p.summary:
            lines.append(f"  {p.summary}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Admin tools (roster: students & guardians)
#
# Read tools (list_students, list_grades, get_student) are always available.
# Write tools (add_/edit_student, add_/edit_parent, link_guardian_to_student)
# are gated behind PS_ENABLE_WRITES and audited. v1 is create/edit only — no
# destructive operations. See the vault note "ParentSquare admin API mapping".
# ---------------------------------------------------------------------------


def _roster_students(client: PSClient, school_id: int):
    """Fetch and parse the full admin student roster (one call, client paginates)."""
    data = client.get_json(f"/schools/{school_id}/roster/students_data?_=1")
    return parse_roster_students(data.get("data", []))


def _roster_parents(client: PSClient, school_id: int):
    """Fetch and parse the full admin parent/guardian roster (one call)."""
    data = client.get_json(f"/schools/{school_id}/roster/parents_data?_=1")
    return parse_roster_parents(data.get("data", []))


def _student_grade_id(client: PSClient, school_id: int, student_id: int) -> str:
    """Resolve a student's current grade_id from its edit form (for kid links)."""
    js = client.get_text(f"/schools/{school_id}/students/{student_id}/edit")
    return extract_student_edit_fields(js).get("grade_id", "")


def _write_result(tool: str, args: dict, resp) -> str:
    """Interpret a form-write Response, audit it, and format a user message.

    Surfaces ParentSquare's own flash message when present (e.g. the invite
    endpoints report how many users were actually notified).
    """
    ok = write_succeeded(resp.status_code, resp.headers.get("content-type", ""), resp.text)
    flash = parse_flash_message(resp.text)
    detail = f"HTTP {resp.status_code}" + (f": {flash}" if flash else "")
    audit_write(tool, args, ok, detail=detail)
    if ok:
        return f"✅ {flash}" if flash else "✅ Success."
    if flash:
        return f"❌ {flash} (HTTP {resp.status_code})"
    snippet = " ".join(resp.text.split())[:200]
    return f"❌ Operation failed (HTTP {resp.status_code}). ParentSquare response: {snippet}"


# A 5xx tells us nothing about whether a write committed — ParentSquare's error
# page is rendered after the transaction. A write that crashes on render can and
# does leave the record behind (see build_add_student_body), so reporting a hard
# failure invites a retry that duplicates a real record. The read-back decides.
_SERVER_ERROR_NOTE = (
    "ParentSquare crashed rendering its response, which happens after the record "
    "is saved, so the status code says nothing about whether the write landed."
)


def _write_result_verified(tool: str, args: dict, resp, verified: bool | None) -> str:
    """Interpret a form-write Response using a read-back result, audit, and format.

    A 200 JS "reload" response only proves ParentSquare accepted the POST — some
    silent failures still return one. The write tools therefore re-read
    authoritative state and pass the outcome as ``verified``:

    - ``True``  — the change was found on read-back (persisted).
    - ``False`` — the POST was accepted but the change is absent (likely a silent
      failure; this is the false-positive the old heuristic missed).
    - ``None``  — the read-back itself could not run (e.g. MFA/network); the write
      may well have succeeded but we can't confirm it.

    The read-back also outranks a *server-side* failure. ParentSquare renders its
    error page after the transaction commits, so a 5xx can hide a write that
    landed — ``add_student`` did exactly that for every create until the missing
    ``student[section_ids][]`` param was found. Reporting such a response as
    ``❌`` invites a retry, and a retry duplicates a real record with no API
    route to undo it. So on a 5xx, where ParentSquare has returned a stack trace
    rather than a verdict, the read-back decides. A 4xx or a ``200`` +
    ``alert-danger`` *is* a verdict — ParentSquare explicitly rejecting the
    write — and is still reported as a failure regardless of read-back (the
    record found there would be a pre-existing one). ``write_succeeded`` is
    deliberately left strict: a 500 remains a failure for every endpoint that
    has no read-back to overrule it.
    """
    http_ok = write_succeeded(resp.status_code, resp.headers.get("content-type", ""), resp.text)
    if not http_ok:
        snippet = " ".join(resp.text.split())[:200]
        crashed = resp.status_code >= 500  # no verdict from ParentSquare, only a stack trace
        if crashed and verified is True:
            audit_write(
                tool, args, True, detail=f"HTTP {resp.status_code}; verified by read-back"
            )
            return (
                f"✅ Success (verified). Note: ParentSquare returned HTTP "
                f"{resp.status_code}. {_SERVER_ERROR_NOTE} Reading it back confirms the "
                "record was created — do NOT retry: a retry would create a duplicate. "
                "Worth reporting, since a 5xx here usually means a malformed request."
            )
        if crashed and verified is None:
            audit_write(
                tool, args, False, detail=f"HTTP {resp.status_code}; read-back unavailable"
            )
            return (
                f"⚠️ ParentSquare returned HTTP {resp.status_code} and the read-back could "
                f"not run, so it is unknown whether the write landed. {_SERVER_ERROR_NOTE} "
                "Do NOT retry blindly — check with list_students / get_student first. "
                f"ParentSquare response: {snippet}"
            )
        audit_write(tool, args, False, detail=f"HTTP {resp.status_code}")
        return f"❌ Operation failed (HTTP {resp.status_code}). ParentSquare response: {snippet}"
    if verified is False:
        audit_write(tool, args, False, detail=f"HTTP {resp.status_code}; read-back not found")
        return (
            "⚠️ ParentSquare accepted the request (HTTP 200) but a read-back could "
            "not find the change, so it likely did NOT persist. Verify with "
            "list_students / get_student before retrying."
        )
    if verified is None:
        audit_write(tool, args, True, detail=f"HTTP {resp.status_code}; unverified")
        return (
            "✅ Submitted (HTTP 200), but automatic verification could not run. "
            "Confirm the change with list_students / get_student."
        )
    audit_write(tool, args, True, detail=f"HTTP {resp.status_code}; verified")
    return "✅ Success (verified)."


async def _readback(app, context, fn):
    """Run a post-write verification read. -> (result, error_message|None).

    Never raises: the read-back now decides whether a write is reported as a
    success (see ``_write_result_verified``), so a failure to read must degrade
    to "unverified" rather than escape and hide the write's outcome entirely.
    """
    try:
        return await _with_mfa_retry(app, context, fn)
    except Exception as exc:  # noqa: BLE001 — verification must never mask the write
        return None, f"read-back failed: {exc}"


async def _student_guardians(app, context, student_id: int):
    """Read a student's current guardian list via the profile GraphQL. -> (guardians, err)."""
    def _fetch():
        data = app.client.graphql(
            STUDENT_PROFILE_QUERY, {"studentId": student_id}, "StudentProfileView"
        )
        return parse_student_profile(data)

    profile, err = await _readback(app, context, _fetch)
    if err:
        return None, err
    return (profile.parents if profile else []), None


def _write_gated(func):
    """Gate a write tool behind ``PS_ENABLE_WRITES``.

    When writes are disabled, the blocked attempt is audited and the friendly
    disabled message is returned without invoking the tool. ``functools.wraps``
    preserves the wrapped function's signature so MCPServer still builds the
    correct tool schema (it introspects via ``inspect.signature``, which follows
    ``__wrapped__``).
    """
    sig = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not writes_enabled():
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            audit_args = {k: v for k, v in bound.arguments.items() if k != "context"}
            audit_write(func.__name__, audit_args, False, detail="blocked: writes disabled")
            return WRITES_DISABLED_MESSAGE
        return await func(*args, **kwargs)

    return wrapper


@mcp.tool(name="list_students")
async def list_students(
    school_id: int,
    grade: str | None = None,
    name_contains: str | None = None,
    context: Context[Any, Any] = None,
) -> dict | str:
    """List students on a school's admin roster (id, name, grade, SIS id, guardians).

    Returns the full roster in one call. Use the `id` from results as `student_id`
    in get_student, edit_student, add_parent, and link_guardian_to_student.

    Args:
        school_id: School ID (from list_schools)
        grade: Optional case-insensitive filter on grade name (e.g. "2nd Grade")
        name_contains: Optional case-insensitive substring filter on student name
    """
    app = _app(context)
    students, err = await _with_mfa_retry(app, context, lambda: _roster_students(app.client, school_id))
    if err:
        return err
    if grade:
        g = grade.lower()
        students = [s for s in students if g in s.grade.lower()]
    if name_contains:
        n = name_contains.lower()
        students = [s for s in students if n in s.name.lower()]
    return {
        "school": _school_name(app, school_id),
        "count": len(students),
        "students": [
            {
                "student_id": s.id,
                "name": s.name,
                "grade": s.grade,
                "student_sis_id": s.student_sis_id,
                "state_id": s.state_id,
                "account_status": s.account_status,
                "guardians": s.parents,
            }
            for s in students
        ],
    }


@mcp.tool(name="list_parents")
async def list_parents(
    school_id: int,
    name_contains: str | None = None,
    student_name_contains: str | None = None,
    context: Context[Any, Any] = None,
) -> dict | str:
    """List guardians/parents on a school's admin roster (user_id, name, email, phone, linked students).

    Returns the full parent roster in one call. Use the `user_id` from results in
    edit_parent and link_guardian_to_student.

    Args:
        school_id: School ID (from list_schools)
        name_contains: Optional case-insensitive substring filter on guardian name
        student_name_contains: Optional case-insensitive substring filter on the
            guardian's linked student names
    """
    app = _app(context)
    parents, err = await _with_mfa_retry(app, context, lambda: _roster_parents(app.client, school_id))
    if err:
        return err
    if name_contains:
        n = name_contains.lower()
        parents = [p for p in parents if n in p.name.lower()]
    if student_name_contains:
        s = student_name_contains.lower()
        parents = [p for p in parents if s in p.students.lower()]
    return {
        "school": _school_name(app, school_id),
        "count": len(parents),
        "parents": [
            {
                "user_id": p.user_id,
                "name": p.name,
                "email": p.email,
                "phone": p.phone,
                "secondary_phone": p.secondary_phone,
                "students": p.students,
                "registered": p.registered,
            }
            for p in parents
        ],
    }


@mcp.tool(name="list_grades")
async def list_grades(school_id: int, context: Context[Any, Any] = None) -> dict | str:
    """List a school's grades with their grade_id values (needed for add_student).

    Grades are per-school and discovered at runtime. Use the returned `grade_id`
    with add_student / edit_student.

    Args:
        school_id: School ID (from list_schools)
    """
    app = _app(context)
    soup, err = await _with_mfa_retry(
        app, context, lambda: app.client.get_page(f"/schools/{school_id}/roster/add_remove_students")
    )
    if err:
        return err
    grades = parse_grades(soup)
    return {
        "school": _school_name(app, school_id),
        "grades": [{"grade_id": g.id, "name": g.name} for g in grades],
    }


@mcp.tool(name="get_student")
async def get_student(student_id: int, context: Context[Any, Any] = None) -> dict | str:
    """Get admin detail for one student (name, grade, SIS id, linked guardians, classes).

    Args:
        student_id: Student ID (the `id` from list_students; == roster id)
    """
    app = _app(context)

    def _fetch():
        return app.client.graphql(STUDENT_PROFILE_QUERY, {"studentId": student_id}, "StudentProfileView")

    data, err = await _with_mfa_retry(app, context, _fetch)
    if err:
        return err
    profile = parse_student_profile(data)
    if not profile:
        return f"No student found with id {student_id}."
    return {
        "student_id": profile.student_id,
        "name": profile.full_name,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "school": profile.school_name,
        "school_id": profile.school_id,
        "grade": profile.grade_name,
        "student_sis_id": profile.student_sis_id,
        "guardians": profile.parents,
        "classes": profile.sections,
    }


@mcp.tool(name="add_student")
@_write_gated
async def add_student(
    school_id: int,
    first_name: str,
    last_name: str,
    grade_id: int,
    student_sis_id: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Create a new student on a school's roster. Requires PS_ENABLE_WRITES.

    Use list_grades(school_id) to find the grade_id. The new student's id is not
    returned by ParentSquare — call list_students afterward to retrieve it.

    The roster is read back after the write, so the result says whether the
    student actually exists. If ParentSquare ever returns a 5xx here, do not
    retry on the status code alone: its error page is rendered after the record
    is saved, so the student may well have been created, and there is no API
    route to delete a duplicate.

    Args:
        school_id: School ID
        first_name: Student first name
        last_name: Student last name
        grade_id: Grade ID (from list_grades)
        student_sis_id: Optional SIS/external student ID
    """
    app = _app(context)
    args = {"school_id": school_id, "first_name": first_name, "last_name": last_name,
            "grade_id": grade_id, "student_sis_id": student_sis_id}
    body = build_add_student_body(first_name, last_name, grade_id, student_sis_id or "")
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_form(f"/schools/{school_id}/students", body)
    )
    if err:
        return err
    students, verr = await _readback(
        app, context, lambda: _roster_students(app.client, school_id)
    )
    verified = None if verr else roster_has_student(students, first_name, last_name)
    return _write_result_verified("add_student", args, resp, verified)


@mcp.tool(name="edit_student")
@_write_gated
async def edit_student(
    school_id: int,
    student_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    student_sis_id: str | None = None,
    grade_id: int | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Update an existing student. Only provided fields change. Requires PS_ENABLE_WRITES.

    Current values (name, SIS id, grade) are read from the edit form and
    preserved for any field you don't pass. Class enrollment is never touched —
    use add_class_students / remove_class_students / move_student_to_class.

    Args:
        school_id: School ID
        student_id: Student ID (from list_students)
        first_name: New first name (optional)
        last_name: New last name (optional)
        student_sis_id: New SIS/external ID (optional)
        grade_id: New grade ID (optional; from list_grades)
    """
    app = _app(context)
    args = {"school_id": school_id, "student_id": student_id, "first_name": first_name,
            "last_name": last_name, "student_sis_id": student_sis_id, "grade_id": grade_id}

    def _load():
        return extract_student_edit_fields(
            app.client.get_text(f"/schools/{school_id}/students/{student_id}/edit")
        )

    current, err = await _with_mfa_retry(app, context, _load)
    if err:
        return err
    if not current.get("first_name") and not current.get("last_name"):
        return f"❌ Could not load student {student_id} (not found or no edit access)."
    resolved_grade_id = str(grade_id) if grade_id is not None else current.get("grade_id", "")
    if not resolved_grade_id:
        return (
            f"❌ Could not determine the current grade for student {student_id}; "
            "refusing the edit to avoid blanking it. Pass an explicit grade_id "
            "(see list_grades)."
        )

    body = build_edit_student_body(
        first_name=first_name if first_name is not None else current["first_name"],
        last_name=last_name if last_name is not None else current["last_name"],
        grade_id=resolved_grade_id,
        sis_id=student_sis_id if student_sis_id is not None else current["external_id"],
    )
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_form(f"/schools/{school_id}/students/{student_id}", body)
    )
    if err:
        return err
    return _write_result("edit_student", args, resp)


@mcp.tool(name="add_parent")
@_write_gated
async def add_parent(
    school_id: int,
    student_id: int,
    first_name: str,
    last_name: str,
    email: str | None = None,
    phone: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Create a guardian/parent and link them to a student. Requires PS_ENABLE_WRITES.

    A parent is always created attached to at least one student. The new user id
    is not returned — call list_students or the parent roster afterward.

    Args:
        school_id: School ID
        student_id: Student ID to attach the guardian to (from list_students)
        first_name: Guardian first name
        last_name: Guardian last name
        email: Guardian email (optional)
        phone: Guardian phone (optional)
    """
    app = _app(context)
    args = {"school_id": school_id, "student_id": student_id, "first_name": first_name,
            "last_name": last_name, "email": email, "phone": phone}

    grade_id, err = await _with_mfa_retry(
        app, context, lambda: _student_grade_id(app.client, school_id, student_id)
    )
    if err:
        return err
    if not grade_id:
        return f"❌ Could not resolve grade for student {student_id} (not found?)."
    body = build_add_parent_body(
        school_id, student_id, int(grade_id), first_name, last_name, email or "", phone or ""
    )
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_form(f"/schools/{school_id}/users", body)
    )
    if err:
        return err
    guardians, verr = await _student_guardians(app, context, student_id)
    verified = None if verr else guardian_present(guardians, first_name, last_name)
    return _write_result_verified("add_parent", args, resp, verified)


@mcp.tool(name="edit_parent")
@_write_gated
async def edit_parent(
    school_id: int,
    user_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Update a guardian's name, email, or phone. Requires PS_ENABLE_WRITES.

    Only provided fields change; existing student links are preserved. Get the
    user_id from list_parents(school_id).

    Args:
        school_id: School ID
        user_id: Guardian's user ID (from list_parents)
        first_name: New first name (optional)
        last_name: New last name (optional)
        email: New email (optional)
        phone: New phone (optional)
    """
    app = _app(context)
    args = {"school_id": school_id, "user_id": user_id, "first_name": first_name,
            "last_name": last_name, "email": email, "phone": phone}

    def _load():
        return extract_parent_edit_fields(
            app.client.get_text(
                f"/schools/{school_id}/users/{user_id}/edit_institute_user", params={"role": "PARENT"}
            )
        )

    current, err = await _with_mfa_retry(app, context, _load)
    if err:
        return err
    if not current.get("first_name") and not current.get("last_name"):
        return f"❌ Could not load guardian {user_id} (not found or no edit access)."
    if (email is not None or phone is not None) and not current.get("contact_id"):
        return f"❌ Could not resolve contact record for guardian {user_id}; cannot update email/phone."

    body = build_edit_parent_body(
        first_name=first_name if first_name is not None else current["first_name"],
        last_name=last_name if last_name is not None else current["last_name"],
        contact_id=current.get("contact_id", ""),
        email=email,
        phone=phone,
    )
    resp, err = await _with_mfa_retry(
        app, context,
        lambda: app.client.post_form(f"/schools/{school_id}/users/{user_id}/update_institute_user", body),
    )
    if err:
        return err
    return _write_result("edit_parent", args, resp)


@mcp.tool(name="link_guardian_to_student")
@_write_gated
async def link_guardian_to_student(
    school_id: int,
    user_id: int,
    student_id: int,
    context: Context[Any, Any] = None,
) -> str:
    """Link an existing guardian to an additional student. Requires PS_ENABLE_WRITES.

    Existing guardian-student links are left untouched. (Unlinking is a
    destructive op deferred to the ParentSquare website in v1.)

    Args:
        school_id: School ID
        user_id: Guardian's user ID (from list_parents)
        student_id: Student ID to link (from list_students)
    """
    app = _app(context)
    args = {"school_id": school_id, "user_id": user_id, "student_id": student_id}

    grade_id, err = await _with_mfa_retry(
        app, context, lambda: _student_grade_id(app.client, school_id, student_id)
    )
    if err:
        return err
    if not grade_id:
        return f"❌ Could not resolve grade for student {student_id} (not found?)."
    body = build_link_guardian_body(school_id, student_id, int(grade_id))
    resp, err = await _with_mfa_retry(
        app, context,
        lambda: app.client.post_form(f"/schools/{school_id}/users/{user_id}/update_institute_user", body),
    )
    if err:
        return err
    guardians, verr = await _student_guardians(app, context, student_id)
    verified = None if verr else guardian_linked(guardians, user_id)
    return _write_result_verified("link_guardian_to_student", args, resp, verified)


@mcp.tool(name="invite_parent")
@_write_gated
async def invite_parent(
    school_id: int,
    user_id: int,
    context: Context[Any, Any] = None,
) -> str:
    """Send (or resend) a ParentSquare registration invitation to one guardian. Requires PS_ENABLE_WRITES.

    ParentSquare emails the guardian an invite to activate their account. The
    same endpoint is used to resend to an already-invited (but not yet
    registered) guardian. Get the user_id from list_parents(school_id); only
    guardians with registered=false need inviting.

    Args:
        school_id: School ID
        user_id: Guardian's user ID (from list_parents)
    """
    app = _app(context)
    args = {"school_id": school_id, "user_id": user_id}
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_form(f"/schools/{school_id}/users/{user_id}/invite", {})
    )
    if err:
        return err
    return _write_result("invite_parent", args, resp)


@mcp.tool(name="bulk_invite_parents")
@_write_gated
async def bulk_invite_parents(
    school_id: int,
    user_ids: list[int],
    context: Context[Any, Any] = None,
) -> str:
    """Send registration invitations to multiple guardians at once. Requires PS_ENABLE_WRITES.

    ParentSquare sends each unregistered guardian an email/text invite and
    automatically skips any that are already registered (the returned message
    reports how many of the selected users were actually notified). Get user_ids
    from list_parents(school_id) — typically those with registered=false. To
    "invite all", pass every unregistered user_id from list_parents.

    Args:
        school_id: School ID
        user_ids: Guardian user IDs to invite (from list_parents)
    """
    app = _app(context)
    if not user_ids:
        return "❌ No user_ids provided — nothing to invite."
    args = {"school_id": school_id, "user_ids": user_ids}
    body = build_bulk_invite_body(user_ids)
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_json_raw(f"/schools/{school_id}/users/invite", body)
    )
    if err:
        return err
    return _write_result("bulk_invite_parents", args, resp)


# ---------------------------------------------------------------------------
# Admin: classes (sections) and staff
# ---------------------------------------------------------------------------


def _sections_mini(client: PSClient, school_id: int):
    """Fetch and parse the full class list for a school (one call)."""
    data = client.get_json(f"/api/v2/schools/{school_id}/sections_mini")
    return parse_sections_mini(data.get("data", []))


def _section_detail(client: PSClient, section_id: int):
    """Fetch and parse one class plus its staff associations."""
    payload = client.get_json(f"/api/v2/sections/{section_id}", params={"include_staff": "true"})
    return parse_section_detail(payload)


def _class_dict(c) -> dict:
    return {
        "section_id": c.id,
        "name": c.name,
        "grades": c.grade_names,
        "grade_ids": [g for g in c.grade_ids.split(",") if g],
        "room": c.external_id,
        "teachers": c.teachers,
        "assistant_count": c.assistants,
        "room_parent_count": c.room_parents,
        "student_count": c.student_count,
        "visibility": c.visibility_status,
    }


def _staff_dict(s) -> dict:
    return {
        "assoc_id": s.assoc_id,
        "user_id": s.user_id,
        "name": s.name,
        "role": s.role,
        "class_title": s.class_title,
    }


def _json_write_result(tool: str, args: dict, resp) -> str:
    """Interpret a JSON:API write Response, audit it, and format a user message.

    The class/section endpoints answer with real JSON (2xx on success), so the
    UJS-oriented ``write_succeeded`` / flash parsing used by the student and
    guardian tools does not apply here.
    """
    ok = json_write_ok(resp.status_code)
    if ok:
        audit_write(tool, args, True, detail=f"HTTP {resp.status_code}")
        return "✅ Success."
    detail = json_write_error(resp.status_code, resp.text)
    audit_write(tool, args, False, detail=detail)
    return f"❌ Operation failed ({detail})"


async def _load_class(app, context, section_id: int):
    """Read a class + staff for a read-modify-write. -> (ClassDetail|None, err)."""
    detail, err = await _with_mfa_retry(app, context, lambda: _section_detail(app.client, section_id))
    if err:
        return None, err
    if not detail:
        return None, f"❌ No class found with section_id {section_id}."
    return detail, None


async def _put_class_staff(app, context, tool: str, args: dict, section_id: int, staff: list):
    """PUT the full staff list for a class (the endpoint replaces, never merges)."""
    body = build_staff_replace_body(staff)
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.send_json("PUT", f"/api/v2/sections/{section_id}/staff", body)
    )
    if err:
        return err
    return _json_write_result(tool, args, resp)


def _section_membership_write_lock(app) -> asyncio.Lock:
    """Return the app-wide lock protecting section membership mutations."""
    lock = getattr(app, "section_membership_write_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.section_membership_write_lock = lock
    return lock


async def _find_new_staff_id(app, context, school_id: int, first_name: str, last_name: str,
                             email: str | None):
    """Look up a just-created staff member's user_id. -> (user_id|None, reason)."""

    def _fetch():
        data = app.client.get_json(f"/schools/{school_id}/roster/staff_data")
        return parse_roster_staff(data.get("data", []))

    staff, err = await _readback(app, context, _fetch)
    if err:
        return None, "the staff roster could not be read."
    if email:
        matches = [s for s in staff if (s.email or "").lower() == email.lower()]
        if len(matches) == 1:
            return matches[0].user_id, ""
    wanted = f"{last_name}, {first_name}".lower()
    matches = [s for s in staff if s.name.lower() == wanted]
    if len(matches) == 1:
        return matches[0].user_id, ""
    if not matches:
        return None, "they could not be found in the staff roster."
    return None, f"{len(matches)} staff members share that name, so the right one is ambiguous."


@mcp.tool(name="list_classes")
async def list_classes(
    school_id: int,
    name_contains: str | None = None,
    grade: str | None = None,
    context: Context[Any, Any] = None,
) -> dict | str:
    """List a school's classes (sections) with grades, teachers, and room-parent counts.

    Returns every class in one call. Use the `section_id` from results with
    get_class, edit_class, add_class_staff, remove_class_staff, and
    set_class_visibility.

    Args:
        school_id: School ID (from list_schools)
        name_contains: Optional case-insensitive substring filter on class name
        grade: Optional case-insensitive filter on grade name (e.g. "Kindergarten")
    """
    app = _app(context)
    classes, err = await _with_mfa_retry(app, context, lambda: _sections_mini(app.client, school_id))
    if err:
        return err
    if name_contains:
        n = name_contains.lower()
        classes = [c for c in classes if n in c.name.lower()]
    if grade:
        g = grade.lower()
        classes = [c for c in classes if g in c.grade_names.lower()]
    return {
        "school": _school_name(app, school_id),
        "count": len(classes),
        "classes": [_class_dict(c) for c in classes],
    }


@mcp.tool(name="get_class")
async def get_class(section_id: int, context: Context[Any, Any] = None) -> dict | str:
    """Get one class with its full staff list (teachers, assistants, room parents).

    Each staff entry includes the `user_id` (the person) and `assoc_id` (their
    link to this class). Use this before changing staff to see the current state.

    Args:
        section_id: Class/section ID (from list_classes)
    """
    app = _app(context)
    detail, err = await _load_class(app, context, section_id)
    if err:
        return err
    return {
        "section_id": detail.id,
        "name": detail.name,
        "room": detail.external_id,
        "grade_ids": detail.grade_ids,
        "active": detail.active,
        "staff": [_staff_dict(s) for s in detail.staff],
    }


@mcp.tool(name="list_staff")
async def list_staff(
    school_id: int,
    name_contains: str | None = None,
    context: Context[Any, Any] = None,
) -> dict | str:
    """List a school's staff and admins (user_id, name, email, phone, role/title).

    Use the returned `user_id` with add_class_staff to assign a teacher to a class.

    Args:
        school_id: School ID (from list_schools)
        name_contains: Optional case-insensitive substring filter on staff name
    """
    app = _app(context)

    def _fetch():
        data = app.client.get_json(f"/schools/{school_id}/roster/staff_data")
        return parse_roster_staff(data.get("data", []))

    staff, err = await _with_mfa_retry(app, context, _fetch)
    if err:
        return err
    if name_contains:
        n = name_contains.lower()
        staff = [s for s in staff if n in s.name.lower()]
    return {
        "school": _school_name(app, school_id),
        "count": len(staff),
        "staff": [
            {
                "user_id": s.user_id,
                "name": s.name,
                "email": s.email,
                "phone": s.phone,
                "staff_id": s.staff_id,
                "role_title": s.role_title,
                "registered": s.registered,
            }
            for s in staff
        ],
    }


@mcp.tool(name="add_class")
@_write_gated
async def add_class(
    school_id: int,
    name: str,
    grade_ids: list[int],
    context: Context[Any, Any] = None,
) -> str:
    """Create a new class (section) at a school. Requires PS_ENABLE_WRITES.

    New classes are created **hidden** — they are not visible to staff, parents,
    or students until you call set_class_visibility. Assign teachers afterwards
    with add_class_staff.

    Args:
        school_id: School ID
        name: Class name (e.g. "Ms. Smith's Class")
        grade_ids: One or more grade IDs the class belongs to (from list_grades)
    """
    app = _app(context)
    args = {"school_id": school_id, "name": name, "grade_ids": grade_ids}
    if not name.strip():
        return "❌ A class name is required."
    if not grade_ids:
        return "❌ At least one grade_id is required (see list_grades)."
    body = build_add_class_body(school_id, name, [str(g) for g in grade_ids])
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.send_json("POST", f"/api/v2/schools/{school_id}/sections", body)
    )
    if err:
        return err
    result = _json_write_result("add_class", args, resp)
    if not result.startswith("✅"):
        return result
    try:
        new_id = resp.json()["data"]["attributes"]["id"]
    except (ValueError, KeyError, TypeError):
        return f"{result} Created \"{name}\" (hidden). Find its section_id with list_classes."
    return (
        f"✅ Created class \"{name}\" (section_id={new_id}). It is hidden until you "
        "call set_class_visibility; add teachers with add_class_staff."
    )


@mcp.tool(name="edit_class")
@_write_gated
async def edit_class(
    section_id: int,
    school_id: int,
    name: str | None = None,
    room: str | None = None,
    grade_ids: list[int] | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Rename a class or change its room code / grades. Requires PS_ENABLE_WRITES.

    Only provided fields change — current values are read back from the class
    first and resent, because the underlying endpoint replaces what it is given.
    Staff are not affected; use add_class_staff / remove_class_staff for those.

    Args:
        section_id: Class/section ID (from list_classes)
        school_id: School ID the class belongs to
        name: New class name (optional)
        room: New room code / external ID, e.g. "Room 5" (optional)
        grade_ids: New list of grade IDs (optional; replaces the current grades)
    """
    app = _app(context)
    args = {"section_id": section_id, "school_id": school_id, "name": name,
            "room": room, "grade_ids": grade_ids}
    current, err = await _load_class(app, context, section_id)
    if err:
        return err
    resolved_grades = [str(g) for g in grade_ids] if grade_ids is not None else current.grade_ids
    if not resolved_grades:
        return (
            f"❌ Could not determine the current grades for class {section_id}; "
            "refusing the edit to avoid blanking them. Pass explicit grade_ids "
            "(see list_grades)."
        )
    body = build_edit_class_body(
        school_id=school_id,
        section_id=section_id,
        name=name if name is not None else current.name,
        external_id=room if room is not None else current.external_id,
        grade_ids=resolved_grades,
    )
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.send_json("PATCH", f"/api/v2/sections/{section_id}", body)
    )
    if err:
        return err
    return _json_write_result("edit_class", args, resp)


@mcp.tool(name="set_class_visibility")
@_write_gated
async def set_class_visibility(
    school_id: int,
    section_ids: list[int],
    visible: bool,
    date: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Show or hide classes for staff, parents, and students. Requires PS_ENABLE_WRITES.

    Newly created classes start hidden, so a class made with add_class needs this
    before anyone can see or post to it. Hidden classes are not visible to staff,
    parents, or students.

    Args:
        school_id: School ID
        section_ids: Class/section IDs to update (from list_classes)
        visible: True to make the classes visible, False to hide them
        date: Optional YYYY-MM-DD date the change takes effect; defaults to today
    """
    app = _app(context)
    args = {"school_id": school_id, "section_ids": section_ids, "visible": visible, "date": date}
    if not section_ids:
        return "❌ No section_ids provided — nothing to update."
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return f"❌ Invalid date {date!r}. Use YYYY-MM-DD, or omit it to apply today."
    body = build_visibility_body(
        school_id, section_ids, visible, date or date_cls.today().isoformat()
    )
    resp, err = await _with_mfa_retry(
        app,
        context,
        lambda: app.client.send_json(
            "PUT", f"/api/v2/schools/{school_id}/sections/bulk_update_class_visibility", body
        ),
    )
    if err:
        return err
    return _json_write_result("set_class_visibility", args, resp)


@mcp.tool(name="add_staff")
@_write_gated
async def add_staff(
    school_id: int,
    first_name: str,
    last_name: str,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    staff_id: str | None = None,
    is_admin: bool = False,
    section_ids: list[int] | None = None,
    class_role: str = "TEACHER",
    context: Context[Any, Any] = None,
) -> str:
    """Add a teacher or other staff member to a school. Requires PS_ENABLE_WRITES.

    ParentSquare emails the new account an address-verification/registration
    invite. Passing section_ids assigns them to those classes right away (as
    class_role, default TEACHER) — verified live, because ParentSquare's own
    "invited classes" field does not create the assignment until the person
    registers, so this tool makes the assignment itself.

    Args:
        school_id: School ID
        first_name: Staff member's first name
        last_name: Staff member's last name
        email: Email address (optional but needed for them to register)
        phone: Phone number (optional)
        title: Title shown at the school, e.g. "3rd Grade Teacher" (optional)
        staff_id: The school's own staff/external ID (optional)
        is_admin: True to grant school Admin rather than Staff access
        section_ids: Class/section IDs to assign them to (from list_classes)
        class_role: Role for those classes — TEACHER (default) or ASSISTANT
    """
    app = _app(context)
    args = {"school_id": school_id, "first_name": first_name, "last_name": last_name,
            "email": email, "phone": phone, "title": title, "staff_id": staff_id,
            "is_admin": is_admin, "section_ids": section_ids, "class_role": class_role}
    resolved_role = None
    if section_ids:
        try:
            resolved_role = normalize_role(class_role)
        except ValueError as exc:
            return f"❌ {exc}"
    body = build_add_staff_body(
        school_id=school_id,
        first_name=first_name,
        last_name=last_name,
        email=email or "",
        phone=phone or "",
        staff_id=staff_id or "",
        title=title or "",
        role="ADMIN" if is_admin else "STAFF",
        section_ids=section_ids,
    )
    resp, err = await _with_mfa_retry(
        app, context, lambda: app.client.post_form(f"/schools/{school_id}/users", body)
    )
    if err:
        return err
    result = _write_result("add_staff", args, resp)
    user_id, lookup_err = None, ""
    if not result.startswith("✅"):
        if resp.status_code < 500:
            return result  # ParentSquare explicitly rejected the write
        # A 5xx is a crash, not a verdict — the record may well have committed
        # (see _write_result_verified). Read the roster back before reporting a
        # failure: a blind retry would create a duplicate staff member.
        user_id, lookup_err = await _find_new_staff_id(
            app, context, school_id, first_name, last_name, email
        )
        if not user_id:
            return result
        audit_write(
            "add_staff", args, True,
            detail=f"HTTP {resp.status_code} reported failure; read-back found the record",
        )
        result = (
            f"✅ Success (verified). Note: ParentSquare returned HTTP {resp.status_code}, "
            "but its error page is rendered after the record is saved and the staff "
            "member is on the roster — the account was created. Do NOT retry: a retry "
            "would create a duplicate."
        )
    created = f"{result} Added {first_name} {last_name}."
    if not section_ids:
        if user_id:
            return f"{created} user_id {user_id}."
        return f"{created} Find their user_id with list_staff."

    if not user_id:
        user_id, lookup_err = await _find_new_staff_id(
            app, context, school_id, first_name, last_name, email
        )
    if not user_id:
        return (
            f"{created} ⚠️ But their class assignment could not be completed: {lookup_err} "
            "Find them with list_staff and assign the classes with add_class_staff."
        )
    assigned, failed = [], []
    for section_id in section_ids:
        outcome = await add_class_staff(
            section_id, [user_id], resolved_role, context=context
        )
        (assigned if outcome.startswith(("✅", "ℹ️")) else failed).append(str(section_id))
    detail = f"{created} user_id {user_id}."
    if assigned:
        detail += f" Assigned as {resolved_role} to class(es) {', '.join(assigned)}."
    if failed:
        detail += (
            f" ⚠️ Could not assign class(es) {', '.join(failed)} — retry with add_class_staff."
        )
    return detail


@mcp.tool(name="edit_staff")
@_write_gated
async def edit_staff(
    school_id: int,
    user_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    staff_id: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Update a staff member's name, email, phone, title, or staff ID. Requires PS_ENABLE_WRITES.

    Only provided fields change; everything else — including their class
    assignments and STAFF/ADMIN access level — is left untouched. Get the
    user_id from list_staff(school_id). To change which classes they teach, use
    add_class_staff / remove_class_staff instead. Guardians are rejected: use
    edit_parent for those.

    Args:
        school_id: School ID
        user_id: Staff member's user ID (from list_staff)
        first_name: New first name (optional)
        last_name: New last name (optional)
        email: New email (optional)
        phone: New phone (optional)
        title: New title shown at the school, e.g. "3rd Grade Teacher" (optional)
        staff_id: The school's own staff/external ID (optional)
    """
    app = _app(context)
    args = {"school_id": school_id, "user_id": user_id, "first_name": first_name,
            "last_name": last_name, "email": email, "phone": phone, "title": title,
            "staff_id": staff_id}

    def _load():
        try:
            account_role = (
                app.client.get_json(f"/api/v2/schools/{school_id}/users/{user_id}")
                .get("data", {}).get("attributes", {}).get("role") or ""
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (403, 404):
                return {"not_found": True}
            raise
        fields = extract_staff_edit_fields(
            app.client.get_text(
                f"/schools/{school_id}/users/{user_id}/edit_institute_user",
                params={"role": "STAFF"},
            )
        )
        fields["account_role"] = account_role.upper()
        return fields

    current, err = await _with_mfa_retry(app, context, _load)
    if err:
        return err
    if current.get("not_found"):
        return f"❌ No user {user_id} at school {school_id}. Get the user_id from list_staff."
    if current.get("account_role") == "PARENT":
        return (
            f"❌ User {user_id} is a guardian, not a staff member. Use edit_parent instead "
            "(editing them as staff would create conflicting roles)."
        )
    if not current.get("first_name") and not current.get("last_name"):
        return f"❌ Could not load staff member {user_id} (not found or no edit access)."
    if not current.get("sua_id"):
        return (
            f"❌ Could not resolve the school record for staff member {user_id}; "
            "refusing the edit to avoid clearing their title and school access."
        )
    if (email is not None or phone is not None) and not current.get("contact_id"):
        return f"❌ Could not resolve contact record for staff member {user_id}; cannot update email/phone."
    if staff_id is not None and not current.get("staff_contact_id"):
        return f"❌ Could not resolve the staff ID field for staff member {user_id}."

    body = build_edit_staff_body(
        school_id=school_id,
        first_name=first_name if first_name is not None else current["first_name"],
        last_name=last_name if last_name is not None else current["last_name"],
        contact_id=current.get("contact_id", ""),
        sua_id=current["sua_id"],
        school_title=title if title is not None else current.get("school_title", ""),
        staff_contact_id=current.get("staff_contact_id", ""),
        staff_external_id=staff_id,
        email=email,
        phone=phone,
        role="ADMIN" if current.get("account_role") == "ADMIN" else "STAFF",
    )
    resp, err = await _with_mfa_retry(
        app, context,
        lambda: app.client.post_form(f"/schools/{school_id}/users/{user_id}/update_institute_user", body),
    )
    if err:
        return err
    return _write_result("edit_staff", args, resp)


@mcp.tool(name="add_class_staff")
@_write_gated
async def add_class_staff(
    section_id: int,
    user_ids: list[int],
    role: str,
    class_title: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Assign teachers, assistants, or room parents to a class. Requires PS_ENABLE_WRITES.

    SAFETY: Never call this tool or remove_class_staff in parallel, even for
    different sections. ParentSquare replaces the full staff list, and concurrent
    writes have caused unrelated associations to disappear. The server serializes
    calls within this process; still use one call at a time and verify with a fresh
    get_class read before the next class-staff write.

    The class's existing staff are read first and preserved — only the listed
    people are added, all in a single update. Anyone already holding the role is
    skipped; anyone on the class under a different role is moved to this one.
    Room parents are ordinary guardians: get their user_id from list_parents.
    Teachers and assistants come from list_staff.

    Args:
        section_id: Class/section ID (from list_classes)
        user_ids: One or more user IDs (list_staff for staff, list_parents for room parents)
        role: One of TEACHER, ASSISTANT, ROOM_PARENT — applied to everyone in user_ids
        class_title: Optional label shown on the class (defaults to the role name)
    """
    app = _app(context)
    args = {
        "section_id": section_id,
        "user_ids": user_ids,
        "role": role,
        "class_title": class_title,
    }
    if not user_ids:
        return "❌ No user_ids provided — nothing to add."
    try:
        resolved_role = normalize_role(role)
    except ValueError as exc:
        return f"❌ {exc}"
    wanted = list(dict.fromkeys(user_ids))
    async with _section_membership_write_lock(app):
        current, err = await _load_class(app, context, section_id)
        if err:
            return err

        by_user = {s.user_id: s for s in current.staff}
        already = [u for u in wanted if u in by_user and by_user[u].role == resolved_role]
        changing = [u for u in wanted if u not in already]
        if not changing:
            names = ", ".join(by_user[u].name or str(u) for u in already)
            return (
                f"ℹ️ Already {resolved_role} on \"{current.name}\": {names} — no change made."
            )

        staff = [s for s in current.staff if s.user_id not in changing]
        for user_id in changing:
            existing = by_user.get(user_id)
            staff.append(
                ClassStaff(
                    assoc_id=existing.assoc_id if existing else None,
                    user_id=user_id,
                    role=resolved_role,
                    class_title=class_title or default_class_title(resolved_role),
                    first_name=existing.first_name if existing else "",
                    last_name=existing.last_name if existing else "",
                )
            )
        result = await _put_class_staff(app, context, "add_class_staff", args, section_id, staff)
        if result.startswith("✅") and already:
            skipped = ", ".join(by_user[u].name or str(u) for u in already)
            return f"{result} ({len(already)} already had the role: {skipped}.)"
        return result


@mcp.tool(name="remove_class_staff")
@_write_gated
async def remove_class_staff(
    section_id: int,
    user_ids: list[int] | None = None,
    role: str | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Remove staff or room parents from a class. Requires PS_ENABLE_WRITES.

    SAFETY: Never call this tool or add_class_staff in parallel, even for
    different sections. ParentSquare replaces the full staff list, and concurrent
    writes have caused unrelated associations to disappear. The server serializes
    calls within this process; still use one call at a time and verify with a fresh
    get_class read before the next class-staff write.

    Pass user_ids to remove specific people, role to remove everyone holding that
    role on the class (e.g. role="ROOM_PARENT" clears that class's room parents),
    or both to remove only those people who hold that role. Everyone else on the
    class is preserved. To clear room parents school-wide, call list_classes and
    repeat this for each class with room_parent_count > 0.

    Args:
        section_id: Class/section ID (from list_classes)
        user_ids: One or more people to remove (optional if role is given)
        role: Remove all holders of this role: TEACHER, ASSISTANT, or ROOM_PARENT
    """
    app = _app(context)
    args = {"section_id": section_id, "user_ids": user_ids, "role": role}
    if not user_ids and not role:
        return "❌ Pass user_ids, a role, or both — refusing to remove all staff."
    resolved_role = None
    if role:
        try:
            resolved_role = normalize_role(role)
        except ValueError as exc:
            return f"❌ {exc}"
    targets = set(user_ids or ())
    async with _section_membership_write_lock(app):
        current, err = await _load_class(app, context, section_id)
        if err:
            return err

        def _matches(s) -> bool:
            return (not targets or s.user_id in targets) and (
                resolved_role is None or s.role == resolved_role
            )

        removing = [s for s in current.staff if _matches(s)]
        if not removing:
            return f"ℹ️ Nothing to remove — no matching staff on \"{current.name}\"."
        remaining = [s for s in current.staff if not _matches(s)]
        result = await _put_class_staff(
            app, context, "remove_class_staff", args, section_id, remaining
        )
        if result.startswith("✅"):
            names = ", ".join(s.name or str(s.user_id) for s in removing)
            return f"✅ Removed {len(removing)} from \"{current.name}\": {names}."
        return result


# ---------------------------------------------------------------------------
# Student class enrollment
# ---------------------------------------------------------------------------


def _class_students(client: PSClient, section_id: int):
    """Read a class roster. -> list[ClassStudent]."""
    return parse_class_students(client.get_json(f"/api/v2/sections/{section_id}/students"))


async def _load_class_students(app, context, section_id: int):
    """Read a class roster with MFA retry. -> (list[ClassStudent]|None, err)."""
    return await _with_mfa_retry(app, context, lambda: _class_students(app.client, section_id))


async def _load_sections_map(app, context, school_id: int):
    """Read the whole school's student -> classes map. -> (dict|None, err).

    One request covers every student, because there is no per-student read:
    ``GET /api/v2/students/{id}/sections`` 404s and the GraphQL profile exposes
    no section id.
    """
    return await _with_mfa_retry(
        app,
        context,
        lambda: parse_student_sections_map(
            app.client.get_html(f"/schools/{school_id}/roster/assign_classes")
        ),
    )


async def _replace_student_sections(app, context, tool: str, args: dict,
                                    student_id: int, section_ids: list[int]):
    """PUT a student's full class list (the endpoint replaces, never merges)."""
    body = build_student_sections_body(student_id, section_ids)
    resp, err = await _with_mfa_retry(
        app,
        context,
        lambda: app.client.send_json("PUT", f"/api/v2/students/{student_id}/sections", body),
    )
    if err:
        return err
    return _json_write_result(tool, args, resp)


@mcp.tool(name="list_class_students")
async def list_class_students(section_id: int, context: Context[Any, Any] = None) -> dict | str:
    """List the students enrolled in one class.

    Args:
        section_id: Class/section ID (from list_classes)
    """
    app = _app(context)
    students, err = await _load_class_students(app, context, section_id)
    if err:
        return err
    return {
        "section_id": section_id,
        "count": len(students),
        "students": [
            {
                "student_id": s.student_id,
                "name": s.name,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "student_sis_id": s.student_sis_id,
            }
            for s in students
        ],
    }


@mcp.tool(name="add_class_students")
@_write_gated
async def add_class_students(
    section_id: int,
    student_ids: list[int],
    context: Context[Any, Any] = None,
) -> str:
    """Enroll students in a class. Requires PS_ENABLE_WRITES.

    SAFETY: Never call this tool or another class-staff/student-enrollment write
    in parallel. The server serializes section-membership writes within this
    process; still use one call at a time and verify with a fresh read.

    Adds everyone listed in a single call without disturbing the students
    already in the class or their other classes. Students already enrolled are
    skipped, so re-running is safe.

    To assign a whole grade at the start of the year, call this once per
    classroom with that classroom's students — list_students(grade=...) gives
    the ids.

    Args:
        section_id: Class/section ID (from list_classes)
        student_ids: One or more student IDs (from list_students)
    """
    app = _app(context)
    args = {"section_id": section_id, "student_ids": student_ids}
    if not student_ids:
        return "❌ No student_ids provided — nothing to add."
    wanted = list(dict.fromkeys(student_ids))

    async with _section_membership_write_lock(app):
        current, err = await _load_class_students(app, context, section_id)
        if err:
            return err
        enrolled = {s.student_id for s in current}
        already = [s for s in wanted if s in enrolled]
        adding = [s for s in wanted if s not in enrolled]
        if not adding:
            return f"ℹ️ All {len(already)} already in the class — no change made."

        body = build_add_students_body(section_id, adding)
        resp, err = await _with_mfa_retry(
            app,
            context,
            lambda: app.client.send_json(
                "PUT", f"/api/v2/sections/{section_id}/add_students", body
            ),
        )
        if err:
            return err
        result = _json_write_result("add_class_students", args, resp)
        if not result.startswith("✅"):
            return result
        msg = f"✅ Added {len(adding)} student(s) to the class."
        if already:
            msg += f" ({len(already)} were already enrolled.)"
        return msg


@mcp.tool(name="remove_class_students")
@_write_gated
async def remove_class_students(
    school_id: int,
    section_id: int,
    student_ids: list[int],
    context: Context[Any, Any] = None,
) -> str:
    """Remove specific students from a class. Requires PS_ENABLE_WRITES.

    SAFETY: Never call this tool or another class-staff/student-enrollment write
    in parallel. The server serializes the complete school-map read and all
    resulting student writes within this process.

    Each student's other classes are read first and preserved — only this class
    is dropped. student_ids is required: this tool will not empty a class.

    Args:
        school_id: School ID the class belongs to
        section_id: Class/section ID (from list_classes)
        student_ids: The students to remove (from list_class_students)
    """
    app = _app(context)
    args = {"school_id": school_id, "section_id": section_id, "student_ids": student_ids}
    if not student_ids:
        return "❌ No student_ids provided — refusing to remove all students from the class."
    wanted = list(dict.fromkeys(student_ids))

    async with _section_membership_write_lock(app):
        current, err = await _load_class_students(app, context, section_id)
        if err:
            return err
        enrolled = {s.student_id: s for s in current}
        removing = [s for s in wanted if s in enrolled]
        if not removing:
            return "ℹ️ None of those students are in this class — no change made."

        sections_map, err = await _load_sections_map(app, context, school_id)
        if err:
            return err

        failures: list[str] = []
        removed: list[str] = []
        for student_id in removing:
            keep = [
                c["section_id"]
                for c in sections_map.get(student_id, [])
                if c["section_id"] != section_id
            ]
            result = await _replace_student_sections(
                app, context, "remove_class_students", {**args, "student_id": student_id},
                student_id, keep,
            )
            name = enrolled[student_id].name or str(student_id)
            if result.startswith("✅"):
                removed.append(name)
            else:
                failures.append(f"{name}: {result}")

        if failures and not removed:
            return "❌ Removed nobody. " + "; ".join(failures)
        msg = f"✅ Removed {len(removed)} student(s) from the class: {', '.join(removed)}."
        if failures:
            msg += f" ⚠️ {len(failures)} failed: " + "; ".join(failures)
        return msg


@mcp.tool(name="move_student_to_class")
@_write_gated
async def move_student_to_class(
    school_id: int,
    student_id: int,
    to_section_id: int,
    from_section_id: int | None = None,
    context: Context[Any, Any] = None,
) -> str:
    """Switch a student from one class to another. Requires PS_ENABLE_WRITES.

    SAFETY: Never call this tool or another class-staff/student-enrollment write
    in parallel. The student's full class list is replaced, so the server holds
    one global lock across the read, computation, and PUT within this process.

    Applies both halves of the switch in a single request. The student's other
    classes are preserved. Omit from_section_id to add the new class while
    keeping every current one.

    Args:
        school_id: School ID the classes belong to
        student_id: Student ID (from list_students)
        to_section_id: Class to move the student into (from list_classes)
        from_section_id: Class to move them out of (optional)
    """
    app = _app(context)
    args = {"school_id": school_id, "student_id": student_id,
            "to_section_id": to_section_id, "from_section_id": from_section_id}
    if from_section_id == to_section_id:
        return "❌ from_section_id and to_section_id are the same — nothing to do."

    async with _section_membership_write_lock(app):
        sections_map, err = await _load_sections_map(app, context, school_id)
        if err:
            return err
        current = [c["section_id"] for c in sections_map.get(student_id, [])]
        if from_section_id is not None and from_section_id not in current:
            return f"❌ Student {student_id} is not in class {from_section_id} — no change made."
        if to_section_id in current and from_section_id is None:
            return f"ℹ️ Student {student_id} is already in class {to_section_id} — no change made."

        keep = [s for s in current if s != from_section_id]
        if to_section_id not in keep:
            keep.append(to_section_id)
        result = await _replace_student_sections(
            app, context, "move_student_to_class", args, student_id, keep
        )
        if not result.startswith("✅"):
            return result
        if from_section_id is None:
            return f"✅ Added student {student_id} to class {to_section_id}."
        return f"✅ Moved student {student_id} from class {from_section_id} to {to_section_id}."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
