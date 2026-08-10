"""Plain-text rendering of html clips.

Lives in its own module because both ends of the clipboard need it and they sit
on opposite sides of an import cycle: capture uses it when an app offers html
with no plain flavor at all, and the Wayland backend uses it when recovering a
rich clip that has to be published with a plain flavor beside the markup.

Deliberately minimal — enough that a paste into a plain-text target lands
something readable, not a faithful renderer. Depends only on the standard
library, so the lightweight ``_store`` subprocess can import it.
"""
from __future__ import annotations

from typing import List

# Tags that end a line of text, and cells that are separated by tabs — so a
# copied table or list doesn't collapse into one run-on line.
_BREAK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "pre", "article", "section", "table"}
_CELL = {"td", "th"}
# Elements whose text is markup machinery rather than content. Only ones with a
# real closing tag belong here: a void element like <meta> never fires
# handle_endtag, so counting it as "skip until closed" swallows the whole
# document — and a <meta charset> header is exactly what apps prepend to an html
# clip, so that mistake silently emptied every clip it touched.
_SKIP = {"script", "style", "title"}


def is_single_image(html: str) -> bool:
    """True when the fragment's only content is one ``<img>`` and nothing else.

    That is what a browser puts on the clipboard for "Copy image": the picture
    really is the clip, so an image offered alongside it should win over the
    markup. A spreadsheet or document selection, by contrast, carries a table or
    real text, and there the markup is the content.

    Parsed rather than pattern-matched on purpose. The regex this replaced had
    nested quantifiers over an alternation, which backtracks exponentially on
    input like ``<!--`` followed by many ``--><!--`` repetitions — and the input
    here is html handed over by whatever app the user copied from, so a
    malformed or hostile payload could hang capture. Parsing is linear, and it
    is more accurate besides."""
    from html.parser import HTMLParser

    # Wrappers and metadata that carry no visible content — apps routinely
    # prepend a <meta charset> header, and some wrap the fragment in html/body.
    ignore = {"html", "head", "body", "meta", "title", "!doctype"}

    class _Scan(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.elements: List[str] = []
            self.has_text = False

        def handle_starttag(self, tag, attrs):
            if tag not in ignore:
                self.elements.append(tag)

        handle_startendtag = handle_starttag

        def handle_data(self, data):
            if data.strip():
                self.has_text = True

    scan = _Scan()
    try:
        scan.feed(html)
        scan.close()
    except Exception:
        return False
    return scan.elements == ["img"] and not scan.has_text


def html_to_text(html: str) -> str:
    """A readable plain-text rendering of `html` ('' if it can't be parsed)."""
    from html import unescape
    from html.parser import HTMLParser

    class _Strip(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.out: List[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in _SKIP:
                self.skip += 1
            elif tag in _BREAK:
                self.out.append("\n")
            elif tag in _CELL and self.out and not self.out[-1].endswith(("\n", "\t")):
                self.out.append("\t")

        def handle_startendtag(self, tag, attrs):
            if tag in _BREAK:
                self.out.append("\n")

        def handle_endtag(self, tag):
            if tag in _SKIP and self.skip:
                self.skip -= 1
            elif tag in _BREAK:
                self.out.append("\n")

        def handle_data(self, data):
            if not self.skip:
                self.out.append(data)

    parser = _Strip()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    text = unescape("".join(parser.out))
    # Both the opening and closing of a block tag emit a break, so every one of
    # them leaves an empty line behind — between two table rows that would read
    # as a blank row. Blank lines carry no information in a plain-text fallback,
    # so drop them all rather than try to tell structural ones from real ones.
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()
