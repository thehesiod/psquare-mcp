from __future__ import annotations

BASE_URL = "https://www.parentsquare.com"

URLS = {
    "signin": "/signin",
    "feeds": "/schools/{school_id}/feeds",
    "post": "/feeds/{feed_id}",
    "chats": "/schools/{school_id}/users/{user_id}/chats",
    "chat": "/schools/{school_id}/users/{user_id}/chats/{chat_id}",
    "calendar": "/schools/{school_id}/calendars",
    "calendar_ics": "/schools/{school_id}/calendars.ics",
    "photos": "/schools/{school_id}/feeds/photos",
    "videos": "/schools/{school_id}/feeds/videos",
    "files": "/schools/{school_id}/feeds/files",
    "directory": "/schools/{school_id}/users",
    "groups": "/schools/{school_id}/groups",
    "group_feed": "/schools/{school_id}/groups/{group_id}/feeds",
    "student_dashboard": "/students/{student_id}/dashboard",
    "sign_ups": "/schools/{school_id}/sign_ups",
    "notices": "/schools/{school_id}/notices",
    "polls": "/schools/{school_id}/polls",
    "links": "/schools/{school_id}/links",
    "payments": "/schools/{school_id}/feeds/payment_feeds",
    "volunteer_hours": "/schools/{school_id}/users/{user_id}/volunteer_records/new",
    "forms": "/schools/{school_id}/signable_forms",
}

DEFAULT_DOWNLOAD_DIR = "~/Downloads/parentsquare"
