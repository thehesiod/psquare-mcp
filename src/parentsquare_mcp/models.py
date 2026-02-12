from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class School:
    id: int
    name: str
    features: list[str] = field(default_factory=list)


@dataclass
class Student:
    id: int
    name: str
    school_id: int
    grade: str | None = None


@dataclass
class FeedPost:
    id: int
    title: str
    author: str
    date: str
    summary: str
    comment_count: int = 0
    has_attachments: bool = False
    post_type: str = ""
    attachment_names: list[str] = field(default_factory=list)
    signup_progress: str = ""  # e.g. "53/103 Items • 6/14 Sign Ups"


@dataclass
class Attachment:
    name: str
    url: str
    file_type: str  # "image", "document", "video"
    thumbnail_url: str | None = None


@dataclass
class Comment:
    author: str
    date: str
    text: str


@dataclass
class SignupItem:
    name: str              # "Donate one 48 oz container of Goldfish"
    time_slot: str = ""    # "08:00 AM to 08:30 AM" or ""
    filled: int = 0
    total: int = 0
    signed_up: list[str] = field(default_factory=list)


@dataclass
class PostDetail:
    id: int
    title: str
    author: str
    date: str
    body_text: str
    comments: list[Comment] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    signup_items: list[SignupItem] = field(default_factory=list)


@dataclass
class Conversation:
    id: int
    participants: list[str] = field(default_factory=list)
    last_message_preview: str = ""
    date: str = ""
    unread: bool = False


@dataclass
class Message:
    author: str
    date: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class CalendarEvent:
    title: str
    start: str  # ISO datetime
    end: str | None = None
    location: str | None = None
    description: str | None = None
    all_day: bool = False


@dataclass
class MediaItem:
    id: int
    url: str
    title: str
    date: str
    file_type: str
    thumbnail_url: str | None = None


@dataclass
class DirectoryEntry:
    name: str
    role: str
    email: str | None = None
    phone: str | None = None


@dataclass
class Group:
    id: int
    name: str
    member_count: int = 0
    description: str | None = None
    category: str = ""
    post_count: int = 0
    is_member: bool = False


@dataclass
class Notice:
    title: str
    notice_type: str  # "alert" or "document"
    date: str
    school: str = ""


@dataclass
class PollOption:
    text: str
    votes: int = 0
    is_winner: bool = False


@dataclass
class Poll:
    id: int
    question: str
    author: str
    date: str
    options: list[PollOption] = field(default_factory=list)
    total_votes: int = 0
    user_voted: bool = False


@dataclass
class SchoolLink:
    name: str
    url: str
    school: str = ""


@dataclass
class VolunteerRecord:
    month: str          # "Dec 2025"
    activity: str       # "School Event"
    note: str = ""      # "robotics"
    hours: str = ""     # "2:30 hrs"


@dataclass
class PaymentItem:
    name: str           # "one child - one day"
    price: str          # "$40"


@dataclass
class PaymentPost:
    id: int
    title: str
    author: str
    date: str
    sections: list[tuple[str, list[PaymentItem]]] = field(default_factory=list)  # (section_name, items)


@dataclass
class PaymentSummary:
    upcoming: int = 0
    paid_for: int = 0
    total_paid: str = ""    # "$250"
    posts: list[PaymentPost] = field(default_factory=list)


@dataclass
class StudentDashboard:
    student_name: str
    school_name: str
    grade: str | None = None
    teachers: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
