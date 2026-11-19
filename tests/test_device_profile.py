"""Tests for the per-device connection-profile recorder."""
import os
import sys

import pytest

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


@pytest.mark.parametrize("notice", [
    'Last login: from User "{user}" has logged in {count} times',
    '\tLAST SUCCESSFUL LOGIN: {user} from {host}',
    'Last failed login: {user}, attempt {count}',
    'Last unsuccessful login: {user}, attempt {count}',
    'Login: {count} successful, 2 failed',
    'Last login: from User "{user}" has logged in\n  {count} times',
    'Last login: from User "{user}"\n  has logged in {count} times\n  from {host}',
    'Last successful login:\n  from {host}',
])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_session_notices_do_not_change_banner_fingerprint(notice, newline):
    """Session users, counters and wrapped source hosts are not static banners."""
    first = notice.format(user="admin", count=12, host="client-a.example")
    second = notice.format(user="operator", count=137, host="client-b.example")
    legal = "AUTHORIZED USE ONLY. All activity monitored."
    for session in (first, second):
        banner = (session + "\n" + legal).replace("\n", newline)
        assert _norm_banner(banner) == legal
        assert _fingerprint(banner) == _fingerprint(legal)
        assert _fingerprint(session.replace("\n", newline)) == ""


def test_session_filter_preserves_legal_text_and_ansi_handling():
    """Only session headers are ignored; legal mentions and numbers still count."""
    legal = "We retain your last login: details for 30 days."
    banner = '\x1b[32m  Last login: admin count 12\x1b[0m\n' + legal
    assert _norm_banner(banner) == legal
    assert _fingerprint(banner) != _fingerprint(banner.replace("30 days", "60 days"))


def test_record_session_changes_are_quiet_but_motd_changes_warn(tmp_path, caplog):
    """Exercise persistence and warning detection, not just the hash helper."""
    s = _store(tmp_path)
    legal = "AUTHORIZED USE ONLY\n"
    first = 'Last login: from User "admin" has logged in 12 times\n'
    second = 'Last login: from User "operator" has logged in\n  137 times\n'
    s.record(address="192.0.2.1", banner=first + legal)
    s = _store(tmp_path)
    with caplog.at_level("WARNING"):
        s.record(address="192.0.2.1", banner=second + legal)
        assert "login banner CHANGED" not in caplog.text
        s.record(address="192.0.2.1", banner=second + legal + "All activity monitored.\n")
    assert caplog.text.count("login banner CHANGED") == 1
    assert s.get("192.0.2.1")["banner_excerpt"] == (
        "AUTHORIZED USE ONLY All activity monitored.")


def test_legacy_banner_fingerprint_settles_after_one_observation(tmp_path, caplog):
    """An old persisted hash is replaced once, without recurring warnings."""
    path = tmp_path / "profiles.json"
    path.write_text(
        '{"192.0.2.1": {"banner_fingerprint": "legacy-session-hash"}}',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        for count in (12, 137, 138):
            s = _store(tmp_path)
            s.record(address="192.0.2.1", banner=(
                f'Last login: from User "admin" has logged in {count} times\n'
                "AUTHORIZED USE ONLY\n"))
    assert caplog.text.count("login banner CHANGED") == 1
    assert s.get("192.0.2.1")["banner_fingerprint"] == _fingerprint("AUTHORIZED USE ONLY")


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
