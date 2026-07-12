"""REST transport for the nw drivers (httpx).

Covers AOS-CX RESTv1 (basic auth, JSON arrays) and the Aruba/HPE gateway REST
(bearer token). httpx is lazy-imported so the module imports without it
installed. TLS verification defaults OFF (lab devices often self-signed);
overridable via the ``LM_NW_VERIFY_TLS`` env knob.

JSON→row mappers are pure functions (testable without HTTP); the RestSession
does the GETs and the driver calls the high-level gather functions.
"""
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("NwRest")


class RestError(Exception):
    """Raised on auth/connect/HTTP failures or unexpected JSON shapes."""


def _verify_tls() -> bool:
    return str(os.environ.get("LM_NW_VERIFY_TLS", "")).strip().lower() in (
        "1", "true", "yes", "on")


class RestSession:
    """httpx async client against a device REST API. Use as an async context
    manager, or call connect()/close()."""

    def __init__(self, device: Dict[str, Any], timeout: float = 10.0):
        d = device or {}
        self.host = str(d.get("address") or "").strip()
        self.port = int(d.get("port") or 0)
        self.username = str(d.get("username") or "").strip()
        self.password = str(d.get("password") or "")
        self.api_token = str(d.get("api_token") or "")
        self.object_type = str(d.get("object_type") or "").strip()
        if not self.host:
            raise RestError("device address not configured")
        scheme = "https"
        base = f"{scheme}://{self.host}" + (f":{self.port}" if self.port else "")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def connect(self) -> None:
        import httpx
        headers = {}
        auth = None
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        elif self.username:
            auth = (self.username, self.password)
        if self.object_type == "cx_switch" and self.username and not self.api_token:
            # AOS-CX RESTv1 accepts basic auth; label the session.
            headers.setdefault("Accept", "application/json")
        self._client = httpx.AsyncClient(base_url=self.base, auth=auth,
                                         headers=headers, timeout=self.timeout,
                                         verify=_verify_tls())

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str) -> Any:
        if not self._client:
            await self.connect()
        try:
            r = await self._client.get(path)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise RestError(f"GET {path} on {self.host}: {e}")

    async def post(self, path: str, json: Any = None) -> Any:
        if not self._client:
            await self.connect()
        try:
            r = await self._client.post(path, json=json)
            r.raise_for_status()
            # Tolerate empty success bodies (204 No Content) — r.json() on an
            # empty body raises, so return None for a contentless 2xx.
            return r.json() if r.content else None
        except Exception as e:
            raise RestError(f"POST {path} on {self.host}: {e}")

    async def put(self, path: str, json: Any = None) -> Any:
        if not self._client:
            await self.connect()
        try:
            r = await self._client.put(path, json=json)
            r.raise_for_status()
            return r.json() if r.content else None
        except Exception as e:
            raise RestError(f"PUT {path} on {self.host}: {e}")


