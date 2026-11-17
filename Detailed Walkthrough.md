# Cisco ACI to NetBox Collector: Detailed Walkthrough

A Python script that reads the live state of a Cisco ACI fabric from the APIC REST API and synchronizes devices, interfaces, IP assignments and hardware inventory into [NetBox](https://netbox.dev/).

---

## Use Cases

- **Initial NetBox population:** Bootstrap every leaf and spine in a fabric into NetBox in one run, including site, role, device type, serial, management IP, ports and optics.
- **Ongoing sync / drift detection:** Re-run on a schedule to keep NetBox current as ports come up, optics are swapped, descriptions change or switches are RMA'd.
- **Device type correction:** Detects devices filed under the wrong NetBox Device Type (e.g. a spine recorded as an APIC server) and corrects them from the APIC hardware model, if you opt in.
- **Interface type correction:** Classifies ports from their negotiated speed (1G to 400G). It never downgrades a type and never overwrites hand-curated optical, LAG or virtual types.
- **Hardware inventory tracking:** Records the part number and serial of every installed transceiver, plus line cards, supervisors and fabric modules on modular spines.
- **L3Out documentation:** Imports border-leaf L3Out routed subinterfaces (linked to their parent port) and router-ID loopbacks, along with their IPs.
- **Safe dry-run mode:** Shows every create and update, including which IP (and VRF) would land on which interface, without writing anything.

---

## Supported Platforms

| Platform | Data Source | NetBox Platform Slug |
|---|---|---|
| Cisco ACI leaf & spine switches (Nexus 9000 in ACI mode) | APIC REST API (`/api/node/class/...`) | `aci` |

APIC controllers themselves are excluded by the node query (`role != controller`). Only nodes whose fabric state is `active` are processed.

Verified against ACI 5.2 (switch software `n9000-15.2`) with NetBox 4.6. The APIC classes used are long-standing parts of the ACI object model, but other releases have not been tested.

---

## Prerequisites

### NetBox Requirements

These objects must exist in NetBox before the first run:

| Object | Needed for | Notes |
|---|---|---|
| Platform with slug **`aci`** | every device | Set on every synced device. |
| Sites listed in `aci_site_map.json` | creating new devices only | Existing devices keep their NetBox site. |
| Device Roles listed in `aci_role_map.json` | creating new devices only | Existing devices keep their NetBox role. |
| Device Types for your switch models (e.g. `N9K-C93180YC-FX3`, `Nexus 9332C`) | device type matching | The [netbox-community devicetype-library](https://github.com/netbox-community/devicetype-library) has definitions for most Nexus 9000 models. |
| Device Type with slug **`unknown-aci-switch`** | creating a device whose model isn't found | A placeholder. Optional if all your models exist, or if `AUTO_CREATE_DEVICE_TYPES=true`. |
| Manufacturer **`Cisco`** | `AUTO_CREATE_DEVICE_TYPES=true` only | |
| IP Prefixes for your management subnets | correct masks on management IPs | Without a matching prefix, IPs are created as `/32` (`/128` for IPv6). |

The API token needs these permissions:

| Object | Permissions |
|---|---|
| `dcim.device` | view, add, change |
| `dcim.interface` | view, add, change |
| `dcim.inventoryitem` | view, add, change |
| `ipam.ipaddress` | view, add, change |
| `dcim.site`, `dcim.devicerole`, `dcim.platform`, `dcim.manufacturer`, `ipam.prefix` | view |
| `dcim.devicetype` | view (plus add if `AUTO_CREATE_DEVICE_TYPES=true`) |

The script **never deletes** anything in NetBox.

### APIC Requirements

- HTTPS access from where the script runs to the APIC (port 443).
- A **read-only** APIC user, e.g. a local or AAA user with the `read-all` role on the `all` security domain. The script only issues `aaaLogin`, `aaaLogout` and GET class queries.
- Any APIC in the cluster can be used as `APIC_HOST`.

---

## Dependencies

```bash
pip install pynetbox "requests>=2.27" urllib3
```

| Package | Purpose |
|---|---|
| `pynetbox` | NetBox REST API client |
| `requests` | APIC REST session (login cookie, paging, retries) |
| `urllib3` | SSL warning suppression for self-signed certificates |

Everything else is Python standard library (`argparse`, `json`, `ipaddress`, `logging`, `re`, `dataclasses`, …). No Netmiko, TextFSM or SSH access to switches is required.

---

## Installation

### Ubuntu / Debian

```bash
# 1. Install Python 3 if not already present
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# 2. (Recommended) Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install pynetbox "requests>=2.27" urllib3

# 4. Get the script and example config files
git clone https://github.com/<your-account>/<this-repo>.git
cd <this-repo>
cp aci_site_map.example.json aci_site_map.json
cp aci_role_map.example.json aci_role_map.json
```

### Windows

```powershell
# 1. Install Python 3 from https://python.org (check "Add to PATH")

# 2. (Recommended) Create a virtual environment
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install pynetbox "requests>=2.27" urllib3

# 4. Copy the example config files
copy aci_site_map.example.json aci_site_map.json
copy aci_role_map.example.json aci_role_map.json
```

---

## Configuration

### Site Map: `aci_site_map.json`

This file maps a substring of the node's hostname (case-insensitive) to a NetBox **site slug**. Patterns are checked in file order and the first match wins.

```json
{
  "DC1-": "dc1",
  "DC2-": "dc2"
}
```

- The site is **only used when a new device has to be created**. Existing devices are never moved between sites.
- Alternatives:
  - `ACI_SITE_MAP="DC1-=dc1,DC2-=dc2"` as an environment variable (takes precedence over the file)
  - `--site-map-file /path/file.json` or `ACI_SITE_MAP_FILE`
- `ACI_DEFAULT_SITE=dc1` is used when no pattern matches.
- If a node matches nothing and no default is set, existing devices still sync. A *new* device fails with `no site-map pattern matches`.

### Role Map: `aci_role_map.json`

This file maps the APIC node role (`leaf`, `spine`) to one or more NetBox **device role slugs**:

```json
{
  "leaf":  ["dc-leaf", "dc-border-leaf"],
  "spine": ["dc-spine"]
}
```

- The **first** slug in each list is the role used when **creating** a device.
- An **existing** device whose role is *any* slug in the list is left alone. This lets ordinary leafs and border leafs share the APIC role `leaf`.
- If an existing device's role isn't in the list, the script reports **role drift** as a warning and does **not** overwrite it.
- `default` (optional) applies to any other APIC role. It falls back to the `leaf` list.
- Alternatives: `ACI_ROLE_MAP="leaf=dc-leaf|dc-border-leaf,spine=dc-spine"`, `--role-map-file` or `ACI_ROLE_MAP_FILE`.
- Without a role map file, the defaults are `aci-leaf`, `aci-spine` and `aci-switch`.

### Environment Variables & CLI Flags

| Variable | CLI flag | Default | Description |
|---|---|---|---|
| `APIC_HOST` | | *(prompt)* | APIC IP or hostname |
| `APIC_USER` / `APIC_PASS` | | *(prompt)* | APIC credentials (the password prompt is hidden) |
| `APIC_VERIFY_SSL` | | `false` | Verify the APIC certificate |
| `NETBOX_URL` | | *(prompt)* | e.g. `https://netbox.example.com`. `https://` is added if missing; a trailing `/` or `/api` is removed. |
| `NETBOX_TOKEN` | | *(prompt)* | NetBox API token (the prompt is hidden) |
| `NETBOX_VERIFY_SSL` | | `false` | Verify the NetBox certificate |
| `DRY_RUN` | `-d`, `--dry-run` | *(prompt)* | Log changes without writing to NetBox |
| `OVERWRITE_DEVICE_TYPE` | `--overwrite-device-type` | *(prompt)* | Let the APIC model replace an existing device's Device Type |
| `AUTO_CREATE_DEVICE_TYPES` | | `false` | Create missing Device Types (manufacturer `Cisco`) |
| `ACI_NODE` | `--node` | *(all)* | Sync one node (exact name, case-insensitive) |
| `ACI_ROLE` | `--role` | *(all)* | `leaf` or `spine` only |
| `ACI_SITE_MAP` / `ACI_SITE_MAP_FILE` | `--site-map-file` | `aci_site_map.json` | Site mapping |
| `ACI_DEFAULT_SITE` | | *(none)* | Site for new nodes that match no pattern |
| `ACI_ROLE_MAP` / `ACI_ROLE_MAP_FILE` | `--role-map-file` | `aci_role_map.json` | Role mapping |
| `ACI_SYNC_INTERFACES` | | `true` | Sync interfaces (and their IPs) |
| `ACI_SYNC_INTERFACE_IPS` | | `true` | Sync loopback / L3Out IPs |
| `ACI_SYNC_INVENTORY` | | `true` | Sync modules and transceivers |
| `ACI_SYNC_INFRA` | | `false` | Also sync fabric infra loopbacks, subinterfaces and `overlay-1` IPs |
| `LOG_LEVEL` | | `INFO` | `DEBUG` also lists every skipped infra interface and every APIC query |

When `DRY_RUN` or `OVERWRITE_DEVICE_TYPE` is set by neither a flag nor an environment variable, the script asks `[y/N]` at startup.

A few constants at the top of the script can also be edited: `ACI_PLATFORM_SLUG` (`aci`), `MGMT_INTERFACE_NAME` (`management`), `DEFAULT_DEVICE_TYPE_SLUG` (`unknown-aci-switch`), `REQUEST_TIMEOUT` and `APIC_PAGE_SIZE`.

**Ubuntu / Linux:**
```bash
export APIC_HOST=apic1.example.com
export APIC_USER=svc-netbox-ro
export NETBOX_URL=https://netbox.example.com
export DRY_RUN=true
export OVERWRITE_DEVICE_TYPE=false
python3 aci_collector.py          # prompts only for APIC_PASS and NETBOX_TOKEN
```

**Windows (PowerShell):**
```powershell
$env:APIC_HOST = "apic1.example.com"
$env:NETBOX_URL = "https://netbox.example.com"
$env:DRY_RUN = "true"
python aci_collector.py
```

---

## Running the Script

```bash
python3 aci_collector.py --dry-run --node DC1-LEAF-101   # one node, preview
python3 aci_collector.py --dry-run --role spine          # all spines, preview
python3 aci_collector.py                                 # full fabric, live
```

Interactive prompts (each is skipped if its environment variable is set):

```
APIC Host:
APIC Username:
APIC Password:
NetBox URL:
NetBox Token:
Overwrite NetBox Device Type with APIC hardware model? [y/N]:
Run in DRY-RUN mode (log changes without saving to NetBox)? [y/N]:
```

**Recommended rollout:**
1. Dry run a single leaf, then a single spine, using `--node`. Check the "Would create/update" lines.
2. Dry run the whole fabric and review the warnings section.
3. Run live for real.

---

## What Gets Synced, and the Rules Behind It

### Devices
- **Matching:** an existing NetBox device is found by **serial number first**, then by **name**. A device found by serial whose name differs is renamed to the APIC name, which handles re-hostnamed switches.
- **New devices** are created with status `active`, platform `aci`, the site from the site map, the first role from the role map, and the matched Device Type (or the placeholder).
- **Existing devices** have their name/platform, serial and (if enabled) device type updated as **separate** updates, so a rejection of one doesn't block the others.

### Device Type Matching
The APIC model string (e.g. `N9K-C9332C`) is expanded into the variants Cisco and the devicetype-library use, such as `N9K-C9332C`, `C9332C`, `Nexus 9332C` and `/K9`-stripped forms. These are matched against each Device Type's model, part number and slug:

1. **Exact** match
2. **Normalized** match (case, `-`, `_`, `/` and spaces ignored)
3. **Fuzzy suffix** match

If more than one Device Type matches at a step, Cisco-manufactured types and an exact part-number match are preferred. If it is still ambiguous, the script **refuses** to change anything and reports the candidates.

### Management IP
- Source order: APIC **OOB** management address, then **in-band**, then the node's fabric address.
- The address goes on an interface named `management` (created as virtual and management-only if missing) and becomes the device's `primary_ip4` / `primary_ip6`.
- An existing NetBox IP is found **by host, whatever its mask**, so `10.0.0.5/24` is reused rather than duplicated as `/32`.
- A new IP takes its mask from the most specific matching NetBox prefix.
- An IP already assigned to **another device** is never taken over. It is reported as a warning instead.

### Interfaces
| Collected | APIC class | NetBox name | Type |
|---|---|---|---|
| Physical ports (incl. breakouts) | `l1PhysIf` + `ethpmPhysIf` | `Ethernet1/1`, `Ethernet1/1/1` | From negotiated speed: `1000base-t` … `400gbase-x-qsfpdd` |
| Port-channels | `pcAggrIf` + `ethpmAggrIf` | `Port-channel1` | `lag` (description = policy group name if none is set) |
| Loopbacks | `l3LbRtdIf` | `Loopback5` | `virtual` |
| L3Out routed subinterfaces | `l3EncRtdIf` | `Ethernet1/49.10` (parent linked) | `virtual` |

- A **new** interface is created only if it is operationally **up** or carries an IP. Interfaces that **already exist** in NetBox are always updated.
- Fields synced: description, MTU (skipped when ACI says `inherit`), admin enabled/disabled, speed (NetBox stores Kbps) and type.
- Types are only **upgraded**. A port that is down or temporarily negotiated lower is never downgraded, and LAG, virtual or specific optical types set by hand are never changed.
- Types follow the **negotiated** speed. A 25G port running a 1G optic is typed `1000base-t`, the same behaviour as the IOS/NX-OS importer.

### What Is Filtered Out (and Why)
| Skipped | Why |
|---|---|
| Interfaces in the infra VRF `overlay-1` (TEP / anycast loopbacks, fabric links) | Fabric plumbing managed by APIC. A spine has dozens. |
| Subinterfaces on ports APIC uses as `fabric` / `discovery` ports | The subinterface APIC pre-builds on every unconnected spine port, before it joins `overlay-1`. |
| Loopbacks with no IP outside `overlay-1` | Nothing useful to document. Router-ID and in-band management loopbacks are kept. |
| SVI / `vlan` interface IPs | ACI anycast gateways: the same IP on every leaf would create duplicates. |

`ACI_SYNC_INFRA=true` turns the first three back on. The per-node log line shows how many were skipped, e.g. `interfaces: 18 (+44 fabric infra skipped)`. Use `LOG_LEVEL=DEBUG` to list them.

### Interface IPs
Loopback and routed / L3Out subinterface IPv4 addresses are imported **with their prefix length as configured in ACI**, and assigned to the matching interface. Dry-run output names the VRF of each, e.g. `Would assign IP 172.16.1.1/30 (VRF common:core) to Ethernet1/49.10`.

### Inventory Items
| Item | APIC class | NetBox name | Label |
|---|---|---|---|
| Transceivers (present only) | `ethpmFcot` | `Ethernet1/49 Transceiver` | `Transceiver` |
| Line cards | `eqptLC` | `Linecard 1` | `Module` |
| Supervisors | `eqptSupC` | `Supervisor 1` | `Module` |
| Fabric modules | `eqptFC` | `Fabric Module 22` | `Module` |

Part number and serial are kept up to date. Modules that repeat the chassis serial (the fixed "Linecard 1" on fixed-form-factor leafs) are skipped. Removed optics are **not** deleted from NetBox.

---

## Output

Every run starts with the settings it loaded and the NetBox version it connected to. It then logs one data line per node and the changes it makes (or would make), and ends with a summary:

```
[INFO] aci_collector: Connected to NetBox https://netbox.example.com (version 4.6.2)
[INFO] aci_collector: Authenticated to APIC apic1.example.com
[INFO] aci_collector: Retrieved 20 active fabric nodes from APIC
[INFO] aci_collector: [DC1-SPINE-201] APIC data -> version: n9000-15.2(8h), model: N9K-C9332C, serial: FDO00000001, interfaces: 18 (+44 fabric infra skipped), IPs: 1, modules: 0, transceivers: 18
[INFO] aci_collector: [DRY RUN] [DC1-SPINE-201] Would update device_type (APIC Server M2 -> Nexus 9332C, via exact): {'device_type': 10}
[INFO] aci_collector: [DRY RUN] [DC1-SPINE-201] Would create interface Ethernet1/1 (100gbase-x-cxp)
[INFO] aci_collector: [DRY RUN] [DC1-SPINE-201] Would create inventory item Ethernet1/1 Transceiver (Part: QSFP-100G-SR4-S, Serial: AVM00000001)

==============================================================================================================
                                      SYNCHRONIZATION SUMMARY (DRY RUN)
==============================================================================================================
Device Name               | Status   | Created | Ports | IPs  | Inv  | Serial    | Type      | Error Details
--------------------------------------------------------------------------------------------------------------
DC1-SPINE-201             | SUCCESS  | no      | 18    | 1    | 18   | NO CHANGE | UPDATED   | None
DC1-LEAF-101              | SUCCESS  | no      | 26    | 0    | 31   | NO CHANGE | NO CHANGE | None
DC1-LEAF-105              | SUCCESS  | YES     | 0     | 0    | 0    | NO CHANGE | NO CHANGE | None
==============================================================================================================
TOTAL: 3  |  SUCCESS: 3  |  SKIPPED: 0  |  FAILED: 0
==============================================================================================================
```

- **Ports / IPs / Inv:** interfaces, IPs and inventory items created or updated (or that *would* be, in a dry run).
- **Created = YES** in a dry run: the device would be created. Its interfaces and inventory are summarized in the log, and synced on the live run once the device exists.
- **Non-fatal issues** (role drift, ambiguous device types, an IP owned by another device, a rejected update) are listed in a **DETAILED NON-FATAL WARNINGS/ERRORS** section without failing the device.
- **Exit code** is `1` if any device failed or the run aborted, otherwise `0`. That makes it suitable for cron or CI.

---

## AAA / Account Lockout Protection

- NetBox is checked **first**. A wrong URL or token aborts the run before any APIC login is spent.
- The script logs in to the APIC **exactly once** per run:
  - An auth rejection (HTTP 401/403) aborts the run and is **never retried**.
  - A login that times out after reaching the APIC is not retried either, since it may already have counted as an attempt.
  - Only a connection that never reached the APIC (e.g. connection refused) is retried, up to 3 times with backoff.
- There is no per-switch login at all, so there are no fleet-wide SSH attempts.

---

## Troubleshooting

**`Cannot reach NetBox at ...`**
Check `NETBOX_URL`, the token, and that the token's user has the permissions listed above. For HTTPS with a self-signed certificate, keep `NETBOX_VERIFY_SSL=false` (the default).

**`APIC ... rejected the credentials (HTTP 401)`**
Wrong APIC username or password, or the user lacks a login domain or role. The script did not retry. Fix the credentials before running again.

**`Device Role 'x' ... does not exist in NetBox` / `Site with slug 'x' does not exist`**
This only happens when creating a new device. Create the role or site in NetBox, or fix `aci_role_map.json` / `aci_site_map.json`.

**`Cannot create 'NODE': no site-map pattern matches its name`**
Add a pattern for that hostname to `aci_site_map.json`, or set `ACI_DEFAULT_SITE`.

**`Role drift: NetBox has role 'x', APIC reports 'leaf'`**
The existing device's role isn't in the role map list for its APIC role. Add the slug to the list (e.g. a border-leaf role), or correct the device in NetBox. The script never overwrites roles.

**`Ambiguous ... DeviceType match` / `Hardware model 'x' not found`**
Several (or no) NetBox Device Types look like the APIC model. Remove duplicates, set the Device Type's **part number** to the exact Cisco PID, import the model from the devicetype-library, or enable `AUTO_CREATE_DEVICE_TYPES`.

**Device type isn't updated even though the models differ**
`OVERWRITE_DEVICE_TYPE` is off: the log says `OVERWRITE_DEVICE_TYPE is disabled`. Pass `--overwrite-device-type` or answer `y` at the prompt.

**`IP x is assigned to another device; not reassigning`**
The management or interface IP already belongs to a different NetBox device, often a stale record from before an RMA. Resolve the conflict in NetBox and re-run.

**Spine shows `Ethernet1/x.NN` subinterfaces or many loopbacks**
Fabric infrastructure filtering is driven by APIC's VRF membership and port usage. Run with `LOG_LEVEL=DEBUG` to see what was skipped, and make sure `ACI_SYNC_INFRA` is not set to `true`.

**Interfaces I expect are missing**
New interfaces are only created when they are **up** or carry an IP. Down ports that already exist in NetBox are still updated. Changing a device's Device Type in NetBox doesn't add the new type's template interfaces; the collector creates the active ones.

**Management IP created as `/32`**
No NetBox prefix contains that address. Add the management subnet as a prefix in NetBox and fix the IP's mask; future runs reuse it.
