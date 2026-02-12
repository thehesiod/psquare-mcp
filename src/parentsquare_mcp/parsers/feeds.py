from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, Tag

from parentsquare_mcp.models import Attachment, Comment, FeedPost, PostDetail, SignupItem

# Random hash prefix pattern: 20+ alphanumeric chars followed by underscore
_HASH_PREFIX_RE = re.compile(r"^[a-zA-Z0-9]{16,}_")


def _filename_from_url(url: str) -> str:
    """Extract a human-readable filename from a URL, URL-decoding it."""
    try:
        parsed = urlparse(url)
        path = parsed.path
        name = unquote(PurePosixPath(path).name)
        # + is used as space in some URL encodings
        name = name.replace("+", " ")
        # Strip leading timestamp prefixes like "inline_images_1770847245492-"
        name = re.sub(r"^inline_images_\d+-?", "", name)
        # Strip "thumb_" prefix from CDN thumbnail URLs
        name = re.sub(r"^thumb_", "", name)
        # Strip random hash prefixes like "fUMwIIoARyb7Gs2reHSA_"
        name = _HASH_PREFIX_RE.sub("", name)
        # If only an extension remains (e.g. ".png"), treat as generic image
        if not name.strip() or re.match(r"^\.\w+$", name.strip()):
            return "image"
        return name.strip()
    except Exception:
        return "file"


def _disposition_filename(url: str) -> str | None:
    """Extract the original upload filename from a response-content-disposition query param."""
    try:
        qs = parse_qs(urlparse(url).query)
        disp = unquote(qs.get("response-content-disposition", [""])[0])
        m = re.search(r'filename="?([^"]+)"?', disp)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _url_path_key(url: str) -> str:
    """Normalize a URL to just its path for cross-referencing thumbnail vs download URLs."""
    return unquote(urlparse(url).path)


