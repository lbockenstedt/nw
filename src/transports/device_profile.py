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
# Postpone evaluation of type hints.
from __future__ import annotations

# hashlib makes the short banner fingerprint.
import hashlib
# json reads/writes the profile store file.
import json
# Standard-library logging (shares the "NwCli" logger with the CLI transport).
import logging
# os handles paths, directory creation and the atomic file replace.
import os
# re strips volatile parts of the banner.
import re
# threading.Lock protects the in-memory store across worker threads.
import threading
# time formats the first/last-seen timestamps.
import time
# Type hints.
from typing import Any, Dict, List, Optional

# Same logger as cli_io so profile events appear next to the connect logs.
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
# Dates, clock times and IPv4 addresses are also volatile and removed.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _norm_banner(banner: str) -> str:
    """Normalize a login banner for fingerprinting: drop ANSI, the volatile
    "previous login" line, and any dates/times/IPs, then collapse whitespace.
    What remains is the *stable* legal/copyright/T&C text — the thing we want to
    notice a change in."""
    # Remove terminal escape codes, then the volatile parts, one at a time.
    s = _ANSI_RE.sub("", banner or "")
    s = _PREV_LOGIN_RE.sub("", s)
    s = _DATE_RE.sub("", s)
    s = _TIME_RE.sub("", s)
    s = _IP_RE.sub("", s)
    # Collapse whitespace so reflowed text still matches.
    return " ".join(s.split())


def _fingerprint(banner: str) -> str:
    """Short stable hash of the normalised banner, or "" when it is empty."""
    norm = _norm_banner(banner)
    # Nothing stable left: no fingerprint.
    if not norm:
        return ""
    # First 16 hex chars of SHA-256 are plenty to detect a change.
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:16]


def _excerpt(banner: str, limit: int = 200) -> str:
    """First ``limit`` characters of the normalised banner (for logs/status)."""
    norm = _norm_banner(banner)
    return norm[:limit]


class ProfileStore:
    """Loads/persists per-device connection profiles and records observations,
    logging first-capture and change events."""

    def __init__(self, path: str = _DEFAULT_PATH):
        """Create the store for ``path`` and load any previously saved profiles."""
        # File path, lock and address -> profile dict.
        self.path = path
        self._lock = threading.Lock()
        self._profiles: Dict[str, Dict[str, Any]] = {}
        # Read the saved profiles (if any).
        self._load()

    def _load(self) -> None:
        """Load profiles from disk; missing or corrupt files leave the store empty."""
        try:
            # Read the JSON file.
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Keep only well-formed entries (address -> dict).
            if isinstance(data, dict):
                self._profiles = {k: v for k, v in data.items()
                                  if isinstance(v, dict)}
        except FileNotFoundError:
            # First run: no file yet.
            pass
        except Exception as e:  # noqa: BLE001 — corrupt/unreadable store is non-fatal
            # Corrupt or unreadable file: start empty.
            logger.debug("device_profile: load failed (%s)", e)

    def _save(self) -> None:
        """Write profiles to disk atomically; failures leave them in memory only."""
        try:
            # Make sure the directory exists.
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # Write to a temp file first, then rename over the real file so a crash
            # mid-write can never leave a half-written store.
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._profiles, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001 — read-only fs / perms: in-memory only
            # Read-only filesystem or permissions: keep in memory only.
            logger.debug("device_profile: save failed (%s)", e)

    def get(self, address: str) -> Optional[Dict[str, Any]]:
        """Profile recorded for ``address``, or None."""
        return self._profiles.get(address)

    def all(self) -> Dict[str, Dict[str, Any]]:
        """Copy of every profile (safe to hand to callers)."""
        with self._lock:
            return {k: dict(v) for k, v in self._profiles.items()}

    def record(self, *, address: str, device_id: str = "", object_type: str = "",
               prompt: str = "", paging_cmd: str = "",
               gates: Optional[List[str]] = None, banner: str = "") -> None:
        """Record a successful connect's observed profile for ``address``,
        logging a first-capture line, and — critically — a change event when the
        banner fingerprint, prompt, or gate set differs from what we last saw."""
        # Ignore calls without an address.
        address = (address or "").strip()
        if not address:
            return
        # De-duplicate and sort the gate names; fingerprint the banner; current UTC time.
        gates = sorted(set(gates or []))
        fp = _fingerprint(banner)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Any error is logged and swallowed so the connect path is never affected.
        try:
            # One writer at a time.
            with self._lock:
                prev = self._profiles.get(address)
                # First connect to this address: create its profile, log it and save.
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
                # Seen before: log anything that changed.
                self._note_changes(address, prev, prompt, gates, fp, banner)
                # Merge forward: keep the union of gates ever seen, update the
                # rest to the latest observation.
                prev["device_id"] = device_id or prev.get("device_id", "")
                prev["object_type"] = object_type or prev.get("object_type", "")
                # Only overwrite prompt/paging command when this connect observed them.
                if prompt:
                    prev["prompt"] = prompt
                if paging_cmd:
                    prev["paging_cmd"] = paging_cmd
                prev["gates"] = sorted(set(prev.get("gates", [])) | set(gates))
                # Only replace the banner data when this connect saw a banner.
                if fp:
                    prev["banner_fingerprint"] = fp
                    prev["banner_excerpt"] = _excerpt(banner)
                # Bump the last-seen time and connect counter, then save.
                prev["last_seen"] = now
                prev["connect_count"] = int(prev.get("connect_count", 0)) + 1
                self._save()
        except Exception as e:  # noqa: BLE001 — profiling must never break connect
            # Never break a connect because of profiling.
            logger.debug("device_profile: record failed for %s (%s)", address, e)

    def _note_changes(self, address: str, prev: Dict[str, Any], prompt: str,
                      gates: List[str], fp: str, banner: str) -> None:
        """Log a warning/info line for each part of the login surface that changed."""
        # Compare the stored banner fingerprint with the new one.
        old_fp = prev.get("banner_fingerprint", "")
        if fp and old_fp and fp != old_fp:
            # The headline case the operator cares about: the stable login text
            # changed — most often a legal / T&C / MOTD banner was added.
            logger.warning(
                "nw device-profile: %s login banner CHANGED (a T&C/legal/MOTD "
                "banner may have been added, or firmware changed) — now: %r",
                address, _excerpt(banner, 160))
        # Hostname/prompt changed since last connect.
        old_prompt = prev.get("prompt", "")
        if prompt and old_prompt and prompt != old_prompt:
            logger.warning("nw device-profile: %s prompt changed %r -> %r",
                           address, old_prompt, prompt)
        # Gates seen now that were never seen before.
        new_gates = set(gates) - set(prev.get("gates", []))
        if new_gates:
            logger.info("nw device-profile: %s now presents new gate(s): %s",
                        address, sorted(new_gates))


# Lazily-created process-wide store and the lock guarding its creation.
_store: Optional[ProfileStore] = None
_store_lock = threading.Lock()


def store() -> ProfileStore:
    """Process-wide singleton store (lazy)."""
    # Double-checked locking: create the store only once even with many threads.
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ProfileStore()
    # The shared store.
    return _store


def record(**kwargs: Any) -> None:
    """Convenience: record an observation on the singleton store. Best-effort."""
    # Swallow any error: recording is optional.
    try:
        store().record(**kwargs)
    except Exception as e:  # noqa: BLE001
        # Log at debug only.
        logger.debug("device_profile: record() failed (%s)", e)
