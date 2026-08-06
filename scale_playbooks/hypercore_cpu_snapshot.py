#!/usr/bin/env python3
"""
===============================================================================
 HyperCore Cluster CPU Snapshot
===============================================================================

Pulls every node and every VM off a Scale Computing HyperCore cluster and lines
up node CPU utilization against the vCPU actually placed on each node.

WHY THIS EXISTS
---------------
Chasing the "VM CPU goes unstable when a node enters maintenance mode" problem.
When a node drains, its VMs live-migrate onto the surviving nodes and the
vCPU:core ratio on those survivors jumps. That oversubscription is what guests
feel as CPU steal / erratic utilization -- and it is invisible if you only look
at one node's CPU graph in the UI.

The signature to look for: a VM sitting near 100% of its own vCPU while its
host node's CPU is NOT correspondingly busy. That gap is steal time, and it
points at oversubscription rather than a guest-side problem.

REQUIREMENTS
------------
    python3 -m pip install requests

CONFIGURE
---------
Everything you need to edit is in the CONFIG block below: credentials, and the
CLUSTERS map holding each cluster's host and node IPs.

Credentials default to reading SC_USERNAME / SC_PASSWORD from the environment,
falling back to whatever is written in the CONFIG block. Either fill them in
there, or export them before running:

    export SC_USERNAME=admin
    export SC_PASSWORD=...

USAGE
-----
    # snapshot a configured cluster by name
    ./hypercore_cpu_snapshot.py --cluster us-otava

    # or point at any cluster / node IP directly
    ./hypercore_cpu_snapshot.py --host 10.8.12.10

    # list what's configured
    ./hypercore_cpu_snapshot.py --list-clusters

    # what happens if I drain this node right now?
    ./hypercore_cpu_snapshot.py --simulate-maintenance node1

    # capture an actual drain: resample every 5s, log every live migration
    ./hypercore_cpu_snapshot.py --watch 5 --csv drain.csv

    # raw data
    ./hypercore_cpu_snapshot.py --json > snapshot.json

READING THE OUTPUT
------------------
    RATIO   Running vCPU on that node divided by its physical threads.
            Under 2x is comfortable. 3x is worth watching. 4x and above is
            where guests typically start reporting erratic CPU.

    CPU     For nodes, host CPU utilization. For VMs, percent of that VM's
            OWN allocated vCPU -- so a 4-vCPU VM at 100% is consuming 4 vCPU
            worth of time, not the whole node.

    MOVE    (watch mode only) a VM changed host nodes between samples, i.e.
            a live migration -- which is exactly what a drain triggers.

Only running VMs count toward the ratio; a powered-off VM reserves no
scheduler time no matter how many vCPU it is configured with.
===============================================================================
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ===========================================================================
# CONFIG -- edit this block
# ===========================================================================

# Credentials. These read from the environment first, so exporting
# SC_USERNAME / SC_PASSWORD works without touching this file.
#
# NOTE: if you replace the fallbacks with real values, this file then contains
# live cluster credentials -- don't commit it or forward it on as-is.
USERNAME = os.environ.get("SC_USERNAME") or "admin"
PASSWORD = os.environ.get("SC_PASSWORD") or "CHANGE_ME"

# Verify TLS certificates. Lab clusters use self-signed certs, so this is off
# by default; turn it on for anything holding a real certificate.
VERIFY_SSL = False

# Known clusters.
#   host  = any node IP or the cluster VIP -- the REST API answers on all of them
#   nodes = LAN IP -> friendly label, purely to make the output readable.
#           Nodes have no user-facing name field in the API, so without this
#           the tables are labeled by bare IP. Unlisted nodes still show up,
#           they just appear under their IP.
#
# Add further clusters by copying the block below.
CLUSTERS = {
    "us-otava": {
        "host": "10.8.12.10",
        "nodes": {
            "10.8.12.10": "otava-node1",
            "10.8.12.11": "otava-node2",
            "10.8.12.12": "otava-node3",
        },
    },
}

# Cluster used when neither --cluster nor --host is given.
DEFAULT_CLUSTER = "us-otava"

# vCPU:core ratios at which the output starts flagging nodes.
RATIO_WARN = 3.0
RATIO_CRIT = 4.0

# ===========================================================================
# End of CONFIG
# ===========================================================================


class HyperCore:
    """Minimal HyperCore REST v1 client."""

    def __init__(self, host, username, password, verify_ssl=False, timeout=30):
        if not host.startswith("http"):
            host = f"https://{host}"
        self.base = host.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify_ssl
        self.session.headers.update({"Accept": "application/json"})

    def get(self, path):
        r = self.session.get(f"{self.base}/rest/v1/{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def nodes(self):
        return self.get("Node")

    def vms(self):
        return self.get("VirDomain")

    def node_stats(self):
        """Bulk node stats. Falls back to per-UUID if the bulk route is absent."""
        try:
            return {s["uuid"]: s for s in self.get("NodeStats") if s.get("uuid")}
        except (requests.HTTPError, KeyError, TypeError):
            out = {}
            for n in self.nodes():
                uuid = n.get("uuid")
                if not uuid:
                    continue
                try:
                    out[uuid] = self.get(f"NodeStats/{uuid}")
                except requests.HTTPError:
                    pass
            return out

    def vm_stats(self):
        try:
            return {s["uuid"]: s for s in self.get("VirDomainStats") if s.get("uuid")}
        except (requests.HTTPError, KeyError, TypeError):
            out = {}
            for v in self.vms():
                uuid = v.get("uuid")
                if not uuid:
                    continue
                try:
                    out[uuid] = self.get(f"VirDomainStats/{uuid}")
                except requests.HTTPError:
                    pass
            return out


# ---------------------------------------------------------------------------
# Field access helpers
#
# HyperCore has renamed a few of these across 9.x, and the stats routes return
# a different shape than the inventory routes, so probe rather than assume.
# ---------------------------------------------------------------------------

def pick(d, *keys, default=None):
    for k in keys:
        v = (d or {}).get(k)
        if v is not None:
            return v
    return default


def node_cores(node):
    """Physical threads the scheduler can hand out on this node."""
    return pick(node, "numThreads", "numCores", "cpuCount", "numCPUs", default=0) or 0


def node_label(node, labels):
    """Nodes have no name field in the API; use the configured label, else IP."""
    ip = pick(node, "lanIP", "backplaneIP")
    if ip and ip in labels:
        return labels[ip]
    return ip or pick(node, "uuid", default="?")


def cpu_pct(stats):
    """cpuUsage is 0-100 on some builds and 0-1 on others. Normalize to percent."""
    v = pick(stats, "cpuUsage", "cpuUsagePercent", "cpuUtilization")
    if v is None:
        return None
    v = float(v)
    return v * 100 if v <= 1.0 else v


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def collect(hc, labels=None):
    """One full pass: nodes + VMs + both stat sets, joined on node UUID."""
    labels = labels or {}
    nodes = hc.nodes()
    vms = hc.vms()
    nstats = hc.node_stats()
    vstats = hc.vm_stats()
    ts = datetime.now(timezone.utc)

    node_rows = {}
    for n in nodes:
        uuid = n.get("uuid")
        if not uuid:
            continue
        node_rows[uuid] = {
            "uuid": uuid,
            "name": node_label(n, labels),
            "ip": pick(n, "lanIP", "backplaneIP", default=""),
            "state": pick(n, "state", default="UNKNOWN"),
            "cores": node_cores(n),
            "cpu_pct": cpu_pct(nstats.get(uuid, {})),
            "vms_running": 0,
            "vcpu_allocated": 0,
        }

    vm_rows = []
    for v in vms:
        uuid = v.get("uuid")
        if not uuid:
            continue
        node_uuid = pick(v, "nodeUUID", "nodeUuid")
        state = pick(v, "state", default="UNKNOWN")
        vcpu = pick(v, "vcpu", "numVCPU", default=0) or 0
        running = str(state).upper() in ("RUNNING", "ON")

        vm_rows.append({
            "uuid": uuid,
            "name": pick(v, "name", default=uuid),
            "state": state,
            "vcpu": vcpu,
            "node_uuid": node_uuid,
            "node": node_rows.get(node_uuid, {}).get("name", "-"),
            # cpu_pct here is percent of the VM's own allocated vCPU, not of the
            # host. A VM pinned at 100% with idle hosts is the steal signature.
            "cpu_pct": cpu_pct(vstats.get(uuid, {})),
        })

        # Only running VMs consume scheduler time, so only they count toward
        # the oversubscription ratio.
        if running and node_uuid in node_rows:
            node_rows[node_uuid]["vms_running"] += 1
            node_rows[node_uuid]["vcpu_allocated"] += vcpu

    for n in node_rows.values():
        n["ratio"] = round(n["vcpu_allocated"] / n["cores"], 2) if n["cores"] else None

    return {
        "timestamp": ts.isoformat(),
        "nodes": sorted(node_rows.values(), key=lambda x: x["name"]),
        "vms": sorted(vm_rows, key=lambda x: (x["node"], x["name"])),
    }


def simulate_maintenance(snap, target):
    """
    Redistribute a node's running vCPU across the survivors and report the
    resulting ratios. HyperCore packs migrating VMs by available capacity, so
    this models an even spread -- treat it as the optimistic case. Watch mode
    against a real drain will show you the actual placement.
    """
    t = target.lower()
    match = [
        n for n in snap["nodes"]
        if t in (n["name"].lower(), n["ip"].lower(), n["uuid"].lower())
    ]
    if not match:
        # Fall back to a substring match so "node1" finds "otava-node1".
        match = [n for n in snap["nodes"] if t in n["name"].lower()]
    if not match:
        return None, "No node matching {!r}. Nodes: ".format(target) + ", ".join(
            f"{n['name']} ({n['ip']})" for n in snap["nodes"]
        )

    drained = match[0]
    survivors = [n for n in snap["nodes"] if n["uuid"] != drained["uuid"]]
    if not survivors:
        return None, "Only one node in the cluster -- nothing to migrate to."

    moving_vcpu = drained["vcpu_allocated"]
    moving_vms = drained["vms_running"]
    per_node = moving_vcpu / len(survivors)

    projected = []
    for n in survivors:
        new_vcpu = n["vcpu_allocated"] + per_node
        projected.append({
            "name": n["name"],
            "cores": n["cores"],
            "ratio_now": n["ratio"],
            "ratio_after": round(new_vcpu / n["cores"], 2) if n["cores"] else None,
            "vcpu_after": round(new_vcpu, 1),
        })

    return {
        "drained": drained["name"],
        "moving_vms": moving_vms,
        "moving_vcpu": moving_vcpu,
        "projected": projected,
    }, None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_pct(v):
    return f"{v:5.1f}%" if v is not None else "    --"


def fmt_ratio(v):
    return f"{v:.2f}x" if v is not None else "  --"


def print_snapshot(snap, show_vms=True):
    print(f"\n=== {snap['timestamp']} ===")

    print("\nNODES")
    print(f"  {'NODE':<16} {'STATE':<12} {'CORES':>5} {'CPU':>7} "
          f"{'VMS':>4} {'vCPU':>5} {'RATIO':>7}")
    for n in snap["nodes"]:
        flag = ""
        if n["ratio"] and n["ratio"] >= RATIO_CRIT:
            flag = "  <-- heavy oversubscription"
        elif n["ratio"] and n["ratio"] >= RATIO_WARN:
            flag = "  <-- watch"
        if str(n["state"]).upper() not in ("RUNNING", "ONLINE", "UNKNOWN"):
            flag += f"  [{n['state']}]"
        print(f"  {n['name']:<16} {str(n['state']):<12} {n['cores']:>5} "
              f"{fmt_pct(n['cpu_pct'])} {n['vms_running']:>4} "
              f"{n['vcpu_allocated']:>5} {fmt_ratio(n['ratio'])}{flag}")

    total_cores = sum(n["cores"] for n in snap["nodes"])
    total_vcpu = sum(n["vcpu_allocated"] for n in snap["nodes"])
    overall = total_vcpu / total_cores if total_cores else None
    print(f"\n  cluster: {total_vcpu} vCPU running on {total_cores} cores "
          f"({fmt_ratio(overall).strip()} overall)")

    if not show_vms:
        return

    print("\nVMS")
    print(f"  {'VM':<32} {'NODE':<16} {'STATE':<10} {'vCPU':>5} {'CPU':>7}")
    for v in snap["vms"]:
        # A VM near 100% of its own vCPU while its host is not busy is the
        # contention signature worth chasing.
        flag = "  <-- pegged" if (v["cpu_pct"] or 0) >= 90 else ""
        print(f"  {v['name'][:32]:<32} {v['node']:<16} {str(v['state']):<10} "
              f"{v['vcpu']:>5} {fmt_pct(v['cpu_pct'])}{flag}")


def print_simulation(sim):
    print(f"\nMAINTENANCE SIMULATION -- draining {sim['drained']}")
    print(f"  {sim['moving_vms']} running VMs / {sim['moving_vcpu']} vCPU "
          f"redistribute to {len(sim['projected'])} survivor(s)\n")
    print(f"  {'NODE':<16} {'CORES':>5} {'RATIO NOW':>10} {'RATIO AFTER':>12}")
    for p in sim["projected"]:
        flag = ""
        if p["ratio_after"] and p["ratio_after"] >= RATIO_CRIT:
            flag = "  <-- expect guest CPU instability here"
        elif p["ratio_after"] and p["ratio_after"] >= RATIO_WARN:
            flag = "  <-- tight"
        print(f"  {p['name']:<16} {p['cores']:>5} {fmt_ratio(p['ratio_now']):>10} "
              f"{fmt_ratio(p['ratio_after']):>12}{flag}")


def diff_snapshots(prev, curr):
    """Report VM migrations and notable node state changes between samples."""
    events = []

    prev_state = {n["uuid"]: n["state"] for n in prev["nodes"]}
    for n in curr["nodes"]:
        old = prev_state.get(n["uuid"])
        if old and old != n["state"]:
            events.append(f"NODE  {n['name']}: {old} -> {n['state']}")

    prev_place = {v["uuid"]: (v["node"], v["state"]) for v in prev["vms"]}
    for v in curr["vms"]:
        old = prev_place.get(v["uuid"])
        if not old:
            continue
        old_node, old_vm_state = old
        if old_node != v["node"]:
            events.append(
                f"MOVE  {v['name']} ({v['vcpu']} vCPU): {old_node} -> {v['node']}")
        elif old_vm_state != v["state"]:
            events.append(f"STATE {v['name']}: {old_vm_state} -> {v['state']}")

    return events


def write_csv_rows(writer, snap):
    for n in snap["nodes"]:
        writer.writerow([snap["timestamp"], "node", n["name"], n["state"],
                         n["cores"], n["cpu_pct"], n["vms_running"],
                         n["vcpu_allocated"], n["ratio"]])
    for v in snap["vms"]:
        writer.writerow([snap["timestamp"], "vm", v["name"], v["state"],
                         "", v["cpu_pct"], "", v["vcpu"], v["node"]])


# ---------------------------------------------------------------------------

def resolve_target(args):
    """
    Work out which host to hit and which node labels to apply.
    Returns (host, labels) on success, or (None, error_message) on failure.
    """
    if args.host:
        # Explicit host wins. Reuse labels from a cluster that lists this IP.
        for cfg in CLUSTERS.values():
            if args.host in cfg.get("nodes", {}) or args.host == cfg.get("host"):
                return args.host, cfg.get("nodes", {})
        return args.host, {}

    name = args.cluster or DEFAULT_CLUSTER
    if name not in CLUSTERS:
        return None, f"Unknown cluster {name!r}. Known: " + ", ".join(sorted(CLUSTERS))

    cfg = CLUSTERS[name]
    return cfg["host"], cfg.get("nodes", {})


def main():
    p = argparse.ArgumentParser(
        description="Pull VMs and node CPU utilization from a HyperCore cluster.",
        epilog="Clusters and credentials live in the CONFIG block at the top "
               "of this file.")
    p.add_argument("--cluster", help="Named cluster from the CONFIG block "
                                     f"(default: {DEFAULT_CLUSTER})")
    p.add_argument("--host", help="Cluster or node IP, bypassing the CONFIG block")
    p.add_argument("--list-clusters", action="store_true",
                   help="Show the configured clusters and exit")
    p.add_argument("--user", default=USERNAME, help="HyperCore username")
    p.add_argument("--password", default=PASSWORD, help="HyperCore password")
    p.add_argument("--verify-ssl", action="store_true", default=VERIFY_SSL,
                   help="Verify TLS certificates")
    p.add_argument("--watch", type=int, metavar="SECONDS",
                   help="Resample every N seconds and report migrations as they happen")
    p.add_argument("--simulate-maintenance", metavar="NODE",
                   help="Project vCPU ratios as if this node were drained "
                        "(label, IP, or UUID)")
    p.add_argument("--nodes-only", action="store_true", help="Skip the VM table")
    p.add_argument("--json", action="store_true", help="Emit raw JSON instead of tables")
    p.add_argument("--csv", metavar="FILE", help="Append every sample to a CSV file")
    args = p.parse_args()

    if args.list_clusters:
        print("\nConfigured clusters:\n")
        for name, cfg in sorted(CLUSTERS.items()):
            default = "  (default)" if name == DEFAULT_CLUSTER else ""
            print(f"  {name}{default}\n    host: {cfg['host']}")
            for ip, label in cfg.get("nodes", {}).items():
                print(f"      {ip:<16} {label}")
            print()
        return 0

    host, labels = resolve_target(args)
    if host is None:
        p.error(labels)  # resolve_target returned an error message

    if not args.password or args.password == "CHANGE_ME":
        p.error("no password set -- export SC_PASSWORD, pass --password, "
                "or fill in PASSWORD in the CONFIG block")

    hc = HyperCore(host, args.user, args.password, verify_ssl=args.verify_ssl)

    csv_file = csv_writer = None
    if args.csv:
        new = not os.path.exists(args.csv)
        csv_file = open(args.csv, "a", newline="")
        csv_writer = csv.writer(csv_file)
        if new:
            csv_writer.writerow(["timestamp", "kind", "name", "state", "cores",
                                 "cpu_pct", "vms_running", "vcpu", "ratio_or_node"])

    try:
        prev = None
        while True:
            try:
                snap = collect(hc, labels)
            except requests.RequestException as e:
                print(f"[{datetime.now():%H:%M:%S}] poll failed: {e}", file=sys.stderr)
                if not args.watch:
                    return 1
                time.sleep(args.watch)
                continue

            if args.json:
                print(json.dumps(snap, indent=2))
            else:
                print_snapshot(snap, show_vms=not args.nodes_only)
                if prev:
                    for e in diff_snapshots(prev, snap):
                        print(f"  * {e}")
                if args.simulate_maintenance:
                    sim, err = simulate_maintenance(snap, args.simulate_maintenance)
                    if err:
                        print(f"\n{err}")
                    else:
                        print_simulation(sim)

            if csv_writer:
                write_csv_rows(csv_writer, snap)
                csv_file.flush()

            if not args.watch:
                return 0
            prev = snap
            time.sleep(args.watch)

    except KeyboardInterrupt:
        return 0
    finally:
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    sys.exit(main())
