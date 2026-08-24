"""Tests for the per-device connection-profile recorder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transports import device_profile  # noqa: E402
from transports.device_profile import ProfileStore, _fingerprint, _norm_banner  # noqa: E402


_BANNER = ("Hewlett Packard Enterprise\n"
           "RESTRICTED RIGHTS legend text that is stable across logins.\n"
           "Your previous successful login (as manager) was on 2026-08-24 "
           "01:40:03\n from 172.16.1.9\n")


def test_norm_banner_strips_volatile_previous_login_and_dates():
    n = _norm_banner(_BANNER)
    assert "previous" not in n.lower()
    assert "2026-08-24" not in n
    assert "172.16.1.9" not in n
    assert "RESTRICTED RIGHTS" in n


def test_fingerprint_stable_across_logins_only_datetime_changes():
    # Same banner, different login timestamp/source IP → same fingerprint.
    b1 = _BANNER
    b2 = _BANNER.replace("2026-08-24 01:40:03", "2026-08-25 09:15:22") \
                .replace("172.16.1.9", "172.16.1.44")
    assert _fingerprint(b1) == _fingerprint(b2)


def test_fingerprint_changes_when_tc_banner_added():
    b1 = _BANNER
    b2 = ("*** AUTHORIZED USE ONLY — all activity monitored ***\n") + _BANNER
    assert _fingerprint(b1) != _fingerprint(b2)


def _store(tmp_path):
    return ProfileStore(path=str(tmp_path / "profiles.json"))


def test_record_new_profile_persists_and_reloads(tmp_path):
    s = _store(tmp_path)
    s.record(address="172.16.1.90", device_id="d1", object_type="aos_switch",
             prompt="DIST-SW#", paging_cmd="no page",
             gates=["press_any_key", "dsr_probe"], banner=_BANNER)
    p = s.get("172.16.1.90")
    assert p["prompt"] == "DIST-SW#"
    assert p["paging_cmd"] == "no page"
    assert set(p["gates"]) == {"press_any_key", "dsr_probe"}
    assert p["connect_count"] == 1
    # Reload from disk → same data.
    s2 = ProfileStore(path=str(tmp_path / "profiles.json"))
    assert s2.get("172.16.1.90")["prompt"] == "DIST-SW#"


def test_record_repeat_login_no_false_change_and_counts(tmp_path, caplog):
    s = _store(tmp_path)
    s.record(address="10.0.0.1", object_type="aos_switch", prompt="SW#",
             banner=_BANNER)
    b2 = _BANNER.replace("2026-08-24 01:40:03", "2026-08-25 09:15:22")
    with caplog.at_level("WARNING"):
        s.record(address="10.0.0.1", object_type="aos_switch", prompt="SW#",
                 banner=b2)
    assert s.get("10.0.0.1")["connect_count"] == 2
    assert "banner CHANGED" not in caplog.text  # datetime-only diff ≠ change


def test_record_detects_tc_banner_added(tmp_path, caplog):
    s = _store(tmp_path)
    s.record(address="10.0.0.2", object_type="aos_switch", prompt="SW#",
             banner=_BANNER)
    tc = "*** AUTHORIZED ACCESS ONLY ***\n" + _BANNER
    with caplog.at_level("WARNING"):
        s.record(address="10.0.0.2", object_type="aos_switch", prompt="SW#",
                 banner=tc)
    assert "login banner CHANGED" in caplog.text


def test_record_detects_prompt_change(tmp_path, caplog):
    s = _store(tmp_path)
    s.record(address="10.0.0.3", object_type="aos_switch", prompt="OLD-SW#",
             banner=_BANNER)
    with caplog.at_level("WARNING"):
        s.record(address="10.0.0.3", object_type="aos_switch", prompt="NEW-SW#",
                 banner=_BANNER)
    assert "prompt changed" in caplog.text
    assert s.get("10.0.0.3")["prompt"] == "NEW-SW#"


def test_record_detects_new_gate(tmp_path, caplog):
    s = _store(tmp_path)
    s.record(address="10.0.0.4", object_type="aos_switch", prompt="SW#",
             gates=["dsr_probe"], banner=_BANNER)
    with caplog.at_level("INFO"):
        s.record(address="10.0.0.4", object_type="aos_switch", prompt="SW#",
                 gates=["dsr_probe", "press_any_key"], banner=_BANNER)
    assert "new gate" in caplog.text
    assert set(s.get("10.0.0.4")["gates"]) == {"dsr_probe", "press_any_key"}


def test_record_blank_address_is_noop(tmp_path):
    s = _store(tmp_path)
    s.record(address="", prompt="SW#", banner=_BANNER)
    assert s.all() == {}


def test_unwritable_path_is_nonfatal(tmp_path):
    # A store whose dir can't be created stays in-memory and never raises.
    s = ProfileStore(path="/proc/nonexistent-dir/profiles.json")
    s.record(address="10.0.0.9", prompt="SW#", banner=_BANNER)
    assert s.get("10.0.0.9")["prompt"] == "SW#"
