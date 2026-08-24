"""Per-device connection-profile recorder.

We learn, on every successful CLI connect, *what it actually took* to talk to a
device — its prompt, the paging-disable command, which login gates it threw
(a "Press any key to continue" / T&C banner, a DSR/DA terminal probe) — and
persist that keyed by device address. Two reasons:

  1. **Change detection.** A device's login surface is not static: someone adds
     a legal / T&C banner, an admin changes the hostname (prompt), a firmware
     upgrade starts issuing a terminal probe it didn't before. Recording a
     *fingerprint* of the login banner (with the volatile "previous login was
     on <date> from <ip>" line stripped, so it's stable across logins) lets us
     flag exactly that: "device X's login banner changed — a T&C banner may have
     been added". That surfaces in the log the moment it happens instead of
     silently changing connect behavior.

  2. **A durable record of connect requirements** per device — the prompt, the
     paging command, the gates — visible in the spoke status, so the knowledge
     of how to reach each device lives somewhere other than in code.

Best-effort and never fatal: a store that can't be read/written falls back to
in-memory only; recording never raises into the connect path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NwCli")

# Persist to a writable, self-update-surviving location (the nw code dir is
# replaced on self-update, so DON'T store there). Overridable for tests / odd
# layouts. Best-effort: if the dir isn't writable we stay in-memory.
_DEFAULT_PATH = os.environ.get(
    "LM_NW_PROFILE_PATH", "/var/lib/lm/nw/device_profiles.json")

# ANSI/VT100 control sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Volatile banner lines/tokens that change every login — excluded from the
# fingerprint so a normal login isn't mistaken for a banner change.
_PREV_LOGIN_RE = re.compile(r"(?im)^.*previous.*login.*$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _norm_banner(banner: str) -> str:
    """Normalize a login banner for fingerprinting: drop ANSI, the volatile
    "previous login" line, and any dates/times/IPs, then collapse whitespace.
    What remains is the *stable* legal/copyright/T&C text — the thing we want to
    notice a change in."""
    s = _ANSI_RE.sub("", banner or "")
    s = _PREV_LOGIN_RE.sub("", s)
    s = _DATE_RE.sub("", s)
    s = _TIME_RE.sub("", s)
    s = _IP_RE.sub("", s)
    return " ".join(s.split())


def _fingerprint(banner: str) -> str:
    norm = _norm_banner(banner)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:16]


def _excerpt(banner: str, limit: int = 200) -> str:
    norm = _norm_banner(banner)
    return norm[:limit]


class ProfileStore:
    """Loads/persists per-device connection profiles and records observations,
    logging first-capture and change events."""

    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._profiles = {k: v for k, v in data.items()
                                  if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 — corrupt/unreadable store is non-fatal
            logger.debug("device_profile: load failed (%s)", e)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._profiles, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001 — read-only fs / perms: in-memory only
            logger.debug("device_profile: save failed (%s)", e)

    def get(self, address: str) -> Optional[Dict[str, Any]]:
        return self._profiles.get(address)

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._profiles.items()}

    def record(self, *, address: str, device_id: str = "", object_type: str = "",
               prompt: str = "", paging_cmd: str = "",
               gates: Optional[List[str]] = None, banner: str = "") -> None:
        """Record a successful connect's observed profile for ``address``,
        logging a first-capture line, and — critically — a change event when the
        banner fingerprint, prompt, or gate set differs from what we last saw."""
        address = (address or "").strip()
        if not address:
            return
        gates = sorted(set(gates or []))
        fp = _fingerprint(banner)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with self._lock:
                prev = self._profiles.get(address)
                if prev is None:
                    self._profiles[address] = {
                        "address": address, "device_id": device_id,
                        "object_type": object_type, "prompt": prompt,
                        "paging_cmd": paging_cmd, "gates": gates,
                        "banner_fingerprint": fp, "banner_excerpt": _excerpt(banner),
                        "first_seen": now, "last_seen": now, "connect_count": 1,
                    }
                    logger.info(
                        "nw device-profile: recorded %s (%s) prompt=%r paging=%r "
                        "gates=%s", address, object_type or "?", prompt,
                        paging_cmd, gates or "-")
                    self._save()
                    return
                self._note_changes(address, prev, prompt, gates, fp, banner)
                # Merge forward: keep the union of gates ever seen, update the
                # rest to the latest observation.
                prev["device_id"] = device_id or prev.get("device_id", "")
                prev["object_type"] = object_type or prev.get("object_type", "")
                if prompt:
                    prev["prompt"] = prompt
                if paging_cmd:
                    prev["paging_cmd"] = paging_cmd
                prev["gates"] = sorted(set(prev.get("gates", [])) | set(gates))
                if fp:
                    prev["banner_fingerprint"] = fp
                    prev["banner_excerpt"] = _excerpt(banner)
                prev["last_seen"] = now
                prev["connect_count"] = int(prev.get("connect_count", 0)) + 1
                self._save()
        except Exception as e:  # noqa: BLE001 — profiling must never break connect
            logger.debug("device_profile: record failed for %s (%s)", address, e)

    def _note_changes(self, address: str, prev: Dict[str, Any], prompt: str,
                      gates: List[str], fp: str, banner: str) -> None:
        old_fp = prev.get("banner_fingerprint", "")
        if fp and old_fp and fp != old_fp:
            # The headline case the operator cares about: the stable login text
            # changed — most often a legal / T&C / MOTD banner was added.
            logger.warning(
                "nw device-profile: %s login banner CHANGED (a T&C/legal/MOTD "
                "banner may have been added, or firmware changed) — now: %r",
                address, _excerpt(banner, 160))
        old_prompt = prev.get("prompt", "")
        if prompt and old_prompt and prompt != old_prompt:
            logger.warning("nw device-profile: %s prompt changed %r -> %r",
                           address, old_prompt, prompt)
        new_gates = set(gates) - set(prev.get("gates", []))
        if new_gates:
            logger.info("nw device-profile: %s now presents new gate(s): %s",
                        address, sorted(new_gates))


_store: Optional[ProfileStore] = None
_store_lock = threading.Lock()


def store() -> ProfileStore:
    """Process-wide singleton store (lazy)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ProfileStore()
    return _store


def record(**kwargs: Any) -> None:
    """Convenience: record an observation on the singleton store. Best-effort."""
    try:
        store().record(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("device_profile: record() failed (%s)", e)