def parse_feed_page(soup: BeautifulSoup) -> list[FeedPost]:
    """Parse /schools/{id}/feeds?page=N -> list of FeedPost.

    Feed structure:
      #feeds-list > div.ps-box > div.feed > div#feed_{id} > div.feed-show
        .feed-title > .subject a span[role=heading]  (title)
        .feed-metadata > .user-name  (author)
        .feed-metadata > .time-ago[data-timestamp]  (date)
        .description.truncated-text  (summary text)
        .nav-pills li  (actions: Appreciate, Comment, Print, Download All/File)
        .appreciations-box  (appreciation count)
        img.feed-image-thumbnail  (has attachments)
    """
    posts: list[FeedPost] = []
    feeds_list = soup.find("div", id="feeds-list")
    if not feeds_list:
        return posts

    for ps_box in feeds_list.find_all("div", class_="ps-box", recursive=False):
        feed_id_el = ps_box.find("div", id=re.compile(r"^feed_\d+"))
        if not feed_id_el:
            continue

        feed_id = int(feed_id_el["id"].replace("feed_", ""))

        # Title — can be <span> or <div> with role="heading"
        heading = ps_box.find(attrs={"role": "heading"})
        title = heading.get_text(strip=True) if heading else ""

        # Author
        author_el = ps_box.find("a", class_="user-name")
        author = author_el.get_text(strip=True) if author_el else ""

        # Date — prefer data-timestamp attribute, fall back to text
        time_el = ps_box.find("span", class_="time-ago")
        date = ""
        if time_el:
            date = time_el.get("data-timestamp", "") or time_el.get_text(strip=True)

        # Summary — prefer expanded (full) text over truncated description.
        # ParentSquare includes both in the HTML; the expanded version is hidden via CSS.
        summary = ""
        expanded_el = ps_box.find("div", class_="expanded-text")
        if expanded_el:
            desc_in_expanded = expanded_el.find("div", class_="description")
            if desc_in_expanded:
                summary = desc_in_expanded.get_text(strip=True)
        if not summary:
            desc_el = ps_box.find("div", class_="description")
            if desc_el:
                summary = desc_el.get_text(strip=True)
        summary = re.sub(r"\s*Read More\s*$", "", summary)
        # Keep desc_el reference for inline image extraction below
        desc_el = ps_box.find("div", class_="description")

        # Attachments: check for Download All/File action, thumbnails, or inline images
        nav_pills = ps_box.find("ul", class_="nav-pills")
        actions_text = nav_pills.get_text() if nav_pills else ""
        has_attachments = "Download" in actions_text or bool(ps_box.find("img", class_="feed-image-thumbnail"))

        # Extract attachment filenames from inline images in description
        attachment_names: list[str] = []
        if desc_el:
            for img in desc_el.find_all("img"):
                src = img.get("src", "")
                if src and "avatar" not in src and "logo" not in src:
                    has_attachments = True
                    name = _filename_from_url(src)
                    if name not in ("file", "image"):
                        attachment_names.append(name)

        # Also extract filenames from gallery/thumbnail images (e.g. attached calendars)
        for img in ps_box.find_all("img", class_="feed-image-thumbnail"):
            url = img.get("fallback", "") or img.get("src", "")
            if url:
                name = _filename_from_url(url)
                if name not in ("file", "image") and name not in attachment_names:
                    attachment_names.append(name)

        # Comment count — look for comment count badge or infer from presence
        comment_count = 0
        comments_box = ps_box.find("div", class_="comments-box")
        if comments_box:
            # Try to find a count in the comments area
            count_match = re.search(r"(\d+)\s+comment", comments_box.get_text(), re.IGNORECASE)
            if count_match:
                comment_count = int(count_match.group(1))

        # Appreciation count
        app_box = ps_box.find("div", class_=re.compile(r"appreciations-box"))
        app_text = app_box.get_text(strip=True) if app_box else ""
        app_match = re.search(r"(\d+)\s+(?:people|person)", app_text)
        appreciate_count = int(app_match.group(1)) if app_match else 0

        # Signup progress — from color-highlight divs in metadata (e.g. "53/103 Items", "6/14 Sign Ups")
        # Only capture summary-level counters (N/N pattern), not per-item "Xof Y filled" rows
        signup_progress = ""
        metadata_el = ps_box.find("div", class_="feed-metadata")
        if metadata_el:
            highlights = metadata_el.find_all("div", class_="color-highlight", recursive=False)
            parts = [h.get_text(strip=True) for h in highlights
                     if h.get_text(strip=True) and re.match(r"\d+/\d+", h.get_text(strip=True))]
            signup_progress = " • ".join(parts)

        # Post type — from feed icon
        post_type = ""
        feed_icon = ps_box.find("div", class_="feed-icon")
        if feed_icon:
            icon = feed_icon.find("i")
            if icon:
                classes = icon.get("class", [])
                if "fa-thumb-tack" in classes:
                    post_type = "pinned"
                elif "fa-camera" in classes:
                    post_type = "photo"
                elif "fa-calendar" in classes:
                    post_type = "event"

        posts.append(
            FeedPost(
                id=feed_id,
                title=title,
                author=author,
                date=date,
                summary=summary,
                comment_count=comment_count,
                has_attachments=has_attachments,
                post_type=post_type,
                attachment_names=attachment_names,
                signup_progress=signup_progress,
            )
        )

    return posts


