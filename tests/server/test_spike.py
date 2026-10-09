from .scanner import P


def test_idle_sync(scanner):
    assert scanner.cmds() == []


def test_job_flow(scanner, api, clerk, location):
    r = api(clerk).post(f"{P}/api/jobs/", {"location": location.pk, "scanner": str(scanner.machine.pk)}, format="json")
    assert r.status_code == 201, r.json()
    cmds = scanner.cmds()
    assert cmds[0]["cmd"] == "program"
