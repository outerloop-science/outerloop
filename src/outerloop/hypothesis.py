"""The bounded hypothesis paragraph shared by records and the board."""

import re

MAX_HYPOTHESIS_CHARS = 1000

_HYP = re.compile(r"Hypothesis[:*\s]+(.+)", re.I)
# Headings, lists and field labels can end the paragraph.
_HYP_END = re.compile(r"^\s{0,3}(?:#|[-*+]\s|\d+[.)]\s)")
_FIELD_LINE = re.compile(r"^\s{0,3}(?:\*\*|__)?[A-Z][\w /-]{0,40}:(?:\*\*|__)?(?:\s|$)")


def report_hypothesis(text: str) -> str:
    """Extract the whole hypothesis paragraph from a run report."""
    hyp = ""
    m = _HYP.search(text)
    if m:
        # Only field-format reports stop at the next field label.
        line_start = text.rfind("\n", 0, m.start()) + 1
        after = m.start() + len("Hypothesis")
        # emphasis around the label (`**Hypothesis:**`) is still the field format
        fielded = not text[line_start : m.start()].strip("*_ \t") and text[after : after + 1] == ":"
        lines: list[str] = []
        for line in text[m.start(1) :].split("\n"):
            if lines and (
                not line.strip() or _HYP_END.match(line) or (fielded and _FIELD_LINE.match(line))
            ):
                break
            lines.append(line)
        hyp = re.sub(r"[`*_]|\s+", lambda g: " " if g.group().isspace() else "", "\n".join(lines))
        hyp = hyp.strip().rstrip("-").strip()
        if len(hyp) > MAX_HYPOTHESIS_CHARS:
            head = hyp[: MAX_HYPOTHESIS_CHARS - 1]
            hyp = (head.rsplit(" ", 1)[0] if " " in head else head) + "…"
    return hyp
