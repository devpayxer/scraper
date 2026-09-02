"""The probe's own diagnosis has to be right, or it sends you chasing ghosts."""
from ebayparts.probe import DEFAULT_PROFILES, ProbeResult, diagnose


def result(status: str, listings: int = 0) -> ProbeResult:
    return ProbeResult(profile="chrome150", warm_up=False, status=status,
                       listings=listings)


class TestDiagnose:
    def test_a_success_anywhere_wins(self):
        assert diagnose([result("403"), result("ok", 60)]) == "ok"

    def test_all_refusals(self):
        assert diagnose([result("403"), result("challenge")]) == "refused"

    def test_all_network_errors(self):
        assert diagnose([result("network"), result("network")]) == "unreachable"

    def test_a_refusal_outranks_network_noise(self):
        """The bug this exists to prevent: a mixed run reported as 'unreachable'.

        If eBay answered even once with a 403, the connection is fine and the
        advice must be about being refused, not about checking the firewall.
        """
        assert diagnose([result("network"), result("403")]) == "refused"

    def test_empty_results_count_as_refusal_not_success(self):
        assert diagnose([result("empty")]) == "refused"

    def test_nothing_probed(self):
        assert diagnose([]) == "unreachable"


def test_default_profiles_are_current_generation():
    """A stale fingerprint is itself anomalous -- guard against regressing."""
    chrome = [p for p in DEFAULT_PROFILES if p.startswith("chrome")]
    assert chrome, "expected at least one chrome profile"
    versions = [int(p.removeprefix("chrome").rstrip("a_androidios")) for p in chrome]
    assert max(versions) >= 145, f"newest chrome profile is only {max(versions)}"


def test_works_requires_ok_status():
    assert result("ok", 10).works
    assert not result("empty").works
    assert not result("403").works
