"""Fetching releases from GitHub (a `responses` stand-in), and deploying them automatically."""

import datetime

import pytest

from .releases import release
from .scanner import P


@pytest.fixture
def check(fleet):
    def run():
        r = fleet.post(f"{P}/api/fleet/check/")
        assert r.status_code == 200, r.json()
        return r.json()

    return run


def test_a_release_is_fetched_and_a_prerelease_is_not_by_default(github, check):
    github.publish(release("1.1.0"))
    github.publish(release("1.2.0-rc.1"), prerelease=True)
    result = check()
    assert result["added"] == ["1.1.0"] and result["errors"] == []


def test_with_prereleases_on_they_are_fetched_too(github, check, set_setting):
    github.publish(release("1.2.0-rc.1"), prerelease=True)
    set_setting("FIRMWARE_PRERELEASES", True)
    assert check()["added"] == ["1.2.0-rc.1"]


def test_drafts_are_never_fetched(github, check):
    github.publish(release("1.1.0"), draft=True)
    assert check()["added"] == []


def test_an_image_that_does_not_match_githubs_digest_is_refused(github, check):
    github.publish(release("1.1.0"), bad_digest=True)
    result = check()
    assert result["added"] == [] and any(
        "1.1.0" in e and "GitHub" in e for e in result["errors"]
    )


def test_a_release_without_a_manifest_is_reported(github, check):
    github.publish(release("1.1.0"), omit=("manifest.json",))
    assert any("manifest" in e for e in check()["errors"])


def test_a_tag_that_does_not_match_its_manifest_is_reported(github, check):
    github.publish(release("1.1.0"), tag="v1.1.1")
    assert any("tag" in e for e in check()["errors"])


def test_one_bad_release_does_not_stop_the_others(github, check):
    github.publish(release("1.1.0"))
    github.publish(release("1.2.0"), bad_digest=True)
    result = check()
    assert result["added"] == ["1.1.0"] and len(result["errors"]) == 1


def test_a_release_held_is_not_fetched_again(github, check):
    github.publish(release("1.1.0"))
    check()
    assert check()["added"] == []


def test_a_pruned_release_is_not_fetched_again(github, check, set_setting):
    set_setting("FIRMWARE_KEEP", 1)
    github.publish(release("1.1.0"))
    check()
    github.publish(release("1.2.0"))
    result = check()
    assert result["added"] == ["1.2.0"] and result["pruned"] == ["1.1.0"]
    assert check()["added"] == []


def test_the_check_is_recorded_with_where_it_came_from(github, check, fleet):
    github.publish(release("1.1.0"))
    check()
    overview = fleet.get(f"{P}/api/fleet/").json()
    assert overview["last_check"]["at"] and overview["last_check"]["added"] == ["1.1.0"]
    (fw,) = overview["firmware"]
    assert fw["source"] == "github" and fw["release_url"].endswith("v1.1.0")


def test_an_unknown_repository_is_reported(github, check):
    github.mock.replace("GET", f"{github.API}/repos/{github.REPO}/releases", status=404)
    assert "no such repository" in check()["errors"][0]


def test_the_rate_limit_is_reported(github, check):
    github.mock.replace(
        "GET",
        f"{github.API}/repos/{github.REPO}/releases",
        status=403,
        headers={"X-RateLimit-Remaining": "0"},
    )
    assert "rate limit" in check()["errors"][0]


def test_a_repository_that_is_not_owner_name_is_reported(github, check, set_setting):
    set_setting("FIRMWARE_REPO", "not a repo")
    assert "owner/name" in check()["errors"][0]


def test_the_token_is_sent_when_set(github, check, set_setting):
    set_setting("FIRMWARE_GITHUB_TOKEN", "ghp_secret")
    check()
    assert github.mock.calls[0].request.headers["Authorization"] == "Bearer ghp_secret"


# Automatic deployment: isolated here, as the live check against a real server could not be ----


@pytest.fixture
def on_old(scanner):
    scanner.fw = "1.0.0"
    scanner.sync()
    return scanner


def test_nothing_is_deployed_with_the_setting_off(github, check, on_old):
    github.publish(release("1.1.0"))
    assert "auto_deploy" not in check()


def test_a_new_release_goes_to_scanners_running_something_older(
    github, check, on_old, set_setting, fleet
):
    set_setting("FIRMWARE_AUTO_DEPLOY", True)
    github.publish(release("1.1.0"))
    result = check()
    assert result["auto_deploy"]["version"] == "1.1.0"
    (d,) = fleet.get(f"{P}/api/fleet/deployments/?scanner={on_old.reader}").json()
    assert (
        d["version"] == "1.1.0"
        and d["state"] == "pending"
        and d["requested_by"] is None
    )


def test_the_newest_it_can_drive_goes_out_not_one_it_cannot(
    github, check, on_old, set_setting, fleet
):
    set_setting("FIRMWARE_AUTO_DEPLOY", True)
    github.publish(release("1.1.0"))
    github.publish(release("1.2.0", min_plugin="99.0.0"))
    result = check()
    assert sorted(result["added"]) == ["1.1.0", "1.2.0"]
    assert result["auto_deploy"]["version"] == "1.1.0"
    overview = fleet.get(f"{P}/api/fleet/").json()
    assert overview["newest"] == "1.1.0"
    (incompatible,) = [f for f in overview["firmware"] if f["version"] == "1.2.0"]
    assert "99.0.0" in incompatible["incompatible"]


def test_a_check_that_fetches_only_what_it_cannot_drive_deploys_nothing(
    github, check, on_old, set_setting
):
    set_setting("FIRMWARE_AUTO_DEPLOY", True)
    github.publish(release("1.3.0", proto=2))
    result = check()
    assert result["added"] == ["1.3.0"] and "auto_deploy" not in result


def test_a_prerelease_is_never_deployed_automatically(
    github, check, on_old, set_setting
):
    set_setting("FIRMWARE_AUTO_DEPLOY", True)
    set_setting("FIRMWARE_PRERELEASES", True)
    github.publish(release("1.2.0-rc.1"), prerelease=True)
    assert "auto_deploy" not in check()


def test_a_scanner_already_on_it_is_left_alone(
    github, check, on_old, set_setting, fleet
):
    set_setting("FIRMWARE_AUTO_DEPLOY", True)
    on_old.fw = "1.1.0"
    on_old.sync()
    github.publish(release("1.1.0"))
    check()
    assert fleet.get(f"{P}/api/fleet/deployments/").json() == []


# The scheduled check -------------------------------------------------------------------------


def test_the_hourly_task_checks_as_often_as_the_setting_says(
    github, plugin, set_setting, time_machine
):
    from inventree_nfc_scanner.firmware import last_check

    set_setting("FIRMWARE_CHECK_HOURS", 24)
    github.publish(release("1.1.0"))
    plugin.check_firmware()
    first = last_check()["at"]
    time_machine.move_to(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    )
    plugin.check_firmware()
    assert last_check()["at"] == first, "not again within the interval"
    time_machine.move_to(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=24)
    )
    plugin.check_firmware()
    assert last_check()["at"] != first


def test_zero_hours_checks_only_on_demand(github, plugin, set_setting):
    from inventree_nfc_scanner.firmware import last_check

    set_setting("FIRMWARE_CHECK_HOURS", 0)
    plugin.check_firmware()
    assert last_check() is None
