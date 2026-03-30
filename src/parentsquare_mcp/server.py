from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field

from parentsquare_mcp.auth import MFARequiredError, MFAState, load_cookies, submit_mfa
from parentsquare_mcp.client import PSClient
from parentsquare_mcp.config import DEFAULT_DOWNLOAD_DIR, URLS
from parentsquare_mcp.download import download_file as do_download
from parentsquare_mcp.parsers.calendar import parse_ics_calendar

from parentsquare_mcp.parsers.feeds import parse_feed_page, parse_post_detail
from parentsquare_mcp.models import Group
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


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Set up session and yield client. Auth is fully lazy — cookies are loaded
    if available, but 1Password and login only happen on the first actual request
    that needs authentication (via PSClient._relogin).
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # Pre-load saved cookies if available (no network call, no 1Password)
    load_cookies(session)

    client = PSClient(session=session)
    download_dir = Path(os.environ.get("PS_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).expanduser()

    # Restore pending MFA state from disk (survives server restarts)
    mfa_state = MFAState.load()
    if mfa_state:
        logger.info("Restored pending MFA state from disk")

    yield AppContext(client=client, download_dir=download_dir, mfa_state=mfa_state)


mcp = FastMCP("ParentSquare", lifespan=app_lifespan)


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
            return ""  # empty string signals success — caller should retry
        elif result.action == "decline":
            return "MFA verification declined. Use submit_mfa_code tool later to complete login."
        else:  # cancel
            return "MFA verification cancelled. Use submit_mfa_code tool later to complete login."
    except McpError:
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
        logger.debug(f"Failed to fetch image: {url}", exc_info=True)
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
        logger.debug(f"Failed to extract PDF text: {url}", exc_info=True)
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
          userCount
          activeFeedsCount
          lastPostAt
          hasUserOrStudent
          feedsPath
        }
      }
    }
  }
}
"""


@mcp.tool(name="list_groups")
async def list_groups(school_id: int, context: Context[Any, Any] = None) -> str:
    """List groups at a school with member counts and descriptions.

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
    groups: list[Group] = []
    for cat in cat_groups:
        cat_name = cat.get("name", "")
        for g in cat.get("groups", []):
            groups.append(Group(
                id=g["id"],
                name=g["name"],
                member_count=g.get("userCount", 0),
                description=g.get("description"),
                category=cat_name,
                post_count=g.get("activeFeedsCount", 0),
                is_member=g.get("hasUserOrStudent", False),
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
        member_tag = "★ member" if g.is_member else ""
        lines.append(f"**[group_id={g.id}] {g.name}** ({g.member_count} members, {g.post_count} posts) {member_tag}")
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
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