def parse_post_detail(soup: BeautifulSoup) -> PostDetail:
    """Parse /feeds/{id} -> PostDetail with body, comments, and attachments.

    Detail page structure:
      .feed-show > .feed-title > .subject span[role=heading]  (title)
      .feed-show > .feed-metadata > .user-name  (author)
      .feed-show > .feed-metadata > .time-ago  (date)
      .feed-show > .description  (body — NOT .truncated-text on detail page)
      .feed-show > .comments-box > .comments > div  (comments)
      img.feed-image-thumbnail  (image attachments)
      .nav-pills a[href*=download]  (file attachments)
    """
    feed_show = soup.find("div", class_="feed-show")

    # Feed ID from URL or id attribute
    feed_id_el = soup.find("div", id=re.compile(r"^feed_\d+"))
    feed_id = int(feed_id_el["id"].replace("feed_", "")) if feed_id_el else 0

    # Title — can be <span> or <div> with role="heading"
    # Use feed_show scope to avoid picking up the school name heading
    # Polls don't use role=heading — fall back to .subject > a or direct text
    heading = feed_show.find(attrs={"role": "heading"}) if feed_show else soup.find(attrs={"role": "heading"})
    title = heading.get_text(strip=True) if heading else ""
    if not title and feed_show:
        subject = feed_show.find("div", class_="subject")
        if subject:
            link = subject.find("a", recursive=False)
            title = link.get_text(strip=True) if link else ""
            if not title:
                # Direct text content (poll detail has question as text node)
                for child in subject.children:
                    if isinstance(child, str) and child.strip():
                        title = child.strip()
                        break

    # Author
    author_el = soup.find("a", class_="user-name")
    author = author_el.get_text(strip=True) if author_el else ""

    # Date
    time_el = soup.find("span", class_="time-ago")
    date = ""
    if time_el:
        date = time_el.get("data-timestamp", "") or time_el.get_text(strip=True)

    # Body text — on detail page, the visible description does NOT have truncated-text class
    # Try the expanded text first, then the non-truncated description
    body_text = ""
    if feed_show:
        # Look for visible .description that's NOT inside .expanded-text
        for desc in feed_show.find_all("div", class_="description", recursive=True):
            parent_classes = " ".join(desc.parent.get("class", []))
            # Skip the hidden expanded text container version
            if "expanded-text" in parent_classes and "display: none" in str(desc.parent.get("style", "")):
                continue
            # Skip truncated text if expanded version exists
            if "truncated-text" in " ".join(desc.get("class", [])):
                expanded = feed_show.find("div", class_="expanded-text")
                if expanded and "display: none" not in str(expanded.get("style", "")):
                    continue
            text = desc.get_text(strip=True)
            if text and len(text) > len(body_text):
                body_text = text

    # Clean up body text
    body_text = re.sub(r"\s*Read More\s*$", "", body_text)

    # Build a path→filename map from S3 download links (they have original upload names)
    download_names: dict[str, str] = {}  # url_path -> original filename
    download_urls: dict[str, str] = {}  # url_path -> full S3 URL
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "s3.amazonaws.com" in href or "response-content-disposition" in href:
            path_key = _url_path_key(href)
            disp_name = _disposition_filename(href)
            if disp_name:
                download_names[path_key] = disp_name
            download_urls[path_key] = href

    # Attachments — gallery/thumbnail images (e.g. photo posts)
    attachments: list[Attachment] = []
    seen_paths: set[str] = set()  # dedupe by normalized path
    for img in soup.find_all("img", class_="feed-image-thumbnail"):
        thumb_url = img.get("src", "")
        full_url = img.get("fallback", "") or thumb_url
        path_key = _url_path_key(full_url)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        # Prefer original filename from download link, fall back to URL parsing
        name = download_names.get(path_key) or _filename_from_url(full_url)
        # Use S3 download URL if available (direct download), else CDN URL
        best_url = download_urls.get(path_key, full_url)
        attachments.append(
            Attachment(
                name=name,
                url=best_url,
                file_type="image",
                thumbnail_url=thumb_url,
            )
        )

    # Attachments — inline images embedded in description (common for image-only posts)
    if feed_show:
        for desc in feed_show.find_all("div", class_="description"):
            for img in desc.find_all("img"):
                src = img.get("src", "")
                if not src:
                    continue
                path_key = _url_path_key(src)
                if path_key in seen_paths:
                    continue
                # Skip tiny icons/avatars
                if "avatar" in src or "logo" in src:
                    continue
                seen_paths.add(path_key)
                name = download_names.get(path_key) or _filename_from_url(src)
                attachments.append(
                    Attachment(
                        name=name,
                        url=src,
                        file_type="image",
                    )
                )

    # Attachments — files (download links not already captured as images)
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not ("s3.amazonaws.com" in href or "response-content-disposition" in href):
            continue
        path_key = _url_path_key(href)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        name = _disposition_filename(href)
        if not name:
            name = link.get_text(strip=True)
            name = re.sub(r"^Download\s*", "", name).strip() or _filename_from_url(href)
        attachments.append(
            Attachment(
                name=name,
                url=href,
                file_type="document",
            )
        )

    # Comments
    comments: list[Comment] = []
    comments_box = soup.find("div", class_="comments-box")
    if comments_box:
        comments_container = comments_box.find("div", class_="comments")
        if comments_container:
            for comment_div in comments_container.find_all("div", recursive=False):
                if not isinstance(comment_div, Tag):
                    continue
                # Each comment has author, date, text
                c_author_el = comment_div.find("a", class_="user-name") or comment_div.find("a")
                c_author = c_author_el.get_text(strip=True) if c_author_el else ""
                c_date_el = comment_div.find("span", class_="time-ago") or comment_div.find("span", class_="date")
                c_date = ""
                if c_date_el:
                    c_date = c_date_el.get("data-timestamp", "") or c_date_el.get_text(strip=True)
                c_text = comment_div.get_text(strip=True)
                # Remove author and date from text
                if c_author and c_text.startswith(c_author):
                    c_text = c_text[len(c_author) :].strip()
                if c_text:
                    comments.append(Comment(author=c_author, date=c_date, text=c_text))

    # Signup items — from volunteer-list / wish-list structure
    signup_items: list[SignupItem] = []
    vol_list = soup.find("div", class_="volunteer-list")
    if vol_list:
        for row in vol_list.find_all("div", class_="wish-list-item-row"):
            # Item name
            name_el = row.find("div", class_="wish-list-item-name")
            item_name = name_el.get_text(strip=True) if name_el else ""

            # Time slot (optional — volunteer slots have times, wish-list items don't)
            time_el = row.find("div", class_="wish-list-item-time")
            time_slot = time_el.get_text(strip=True) if time_el else ""

            # Filled / total counts
            filled = 0
            total = 0
            filled_el = row.find("span", class_="count-filled")
            if filled_el:
                try:
                    filled = int(filled_el.get_text(strip=True))
                except ValueError:
                    pass
            # Parse total from "X of Y filled" text in the quantity area
            qty_el = row.find("div", class_="wish-list-item-quantity")
            if qty_el:
                qty_match = re.search(r"(\d+)\s+of\s+(\d+)", qty_el.get_text())
                if qty_match:
                    filled = int(qty_match.group(1))
                    total = int(qty_match.group(2))
            # Fallback: parse from "N open" + filled
            if not total:
                open_el = row.find("div", class_="count-open")
                if open_el:
                    open_match = re.search(r"(\d+)\s+open", open_el.get_text())
                    if open_match:
                        total = filled + int(open_match.group(1))

            # People signed up
            signed_up: list[str] = []
            signups_el = row.find("div", class_="wish-list-item-sign-ups")
            if signups_el:
                for entry in signups_el.find_all("li", class_="col-sign-ups-entry"):
                    person = entry.get_text(strip=True).lstrip("•").strip()
                    if person:
                        signed_up.append(person)

            if item_name:
                signup_items.append(SignupItem(
                    name=item_name,
                    time_slot=time_slot,
                    filled=filled,
                    total=total,
                    signed_up=signed_up,
                ))

    return PostDetail(
        id=feed_id,
        title=title,
        author=author,
        date=date,
        body_text=body_text,
        comments=comments,
        attachments=attachments,
        signup_items=signup_items,
    )
