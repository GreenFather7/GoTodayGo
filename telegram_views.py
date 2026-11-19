"""Read public counters for known @GoTodayGo posts without executing the widget."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ViewObservation:
    published_at: datetime
    observed_at: datetime
    views: int
    resolution: int
    label: str


class _PostParser(HTMLParser):
    """Only use the footer of the requested post and the date link to that post."""

    def __init__(self, message_id):
        super().__init__()
        self.post = f"GoTodayGo/{message_id}"
        self.url = f"https://t.me/{self.post}"
        self.posts = 0
        self.dates = []
        self.labels = []
        # Each tuple tracks the current element's tag, post, footer, counter and date link.
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        _, ours, footer, counter, date_link = (
            self.stack[-1] if self.stack else ("", False, False, None, False)
        )
        if "data-post" in attrs:
            ours = "tgme_widget_message" in classes and attrs["data-post"] == self.post
            footer, counter, date_link = False, None, False
            if ours:
                self.posts += 1
        if ours and "tgme_widget_message_footer" in classes:
            footer = True
        if ours and footer:
            if "tgme_widget_message_views" in classes:
                counter = []
                self.labels.append(counter)
            if tag == "a":
                date_link = "tgme_widget_message_date" in classes and attrs.get("href") == self.url
            if tag == "time" and date_link:
                self.dates.append(attrs.get("datetime"))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                       "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, ours, footer, counter, date_link))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][3] is not None:
            self.stack[-1][3].append(data)


def _parse_observation(page, message_id, observed_at):
    parser = _PostParser(message_id)
    parser.feed(page)
    parser.close()
    if parser.posts != 1 or len(parser.labels) != 1 or len(parser.dates) != 1:
        raise ValueError(f"Telegram did not show a unique post, view count and date for #{message_id}.")
    stamp = parser.dates[0]
    if not stamp:
        raise ValueError("Telegram did not give the publication timestamp.")
    published_at = datetime.fromisoformat(stamp)
    if published_at.utcoffset() is None:
        raise ValueError("Telegram did not give the publication timezone.")
    label = "".join(parser.labels[0]).strip()
    count = re.fullmatch(r"([0-9]+)(?:\.([0-9]+))?([KMB]?)", label)
    if not count:
        raise ValueError(f"Unrecognized Telegram view count {label!r}.")
    whole, fraction, suffix = count.groups()
    fraction = fraction or ""
    places = {"": 0, "K": 3, "M": 6, "B": 9}[suffix] - len(fraction)
    if places < 0:
        raise ValueError(f"Unrecognized precision of Telegram view count {label!r}.")
    resolution = 10 ** places
    views = int(whole + fraction) * resolution
    return ViewObservation(published_at.astimezone(timezone.utc), observed_at, views, resolution, label)


def get_observation(message_id: int) -> ViewObservation:
    """Measure a confirmed publication ID. Rounded views retain their display resolution."""
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("Expected a confirmed positive Telegram message ID.")
    url = f"https://t.me/GoTodayGo/{message_id}?embed=1&mode=tme"
    request = Request(url, headers={"Accept-Language": "en", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=20) as response:
        if response.geturl() != url or response.headers.get_content_type() != "text/html":
            raise ValueError(f"Unexpected Telegram response for #{message_id}.")
        page = response.read(2_000_001)
        if len(page) > 2_000_000:
            raise ValueError("Telegram widget response exceeded the size limit.")
    return _parse_observation(page.decode("utf-8"), message_id, datetime.now(timezone.utc))
