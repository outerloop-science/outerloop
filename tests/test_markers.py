"""Markers and labels: written and recognized under `outerloop:` only."""

from __future__ import annotations

from outerloop.markers import has_label, has_marker, is_label, label_name, marker


def test_writes_the_new_prefix() -> None:
    assert marker("followup") == "<!-- outerloop:followup -->"
    assert label_name("review") == "outerloop:review"


def test_recognizes_exact_kinds_under_the_prefix() -> None:
    assert has_marker("x <!-- outerloop:claimed --> y", "claimed")
    assert not has_marker("x <!-- autoresearch:claimed --> y", "claimed")  # retired prefix
    assert not has_marker("<!-- outerloop:claimed -->", "claim-released")  # kind is exact
    assert not has_marker("outerloop:claimed", "claimed")  # the HTML-comment form only


def test_labels_match_case_insensitively() -> None:
    assert has_label(["Bug", "OUTERLOOP:Review"], "review")  # GitHub labels are case-insensitive
    assert not has_label(["autoresearch:no-review"], "no-review")  # retired prefix
    assert not has_label(["outerloop:review"], "no-review")
    assert is_label("Outerloop:Steward", "steward")
    assert not is_label("autoresearch:steward", "steward")
    assert not is_label("steward", "steward")  # a bare kind is not the label
