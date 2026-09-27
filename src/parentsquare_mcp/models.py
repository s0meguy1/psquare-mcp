from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
class Link:
    """A link a reader can follow: its visible text and absolute href."""

    text: str
    href: str


@dataclass
class EventInfo:
    """What a post's calendar dropdown says about its event (or RSVP date).

    ``start``/``end`` are exact (UTC) when the post carries a Google Calendar
    link, which encodes them; ``label`` is the site's own summary either way.
    """

    start: datetime | None = None
    end: datetime | None = None
    location: str | None = None
    title: str = ""
    label: str = ""               # "Sep 24, Thursday 6:30 PM To 8:00 PM"
    ics_url: str | None = None    # /feeds/{id}/add_cal.ics
    rsvp: str | None = None       # this parent's RSVP when the post shows one ("yes"/"no"/"maybe")


@dataclass
class FormInfo:
    """A signable form or permission slip attached to a post."""

    title: str = ""
    due: str = ""                 # "Complete by Wednesday, Sep 23"
    note: str = ""                # "Note: No action required. Form deadline has passed"
    closed: bool = False          # the form's fields are disabled
    signed: bool | None = None    # True/False when the page says so, None when it does not
    questions: list[str] = field(default_factory=list)


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
    posted_at: datetime | None = None   # from data-timestamp; ``date`` keeps the raw string
    links: list[Link] = field(default_factory=list)
    is_pinned: bool = False
    kinds: list[str] = field(default_factory=list)  # every kind seen: event, signup, form, poll, ...
    event: EventInfo | None = None
    form: FormInfo | None = None
    is_urgent: bool = False


@dataclass
class Attachment:
    name: str
    url: str
    file_type: str  # "image", "document", "video"
    thumbnail_url: str | None = None
    alt: str | None = None  # the image's alt text / caption, when the poster gave one


@dataclass
class Comment:
    author: str
    date: str
    text: str
    posted_at: datetime | None = None
    id: int | None = None
    links: list[Link] = field(default_factory=list)
    replies: list[Comment] = field(default_factory=list)


@dataclass
class SignupItem:
    name: str              # "Donate one 48 oz container of Goldfish"
    time_slot: str = ""    # "08:00 AM to 08:30 AM" or ""
    filled: int = 0
    total: int = 0
    signed_up: list[str] = field(default_factory=list)
    date: str = ""         # the day heading the slot sits under, e.g. "Thursday, Oct 1"
    status: str = ""       # "open", "closed" (full) or "mine"
    mine: bool = False     # this parent holds the slot


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
    posted_at: datetime | None = None
    links: list[Link] = field(default_factory=list)
    is_pinned: bool = False
    kinds: list[str] = field(default_factory=list)
    event: EventInfo | None = None
    form: FormInfo | None = None
    is_urgent: bool = False
    comment_count: int | None = None  # the page's own "N comments on this post", when shown


@dataclass
class Conversation:
    id: int
    participants: list[str] = field(default_factory=list)
    last_message_preview: str = ""
    date: str = ""
    unread: bool = False
    is_group: bool = False
    message_count: int = 0
    last_message_at: datetime | None = None


@dataclass
class Message:
    author: str
    date: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    id: int | None = None
    chat_id: int | None = None
    is_mine: bool | None = None   # sent by this parent (True) or received (False)
    posted_at: datetime | None = None
    links: list[Link] = field(default_factory=list)


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
    id: int | None = None          # stable, from the box's id="notice-<type>-<id>"
    url: str = ""                  # the notices page, anchored at this notice
    subject: str = ""
    body: str = ""                 # the full email text (or the text message when that is all there is)
    text_message: str = ""         # the SMS version, when sent
    links: list[Link] = field(default_factory=list)
    posted_at: datetime | None = None


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
    posted_at: datetime | None = None


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
class Teacher:
    name: str
    user_id: int | None = None


@dataclass
class StudentSection:
    """One class on a student's dashboard and the staff listed on it."""

    name: str
    teachers: list[Teacher] = field(default_factory=list)


@dataclass
class StudentDashboard:
    student_name: str
    school_name: str
    grade: str | None = None
    teachers: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    student_id: int | None = None
    school_id: int | None = None
    sections: list[StudentSection] = field(default_factory=list)


@dataclass
class Grade:
    id: int
    name: str


@dataclass
class RosterStudent:
    """A row from the admin roster feed (`/roster/students_data`)."""

    id: int                       # student record id (== GraphQL studentId)
    name: str                     # "Last, First"
    grade: str = ""
    student_sis_id: str | None = None   # external/SIS id
    parents: str = ""             # comma-joined guardian display names
    grade_position: int | None = None
    state_id: str | None = None   # state/CALPADS id (roster col 5)
    account_status: str | None = None   # account status (roster col 10)


@dataclass
class RosterParent:
    """A row from the admin parent roster feed (`/roster/parents_data`)."""

    user_id: int
    name: str                     # "Last, First"
    students: str = ""            # linked kids, "Full Name (Grade)" comma-joined
    email: str | None = None
    phone: str | None = None
    registered: bool = False
    secondary_phone: str | None = None   # secondary phone (roster col 6)


@dataclass
class AdminStudentProfile:
    """Admin student detail from the StudentProfileView GraphQL query."""

    student_id: int
    full_name: str
    first_name: str = ""
    last_name: str = ""
    school_id: int = 0
    school_name: str = ""
    grade_name: str = ""
    student_sis_id: str | None = None   # externalId
    parents: list[dict] = field(default_factory=list)   # {name, profile_path}
    sections: list[dict] = field(default_factory=list)   # {name, period, room, teachers}


@dataclass
class SchoolClass:
    """A class/section summary row from the ``sections_mini`` admin feed."""

    id: int
    name: str
    grade_names: str = ""
    grade_ids: str = ""            # comma-joined grade ids
    external_id: str = ""          # room code, e.g. "Room 5"
    display_name: str | None = None
    sis_name: str | None = None
    teachers: str = ""             # comma-joined teacher names
    assistants: int = 0
    room_parents: int = 0
    student_count: int = 0
    posts_count: int = 0
    visibility_status: str = ""    # section_class_status, e.g. "visible_to_all" / "hidden"


@dataclass
class ClassStaff:
    """One ``section_staff_association`` — a staff member or room parent on a class."""

    assoc_id: int | None          # section_staff_association id (None for a new link)
    user_id: int
    role: str                     # TEACHER | ASSISTANT | ROOM_PARENT
    class_title: str = ""
    first_name: str = ""
    last_name: str = ""

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class ClassDetail:
    """A class plus its full staff list, from ``/api/v2/sections/{id}?include_staff=true``."""

    id: int
    name: str
    external_id: str = ""
    display_name: str | None = None
    sis_name: str | None = None
    active: bool = True
    grade_ids: list[str] = field(default_factory=list)
    staff: list[ClassStaff] = field(default_factory=list)


@dataclass
class ClassStudent:
    """A student on a class roster, from ``/api/v2/sections/{id}/students``."""

    student_id: int
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    student_sis_id: str | None = None

    @property
    def name(self) -> str:
        return self.full_name or f"{self.first_name} {self.last_name}".strip()


@dataclass
class RosterStaff:
    """A staff member row from the ``staff_data`` admin feed."""

    user_id: int
    name: str                     # "Last, First"
    email: str | None = None
    phone: str | None = None
    secondary_phone: str | None = None
    staff_id: str = ""
    role_title: str = ""          # "Role | Title", HTML stripped
    registered: bool = False
    record_created: str = ""
    sua_id: int | None = None     # school_user_association id
