from __future__ import annotations

import re

from bs4 import BeautifulSoup

from parentsquare_mcp.models import MediaItem


def _parse_accordion_page(soup: BeautifulSoup, media_type: str) -> list[MediaItem]:
    """Parse a photos/videos/files accordion page.

    Common structure for photos, videos, files tabs:
      div.ps-accordion > div.ps-accordion-group.manage-box#feed{id}
        .ps-accordion-heading .subject  (post title)
        .note-at-top a  (author)
        .note-at-top text  (date info)
        ul.attachments li  (individual items)
    """
    items: list[MediaItem] = []
    accordion = soup.find("div", class_="ps-accordion")
    if not accordion:
        return items

    for group in accordion.find_all("div", class_="ps-accordion-group"):
        group_id_str = group.get("id", "")
        feed_id_match = re.search(r"feed(\d+)", group_id_str)
        feed_id = int(feed_id_match.group(1)) if feed_id_match else 0

        # Post title
        subject = group.find("a", class_="subject")
        post_title = ""
        if subject:
            post_title = subject.get_text(strip=True)
            # Remove folder icon text
            post_title = re.sub(r"^\s*\S\s*", "", post_title).strip() or post_title

        # Date from note-at-top
        note = group.find("div", class_="note-at-top")
        date = ""
        if note:
            note_text = note.get_text(strip=True)
            # Extract date: typically "Author • Updated X ago • Month Day, Year • School"
            date_match = re.search(r"(\w+ \d+,\s*\d{4})", note_text)
            if date_match:
                date = date_match.group(1)
            else:
                # Try relative date
                updated_match = re.search(r"Updated\s+(.+?)(?:\s*[•·]|$)", note_text)
                if updated_match:
                    date = updated_match.group(1).strip()

        # Author from note-at-top first link
        author = ""
        if note:
            author_link = note.find("a")
            if author_link:
                author = author_link.get_text(strip=True)

        # Individual items in ul.attachments
        attachments_ul = group.find("ul", class_="attachments")
        if not attachments_ul:
            continue

        for li in attachments_ul.find_all("li"):
            if media_type == "image":
                # Photo: li.image > a.thumbnail > img.feed-image-thumbnail
                img = li.find("img", class_="feed-image-thumbnail")
                if not img:
                    continue
                thumb_url = img.get("src", "")
                full_url = img.get("fallback", "") or thumb_url
                link = li.find("a", class_="thumbnail")
                asset_id = link.get("data-asset-id", "") if link else ""
                name = asset_id or post_title

                items.append(
                    MediaItem(
                        id=feed_id,
                        url=full_url,
                        thumbnail_url=thumb_url,
                        title=name,
                        date=date,
                        file_type="image",
                    )
                )
            else:
                # File: li > div > a[target=_blank] (download link)
                link = li.find("a", href=True)
                if not link:
                    continue
                href = link.get("href", "")
                name = link.get_text(strip=True)
                # Clean up: remove "Download " prefix and icon text
                name = re.sub(r"^Download\s*", "", name).strip() or "file"

                file_type = "document"
                lower_name = name.lower()
                if any(ext in lower_name for ext in [".jpg", ".jpeg", ".png", ".gif"]):
                    file_type = "image"
                elif any(ext in lower_name for ext in [".mp4", ".mov", ".avi"]):
                    file_type = "video"

                items.append(
                    MediaItem(
                        id=feed_id,
                        url=href,
                        thumbnail_url=None,
                        title=name,
                        date=date,
                        file_type=file_type,
                    )
                )

    return items


def parse_photos_page(soup: BeautifulSoup) -> list[MediaItem]:
    """Parse /schools/{id}/feeds/photos?page=N"""
    return _parse_accordion_page(soup, "image")


def parse_videos_page(soup: BeautifulSoup) -> list[MediaItem]:
    """Parse /schools/{id}/feeds/videos"""
    return _parse_accordion_page(soup, "video")


def parse_files_page(soup: BeautifulSoup) -> list[MediaItem]:
    """Parse /schools/{id}/feeds/files"""
    return _parse_accordion_page(soup, "file")
