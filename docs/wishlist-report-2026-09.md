# parentsquare-mcp wish list: comparison report

_Laptop side-by-side, 2026-09-27. Build: `v0.4.0-ps.1` (package version `0.4.0+ps.1`)._

The owner signed in twice on the laptop: once in a dedicated browser profile and
once, with a fresh login and its own cookie file, in parentsquare-mcp. Every page
below was read in the browser (its rendered DOM and Chrome's `innerText`) and
fetched through the library, then parsed by 0.4.0 and by this build. There were
two passes: one in the morning, then a fresh "after" pass through the new client
that afternoon. Both passes agree. Reads only: nothing was clicked that acts (no
RSVP, sign, vote, pay, comment or reply), with 2.5 s between requests, and no
history crawl beyond feed pages 1–2.

Install on the server:

```
pip install "parentsquare-mcp @ git+https://github.com/s0meguy1/psquare-mcp@v0.4.0-ps.1"
```

## Counts: browser vs parsed

| Surface | Browser | 0.4.0 | This build |
|---|---|---|---|
| Feed page 1 | 10 | **0** | 10 |
| Feed page 2 | 10 | **0** | 10 |
| Group feed (the one group with posts) | 4 | **0** | 4 |
| Sign-ups | 1 | **0** | 1 |
| Forms | 0 ("No forms") | 0 | 0 |
| Polls | 0 ("No polls") | 0 | 0 |
| Payments | 0 ("There are no payments") | 0 | 0 |
| Conversation list | 2 | 2 | 2 |
| Thread A / thread B messages | 3 / 2 | 3 / 2 | 3 / 2 |
| Notices | 2 | 2 | 2 |
| Student dashboard: name / classes / teachers | ✓ / 1 / 1 | **✗ / 0 / 0** | ✓ / 1 / 1 (+ school id, teacher ids) |
| Groups page, `parse_groups_list` | 12 in "My Groups" (rendered by React) | **0, silently** | raises `ParseDriftError`; `PSClient.list_groups` returns 60 via GraphQL |

The polls, forms and payments pages are genuinely empty for this account. The browser shows the empty-state text, and no request loads items later.

## Fields: seven sampled posts, two threads, two notices

The sample covers:
- long text with paragraphs, bullets and 11 links;
- an image-only flyer;
- a PDF;
- an appointment sign-up (21 slots over 2 days);
- an event that takes RSVPs through a signable form;
- two class newsletters.

| Field | 0.4.0 | This build |
|---|---|---|
| Body text == browser `innerText` (whitespace-normalized) | 1 / 7 (only the empty body) | **7 / 7** |
| Feed summaries == detail `innerText` | – (feed parsed nothing) | 7 / 7 |
| Links in bodies | 0 / 24 | **24 / 24** (plus 3 emoji-glyph anchors skipped on purpose) |
| File attachments (both PDFs) | **0 / 2**: the live `/feeds/{id}/attachment/{aid}` links were missed | 2 / 2, original file names |
| Inline images | URLs right, names mangled, no alt | full-size URLs, upload names, alt text on all 4 images that have it |
| Sign-up slots | 21 / 21, no day, no state | 21 / 21 with day and open/closed/mine |
| Chat message text == browser | 3 / 5 (two multi-paragraph messages ran together) | 5 / 5 |
| Chat message ids / direction | none | 5 / 5 / 5 / 5 |
| Chat timestamps with year | 0 / 5 ("Thu, Sep 10 6:38 am") | **5 / 5**, e.g. `2026-09-10T06:38-04:00` |
| Notice ids / bodies / timestamps | none | 2 / 2 each; bodies == browser `innerText` less the "Read Less" toggle |

The fixtures in `tests/fixtures/live_2026_09` are these pages, scrubbed. For each
one, `expected_browser.json` holds what Chrome reports for the *scrubbed* file,
so the text and link tests compare against a real browser.

## Answers

**W5 — comments.** None of the owner's posts shows a comment, and none has one. This
school sets comments to private: every comment box says "Private • Only you and
<author> can view", and every detail page says "0 comments on this post".
Comments are server-rendered inside `.comments-box` (no XHR), so the parser now
reads them there. It also reads the page's own count, and it was checked against
markup built from the site's own comment selectors. So 0 comments is correct for
this school, not a miss.

**W6 — alerts.** **Alerts listed under Notices do not appear in the feed.** Both
current alerts are district-sent, dated Sep 8 and Sep 18. Neither appears
anywhere in feed pages 1–2, which span Sep 1–25. pstriage therefore cannot see
alerts today. `parse_notices` now gives each notice:
- a stable `id` (from `div#notice-<type>-<id>`);
- a `url` (the notices page anchored at the notice; there is no per-notice page);
- the email `subject` and full `body`;
- the SMS `text_message`;
- `links`;
- `posted_at`, with the year inferred from the weekday, in the school's zone.

Poll it as a third surface. The page only lists the past 3 weeks.

**W11 — expired sessions.** With the cookies minus `ps_s`, and also with no
cookies at all, both `/schools/{id}/feeds` and the chats page answer
`302 → /signin`. They land on a 200 sign-in page that renders
`gon.user_id=null` (while still naming `gon.institute_id`). So the existing
`/signin` check already catches those two pages. The root page is the known
200-without-auth case. The client now treats any HTML page rendering
`gon.user_id=null`, and any JSON 401, as signed out. It logs in again once, and
raises `SessionExpired` if the page is still signed out (or immediately, with
`credentials=None`), instead of parsing an empty page.

## W18 — endpoints the site uses

Recorded from the browser's network log on every page above, plus the site's
JavaScript bundle.

| Surface | Method | Path | Returns | Auth |
|---|---|---|---|---|
| Feed | GET | `/schools/{id}/feeds?page=N` | server-rendered HTML, 10 posts a page | session cookie |
| Post, comments | GET | `/feeds/{id}` | HTML; comments inline in `.comments-box` | session |
| File attachment | GET | `/feeds/{id}/attachment/{aid}` | 302 to a pre-signed S3 URL (its `response-content-disposition` carries the file name) | session, then the signature |
| Event | GET | `/feeds/{id}/add_cal.ics?district_id=…` | one-event ICS | session |
| Chat list | GET | `/schools/{id}/users/{uid}/chats` | HTML | session |
| Chat thread | GET | `/schools/{id}/users/{uid}/chats/{chat_id}?lang=en` | HTML (the web app fetches it via Rails UJS) | session |
| Mark thread read *(write)* | POST | `/api/v2/chats/{chat_id}/read` `{message_id}` | JSON | session + CSRF; the browser calls it when a thread opens, the library never does |
| Rename group chat *(write)* | POST | `/api/v2/chats/{chat_id}/name` | JSON | session + CSRF |
| Realtime | POST | `/pusher/auth` | Pusher channel auth; new messages arrive as JSON over the websocket | session + CSRF |
| Notices | GET | `/schools/{id}/notices` | HTML, past 3 weeks | session |
| Groups | POST | `/graphql` `GetGroups`, `GetGroupMemberships`, `GetGroupStats` | JSON | session + CSRF header |
| Group post counts | GET | `/api/v2/schools/{id}/groups` | JSON:API, all school groups with `active_posts_count` | session |
| School | GET | `/api/v2/schools/{id}` | JSON:API: name, `time_zone` (Rails name), address, phone | session |
| Student dashboard | GET | `/students/{id}/dashboard` | HTML (`gon.institute_id` is the student's school) | session |
| Image alt text *(composer)* | POST | `/api/v2/image_alt_text/alt_texts` | AI alt-text suggestions while staff write a post | not for readers |

**There is no JSON read endpoint for the feed, posts, comments, chat messages
or notices.** The web app renders all of them on the server, so HTML stays the
only read path for those surfaces. The JSON that exists (school, groups) is used
already. `GetGroupMemberships` and `GetGroupStats` would restore the member
counts and membership that 0.4.0's `list_groups` lost. That is a possible
follow-up; it was not needed here.

## W13/W14 — how older chat messages load

The longest thread on this account has 3 messages. Both threads render in full,
and match the "3 Messages" / "2 Messages" counts in the list. The bundle has no
loader for older messages: `#chat-messages` has no scroll handler, and the only
chat JSON calls are the two writes above. No longer thread was available to
test. `Conversation.message_count` now lets pstriage detect a thread page that
shows fewer messages than the list says.

## W16 — fields verified live, or not present on this account

| Field | Status |
|---|---|
| `kinds` / `post_type` | verified: event+form, signup, plain posts |
| Event start/end | verified: exact UTC from the post's Google Calendar link |
| Event location | not present: the one event's calendar link carries none |
| RSVP state | not present: this event takes RSVPs through a form, with no RSVP buttons |
| Sign-up slots this parent took | verified negative: 21 slots, 19 "Closed", 2 "Sign Up", none held by this parent |
| Form signed | verified negative: "Complete Form" call to action, empty answers, deadline passed |
| Payment due / paid | not present: no payments on this account |
| `is_pinned`, `is_urgent` | not present: no pinned or urgent post in feed pages 1–2. Detection keys on a thumb-tack icon, a *pinned*/*urgent* class, or a "Pinned" badge. Notices carry `notice_type="alert"`. |

## W15 — images

Inline images are served full size: in the browser, their natural size equals
the file's (e.g. 3307×4678). The browser has no viewer for them, so the parsed
URL *is* the full-size image. Chat images differ: the page shows a `thumb_`
thumbnail, and the viewer opens `a.thumbnail[data-url]` (`full_…`, signed).
This build returns that URL, and was checked against the browser. Five of the
owner's 76 stored image URLs pointed at thumbnails, which fits this explanation.
The photos gallery is empty on this account.

## Offline items

| Item | Done when | Evidence |
|---|---|---|
| W2 | renamed classes raise, empty pages don't | `tests/test_parse_drift.py` |
| W7 | every call raises `requests.Timeout` against a server that never answers | `tests/test_timeouts.py`: 18 calls |
| W8 | each exception from its real trigger | `tests/test_errors.py` |
| W9 | two clients, two cookie files, one process; `credentials=None` raises `SessionExpired` | `tests/test_client_session.py` |
| W10 | 10 requests on an unchanged jar write once; mode 600; atomic | `tests/test_client_session.py` |
| W12 | an oversized file stops at the cap | `tests/test_attachments.py` |

**Live note on W10:** ParentSquare rotates `ps_s` on every response. That was 25
cookie writes for 25 requests through the new client, so a long-running client
still writes about once per request, because the jar really does change each
time. What changed is how: each write is atomic, mode 600, and logged at DEBUG,
so those lines leave the INFO log.

Not reproducible: the 403 `browser_unsupported` page. On 2026-09-27 `/signin`
answered 200 to an empty User-Agent and to curl alike. `BrowserUnsupported`
keys on status 403 plus the `browser_unsupported` token, and is tested with a
synthetic response.

## For pstriage

- Names it calls keep working: `PSClient` (`get_page`, `get_raw`, `get_ics`,
  `discover_account`, `account`, `_relogin`), `make_session`, `URLS`, and
  everything it imports from `auth`. `auth.COOKIE_FILE` / `MFA_STATE_FILE` are
  read at call time, so reassigning them still works. All the listed parsers keep
  their signatures; the new parameters are keyword arguments with defaults.
  `MFARequiredError` is still a plain `Exception`.
- Replacing workarounds:
  - `_normalize_feed_soup` → nothing (the parsers find posts at any depth).
  - `PS_CREDENTIAL_PROVIDER=none` + replacing `_relogin` →
    `PSClient(credentials=None, cookie_path=…, mfa_state_path=…)`.
  - `parse_student_profile` → `parse_student_dashboard`.
  - The copied PDF path → `parentsquare_mcp.attachments.fetch_pdf_text`.
  - Year-from-weekday → `Message.posted_at`: pass `tz=client.school_tz(school_id)`.
  - Re-parsing ids and direction → `Message.id`, `.chat_id`, `.is_mine`,
    `Conversation.is_group`.
- `connect._friendly` substring matching → `except LoginFailed` /
  `MFACodeInvalid` / `MFANotEstablished` / `SessionExpired`. The old messages are
  unchanged, so both work during the move.
- `PoliteFetcher` should also retry `requests.Timeout` and
  `requests.ConnectionError`. `RateLimited.retry_after` gives the wait.
- Expect `ParseDriftError` from the parsers when a page holds items that do not
  parse, and alert on it rather than treating it as a quiet day.
  `parse_groups_list` now always raises on the live groups page; use
  `PSClient.list_groups(school_id)`.
- `tests/test_feed_normalization.py::test_unpatched_parser_reproduces_the_live_bug`
  will fail, as the brief expected.
