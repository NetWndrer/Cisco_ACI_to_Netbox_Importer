#!/usr/bin/env python3
"""
aci_collector.py

Collects active fabric switches (leafs/spines) from a Cisco APIC and syncs them into NetBox
(source of truth).

Standalone by design: it shares no code with netbox_ios_nxos_importer.py, but mirrors that
importer's capabilities and conventions (interface naming, interface typing, device-type
matching, IP handling, inventory, dry-run, lockout safety, summary report) so both tools
produce consistent NetBox data. Each platform importer stays independent so new platforms can
be added later without coupling.

What it syncs per fabric node:
  - Device:      created if missing; serial, platform, device type (strict matching;
                 overwriting an existing device's type is opt-in)
  - Mgmt IP:     assigned to the management interface and set as primary IP
                 (mask inferred from NetBox prefixes)
  - Interfaces:  physical ports, port-channels, loopbacks, routed subinterfaces
                 (description, MTU, speed, admin state, type; subinterfaces linked to parent)
  - IPs:         loopback / routed port / L3Out subinterface IPs.
                 Tenant SVI (pervasive gateway) IPs are never synced: the same anycast IP exists
                 on every leaf.
  - Fabric infra: loopbacks and routed subinterfaces in the infra VRF (overlay-1: TEPs, anycast
                 proxies, the leaf<->spine fabric links) are APIC-managed plumbing and are skipped
                 by default (ACI_SYNC_INFRA=true to include them and their IPs).
  - Inventory:   line cards, supervisors, fabric modules, transceivers

All data comes from a few fabric-wide APIC REST class queries: one APIC login per run,
no SSH to the switches.

Configuration (environment variables; most also have CLI flags, see --help):
  APIC_HOST, APIC_USER, APIC_PASS, APIC_VERIFY_SSL (default false)
  NETBOX_URL, NETBOX_TOKEN, NETBOX_VERIFY_SSL (default false)
  DRY_RUN, OVERWRITE_DEVICE_TYPE, AUTO_CREATE_DEVICE_TYPES, LOG_LEVEL
  ACI_NODE (single node name), ACI_ROLE (leaf | spine)
  ACI_SITE_MAP ("PATTERN=site-slug,PATTERN2=site-slug2") or ACI_SITE_MAP_FILE
  (default: aci_site_map.json next to this script), ACI_DEFAULT_SITE
  ACI_SYNC_INTERFACES, ACI_SYNC_INTERFACE_IPS, ACI_SYNC_INVENTORY (default true)
  ACI_SYNC_INFRA (default false)
"""

from __future__ import annotations

import argparse
import functools
import getpass
import ipaddress
import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

import pynetbox
import requests
import urllib3

# Suppress insecure connection warnings when SSL verification is disabled
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("aci_collector")


# ==========================================
# Configuration
# ==========================================
def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y")


REQUEST_TIMEOUT = (5, 30)   # (connect, read) seconds
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2      # seconds, doubles each attempt
APIC_PAGE_SIZE = 5000       # objects per APIC page for class queries

ACI_PLATFORM_SLUG = "aci"           # matches existing NetBox platform slug
MGMT_INTERFACE_NAME = "management"  # matches existing NetBox interface convention
DEFAULT_DEVICE_STATUS = "active"    # APIC query already filters to fabricSt="active"
DEFAULT_DEVICE_TYPE_SLUG = "unknown-aci-switch"  # placeholder for new devices with unknown model
DEFAULT_MANUFACTURER_NAME = "Cisco"
# APIC role -> allowed NetBox device role slugs. The FIRST slug is used when creating a device;
# an existing device whose role is any slug in the list is left as-is (no drift warning).
# Override with aci_role_map.json, --role-map-file / ACI_ROLE_MAP_FILE, or ACI_ROLE_MAP.
DEFAULT_ROLE_MAP = {
    "leaf": ["aci-leaf"],
    "spine": ["aci-spine"],
    "default": ["aci-switch"],
}

AUTO_CREATE_DEVICE_TYPES = env_bool("AUTO_CREATE_DEVICE_TYPES", False)
SYNC_INTERFACES = env_bool("ACI_SYNC_INTERFACES", True)
SYNC_INTERFACE_IPS = env_bool("ACI_SYNC_INTERFACE_IPS", True)
SYNC_INVENTORY = env_bool("ACI_SYNC_INVENTORY", True)
SYNC_INFRA = env_bool("ACI_SYNC_INFRA", False)  # infra-VRF loopbacks/subinterfaces and their IPs
INFRA_VRF_NAMES = {"overlay-1"}     # ACI infra VRF (TEPs, fabric links)

SITE_MAP_FILE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aci_site_map.json")
ROLE_MAP_FILE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aci_role_map.json")


# ==========================================
# Normalized device record
# ==========================================
@dataclass
class DeviceRecord:
    """Common shape the collector normalizes to before handing off to the NetBox sync step."""
    name: str
    mgmt_ip: str                  # bare IP, no CIDR
    mgmt_source: str              # "oob" | "inb" | "address" - useful for troubleshooting
    role: str                     # "leaf" | "spine"
    model: Optional[str] = None
    serial: Optional[str] = None
    pod_id: Optional[str] = None
    node_id: Optional[str] = None
    dn: Optional[str] = None      # e.g. "topology/pod-1/node-101"
    version: Optional[str] = None
    platform: str = "aci"
    # Same shapes the IOS/NX-OS importer uses:
    #   interfaces:    {canonical_name: {description, mtu, state, enabled, speed}}
    #   inventory:     {"chassis": [...], "modules": [...], "transceivers": [...]}, items {name, pid, sn}
    #   interface_ips: [(canonical_interface_name, "a.b.c.d/len", vrf)]
    interfaces: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=lambda: {"chassis": [], "modules": [], "transceivers": []})
    interface_ips: list = field(default_factory=list)
    infra_interfaces_skipped: int = 0
    port_usage: dict = field(default_factory=dict)   # physical port -> set of APIC usage values
    collection_warnings: list = field(default_factory=list)


# ==========================================
# Name / slug normalization (mirrors netbox_ios_nxos_importer.py)
# ==========================================
def normalize_slug(slug) -> str:
    """Normalize slugs for robust, case-insensitive string matching"""
    if not slug:
        return ""
    return str(slug).lower().replace("-", "").replace("_", "").replace("/", "").replace(" ", "").strip()


def slugify_model(model_str) -> str:
    """Convert a hardware model/PID string into a valid NetBox URL-safe slug"""
    if not model_str:
        return ""
    return re.sub(r'[^a-z0-9]+', '-', str(model_str).lower()).strip('-')


def normalize_interface_name(name) -> str:
    """Normalize Cisco interface abbreviations to canonical full names.
    ACI reports 'eth1/1', 'po1', 'lo0', 'eth1/49.10' -> 'Ethernet1/1', 'Port-channel1', 'Loopback0',
    'Ethernet1/49.10' (same names the NX-OS importer produces)."""
    if not name:
        return ""
    name = name.strip()

    # Order matters; every pattern must be followed by a digit (see lookahead below).
    mapping = {
        r'^(gigabitethernet|gigabit|gige|gig|gi)': 'GigabitEthernet',
        r'^(tengigabitethernet|tengigabit|tengige|tengig|teng|ten|te)': 'TenGigabitEthernet',
        r'^(twentyfivegigabitethernet|twentyfivegigabit|twentyfivegige|twentyfivegig|twentyfiveg|twe)': 'TwentyFiveGigabitEthernet',
        r'^(twogigabitethernet|twogige|tw)': 'TwoGigabitEthernet',
        r'^(fivegigabitethernet|fivegige|fi)': 'FiveGigabitEthernet',
        r'^(appgigabitethernet|ap)': 'AppGigabitEthernet',
        r'^(fortygigabitethernet|fortygigabit|fortygige|fortyg|fo)': 'FortyGigabitEthernet',
        r'^(hundredgigabitethernet|hundredgigabit|hundredgige|hundredgig|hundredg|hu)': 'HundredGigabitEthernet',
        r'^(fastethernet|fast|fa)': 'FastEthernet',
        r'^(ethernet|eth)': 'Ethernet',
        r'^(port-channel|po)': 'Port-channel',
        r'^(vlan|vl)': 'Vlan',
        r'^(loopback|lo)': 'Loopback',
        r'^(mgmt-ethernet|management|mgmt)': 'mgmt',
        r'^(me)': 'Management',
    }
    for pattern, canonical in mapping.items():
        match = re.match(pattern + r'(?=\s*\d)', name, re.IGNORECASE)
        if match:
            return f"{canonical}{name[match.end():].strip()}"
    return name


