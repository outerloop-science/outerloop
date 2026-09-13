"""The bounded hypothesis paragraph shared by records and the board."""

import re

MAX_HYPOTHESIS_CHARS = 1000

# a real section or field label at the start of its line (a heading, a list
# item, a bold or plain "Hypothesis:"), never the word inside prose
_HYP = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?(?P<label>Hypothesis)[:*\s]+(.+)",
    re.I | re.M,
)
# Headings, lists and field labels can end the paragraph.
_HYP_END = re.compile(r"^\s{0,3}(?:#|[-*+]\s|\d+[.)]\s)")
_FIELD_LINE = re.compile(r"^\s{0,3}(?:\*\*|__)?[A-Z][\w /-]{0,40}:(?:\*\*|__)?(?:\s|$)")


def report_hypothesis(text: str) -> str:
    """Extract the whole hypothesis paragraph from a run report."""
    hyp = ""
    m = _HYP.search(text)
    if m:
        # Only field-format reports stop at the next field label.
        label = m.start("label")
        line_start = text.rfind("\n", 0, label) + 1
        after = label + len("Hypothesis")
        # emphasis around the label (`**Hypothesis:**`) is still the field format
        fielded = not text[line_start:label].strip("*_ \t") and text[after : after + 1] == ":"
        lines: list[str] = []
        for line in text[m.end(2) - len(m.group(2)) :].split("\n"):
            # an empty section: the first captured line is already the next
            # heading, list item or field, so there is no hypothesis
            if not lines and (_HYP_END.match(line) or _FIELD_LINE.match(line)):
                break
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
