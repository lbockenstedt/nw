"""REST transport for the nw drivers (httpx).

Covers AOS-CX RESTv1 (basic auth, JSON arrays) and the Aruba/HPE gateway REST
(bearer token). httpx is lazy-imported so the module imports without it
installed. TLS verification defaults OFF (lab devices often self-signed);
overridable via the ``LM_NW_VERIFY_TLS`` env knob.

JSON→row mappers are pure functions (testable without HTTP); the RestSession
does the GETs and the driver calls the high-level gather functions.
"""
import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

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
    path = "/rest/v1/system" if object_type == "cx_switch" else "/api/system/info"
    mapper = map_system_cx if object_type == "cx_switch" else map_system_gateway
    return mapper(await session.get(path))


async def rest_get_arp(session: RestSession, object_type: str) -> List[dict]:
    path = "/rest/v1/system/arp" if object_type == "cx_switch" else "/api/arp"
    mapper = map_arp_cx if object_type == "cx_switch" else map_arp_gateway
    return mapper(await session.get(path))


async def rest_get_mac_table(session: RestSession, object_type: str) -> List[dict]:
    path = "/rest/v1/system/mac-table" if object_type == "cx_switch" else "/api/mac-table"
    # AOS-CX mac-table mapper ~ gateway mac mapper.
    return map_mac_gateway(await session.get(path))


async def rest_get_interfaces(session: RestSession, object_type: str) -> List[dict]:
    path = "/rest/v1/interfaces" if object_type == "cx_switch" else "/api/interfaces"
    mapper = map_interfaces_cx if object_type == "cx_switch" else map_interfaces_gateway
    return mapper(await session.get(path))