from __future__ import annotations

import re

from bs4 import BeautifulSoup

from parentsquare_mcp.models import Attachment, Conversation, Message


def parse_conversation_list(soup: BeautifulSoup) -> list[Conversation]:
    """Parse /schools/{id}/users/{uid}/chats -> list of Conversation.

    Structure:
      #chat-threads-container > a.a-chat-thread
        .users span[class^=user-thread-chat-name-]  (participant names)
        .badge.badge-outline  (message count)
        div.date  (date text)
        div.chat-message .reaction-icons  (preview text)
        fa-user = DM, fa-users = group chat
    """
    convos: list[Conversation] = []
    container = soup.find("div", id="chat-threads-container")
    if not container:
        return convos

    for link in container.find_all("a", class_=re.compile(r"a-chat-thread")):
        # Thread ID from class or href
        href = link.get("href", "")
        chat_id = 0
        id_match = re.search(r"/chats/(\d+)", href)
        if id_match:
            chat_id = int(id_match.group(1))
        if not chat_id:
            # Try from class name
            class_match = re.search(r"a-chat-thread-(\d+)", " ".join(link.get("class", [])))
            if class_match:
                chat_id = int(class_match.group(1))

        # Participants
        name_span = link.find("span", class_=re.compile(r"user-thread-chat-name"))
        participants = []
        if name_span:
            name_text = name_span.get_text(strip=True)
            # May have multiple names comma-separated
            participants = [n.strip() for n in name_text.split(",") if n.strip()]

        # Date
        date_div = link.find("div", class_="date")
        date = ""
        if date_div:
            # Get text but skip the reply icon
            date = date_div.get_text(strip=True)

        # Preview text
        preview_el = link.find("span", class_="reaction-icons")
        preview = preview_el.get_text(strip=True) if preview_el else ""
        if not preview:
            chat_msg = link.find("div", class_="chat-message")
            if chat_msg:
                preview = chat_msg.get_text(strip=True)

        # Message count from badge
        badge = link.find("span", class_="badge")
        msg_count = 0
        if badge:
            count_text = badge.get_text(strip=True)
            if count_text.isdigit():
                msg_count = int(count_text)

        # Unread — check aria-label or class hints
        aria = link.get("aria-label", "")
        unread = "unread" in aria.lower()

        convos.append(
            Conversation(
                id=chat_id,
                participants=participants,
                last_message_preview=preview[:200],
                date=date,
                unread=unread,
            )
        )

    return convos


def parse_chat_thread(soup: BeautifulSoup) -> list[Message]:
    """Parse /schools/{id}/users/{uid}/chats/{chat_id} -> list of Message.

    Structure:
      .ps-chat-right-box
        .chat-day-header > .chat-day-header-text  (date separator)
        div.chat-message.chat-box.chat-thread  (individual messages)
          .user a  (author)
          .chat-message.row .col-xs-12 p  (message text)
          .chat-message-timestamp .date  (time)
    """
    messages: list[Message] = []

    chat_box = soup.find("div", class_="ps-chat-right-box")
    if not chat_box:
        return messages

    current_date = ""

    for el in chat_box.find_all(["div"], recursive=True):
        classes = " ".join(el.get("class", []))

        # Day header — updates current_date context
        if "chat-day-header-text" in classes:
            current_date = el.get_text(strip=True)
            continue

        # Individual message — must have chat-box and chat-thread classes
        if "chat-box" in classes and "chat-thread" in classes and el.get("id", "").startswith("chat-message-"):
            # Author
            user_span = el.find("span", class_="user")
            author_el = user_span.find("a") if user_span else None
            author = author_el.get_text(strip=True) if author_el else ""

            # Message text
            msg_row = el.find("div", class_="chat-message")
            if msg_row:
                # Find the nested chat-message row (not the outer one)
                inner = msg_row.find("div", class_="col-xs-12")
                text = inner.get_text(strip=True) if inner else msg_row.get_text(strip=True)
            else:
                text = ""

            # Timestamp
            ts_el = el.find("div", class_="chat-message-timestamp")
            time_str = ""
            if ts_el:
                date_span = ts_el.find("span", class_="date")
                time_str = date_span.get_text(strip=True) if date_span else ts_el.get_text(strip=True)

            # Combine date and time
            full_date = f"{current_date} {time_str}".strip() if current_date else time_str

            # Attachments in messages
            attachments: list[Attachment] = []
            for img in el.find_all("img"):
                src = img.get("src", "")
                if src and "parentsquare" in src:
                    attachments.append(
                        Attachment(name="image", url=src, file_type="image")
                    )
            for a_tag in el.find_all("a", href=True):
                href = a_tag["href"]
                if "s3.amazonaws.com" in href or "response-content-disposition" in href:
                    attachments.append(
                        Attachment(
                            name=a_tag.get_text(strip=True) or "file",
                            url=href,
                            file_type="document",
                        )
                    )

            if text or attachments:
                messages.append(
                    Message(
                        author=author,
                        date=full_date,
                        text=text,
                        attachments=attachments,
                    )
                )

    return messages
