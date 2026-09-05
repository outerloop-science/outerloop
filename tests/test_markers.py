"""Markers and labels: written under `outerloop:`, recognized under both prefixes."""

from __future__ import annotations

from outerloop.markers import has_label, has_marker, is_label, label_name, legacy_marker, marker


def test_writes_the_new_prefix() -> None:
    assert marker("followup") == "<!-- outerloop:followup -->"
    assert legacy_marker("followup") == "<!-- autoresearch:followup -->"
    assert label_name("review") == "outerloop:review"


def test_recognizes_both_prefixes_and_exact_kinds() -> None:
    assert has_marker("x <!-- outerloop:claimed --> y", "claimed")
    assert has_marker("x <!-- autoresearch:claimed --> y", "claimed")  # pre-rename comments
    assert not has_marker("<!-- outerloop:claimed -->", "claim-released")  # kind is exact
    assert not has_marker("outerloop:claimed", "claimed")  # the HTML-comment form only


def test_labels_match_either_prefix_case_insensitively() -> None:
    assert has_label(["Bug", "OUTERLOOP:Review"], "review")  # GitHub labels are case-insensitive
    assert has_label(["autoresearch:no-review"], "no-review")
    assert not has_label(["outerloop:review"], "no-review")
    assert is_label("Autoresearch:Steward", "steward")
    assert not is_label("steward", "steward")  # a bare kind is not the label
