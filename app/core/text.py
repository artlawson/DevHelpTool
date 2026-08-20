import re
from collections.abc import Callable

from app.config import settings


# Shared by app/slack/formatting.py and app/cli.py - both hyperlink issue keys
# to the same Jira URL, just in different link syntax (Slack's <url|text> vs
# a terminal's OSC 8 sequence).
def jira_issue_url(key: str) -> str:
    return f"{settings.jira_base_url.rstrip('/')}/browse/{key}"


# Splits `text` on spans matching `link_pattern` (an already-built link, in
# whatever syntax the caller uses - Slack's <url|text>, a terminal's OSC 8
# escape sequence, etc.) and runs `transform` only on the text between those
# spans. Guards a later substitution pass against corrupting an existing link
# whose target URL happens to contain a substring that would also match the
# pattern being substituted (e.g. a Jira issue key appearing inside the URL of
# an already-linked PR) - shared by app/slack/formatting.py and app/cli.py,
# which each hyperlink the same referenced_issues/referenced_prs data in a
# different link syntax.
def apply_outside_links(
    text: str, link_pattern: re.Pattern[str], transform: Callable[[str], str]
) -> str:
    segments = link_pattern.split(text)
    links = link_pattern.findall(text)
    pieces = [transform(segments[0])]
    for link, segment in zip(links, segments[1:], strict=True):
        pieces.append(link)
        pieces.append(transform(segment))
    return "".join(pieces)
