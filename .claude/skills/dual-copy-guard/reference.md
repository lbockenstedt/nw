# Dual-Copy / Twin Pairs — authoritative reference

All paths relative to the workspace root (`/Users/lbockenstedt/vscode`, the nw.git
checkout that holds the sibling repos `lm/`, `cs/`, `pxmx/`, `dns/`, `dhcp/`, …).

Sync-rule legend:
- **byte** — must be byte-identical; one side is canonical, the other is a `cp`.
- **parity** — functional parity across languages/paths; sync behavior + shared
  data (key lists, field arrays, thresholds, logic), not literal text.
- **twin** — two copies of the same module in different repos; keep logic +
  shared constants matched.

---

## 1. sim-views.js  — the sim-config WebUI  (parity)
- `lm/WebUI/sim-views.js`  ⇄  `cs/lm-spoke/static/sim-views.js`
- **Both are hand-edited together.** Neither is canonical.
- Sync: the sim name lists, `CS_CONTROL_FLAGS`, the sim-conf editor field arrays
  (the `[simulation]`/`[address]` `['key','Label'[,'type']]` rows), the
  `csSimField` types (`'list'` etc.) and serializers.
- **Also both backends** when the change needs server support (e.g. the `'list'`
  textarea serializer needed backend handling on each side).
- Compare: diff the field arrays + flag lists by hand; they are not textually
  identical (different surrounding shell), so judge intent.

## 2. client common.sh  — shared bash lib  (byte)
- CANONICAL: `cs/clients/lib/common.sh`
- GENERATED: `cs/clients/linux/common.sh`  (the deploy path ships flat files, so
  the lib is `cp`-copied)
- Rule: **byte-identical**. Edit the canonical, then:
  `cp cs/clients/lib/common.sh cs/clients/linux/common.sh`
- Verify: `cmp cs/clients/lib/common.sh cs/clients/linux/common.sh`
- The generated copy carries a "GENERATED-COPY NOTICE" header pointing here.

## 3. common.sh ⇄ common.ps1  — client shared lib, cross-platform  (parity)
- `cs/clients/lib/common.sh`  ⇄  `cs/clients/windows/common.ps1`
- Windows is PowerShell 5.1; port behavior, not text. Specifically keep matched:
  - `CS_OVERRIDE_KEYS` (bash array) == `$script:CS_OVERRIDE_KEYS` (ps1 array)
  - the bucket algorithm = **crc32(hostname) % 10** on BOTH (must also match the
    spoke's `sim_config.bucket_for()`; Windows once used SHA256 — that was a bug)
  - the DNS ceiling / gateway circuit-breaker helpers and the dns_lat selection
    (threshold/recheck/probe/select) semantics
- Compare: read both, confirm the key lists + thresholds + algorithm match.

## 4. linux ⇄ windows client scripts  — full sim parity  (parity)
- `cs/clients/linux/*.sh`  ⇄  `cs/clients/windows/*.ps1`
- Every sim and orchestrator behavior exists on BOTH platforms. When a sim or a
  behavior lands on one, it must land on the other (see the `add-simulation`
  skill for the per-sim recipe).
- Cross-platform mapping (bash → PowerShell): `dig`→`Resolve-DnsName`,
  `nmcli`/`wpa_supplicant`→`netsh wlan`/WLAN-profile XML, `ip route`→`Get-NetRoute`,
  `ip link set`→`Enable/Disable-NetAdapter`, `apt`→`winget`, `pgrep -f`→
  `Get-CimInstance Win32_Process`, `nohup … &`→`Start-Process`, systemd/.desktop→
  Scheduled Task, `shutdown -r +N`(min)→`shutdown /r /t`(sec).
- Orchestrators to keep aligned: `simulation.sh`↔`simulation.ps1` (bucket read,
  `randomizable_sims`, override list, dispatch, `report_status`/`Send-Status`
  active_simulations+config), `update.sh`↔`update.ps1` (content-hash
  `/api/scripts/manifest?platform=<p>` sha256 sync + `/api/config/overrides`),
  `startup.sh`↔`startup.ps1`.
- Version headers: `version=` (`.sh`) / `$version =` (`.ps1`) — hand-bumped;
  keep the fleet baseline consistent (currently `0.01`).

## 5. sim_quota.py  — quota engine hub/spoke twin  (twin)
- `cs/lm-spoke/src/sim_quota.py`  ⇄  `lm/core/src/simulations/sim_quota.py`
- Keep matched: `SIM_QUOTA_KEYS`, `QUOTA_TIERS`, normalization defaults, and any
  shared quota logic. A key added on one side but not the other breaks the
  hub↔spoke quota contract.
- Note the separate but related engine `cs/lm-spoke/src/sim_quota_engine.py`
  (spoke-side placement/harvest/stacking) has no hub twin — don't confuse it.

## 6. dns / dhcp  — dual module copies  (twin, drift-prone)
- `dns/`  ⇄  `lm/dns/`   and   `dhcp/`  ⇄  `lm/dhcp/`
- These intentionally exist in two shapes (standalone module repo + the copy
  under `lm/`). **Don't delete either.** When you touch one, port to the other.
- (The workspace root itself is the nw.git checkout — see the memory
  `vscode-root-is-nw-checkout-and-dns-dhcp-dual-shape`.)

## 7. hub ⇄ cs-spoke twin for SHARED client-sim modules  (twin)
- A fix to a client-simulation module that exists on BOTH the hub and the cs
  spoke must be ported to the spoke twin. Test paths and Alert paths can diverge
  if only one is fixed (lesson: `mist-cs-spoke-port-gap-lesson`).
- Example shape: an Aruba-Central/Mist client module living in `lm/core/...`
  with a mirror used by the cs spoke. When you fix the hub copy, grep the cs
  spoke (`cs/lm-spoke/src/…`) for the twin and apply the same fix.

## 8. dns_fail.txt  — bogus-domains data file  (byte, triple)
- `cs/configs/dns_fail.txt`  ⇄  `cs/clients/linux/dns_fail.txt`  ⇄
  `cs/clients/windows/dns_fail.txt`
- The DNS-failure sim's flood list. Keep the three copies identical when
  regenerated. Verify with `cmp` pairwise.

---

## Quick audit commands
```bash
cd /Users/lbockenstedt/vscode
# byte pairs
cmp cs/clients/lib/common.sh cs/clients/linux/common.sh && echo "common.sh in sync"
cmp cs/configs/dns_fail.txt cs/clients/linux/dns_fail.txt && echo "dns_fail.txt L in sync"
cmp cs/configs/dns_fail.txt cs/clients/windows/dns_fail.txt && echo "dns_fail.txt W in sync"
# parity/twin pairs — read + compare the shared data, don't cmp:
#   CS_OVERRIDE_KEYS   in cs/clients/lib/common.sh vs cs/clients/windows/common.ps1
#   SIM_QUOTA_KEYS     in cs/lm-spoke/src/sim_quota.py vs lm/core/src/simulations/sim_quota.py
#   sim lists+fields   in lm/WebUI/sim-views.js vs cs/lm-spoke/static/sim-views.js
#   linux *.sh sims    vs windows *.ps1 sims (filename set + dispatch)
```

When adding to this reference: a pair belongs here if editing one side without
the other produces a silent, hard-to-spot bug. Record the canonical direction
and the sync rule.