# ==========================================
# Speed parsing and interface typing (mirrors netbox_ios_nxos_importer.py, plus 400G)
# ==========================================
def parse_speed(speed_val) -> Optional[int]:
    """Convert a speed string ('10G', 'a-1000', '100M', '400G') to Mbps; None if unknown/auto/inherit."""
    if not speed_val:
        return None
    speed_str = str(speed_val).lower().strip()
    speed_str = re.sub(r'^(?:speed\s*:?\s*|a-|auto-)', '', speed_str).strip()
    if speed_str in ("", "unassigned", "unknown", "inherit") or "auto" in speed_str:
        return None

    for regex, factor in ((r'([\d.]+)\s*(?:g|gbps)', 1000), (r'([\d.]+)\s*(?:m|mbps)', 1)):
        m = re.search(regex, speed_str)
        if m:
            try:
                return int(float(m.group(1)) * factor)
            except ValueError:
                pass
    m = re.search(r'([\d.]+)\s*(?:k|kbps|kbit)', speed_str)
    if m:
        try:
            return int(float(m.group(1)) / 1000)
        except ValueError:
            pass

    digits = "".join(filter(str.isdigit, speed_str))
    if not digits:
        return None
    speed_int = int(digits)
    if speed_int >= 1000000000:
        return int(speed_int / 1000000)
    if speed_int >= 10000:
        if speed_int in (10000, 25000, 40000, 100000, 400000):
            return speed_int
        return int(speed_int / 1000)
    for speed in (10, 100, 1000, 2500, 5000, 10000, 25000, 40000, 100000, 400000):
        if abs(speed_int - speed) / speed <= 0.05:
            return speed
    return None


def classify_interface_type(name: str) -> str:
    """Name-based NetBox interface type (fallback when no speed is known)."""
    n = name.lower()
    if n.startswith("po") or "port-channel" in n:
        return "lag"
    if n.startswith(("vl", "lo")) or "vlan" in n or "loopback" in n or "." in n:
        return "virtual"
    if n.startswith(("mgmt", "me")):
        return "1000base-t"
    if re.search(r'^(hu|hundred)', n):
        return "100gbase-x-cxp"
    if re.search(r'^(fo|forty)', n):
        return "40gbase-x-qsfpp"
    if re.search(r'^(twe|twentyfive)', n):
        return "25gbase-x-sfp28"
    if re.search(r'^(te|ten)', n):
        return "10gbase-x-sfpp"
    return "1000base-t"


def classify_interface_type_by_speed(name: str, speed_str) -> str:
    """NetBox interface type from name + operational speed."""
    n = name.lower()
    if n.startswith("po") or "port-channel" in n:
        return "lag"
    if n.startswith(("vl", "lo")) or "vlan" in n or "loopback" in n or "." in n:
        return "virtual"
    speed = parse_speed(speed_str)
    if speed:
        if speed >= 400000:
            return "400gbase-x-qsfpdd"
        if speed >= 100000:
            return "100gbase-x-cxp"
        if speed >= 40000:
            return "40gbase-x-qsfpp"
        if speed >= 25000:
            return "25gbase-x-sfp28"
        if speed >= 10000:
            return "10gbase-x-sfpp"
        if speed >= 5000:
            return "5gbase-t"
        if speed >= 2500:
            return "2.5gbase-t"
        if speed >= 1000:
            return "1000base-t"
        if speed >= 100:
            return "100base-tx"
    return classify_interface_type(name)


TYPE_SPEED_RANK = {
    "": 0, "other": 0, "100base-tx": 1, "1000base-t": 2, "2.5gbase-t": 3, "5gbase-t": 4,
    "10gbase-x-sfpp": 5, "25gbase-x-sfp28": 6, "40gbase-x-qsfpp": 7, "100gbase-x-cxp": 8,
    "400gbase-x-qsfpdd": 9,
}
GENERIC_TYPES = set(TYPE_SPEED_RANK)


def should_update_type(current_type, correct_type) -> bool:
    """Only upgrade generic types; never demote speed, never touch LAG/virtual or specific optic types."""
    if not correct_type:
        return False
    curr = str(current_type or "").lower().strip()
    corr = str(correct_type).lower().strip()
    if curr == corr or curr in ("virtual", "lag"):
        return False
    if TYPE_SPEED_RANK.get(curr, 0) > TYPE_SPEED_RANK.get(corr, 0):
        return False
    if curr not in GENERIC_TYPES:
        for band in (("400g",), ("100g",), ("40g",), ("25g",), ("10g",), ("1000base", "1g")):
            if any(b in curr for b in band) and any(b in corr for b in band):
                return False
    return True


