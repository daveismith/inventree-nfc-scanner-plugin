"""The weekly proposal of InvenTree versions to test against."""

from tests.propose_versions import propose


def test_newest_patch_of_each_minor_after_the_oldest():
    tags = ["1.4.2", "1.4.3", "1.4.4", "1.5.0", "1.5.6", "1.6.0", "1.6.1", "1.3.9"]
    assert propose(["1.4.3", "1.5.6"], tags) == ["1.4.3", "1.5.6", "1.6.1"]


def test_the_oldest_stays_as_it_is():
    """It is MIN_VERSION: a newer patch of it does not replace it."""
    assert propose(["1.4.3", "1.5.6"], ["1.4.4", "1.5.6"]) == ["1.4.3", "1.5.6"]


def test_unchanged_when_nothing_is_newer():
    assert propose(["1.4.3", "1.5.6"], ["1.4.3", "1.5.6", "1.5.5"]) == [
        "1.4.3",
        "1.5.6",
    ]


def test_tags_that_are_not_releases_are_ignored():
    assert propose(["1.4.3"], ["v1.5.0", "1.6.0-rc1", "stable", ""]) == [
        "1.4.3",
        "1.5.0",
    ]