# ── Pure JSON mappers (testable) ──────────────────────────────────────────────
def _elements(payload: Any) -> List[dict]:
    """Normalize a REST list payload to a list of dicts. AOS-CX RESTv1 returns
    a bare JSON array; RESTv10 returns {collection: {elements: [...]}}."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        coll = payload.get("collection") or payload.get("elements") or payload
        if isinstance(coll, dict):
            els = coll.get("elements")
            if isinstance(els, list):
                return [x for x in els if isinstance(x, dict)]
    return []


def map_arp_cx(payload: Any) -> List[dict]:
    rows = []
    for e in _elements(payload):
        rows.append({
            "ip": str(e.get("ip_address") or e.get("ip") or "").strip(),
            "mac": str(e.get("mac_address") or e.get("mac") or "").strip(),
            "interface": str(e.get("port_id") or e.get("port") or
                             e.get("interface") or "").strip(),
        })
    return rows


def map_interfaces_cx(payload: Any) -> List[dict]:
    rows = []
    for e in _elements(payload):
        name = str(e.get("name") or e.get("if_name") or "").strip()
        if not name:
            continue
        speed = e.get("speed") or e.get("link_speed") or 0
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = 0
        rows.append({
            "name": name,
            "ip": str(e.get("ip4_address") or e.get("ip_address") or "").strip(),
            "mac": str(e.get("mac_address") or e.get("mac") or "").strip(),
            "vlan": str(e.get("vlan_id") or e.get("vlan") or "").strip(),
            "status": "up" if str(e.get("oper_status") or e.get("link") or
                                  "").lower() in ("up", "active", "1") else "down",
            "speed": speed,
        })
    return rows


def map_system_cx(payload: Any) -> dict:
    p = payload if isinstance(payload, dict) else {}
    return {
        "model": str(p.get("model_name") or p.get("model") or "").strip(),
        "serial": str(p.get("serial_number") or p.get("serial") or "").strip(),
        "firmware": str(p.get("firmware_version") or p.get("os_version") or
                        p.get("firmware") or "").strip(),
        "interfaces_count": int(p.get("port_count") or 0 or 0),
    }


def map_arp_gateway(payload: Any) -> List[dict]:
    rows = []
    for e in _elements(payload):
        rows.append({
            "ip": str(e.get("ip") or e.get("ip_address") or "").strip(),
            "mac": str(e.get("mac") or e.get("mac_address") or "").strip(),
            "interface": str(e.get("interface") or e.get("port") or "").strip(),
        })
    return rows


def map_mac_gateway(payload: Any) -> List[dict]:
    rows = []
    for e in _elements(payload):
        rows.append({
            "mac": str(e.get("mac") or e.get("mac_address") or "").strip(),
            "vlan": str(e.get("vlan") or e.get("vlan_id") or "").strip(),
            "interface": str(e.get("interface") or e.get("port") or "").strip(),
        })
    return rows


def map_interfaces_gateway(payload: Any) -> List[dict]:
    return map_interfaces_cx(payload)


def map_system_gateway(payload: Any) -> dict:
    p = payload if isinstance(payload, dict) else {}
    return {
        "model": str(p.get("model") or p.get("device_model") or "").strip(),
        "serial": str(p.get("serial") or p.get("serial_number") or "").strip(),
        "firmware": str(p.get("firmware") or p.get("version") or
                        p.get("software_version") or "").strip(),
        "interfaces_count": int(p.get("interface_count") or 0 or 0),
    }


# ── High-level async gathers (the RestDriver calls these) ────────────────────
async def rest_get_device_info(session: RestSession, object_type: str) -> dict:
    """GET the vendor system endpoint and map to
    ``{model, serial, firmware, interfaces_count}``."""
    path = "/rest/v1/system" if object_type == "cx_switch" else "/api/system/info"
    mapper = map_system_cx if object_type == "cx_switch" else map_system_gateway
    return mapper(await session.get(path))


async def rest_get_arp(session: RestSession, object_type: str) -> List[dict]:
    """GET the vendor ARP endpoint and map to ``[{ip, mac, interface}]``."""
    path = "/rest/v1/system/arp" if object_type == "cx_switch" else "/api/arp"
    mapper = map_arp_cx if object_type == "cx_switch" else map_arp_gateway
    return mapper(await session.get(path))


async def rest_get_mac_table(session: RestSession, object_type: str) -> List[dict]:
    """GET the vendor MAC-table endpoint and map to
    ``[{mac, vlan, interface}]``."""
    path = "/rest/v1/system/mac-table" if object_type == "cx_switch" else "/api/mac-table"
    # AOS-CX mac-table mapper ~ gateway mac mapper.
    return map_mac_gateway(await session.get(path))


async def rest_get_interfaces(session: RestSession, object_type: str) -> List[dict]:
    """GET the vendor interfaces endpoint and map to
    ``[{name, ip, mac, vlan, status, speed}]``."""
    path = "/rest/v1/interfaces" if object_type == "cx_switch" else "/api/interfaces"
    mapper = map_interfaces_cx if object_type == "cx_switch" else map_interfaces_gateway
    return mapper(await session.get(path))


# ── Cert install (AOS-CX REST v10) ───────────────────────────────────────────
# AOS-CX manages server certs under REST v10 (NOT v1): install the cert+key
# inline via PUT /rest/v10_xx/certificates/{name}, then bind it to the
# https-server via the system configuration's certificate_association. The v10
# minor-version path is device-specific, so it's resolved from the firmware
# version (or an explicit LM_NW_CX_REST_VER override). Only cx_switch is wired
# here — the gateway REST cert endpoint is platform-dependent (ArubaOS
# controller vs AOS-CX gateway) and not implemented; aos_switch / ex_switch
# need SSH/SFTP plumbing that the CLI driver doesn't have yet.

def _cx_cert_name(domain: str) -> str:
    """Sanitize a cert domain into an AOS-CX certificate name
    (``lm-le-<sanitized-domain>``). AOS-CX cert names are conservative on
    allowed characters, so everything outside ``[a-z0-9-]`` becomes ``-``;
    a wildcard leading label (``*.``) becomes ``wild``; an empty domain falls
    back to ``cert``. Capped at 63 chars (a typical name ceiling)."""
    import re
    base = (domain or "cert").strip().lower().replace("*", "wild")
    base = re.sub(r"[^a-z0-9-]", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = "cert"
    return f"lm-le-{base}"[:63]


async def _cx_v10_prefix(session: "RestSession") -> str:
    """Resolve the AOS-CX REST v10 minor-version path prefix (e.g. ``v10_13``).

    Override via ``LM_NW_CX_REST_VER`` (e.g. ``v10_13``) when the firmware
    parse is unavailable or the deployment pins a specific REST version. Else
    parse ``10.XX`` out of ``GET /rest/v1/system``'s ``firmware_version``
    (``RL.10.13.0001`` → ``v10_13``). Falls back to ``v10_09`` (broadly
    supported across current AOS-CX) when neither yields a version."""
    import os
    import re
    override = os.getenv("LM_NW_CX_REST_VER", "").strip().lstrip("/")
    if override:
        return override
    try:
        sysinfo = await session.get("/rest/v1/system")
        fw = str((sysinfo or {}).get("firmware_version")
                 or (sysinfo or {}).get("os_version") or "")
        m = re.search(r"10\.(\d{1,2})", fw)
        if m:
            return f"v10_{int(m.group(1)):02d}"
    except Exception:
        pass
    return "v10_09"


async def rest_install_cert(session: "RestSession", object_type: str,
                            fullchain: str, privkey: str, chain: str,
                            domain: str) -> dict:
    """Install a CA-signed cert (LE fullchain + key) on a REST-managed device
    and bind it to the HTTPS server. AOS-CX (``cx_switch``) only — raises
    ``RestError`` for any other object_type (the caller surfaces the ERROR).

    Sequence (AOS-CX REST v10):
      1. ``PUT /rest/{prefix}/certificates/{cert_name}`` with the fullchain +
         private key concatenated in one ``certificate`` PEM blob (password
         ``""`` — certbot produces an unencrypted key). AOS-CX stores the
         cert+key; it does NOT need a TA profile for the HTTPS *server* role
         (the client validates the leaf; the switch presents it as-is).
      2. ``GET /rest/{prefix}/system?selector=configuration`` → set
         ``certificate_association["https-server"] = cert_name`` →
         ``PUT /rest/{prefix}/system`` with the modified body so the switch
         presents the new cert on its HTTPS endpoint.

    Returns ``{cert_name, service, rest_prefix}``. Raises ``RestError`` with
    the step + host on any failure (the driver wraps it in an ERROR envelope)."""
    if object_type != "cx_switch":
        raise RestError(f"REST cert install not implemented for {object_type}")
    if not fullchain or "BEGIN CERTIFICATE" not in fullchain:
        raise RestError("invalid or empty fullchain PEM")
    if not privkey or "PRIVATE KEY" not in privkey:
        raise RestError("invalid or empty privkey PEM")

    prefix = await _cx_v10_prefix(session)
    cert_name = _cx_cert_name(domain)

    # 1. Install cert + key (inline PEM: fullchain, blank line, privkey).
    cert_blob = fullchain.rstrip() + "\n\n" + privkey.rstrip() + "\n"
    await session.put(f"/rest/{prefix}/certificates/{cert_name}",
                      json={"certificate": cert_blob, "password": ""})

    # 2. Bind it to the https-server via the system configuration.
    syscfg = await session.get(f"/rest/{prefix}/system?selector=configuration")
    if not isinstance(syscfg, dict):
        raise RestError("system configuration GET did not return a JSON object")
    assoc = syscfg.get("certificate_association")
    if not isinstance(assoc, dict):
        raise RestError(
            "system configuration has no certificate_association object "
            "(cannot bind https-server)")
    assoc["https-server"] = cert_name
    await session.put(f"/rest/{prefix}/system", json=syscfg)

    return {"cert_name": cert_name, "service": "https-server",
            "rest_prefix": prefix}