# ==========================================
# Device type resolution (mirrors netbox_ios_nxos_importer.py)
# ==========================================
def build_device_type_cache(nb) -> dict:
    """Pre-load all DeviceTypes and Manufacturers from NetBox into a fast lookup structure."""
    cache = {"by_exact": {}, "by_norm": {}, "all": [], "manufacturers": {}}
    try:
        for mfg in nb.dcim.manufacturers.all():
            cache["manufacturers"][normalize_slug(mfg.name)] = mfg.id
            cache["manufacturers"][normalize_slug(mfg.slug)] = mfg.id
        for dt in nb.dcim.device_types.all():
            cache["all"].append(dt)
            for val in ((dt.model or "").strip(), (getattr(dt, "part_number", "") or "").strip()):
                if val:
                    cache["by_exact"].setdefault(val.lower(), []).append(dt)
                    cache["by_norm"].setdefault(normalize_slug(val), []).append(dt)
            if dt.slug:
                cache["by_norm"].setdefault(normalize_slug(dt.slug), []).append(dt)
        log.info("Pre-loaded %d device types from NetBox", len(cache["all"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to pre-load device types from NetBox: %s", exc)
    return cache


def candidate_model_variants(raw_model: str) -> list[str]:
    """PID/model variants incl. friendly Cisco names (N9K-C93180YC-FX <-> C93180YC-FX <-> Nexus 93180YC-FX)."""
    if not raw_model:
        return []
    variants: list[str] = []

    def add(v):
        if v and v not in variants:
            variants.append(v)

    clean = raw_model.strip()
    add(clean)
    add(re.sub(r'/K9$', '', clean, flags=re.IGNORECASE).strip())
    for base in list(variants):
        nx = re.match(r'^(?:N[3579]K-)?C([3579]\d{3}\S*)$', base.upper())
        if nx:
            core = nx.group(1)
            add(f"N{core[0]}K-C{core}")
            add(f"C{core}")
            add(f"Nexus {core}")
            add(f"Nexus C{core}")
    return variants


def _disambiguate(candidates, cisco_mfg_id, discovered_model=""):
    unique = list({dt.id: dt for dt in candidates}.values())
    if len(unique) <= 1:
        return unique
    if cisco_mfg_id:
        cisco = [dt for dt in unique if getattr(dt, "manufacturer", None) and dt.manufacturer.id == cisco_mfg_id]
        if len(cisco) == 1:
            return cisco
        if cisco:
            unique = cisco
    if discovered_model and len(unique) > 1:
        pid = discovered_model.strip().lower()
        exact = [dt for dt in unique if (getattr(dt, "part_number", "") or "").strip().lower() == pid]
        if len(exact) == 1:
            return exact
    return unique


def resolve_device_type(nb, device_name, discovered_model, dt_cache, dry_run=False):
    """Resolve a hardware model to a NetBox DeviceType. Returns (device_type_or_None, info_str).
    Exact -> normalized -> fuzzy suffix -> optional auto-create; refuses ambiguous matches."""
    if not discovered_model or not dt_cache:
        return None, "No model or device type cache available"
    variants = candidate_model_variants(discovered_model)
    cisco_mfg_id = dt_cache["manufacturers"].get(normalize_slug(DEFAULT_MANUFACTURER_NAME))

    steps = (
        ("exact", lambda: [dt for v in variants for dt in dt_cache["by_exact"].get(v.lower(), [])]),
        ("normalized", lambda: [dt for v in variants for dt in dt_cache["by_norm"].get(normalize_slug(v), [])]),
        ("fuzzy-suffix", lambda: [
            dt for v in variants if len(normalize_slug(v)) >= 5 for dt in dt_cache["all"]
            if normalize_slug(dt.model).endswith(normalize_slug(v))
            or (normalize_slug(getattr(dt, "part_number", "")) and normalize_slug(getattr(dt, "part_number", "")).endswith(normalize_slug(v)))
        ]),
    )
    for method, finder in steps:
        hits = _disambiguate(finder(), cisco_mfg_id, discovered_model)
        if len(hits) == 1:
            log.debug("[%s] Matched DeviceType '%s' via %s", device_name, hits[0].model, method)
            return hits[0], method
        if len(hits) > 1:
            names = ", ".join(f"{d.model} (ID:{d.id})" for d in hits)
            return None, f"Ambiguous {method} DeviceType match for '{discovered_model}': [{names}]"

    if not AUTO_CREATE_DEVICE_TYPES:
        return None, f"Hardware model '{discovered_model}' not found in NetBox Device Types"
    if not cisco_mfg_id:
        return None, f"Cannot auto-create DeviceType '{discovered_model}': manufacturer '{DEFAULT_MANUFACTURER_NAME}' not found"

    clean = discovered_model.strip()
    if dry_run:
        log.info("[DRY RUN] [%s] Would auto-create DeviceType %s", device_name, clean)
        return None, f"DeviceType '{clean}' would be auto-created (dry run)"
    try:
        new_dt = nb.dcim.device_types.create(manufacturer=cisco_mfg_id, model=clean,
                                             slug=slugify_model(clean), part_number=clean)
        dt_cache["all"].append(new_dt)
        dt_cache["by_exact"].setdefault(clean.lower(), []).append(new_dt)
        dt_cache["by_norm"].setdefault(normalize_slug(clean), []).append(new_dt)
        log.info("[DEVICE-TYPE] Auto-created DeviceType %s (ID: %s)", clean, new_dt.id)
        return new_dt, "auto-created"
    except Exception as exc:  # noqa: BLE001
        return None, f"Failed to auto-create DeviceType '{clean}': {exc}"


# ==========================================
# APIC helpers
# ==========================================
class APICAuthError(Exception):
    """APIC rejected the credentials. Never retried (account lockout protection)."""


def with_retries(retry_on=(requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt < MAX_RETRIES:
                        wait = RETRY_BACKOFF_BASE ** attempt
                        log.warning("%s failed (attempt %d/%d): %s - retrying in %ds",
                                    func.__name__, attempt, MAX_RETRIES, exc, wait)
                        time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator


NODE_DN_RE = re.compile(r'^(topology/pod-\d+/node-\d+)/')


def node_dn_of(dn: Optional[str]) -> Optional[str]:
    m = NODE_DN_RE.match(dn or "")
    return m.group(1) if m else None


def unwrap(obj: dict) -> tuple[str, dict, list]:
    """APIC imdata item {className: {attributes, children}} -> (className, attributes, children)."""
    if not obj:
        return "", {}, []
    cls, body = next(iter(obj.items()))
    return cls, (body or {}).get("attributes", {}) or {}, (body or {}).get("children", []) or []


def child_attrs(children: list, cls_name: str) -> dict:
    for ch in children:
        cls, attrs, _ = unwrap(ch)
        if cls == cls_name:
            return attrs
    return {}


def to_int(val) -> Optional[int]:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


# ==========================================
# DeviceCollector ABC
# ==========================================
class DeviceCollector(ABC):
    """Platform collectors return normalized DeviceRecords for the NetBox sync step."""

    @abstractmethod
    def collect(self) -> list[DeviceRecord]:
        ...


class ACICollector(DeviceCollector):
    def __init__(self, apic_host: str, user: str, password: str, verify_ssl: bool = False,
                 name_filter: Optional[str] = None, role_filter: Optional[str] = None):
        self.apic_host = apic_host
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.name_filter = (name_filter or "").strip().lower() or None
        self.role_filter = (role_filter or "").strip().lower() or None

    # ---- session lifecycle -------------------------------------------------
    # Login is retried ONLY on connection failures (request never reached the APIC).
    # A read timeout or an auth rejection is not retried, so one run = at most one counted login.
    @with_retries(retry_on=(requests.exceptions.ConnectionError,))
    def _post_login(self, url: str, payload: dict):
        return self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)

    def _login(self) -> None:
        url = f"https://{self.apic_host}/api/aaaLogin.json"
        payload = {"aaaUser": {"attributes": {"name": self.user, "pwd": self.password}}}
        res = self._post_login(url, payload)
        if res.status_code in (401, 403):
            raise APICAuthError(
                f"APIC {self.apic_host} rejected the credentials (HTTP {res.status_code}); "
                f"not retried to avoid account lockout"
            )
        res.raise_for_status()
        log.info("Authenticated to APIC %s", self.apic_host)

    def _logout(self) -> None:
        try:
            self.session.post(
                f"https://{self.apic_host}/api/aaaLogout.json",
                json={"aaaUser": {"attributes": {"name": self.user}}},
                timeout=REQUEST_TIMEOUT,
            )
            log.info("Logged out of APIC session")
        except requests.exceptions.RequestException as exc:
            log.warning("APIC logout failed (non-fatal): %s", exc)

    @contextmanager
    def _apic_session(self) -> Iterator[None]:
        self._login()
        try:
            yield
        finally:
            self._logout()

    # ---- data retrieval ------------------------------------------------------
    @with_retries()
    def _get_json(self, url: str) -> dict:
        res = self.session.get(url, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        try:
            return res.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            log.error("APIC returned non-JSON response for %s", url)
            return {}

    def _get_class(self, cls: str, params: str = "") -> list[dict]:
        """Fabric-wide class query with paging."""
        out: list[dict] = []
        page = 0
        while True:
            query = f"{params}&" if params else ""
            url = (f"https://{self.apic_host}/api/node/class/{cls}.json?"
                   f"{query}page-size={APIC_PAGE_SIZE}&page={page}")
            data = self._get_json(url)
            imdata = data.get("imdata", []) or []
            out.extend(imdata)
            total = to_int(data.get("totalCount")) or 0
            if not imdata or len(out) >= total:
                break
            page += 1
        log.debug("APIC class %s: %d objects", cls, len(out))
        return out

    def _get_fabric_nodes(self) -> list[dict]:
        return self._get_class(
            "fabricNode",
            'query-target-filter=and(not(wcard(fabricNode.role,"controller")),eq(fabricNode.fabricSt,"active"))',
        )

    @staticmethod
    def _clean_ip(raw: Optional[str]) -> Optional[str]:
        """Strip CIDR, validate, reject 0.0.0.0/unset."""
        if not raw:
            return None
        try:
            ip = ipaddress.ip_address(raw.split("/")[0])
        except ValueError:
            return None
        return None if ip.is_unspecified else str(ip)

    def _resolve_mgmt_ip(self, attrs: dict) -> tuple[Optional[str], str]:
        """Prefer OOB, fall back to in-band, then to node address."""
        for key, source in (("oobMgmtAddr", "oob"), ("inbMgmtAddr", "inb"), ("address", "address")):
            ip = self._clean_ip(attrs.get(key))
            if ip:
                return ip, source
        return None, "none"

    # ---- per-object-class collection ------------------------------------------
    def _collect_interfaces(self, records: dict[str, DeviceRecord]) -> None:
        # Physical ports + live state
        for obj in self._get_class("l1PhysIf", "rsp-subtree=children&rsp-subtree-class=ethpmPhysIf"):
            _, a, children = unwrap(obj)
            rec = records.get(node_dn_of(a.get("dn")))
            if not rec or not a.get("id"):
                continue
            oper = child_attrs(children, "ethpmPhysIf")
            oper_st = (oper.get("operSt") or "down").lower()
            oper_speed = oper.get("operSpeed") or ""
            # Prefer the negotiated speed when the port is up; otherwise an explicitly configured speed
            speed = oper_speed if oper_st == "up" and parse_speed(oper_speed) else (a.get("speed") or "")
            # APIC port usage, e.g. "discovery", "fabric", "epg", "infra,epg"
            rec.port_usage[normalize_interface_name(a["id"])] = {
                u.strip().lower() for u in str(a.get("usage") or "").split(",") if u.strip()}
            rec.interfaces[normalize_interface_name(a["id"])] = {
                "description": a.get("descr", "") or "",
                "mtu": to_int(a.get("mtu")),          # None when "inherit"
                "state": oper_st,
                "enabled": (a.get("adminSt") or "up").lower() == "up",
                "speed": speed,
            }

        # Port-channels (+ live state)
        for obj in self._get_class("pcAggrIf", "rsp-subtree=children&rsp-subtree-class=ethpmAggrIf"):
            _, a, children = unwrap(obj)
            rec = records.get(node_dn_of(a.get("dn")))
            if not rec or not a.get("id"):
                continue
            oper = child_attrs(children, "ethpmAggrIf")
            rec.interfaces[normalize_interface_name(a["id"])] = {
                # ACI usually leaves descr empty; the interface policy group name is the useful label
                "description": a.get("descr") or a.get("name") or "",
                "mtu": to_int(a.get("mtu")),
                "state": (oper.get("operSt") or "down").lower(),
                "enabled": (a.get("adminSt") or "up").lower() == "up",
                "speed": oper.get("operSpeed") or "",
            }

        # Loopbacks and routed (L3Out) subinterfaces
        for cls in ("l3LbRtdIf", "l3EncRtdIf"):
            for obj in self._get_class(cls):
                _, a, _ = unwrap(obj)
                rec = records.get(node_dn_of(a.get("dn")))
                if not rec or not a.get("id"):
                    continue
                enabled = (a.get("adminSt") or "up").lower() == "up"
                rec.interfaces[normalize_interface_name(a["id"])] = {
                    "description": a.get("descr", "") or "",
                    "mtu": to_int(a.get("mtu")),
                    "state": "up" if enabled else "down",
                    "enabled": enabled,
                    "speed": "",
                }

        if not SYNC_INFRA:
            self._drop_infra_interfaces(records)

    IPV4_IF_DN_RE = re.compile(r'/dom-([^/]+)/if-\[([^\]]+)\]')

    def _drop_infra_interfaces(self, records: dict[str, DeviceRecord]) -> None:
        """Remove loopbacks and routed subinterfaces that live in the infra VRF (overlay-1).
        Physical ports are always kept, even when they carry fabric links."""
        try:
            infra_ifs = self._get_class(
                "ipv4If", 'query-target-filter=or(' + ",".join(
                    f'wcard(ipv4If.dn,"/dom-{v}/")' for v in sorted(INFRA_VRF_NAMES)) + ')')
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not identify fabric infra interfaces (keeping them all): %s", exc)
            for rec in records.values():
                rec.collection_warnings.append(f"Infra interface filter unavailable: {exc}")
            return

        infra_by_node: dict[str, set[str]] = {}
        for obj in infra_ifs:
            _, a, _ = unwrap(obj)
            dn = a.get("dn", "")
            m = self.IPV4_IF_DN_RE.search(dn)
            node = node_dn_of(dn)
            if m and node and m.group(1) in INFRA_VRF_NAMES:
                infra_by_node.setdefault(node, set()).add(normalize_interface_name(m.group(2)))

        for node_dn, rec in records.items():
            infra = infra_by_node.get(node_dn, set())
            dropped = [n for n in rec.interfaces if n in infra and (n.startswith("Loopback") or "." in n)]
            for n in dropped:
                del rec.interfaces[n]
            if dropped:
                log.debug("[%s] Skipping %d fabric infra interfaces: %s", rec.name, len(dropped), dropped)
                rec.infra_interfaces_skipped = len(dropped)

    FABRIC_PORT_USAGE = {"fabric", "fabric-ext", "discovery", "infra"}

    def _drop_fabric_leftovers(self, records: dict[str, DeviceRecord], ips_known: bool) -> None:
        """Second pass for fabric plumbing the overlay-1 check can't see (e.g. the subinterface APIC
        pre-builds on every unconnected spine port, which isn't bound to overlay-1 until a link comes up):
          - routed subinterfaces whose parent port APIC uses as a fabric/discovery port
          - loopbacks that carry no IP outside the infra VRF (only when IPs were collected)"""
        for rec in records.values():
            ips_on = {if_name for if_name, _cidr, _vrf in rec.interface_ips}
            dropped = []
            for name in list(rec.interfaces):
                if "." in name and rec.port_usage.get(name.split(".")[0], set()) & self.FABRIC_PORT_USAGE:
                    dropped.append(name)
                elif ips_known and name.startswith("Loopback") and name not in ips_on:
                    dropped.append(name)
            for name in dropped:
                del rec.interfaces[name]
            if dropped:
                rec.infra_interfaces_skipped += len(dropped)
                log.debug("[%s] Skipping %d fabric/unused interfaces: %s", rec.name, len(dropped), dropped)

    IPV4_ADDR_DN_RE = re.compile(r'/dom-([^/]+)/if-\[([^\]]+)\]/addr-\[([^\]]+)\]')

    def _collect_interface_ips(self, records: dict[str, DeviceRecord]) -> None:
        for obj in self._get_class("ipv4Addr"):
            _, a, _ = unwrap(obj)
            dn = a.get("dn", "")
            rec = records.get(node_dn_of(dn))
            m = self.IPV4_ADDR_DN_RE.search(dn)
            if not rec or not m:
                continue
            vrf, raw_if, cidr = m.group(1), m.group(2), a.get("addr") or m.group(3)
            if_name = normalize_interface_name(raw_if)
            if if_name.lower().startswith("vlan"):
                continue  # tenant SVI / pervasive gateway: same anycast IP on every leaf
            if vrf in INFRA_VRF_NAMES and not SYNC_INFRA:
                continue
            if not (if_name.startswith(("Loopback", "Ethernet"))):
                continue
            try:
                ipaddress.ip_interface(cidr)
            except ValueError:
                continue
            rec.interface_ips.append((if_name, cidr, vrf))

    SLOT_RE = re.compile(r'/(lc|sup|fc)slot-(\d+)/')
    SLOT_LABEL = {"lc": "Linecard", "sup": "Supervisor", "fc": "Fabric Module"}

    def _collect_inventory(self, records: dict[str, DeviceRecord]) -> None:
        for obj in self._get_class("eqptCh"):
            _, a, _ = unwrap(obj)
            rec = records.get(node_dn_of(a.get("dn")))
            if rec and (a.get("model") or a.get("ser")):
                rec.inventory["chassis"].append(
                    {"name": "Chassis", "pid": (a.get("model") or "").strip(), "sn": (a.get("ser") or "").strip()})

        for cls in ("eqptLC", "eqptSupC", "eqptFC"):
            for obj in self._get_class(cls):
                _, a, _ = unwrap(obj)
                dn = a.get("dn", "")
                rec = records.get(node_dn_of(dn))
                pid, sn = (a.get("model") or "").strip(), (a.get("ser") or "").strip()
                if not rec or not (pid or sn):
                    continue
                m = self.SLOT_RE.search(dn)
                name = f"{self.SLOT_LABEL[m.group(1)]} {m.group(2)}" if m else (a.get("descr") or cls)
                rec.inventory["modules"].append({"name": name, "pid": pid, "sn": sn})

        for obj in self._get_class("ethpmFcot"):
            _, a, _ = unwrap(obj)
            dn = a.get("dn", "")
            rec = records.get(node_dn_of(dn))
            if not rec or str(a.get("isFcotPresent", "")).lower() not in ("true", "yes"):
                continue
            port = re.search(r'/phys-\[([^\]]+)\]', dn)
            sn = (a.get("guiSN") or "").strip()
            if not port or not sn:
                continue
            rec.inventory["transceivers"].append({
                "name": f"{normalize_interface_name(port.group(1))} Transceiver",
                "pid": (a.get("guiPN") or a.get("typeName") or "").strip(),
                "sn": sn,
            })

        # Drop modules duplicating the chassis serial (e.g. fixed leaf 'Linecard 1')
        for rec in records.values():
            ch_sns = {c["sn"] for c in rec.inventory["chassis"] if c.get("sn")} | ({rec.serial} if rec.serial else set())
            rec.inventory["modules"] = [m for m in rec.inventory["modules"] if not (m["sn"] and m["sn"] in ch_sns)]

    # ---- main collection -------------------------------------------------------
    def collect(self) -> list[DeviceRecord]:
        records: dict[str, DeviceRecord] = {}
        with self._apic_session():
            nodes = self._get_fabric_nodes()
            log.info("Retrieved %d active fabric nodes from APIC", len(nodes))

            for node in nodes:
                _, attrs, _ = unwrap(node)
                name = attrs.get("name")
                role = (attrs.get("role") or "unknown").lower()
                if self.name_filter and (name or "").lower() != self.name_filter:
                    continue
                if self.role_filter and role != self.role_filter:
                    continue
                mgmt_ip, source = self._resolve_mgmt_ip(attrs)
                if not mgmt_ip:
                    log.warning("Skipping %s - no usable OOB or in-band mgmt IP", name)
                    continue
                dn = attrs.get("dn") or f"topology/pod-{attrs.get('podId')}/node-{attrs.get('id')}"
                records[dn] = DeviceRecord(
                    name=name, mgmt_ip=mgmt_ip, mgmt_source=source, role=role,
                    model=attrs.get("model"), serial=attrs.get("serial"),
                    pod_id=attrs.get("podId"), node_id=attrs.get("id"),
                    dn=dn, version=attrs.get("version"),
                )

            if not records:
                return []

            # Each block is isolated: a failed query degrades that data set, not the whole run
            collected = set()
            for enabled, label, func in (
                (SYNC_INTERFACES, "interfaces", self._collect_interfaces),
                (SYNC_INTERFACES and SYNC_INTERFACE_IPS, "interface IPs", self._collect_interface_ips),
                (SYNC_INVENTORY, "inventory", self._collect_inventory),
            ):
                if not enabled:
                    continue
                try:
                    func(records)
                    collected.add(label)
                except APICAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.error("APIC %s collection failed: %s", label, exc)
                    for rec in records.values():
                        rec.collection_warnings.append(f"APIC {label} collection failed: {exc}")

            if "interfaces" in collected and not SYNC_INFRA:
                self._drop_fabric_leftovers(records, ips_known="interface IPs" in collected)

        for rec in records.values():
            log.info(
                "[%s] APIC data -> version: %s, model: %s, serial: %s, interfaces: %d "
                "(+%d fabric infra skipped), IPs: %d, modules: %d, transceivers: %d",
                rec.name, rec.version or "N/A", rec.model or "N/A", rec.serial or "N/A",
                len(rec.interfaces), rec.infra_interfaces_skipped, len(rec.interface_ips),
                len(rec.inventory["modules"]), len(rec.inventory["transceivers"]),
            )
        return list(records.values())


# ==========================================
# NetBox sync
# ==========================================
class NetBoxSyncer:
    """Idempotent create/update into NetBox."""

    def __init__(self, url: str, token: str, verify_ssl: bool = False, dry_run: bool = False,
                 overwrite_device_type: bool = False, role_map: Optional[dict[str, list[str]]] = None):
        self.nb = pynetbox.api(url, token=token)
        self.nb.http_session.verify = verify_ssl
        self.dry_run = dry_run
        self.overwrite_device_type = overwrite_device_type
        self.role_map = role_map or DEFAULT_ROLE_MAP
        self._lookup_cache: dict[tuple[str, str], object] = {}
        self._dt_cache: Optional[dict] = None
        self._prefix_cache: Optional[dict] = None

    # ---- lookups / caches ----------------------------------------------------
    def _cached_get(self, endpoint, kind: str, slug: str):
        key = (kind, slug)
        if key not in self._lookup_cache:
            self._lookup_cache[key] = endpoint.get(slug=slug)
        return self._lookup_cache[key]

    @staticmethod
    def _safe_get(endpoint, **filters):
        """endpoint.get() that tolerates multiple matches (pynetbox raises ValueError)."""
        try:
            return endpoint.get(**filters)
        except ValueError:
            results = list(endpoint.filter(**filters))
            log.warning("Multiple NetBox objects match %s; using the first (ID %s)", filters, results[0].id)
            return results[0]

    @property
    def dt_cache(self) -> dict:
        if self._dt_cache is None:
            self._dt_cache = build_device_type_cache(self.nb)
        return self._dt_cache

    @property
    def prefix_cache(self) -> dict:
        if self._prefix_cache is None:
            self._prefix_cache = {}
            try:
                for p in self.nb.ipam.prefixes.all():
                    try:
                        self._prefix_cache[ipaddress.ip_network(str(p.prefix), strict=False)] = None
                    except ValueError:
                        pass
                log.info("Cached %d subnet prefixes from NetBox", len(self._prefix_cache))
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to pre-load prefixes (will fall back to /32 or /128): %s", exc)
        return self._prefix_cache

    def _infer_prefixlen(self, host: str) -> int:
        """Mask of the most specific NetBox prefix containing the host; /32 (/128) if none."""
        ip = ipaddress.ip_address(host)
        matches = [net for net in self.prefix_cache if net.version == ip.version and ip in net]
        if matches:
            return max(matches, key=lambda n: n.prefixlen).prefixlen
        return 32 if ip.version == 4 else 128

    def _find_existing(self, device: DeviceRecord):
        nb_device = None
        if device.serial:
            nb_device = self._safe_get(self.nb.dcim.devices, serial=device.serial)
        if not nb_device:
            nb_device = self._safe_get(self.nb.dcim.devices, name=device.name)
        return nb_device

    # ---- writes ----------------------------------------------------------------
    def _patch(self, record, fields: dict, what: str, result: dict) -> bool:
        """Isolated PATCH of only the given fields; on failure revert the attributes so they
        don't ride along on a later save()."""
        if not fields:
            return False
        if self.dry_run:
            log.info("[DRY RUN] [%s] Would update %s: %s", result["device"], what, fields)
            return True
        originals = {k: getattr(record, k, None) for k in fields}
        try:
            for k, v in fields.items():
                setattr(record, k, v)
            record.save()
            log.info("[%s] Updated %s: %s", result["device"], what, fields)
            return True
        except Exception as exc:  # noqa: BLE001
            for k, v in originals.items():
                setattr(record, k, v)
            detail = getattr(exc, "error", str(exc))
            log.error("[%s] NetBox rejected %s update %s: %s", result["device"], what, fields, detail)
            result["warnings"].append(f"{what} update failed: {detail}")
            return False

    def _assign_ip(self, address: str, iface, nb_device, result: dict):
        """Assign an IP (host or CIDR) to an interface. Looks up by host regardless of mask, infers
        the mask from NetBox prefixes when none is given, never steals an IP from another device."""
        host = address.split("/")[0]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            result["warnings"].append(f"Invalid IP '{address}'")
            return None

        existing = list(self.nb.ipam.ip_addresses.filter(address=host))
        ip_obj = None
        if existing:
            on_iface = [i for i in existing if getattr(i, "assigned_object_id", None) == iface.id
                        and getattr(i, "assigned_object_type", None) == "dcim.interface"]
            ip_obj = on_iface[0] if on_iface else existing[0]
            if len(existing) > 1:
                log.warning("[%s] %d NetBox IPs match %s; using %s", result["device"], len(existing), host, ip_obj.address)
        else:
            cidr = address if "/" in address else f"{host}/{self._infer_prefixlen(host)}"
            if self.dry_run:
                log.info("[DRY RUN] [%s] Would create IP %s on %s", result["device"], cidr, iface.name)
                result["ips_synced"] += 1
                return None
            ip_obj = self.nb.ipam.ip_addresses.create(
                address=cidr, assigned_object_type="dcim.interface", assigned_object_id=iface.id)
            log.info("[%s] Created IP %s on %s", result["device"], cidr, iface.name)
            result["ips_synced"] += 1
            return ip_obj

        if getattr(ip_obj, "assigned_object_type", None) == "dcim.interface" and ip_obj.assigned_object_id == iface.id:
            return ip_obj

        # Don't take an IP away from a different device
        assigned = getattr(ip_obj, "assigned_object", None)
        owner = getattr(assigned, "device", None) if assigned else None
        if owner is not None and getattr(owner, "id", None) not in (None, nb_device.id):
            msg = f"IP {ip_obj.address} is assigned to another device ({getattr(owner, 'name', owner.id)}); not reassigning"
            log.warning("[%s] %s", result["device"], msg)
            result["warnings"].append(msg)
            return None

        if self._patch(ip_obj, {"assigned_object_type": "dcim.interface", "assigned_object_id": iface.id},
                       f"IP {ip_obj.address} -> {iface.name}", result):
            result["ips_synced"] += 1
        return ip_obj

    def _find_or_create_mgmt_interface(self, nb_device, result: dict):
        if iface := self._safe_get(self.nb.dcim.interfaces, device_id=nb_device.id, name=MGMT_INTERFACE_NAME):
            return iface
        if self.dry_run:
            log.info("[DRY RUN] [%s] Would create missing '%s' interface", result["device"], MGMT_INTERFACE_NAME)
            return None
        log.info("[%s] Creating missing '%s' interface", result["device"], MGMT_INTERFACE_NAME)
        return self.nb.dcim.interfaces.create(device=nb_device.id, name=MGMT_INTERFACE_NAME,
                                              type="virtual", mgmt_only=True)

    def _sync_mgmt_ip(self, nb_device, device: DeviceRecord, result: dict) -> None:
        iface = self._find_or_create_mgmt_interface(nb_device, result)
        if not iface:
            if self.dry_run:
                log.info("[DRY RUN] [%s] Would assign mgmt IP %s to '%s' and set it as primary IP",
                         result["device"], device.mgmt_ip, MGMT_INTERFACE_NAME)
                result["ips_synced"] += 1
            return
        ip_obj = self._assign_ip(device.mgmt_ip, iface, nb_device, result)
        if not ip_obj:
            return
        field_name = "primary_ip4" if ipaddress.ip_address(device.mgmt_ip).version == 4 else "primary_ip6"
        current = getattr(nb_device, field_name, None)
        if getattr(current, "id", None) != ip_obj.id:
            self._patch(nb_device, {field_name: ip_obj.id}, f"{field_name}", result)

    def _enrich_interface(self, iface, details: dict, result: dict) -> None:
        updates = {}
        desc = details.get("description") or ""
        if desc and iface.description != desc:
            updates["description"] = desc
        mtu = details.get("mtu")
        if isinstance(mtu, int) and mtu > 0 and iface.mtu != mtu:
            updates["mtu"] = mtu
        speed = parse_speed(details.get("speed"))
        if speed and iface.speed != speed * 1000:
            # NetBox stores interface speed in Kbps
            updates["speed"] = speed * 1000
        enabled = details.get("enabled", True)
        if iface.enabled != enabled:
            updates["enabled"] = enabled
        current_type = getattr(iface.type, "value", iface.type) if iface.type else ""
        correct_type = classify_interface_type_by_speed(iface.name, details.get("speed"))
        if should_update_type(current_type, correct_type):
            updates["type"] = correct_type
        self._patch(iface, updates, f"interface {iface.name}", result)

    def _sync_interfaces(self, nb_device, device: DeviceRecord, result: dict) -> None:
        existing = {i.name: i for i in self.nb.dcim.interfaces.filter(device_id=nb_device.id)}
        ips_by_if: dict[str, list[str]] = {}
        vrf_of: dict[str, str] = {}
        for if_name, cidr, vrf in device.interface_ips:
            ips_by_if.setdefault(if_name, []).append(cidr)
            vrf_of[cidr] = vrf

        parents = {n: d for n, d in device.interfaces.items() if "." not in n}
        subifs = {n: d for n, d in device.interfaces.items() if "." in n}
        planned: set[str] = set()   # dry run: interfaces already reported as "would create"

        for name, details in list(parents.items()) + list(subifs.items()):
            try:
                is_sub = "." in name
                iface = existing.get(name)
                if not iface:
                    # Same rule as the IOS/NX-OS importer: create interfaces that are up or carry an IP;
                    # interfaces that already exist in NetBox are always enriched.
                    if details.get("state") != "up" and name not in ips_by_if:
                        continue
                    if_type = "virtual" if is_sub else classify_interface_type_by_speed(name, details.get("speed"))
                    payload = {"device": nb_device.id, "name": name, "type": if_type,
                               "enabled": details.get("enabled", True)}
                    if is_sub:
                        parent_name = name.split(".")[0]
                        parent = existing.get(parent_name)
                        if not parent:
                            if self.dry_run:
                                if parent_name not in planned:
                                    log.info("[DRY RUN] [%s] Would create parent interface %s",
                                             result["device"], parent_name)
                                    planned.add(parent_name)
                            else:
                                parent = self.nb.dcim.interfaces.create(
                                    device=nb_device.id, name=parent_name, type=classify_interface_type(parent_name))
                                existing[parent_name] = parent
                                log.info("[%s] Created parent interface %s", result["device"], parent_name)
                        if parent:
                            payload["parent"] = parent.id
                    if self.dry_run:
                        log.info("[DRY RUN] [%s] Would create interface %s (%s)", result["device"], name, if_type)
                        planned.add(name)
                        for cidr in ips_by_if.get(name, []):
                            log.info("[DRY RUN] [%s] Would assign IP %s (VRF %s) to %s",
                                     result["device"], cidr, vrf_of.get(cidr, "?"), name)
                            result["ips_synced"] += 1
                        result["interfaces_synced"] += 1
                        continue
                    iface = self.nb.dcim.interfaces.create(**payload)
                    existing[name] = iface
                    log.info("[%s] Created interface %s (%s)", result["device"], name, if_type)
                elif is_sub:
                    parent = existing.get(name.split(".")[0])
                    current_parent = getattr(iface, "parent", None)
                    if parent and getattr(current_parent, "id", None) != parent.id:
                        self._patch(iface, {"parent": parent.id}, f"parent of {name}", result)

                self._enrich_interface(iface, details, result)
                for cidr in ips_by_if.get(name, []):
                    self._assign_ip(cidr, iface, nb_device, result)
                result["interfaces_synced"] += 1
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] Failed to sync interface %s: %s", result["device"], name, exc)
                result["warnings"].append(f"Interface {name} sync failed: {exc}")

    def _sync_inventory(self, nb_device, device: DeviceRecord, result: dict) -> None:
        existing = {i.name: i for i in self.nb.dcim.inventory_items.filter(device_id=nb_device.id)}
        items = [dict(m, label="Module") for m in device.inventory.get("modules", [])] + \
                [dict(x, label="Transceiver") for x in device.inventory.get("transceivers", [])]
        for item in items:
            name, pid, sn, label = item["name"], item.get("pid") or "", item.get("sn") or "", item["label"]
            try:
                if name in existing:
                    cur = existing[name]
                    updates = {k: v for k, v in (("part_id", pid), ("serial", sn), ("label", label))
                               if getattr(cur, k, None) != v}
                    if self._patch(cur, updates, f"inventory item {name}", result):
                        result["inventory_synced"] += 1
                elif self.dry_run:
                    log.info("[DRY RUN] [%s] Would create inventory item %s (Part: %s, Serial: %s)",
                             result["device"], name, pid, sn)
                    result["inventory_synced"] += 1
                else:
                    self.nb.dcim.inventory_items.create(device=nb_device.id, name=name, part_id=pid,
                                                        serial=sn, label=label)
                    log.info("[%s] Created inventory item %s", result["device"], name)
                    result["inventory_synced"] += 1
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] Failed to sync inventory item %s: %s", result["device"], name, exc)
                result["warnings"].append(f"Inventory item {name} sync failed: {exc}")

    # ---- per-device entry point ------------------------------------------------
    def sync(self, device: DeviceRecord, site_slug: Optional[str]) -> dict:
        result = {
            "device": device.name, "status": "failed", "created": False, "serial_updated": False,
            "device_type_updated": False, "interfaces_synced": 0, "ips_synced": 0,
            "inventory_synced": 0, "error": None, "warnings": list(device.collection_warnings),
        }

        # --- Pre-flight entity validation (cached across devices) ---
        # Platform is set on every device. Site and role are only needed when CREATING a device;
        # existing devices keep their NetBox site and role, so a missing mapping never blocks them.
        platform = self._cached_get(self.nb.dcim.platforms, "platform", ACI_PLATFORM_SLUG)
        if not platform:
            raise ValueError(f"Platform with slug '{ACI_PLATFORM_SLUG}' does not exist in NetBox")
        allowed_roles = self.role_map.get(device.role) or self.role_map["default"]

        nb_device = self._find_existing(device)

        if nb_device:
            # --- EXISTING DEVICE ---
            existing_role_slug = nb_device.role.slug if getattr(nb_device, "role", None) else None
            if existing_role_slug and existing_role_slug not in allowed_roles:
                msg = (f"Role drift: NetBox has role '{existing_role_slug}', APIC reports '{device.role}' "
                       f"(expected one of {allowed_roles}). Not overwriting.")
                log.warning("[%s] %s", device.name, msg)
                result["warnings"].append(msg)

            # Each field group is its own PATCH so one rejection can't block the others
            identity = {}
            if nb_device.name != device.name:
                identity["name"] = device.name
            if getattr(getattr(nb_device, "platform", None), "id", None) != platform.id:
                identity["platform"] = platform.id
            self._patch(nb_device, identity, "name/platform", result)

            if device.serial and nb_device.serial != device.serial:
                result["serial_updated"] = self._patch(nb_device, {"serial": device.serial}, "serial", result)

            current_model = nb_device.device_type.model if getattr(nb_device, "device_type", None) else ""
            if device.model and normalize_slug(device.model) != normalize_slug(current_model):
                if not self.overwrite_device_type:
                    log.info("[%s] Hardware model differs (APIC: '%s' vs NetBox: '%s'), but "
                             "OVERWRITE_DEVICE_TYPE is disabled (skipping device_type update)",
                             device.name, device.model, current_model)
                else:
                    dt, info = resolve_device_type(self.nb, device.name, device.model, self.dt_cache, self.dry_run)
                    if dt and dt.id != nb_device.device_type.id:
                        result["device_type_updated"] = self._patch(
                            nb_device, {"device_type": dt.id}, f"device_type ({current_model} -> {dt.model}, via {info})", result)
                    elif not dt:
                        log.warning("[%s] %s (current: '%s')", device.name, info, current_model)
                        result["warnings"].append(f"{info} (current: '{current_model}')")
        else:
            # --- NEW DEVICE ---
            if not site_slug:
                raise ValueError(f"Cannot create '{device.name}': no site-map pattern matches its name "
                                 f"and no ACI_DEFAULT_SITE is set")
            site = self._cached_get(self.nb.dcim.sites, "site", site_slug)
            if not site:
                raise ValueError(f"Site with slug '{site_slug}' does not exist in NetBox")
            role_slug = allowed_roles[0]
            role = self._cached_get(self.nb.dcim.device_roles, "role", role_slug)
            if not role:
                raise ValueError(f"Cannot create '{device.name}': Device Role '{role_slug}' (for APIC role "
                                 f"'{device.role}') does not exist in NetBox. Check the role map.")

            dt, info = resolve_device_type(self.nb, device.name, device.model, self.dt_cache, self.dry_run)
            if not dt:
                dt = self._cached_get(self.nb.dcim.device_types, "device_type", DEFAULT_DEVICE_TYPE_SLUG)
                if not dt:
                    raise ValueError(f"Default Device Type slug '{DEFAULT_DEVICE_TYPE_SLUG}' not found in NetBox")
                log.warning("No matching NetBox device_type for model=%s (%s): %s - using placeholder %s",
                            device.model, device.name, info, DEFAULT_DEVICE_TYPE_SLUG)
                result["warnings"].append(f"{info}; used placeholder '{DEFAULT_DEVICE_TYPE_SLUG}'")

            create_payload = {
                "name": device.name, "site": site.id, "role": role.id, "platform": platform.id,
                "device_type": dt.id, "status": DEFAULT_DEVICE_STATUS, "serial": device.serial or "",
            }
            if self.dry_run:
                log.info("[DRY RUN] Would create NetBox device %s with payload: %s", device.name, create_payload)
                log.info("[DRY RUN] [%s] Interfaces, IPs and inventory would be synced after creation "
                         "(%d interfaces, %d IPs, %d modules, %d transceivers collected)",
                         device.name, len(device.interfaces), len(device.interface_ips),
                         len(device.inventory["modules"]), len(device.inventory["transceivers"]))
                result["created"] = True
                result["status"] = "success"
                return result
            nb_device = self.nb.dcim.devices.create(**create_payload)
            log.info("Created NetBox device: %s", device.name)
            result["created"] = True

        # --- Children: each block isolated so one failure doesn't skip the rest ---
        for label, func, enabled in (
            ("management IP", self._sync_mgmt_ip, True),
            ("interfaces", self._sync_interfaces, SYNC_INTERFACES),
            ("inventory", self._sync_inventory, SYNC_INVENTORY),
        ):
            if not enabled:
                continue
            try:
                func(nb_device, device, result)
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] %s sync failed: %s", device.name, label, exc)
                result["warnings"].append(f"{label} sync failed: {exc}")

        result["status"] = "success"
        return result


# ==========================================
# Site mapping (kept out of the code: environment-specific)
# ==========================================
def load_site_map(path: Optional[str] = None) -> dict[str, str]:
    """Hostname substring -> NetBox site slug.
    Source: ACI_SITE_MAP env ("PATTERN=site-slug,PATTERN2=site-slug2") if set, otherwise a JSON object
    file ({"PATTERN": "site-slug"}) from --site-map-file / ACI_SITE_MAP_FILE / aci_site_map.json."""
    env_map = os.getenv("ACI_SITE_MAP", "").strip()
    if env_map:
        site_map = {}
        for pair in env_map.split(","):
            if "=" in pair:
                pattern, slug = pair.split("=", 1)
                if pattern.strip() and slug.strip():
                    site_map[pattern.strip().upper()] = slug.strip()
        return site_map

    path = path or os.getenv("ACI_SITE_MAP_FILE") or SITE_MAP_FILE_DEFAULT
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Site map file {path} must contain a JSON object of {{\"PATTERN\": \"site-slug\"}}")
    return {str(k).strip().upper(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}


def load_role_map(path: Optional[str] = None) -> dict[str, list[str]]:
    """APIC role ('leaf' / 'spine' / 'default') -> list of allowed NetBox device role slugs.
    Source: ACI_ROLE_MAP env ("leaf=dc-leaf|dc-bl-switch,spine=dc-spine") if set, otherwise a JSON
    file ({"leaf": ["dc-leaf", "dc-bl-switch"], "spine": "dc-spine"}) from --role-map-file /
    ACI_ROLE_MAP_FILE / aci_role_map.json, otherwise DEFAULT_ROLE_MAP. The first slug in each list
    is used when creating a device. 'default' (used for any other APIC role) falls back to the leaf list."""
    raw: dict = {}
    env_map = os.getenv("ACI_ROLE_MAP", "").strip()
    if env_map:
        for pair in env_map.split(","):
            if "=" in pair:
                key, slugs = pair.split("=", 1)
                raw[key] = slugs.split("|")
    else:
        path = path or os.getenv("ACI_ROLE_MAP_FILE") or ROLE_MAP_FILE_DEFAULT
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError(f"Role map file {path} must contain a JSON object like {{\"leaf\": [\"slug\"]}}")
    if not raw:
        return {k: list(v) for k, v in DEFAULT_ROLE_MAP.items()}

    role_map: dict[str, list[str]] = {}
    for key, slugs in raw.items():
        slugs = [slugs] if isinstance(slugs, str) else list(slugs or [])
        cleaned = [str(s).strip() for s in slugs if str(s).strip()]
        if str(key).strip() and cleaned:
            role_map[str(key).strip().lower()] = cleaned
    if "default" not in role_map:
        role_map["default"] = role_map.get("leaf") or next(iter(role_map.values()), DEFAULT_ROLE_MAP["default"])
    return role_map


def resolve_site_slug(device_name: str, site_map: dict[str, str], default_site: Optional[str] = None) -> str:
    """Map a device hostname to a NetBox site slug via the configured substring patterns."""
    name_upper = (device_name or "").upper()
    for pattern, slug in site_map.items():
        if pattern in name_upper:
            return slug
    if default_site:
        return default_site
    raise ValueError(f"Cannot map site for '{device_name}': no site-map pattern matches and no ACI_DEFAULT_SITE set")


# ==========================================
# Summary report
# ==========================================
def print_summary(results: list[dict], dry_run: bool) -> None:
    width = 110
    print("\n" + "=" * width)
    title = "SYNCHRONIZATION SUMMARY" + (" (DRY RUN)" if dry_run else "")
    print(title.center(width))
    print("=" * width)
    print(f"{'Device Name':<25} | {'Status':<8} | {'Created':<7} | {'Ports':<5} | {'IPs':<4} | "
          f"{'Inv':<4} | {'Serial':<9} | {'Type':<9} | {'Error Details':<15}")
    print("-" * width)
    counts = {"success": 0, "skipped": 0, "failed": 0}
    for r in results:
        dev = r.get("device") or "Unknown"
        status = r.get("status", "failed")
        counts[status if status in counts else "failed"] += 1
        err = r.get("error") or "None"
        print(f"{(dev[:22] + '...') if len(dev) > 25 else dev:<25} | {status.upper():<8} | "
              f"{'YES' if r.get('created') else 'no':<7} | {r.get('interfaces_synced', 0):<5} | "
              f"{r.get('ips_synced', 0):<4} | {r.get('inventory_synced', 0):<4} | "
              f"{'UPDATED' if r.get('serial_updated') else 'NO CHANGE':<9} | "
              f"{'UPDATED' if r.get('device_type_updated') else 'NO CHANGE':<9} | "
              f"{(err[:12] + '...') if len(err) > 15 else err:<15}")
    print("=" * width)
    print(f"TOTAL: {len(results)}  |  SUCCESS: {counts['success']}  |  SKIPPED: {counts['skipped']}  |  "
          f"FAILED: {counts['failed']}")
    print("=" * width + "\n")

    if any(r.get("warnings") for r in results):
        print("=" * width)
        print("DETAILED NON-FATAL WARNINGS/ERRORS".center(width))
        print("=" * width)
        for r in results:
            if r.get("warnings"):
                print(f"\n[{r.get('device')}] Non-Fatal Issues:")
                for w in r["warnings"]:
                    print(f"  - {w}")
        print("=" * width + "\n")


# ==========================================
# Entry point
# ==========================================
def normalize_netbox_url(url: str) -> str:
    """Accept 'netbox.example.org' as well as 'https://netbox.example.org/': add https:// when no
    scheme is given, strip a trailing '/' or '/api'."""
    url = (url or "").strip().rstrip("/")
    if not re.match(r'^https?://', url, re.IGNORECASE):
        url = f"https://{url}"
        log.warning("NETBOX_URL has no http:// or https:// prefix; assuming %s", url)
    if url.lower().endswith("/api"):
        url = url[:-4]
    return url


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Sync active Cisco ACI fabric nodes from APIC to NetBox.")
    parser.add_argument("-d", "--dry-run", action="store_true", default=None,
                        help="Fetch from APIC and validate against NetBox, but commit no changes (or DRY_RUN=true).")
    parser.add_argument("--overwrite-device-type", action="store_true", default=None,
                        help="Overwrite an existing device's NetBox device type with the APIC model "
                             "(or OVERWRITE_DEVICE_TYPE=true).")
    parser.add_argument("--node", default=os.getenv("ACI_NODE", ""),
                        help="Only sync this fabric node name (or ACI_NODE).")
    parser.add_argument("--role", choices=("leaf", "spine"), default=(os.getenv("ACI_ROLE") or None),
                        help="Only sync leafs or spines (or ACI_ROLE).")
    parser.add_argument("--role-map-file", default=None,
                        help="JSON file mapping APIC roles (leaf/spine/default) to NetBox device role slugs "
                             "(default: ACI_ROLE_MAP_FILE or aci_role_map.json next to this script).")
    parser.add_argument("--site-map-file", default=None,
                        help="JSON file mapping hostname substrings to NetBox site slugs "
                             "(default: ACI_SITE_MAP_FILE or aci_site_map.json next to this script).")
    args = parser.parse_args()

    # --- Credentials & connection settings ---
    apic_host = os.environ.get("APIC_HOST") or input("APIC Host: ").strip()
    apic_user = os.environ.get("APIC_USER") or input("APIC Username: ").strip()
    apic_pass = os.environ.get("APIC_PASS") or getpass.getpass("APIC Password: ")
    netbox_url = os.environ.get("NETBOX_URL") or input("NetBox URL: ").strip()
    netbox_token = os.environ.get("NETBOX_TOKEN") or getpass.getpass("NetBox Token: ")
    apic_verify = env_bool("APIC_VERIFY_SSL", False)
    netbox_verify = env_bool("NETBOX_VERIFY_SSL", False)

    if not apic_host or not apic_user or not apic_pass:
        log.critical("Missing APIC credentials. Execution aborted.")
        sys.exit(1)
    if not netbox_url or not netbox_token:
        log.critical("Missing NetBox parameters. Execution aborted.")
        sys.exit(1)
    netbox_url = normalize_netbox_url(netbox_url)

    # --- Mode toggles: CLI flag > env var > interactive prompt (same behaviour as the IOS/NX-OS importer) ---
    def resolve_toggle(cli_val, env_name, prompt):
        if cli_val:
            return True
        if os.getenv(env_name) is not None:
            return env_bool(env_name)
        return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes", "true", "1")

    overwrite_dt = resolve_toggle(args.overwrite_device_type, "OVERWRITE_DEVICE_TYPE",
                                  "Overwrite NetBox Device Type with APIC hardware model?")
    dry_run = resolve_toggle(args.dry_run, "DRY_RUN",
                             "Run in DRY-RUN mode (log changes without saving to NetBox)?")

    try:
        site_map = load_site_map(args.site_map_file)
    except (OSError, ValueError) as exc:
        log.critical("Could not load site map: %s", exc)
        sys.exit(1)
    default_site = os.getenv("ACI_DEFAULT_SITE", "").strip() or None
    if not site_map and not default_site:
        log.warning("No site mapping configured (aci_site_map.json / ACI_SITE_MAP / ACI_DEFAULT_SITE): "
                    "existing NetBox devices will sync, but new devices cannot be created.")
    try:
        role_map = load_role_map(args.role_map_file)
    except (OSError, ValueError) as exc:
        log.critical("Could not load role map: %s", exc)
        sys.exit(1)
    log.info("Role map (APIC role -> NetBox role slugs, first is used for new devices): %s", role_map)

    if dry_run:
        log.warning("=== DRY RUN MODE ACTIVE - No changes will be written to NetBox ===")
    if overwrite_dt:
        log.info("OVERWRITE_DEVICE_TYPE ENABLED - APIC hardware models will overwrite NetBox device_type")
    log.info("SSL verification: APIC=%s, NetBox=%s", apic_verify, netbox_verify)

    # Fail fast on a bad NetBox URL/token BEFORE spending an APIC login
    syncer = NetBoxSyncer(netbox_url, netbox_token, verify_ssl=netbox_verify, dry_run=dry_run,
                          overwrite_device_type=overwrite_dt, role_map=role_map)
    try:
        nb_status = syncer.nb.status()
        log.info("Connected to NetBox %s (version %s)", netbox_url, nb_status.get("netbox-version", "unknown"))
    except Exception as exc:  # noqa: BLE001
        log.critical("Cannot reach NetBox at %s (check NETBOX_URL, token and SSL settings): %s", netbox_url, exc)
        sys.exit(1)

    collector = ACICollector(apic_host, apic_user, apic_pass, verify_ssl=apic_verify,
                             name_filter=args.node, role_filter=args.role)
    try:
        devices = collector.collect()
    except APICAuthError as exc:
        log.critical("%s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.critical("Failed to collect devices from APIC: %s", exc)
        sys.exit(1)

    log.info("Normalized %d ACI devices for NetBox sync", len(devices))
    if not devices:
        log.warning("No fabric nodes matched (check --node / --role filters and APIC mgmt addressing)")
        return

    results = []
    for device in devices:
        try:
            try:
                site_slug = resolve_site_slug(device.name, site_map, default_site)
            except ValueError:
                site_slug = None  # only needed if the device has to be created
            log.info("Syncing %s (site for new devices: %s)", device.name, site_slug or "unmapped")
            results.append(syncer.sync(device, site_slug=site_slug))
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to sync %s to NetBox: %s", device.name, exc)
            results.append({"device": device.name, "status": "failed", "error": str(exc), "warnings": []})

    print_summary(results, dry_run)
    if any(r.get("status") == "failed" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
