"""Raw body text -> body plus the optional content fields.

Pure: no file, DB or config access. Imports: nothing from the package.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedBody:
    body: str
    forwarded_content: str | None
    quoted_content: str | None
    headings: str | None
    has_attachment: bool


def parse_body(raw_body: str, content_type: str | None = None) -> ParsedBody:
    """Split a decoded body into body, forwarded and quoted parts, headings.

    `content_type` is the Content-Type header value, used only to infer
    has_attachment. Never raises on odd input; an empty body is valid.
    """
    raise NotImplementedError


def compute_content_hash(body: str) -> str:
    """Hash used to find storage copies (emails.content_hash).

    What goes into the hash (body only vs body plus sender and subject) is not
    decided yet; it belongs to the dedup plan.
    """
    raise NotImplementedError
