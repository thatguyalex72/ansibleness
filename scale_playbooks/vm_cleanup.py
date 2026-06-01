#!/usr/bin/env python3
"""
vm_cleanup.py

Scans HyperCore clusters for powered-on VMs, checks syslog on this host for
user-initiated activity in the last 30 days, then:
  - Powers off VMs with no user activity that have at least one tag
  - Deletes (with storage) VMs with no user activity AND no tags

Usage:
  python3 vm_cleanup.py             # live run
  python3 vm_cleanup.py --dry-run   # preview only, no changes made

Credentials via env vars — source ~/.scale_env before running:
  SC_USERNAME, SC_PASSWORD, GMAIL_ADDRESS, GMAIL_APP_PASSWORD

Cron (on 10.5.11.222, server timezone must be America/New_York):
  0 15 * * 5 . $HOME/.scale_env && /usr/bin/python3 $HOME/vm_cleanup.py >> $HOME/vm_cleanup.log 2>&1
"""

import os
import re
import glob
import time
import smtplib
import argparse
import urllib3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ─────────────────────────────────────────────────────────────────────

CLUSTERS = [
    {"name": "PUB1", "host": "10.5.11.80"},
    {"name": "PUB2", "host": "10.5.11.110"},
    {"name": "PUB3", "host": "10.5.11.100"},
]

HC_USERNAME   = os.environ.get("SC_USERNAME")
HC_PASSWORD   = os.environ.get("SC_PASSWORD")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_PASS    = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_TO      = "anicholson@scalecomputing.com"

SYSLOG_DIR    = "/var/log/remote"
INACTIVE_DAYS = 30

# Activity from these accounts does not count as "user touched"
EXCLUDED_USERS = frozenset({
    "system", "alexapi", "admin", "zabbix",
    "scale", "scalesupport", "parallels",
})


# ── HyperCore API ──────────────────────────────────────────────────────────────

def _session(host):
    s = requests.Session()
    s.auth = (HC_USERNAME, HC_PASSWORD)
    s.verify = False
    return s


def _wait_task(session, host, task_tag, timeout=120):
    """Poll TaskTag until COMPLETE or ERROR. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = session.get(f"https://{host}/rest/v1/TaskTag/{task_tag}", timeout=10)
            state = r.json().get("state", "")
            if state == "COMPLETE":
                return True
            if state in ("ERROR", "FAILED"):
                return False
        except Exception:
            pass
        time.sleep(3)
    return False


def get_powered_on_vms(cluster):
    """Return list of VM dicts with name, uuid, tags, cluster_name, cluster_host."""
    host = cluster["host"]
    try:
        r = _session(host).get(f"https://{host}/rest/v1/VirDomain", timeout=30)
        r.raise_for_status()
        return [
            {
                "name":         vm.get("name", ""),
                "uuid":         vm.get("uuid", ""),
                "tags":         (vm.get("tags") or "").strip(),
                "cluster_name": cluster["name"],
                "cluster_host": host,
            }
            for vm in r.json()
            if vm.get("state") == "RUNNING"
        ]
    except Exception as e:
        print(f"  ERROR querying {cluster['name']} ({host}): {e}")
        return []


def power_off_vm(vm, dry_run):
    """Graceful SHUTDOWN. Returns True on success."""
    if dry_run:
        return True
    host = vm["cluster_host"]
    s = _session(host)
    r = s.post(
        f"https://{host}/rest/v1/VirDomain/action",
        json=[{
            "virDomainUUID": vm["uuid"],
            "actionType":   "SHUTDOWN",
            "cause":        "INTERNAL",
        }],
        timeout=30,
    )
    if r.status_code not in (200, 201):
        return False
    task = r.json().get("taskTag")
    return _wait_task(s, host, task) if task else True


def delete_vm(vm, dry_run):
    """Force-stop then delete VM and all associated storage. Returns True on success."""
    if dry_run:
        return True
    host = vm["cluster_host"]
    s = _session(host)

    # Force stop first — VM must be off before delete will succeed
    stop = s.post(
        f"https://{host}/rest/v1/VirDomain/action",
        json=[{
            "virDomainUUID": vm["uuid"],
            "actionType":   "STOP",
            "cause":        "INTERNAL",
        }],
        timeout=30,
    )
    if stop.status_code in (200, 201):
        task = stop.json().get("taskTag")
        if task:
            _wait_task(s, host, task)

    r = s.delete(f"https://{host}/rest/v1/VirDomain/{vm['uuid']}", timeout=30)
    if r.status_code not in (200, 201):
        return False
    task = r.json().get("taskTag")
    return _wait_task(s, host, task) if task else True


# ── Syslog parsing ─────────────────────────────────────────────────────────────

# Matches: "May 27 15:00:34 scale-* HyperCore: [HyperCore Alert] [Cluster: *] [Info] [username] message"
_LOG_RE = re.compile(
    r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"   # timestamp: "May 27 15:00:34"
    r".*\[Info\]\s+\[([^\]]+)\]\s+"               # [username]
    r"(.+)$"                                       # message
)


def _parse_ts(ts_str):
    """Parse 'May 27 15:00:34' into datetime, inferring year and handling Dec→Jan rollover."""
    now = datetime.now()
    parts = ts_str.split()
    try:
        dt = datetime.strptime(f"{parts[0]} {parts[1]} {now.year} {parts[2]}", "%b %d %Y %H:%M:%S")
        if dt > now + timedelta(days=1):    # future date means it's from last year
            dt = dt.replace(year=now.year - 1)
        return dt
    except (ValueError, IndexError):
        return None


def _extract_vm_name(message):
    """
    Extract VM name from a HyperCore log message.

    Handles the patterns seen in PUB syslog:
      "LAbV updated"                        → LAbV
      "VM RTI_2_win started on node ..."    → RTI_2_win
      "Shutdown request for VM RTI_1"       → RTI_1
    """
    # Explicit "VM <name>" anywhere in the message (covers most system + user patterns)
    m = re.search(r"\bVM\s+(\S+)", message, re.IGNORECASE)
    if m:
        return m.group(1).rstrip(".,")
    # "<name> updated" at the start — the most common user-edit pattern
    m = re.match(r"^(\S+)\s+updated\b", message, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def parse_syslog(syslog_dir, days):
    """
    Read all scale-* log files and return {vm_name_lower: last_activity_datetime}
    for user-initiated actions only (EXCLUDED_USERS and snapshot lines are skipped).
    """
    cutoff   = datetime.now() - timedelta(days=days)
    activity = {}
    seen     = set()    # each line appears twice in syslog due to cluster replication

    for log_file in sorted(glob.glob(os.path.join(syslog_dir, "scale-*"))):
        try:
            with open(log_file, "r", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if line in seen:
                        continue
                    seen.add(line)

                    if "snapshot" in line.lower():
                        continue

                    m = _LOG_RE.match(line)
                    if not m:
                        continue

                    ts_str, username, message = m.group(1), m.group(2), m.group(3)

                    if username.lower() in EXCLUDED_USERS:
                        continue

                    ts = _parse_ts(ts_str)
                    if ts is None or ts < cutoff:
                        continue

                    vm_name = _extract_vm_name(message)
                    if not vm_name:
                        continue

                    key = vm_name.lower()
                    if key not in activity or ts > activity[key]:
                        activity[key] = ts

        except Exception as e:
            print(f"  WARNING: could not read {log_file}: {e}")

    return activity


# ── Email ──────────────────────────────────────────────────────────────────────

def send_report(powered_off, deleted, errors, dry_run):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"{'[DRY RUN] ' if dry_run else ''}VM Cleanup Report — {now_str}"

    lines = []
    if dry_run:
        lines += ["*** DRY RUN — no changes were made ***", ""]

    lines += [
        f"Run time : {now_str}",
        f"Threshold: {INACTIVE_DAYS} days of inactivity",
        "",
        f"Powered Off ({len(powered_off)} VMs):",
    ]
    lines += [f"  {v['name']}  [{v['cluster_name']}]" for v in powered_off] or ["  (none)"]

    lines += ["", f"Deleted — no tags ({len(deleted)} VMs):"]
    lines += [f"  {v['name']}  [{v['cluster_name']}]" for v in deleted] or ["  (none)"]

    if errors:
        lines += ["", f"Errors ({len(errors)}):"]
        lines += [f"  {e}" for e in errors]

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(lines), "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(GMAIL_ADDRESS, GMAIL_PASS)
        srv.send_message(msg)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Power off / delete inactive HyperCore VMs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview actions without making any changes")
    args = parser.parse_args()

    prefix = "[DRY RUN] " if args.dry_run else ""
    cutoff = datetime.now() - timedelta(days=INACTIVE_DAYS)

    print(f"{prefix}VM Cleanup — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"Inactive threshold : {INACTIVE_DAYS} days  (cutoff: {cutoff:%Y-%m-%d})")
    print()

    # 1. Collect powered-on VMs across all clusters
    all_vms = []
    for cluster in CLUSTERS:
        vms = get_powered_on_vms(cluster)
        print(f"  {cluster['name']}: {len(vms)} powered-on VMs")
        all_vms.extend(vms)
    print(f"  Total: {len(all_vms)} VMs\n")

    # 2. Parse syslog for user activity
    print(f"Parsing syslog ({SYSLOG_DIR}/scale-*) …")
    activity = parse_syslog(SYSLOG_DIR, INACTIVE_DAYS)
    print(f"  User activity found for {len(activity)} unique VMs\n")

    # 3. Classify
    to_power_off, to_delete = [], []
    for vm in all_vms:
        if "twingate" in vm["name"].lower():
            continue                        # infrastructure VM — never touch
        last = activity.get(vm["name"].lower())
        if last is not None and last >= cutoff:
            continue                        # active — leave it alone
        if vm["tags"]:
            to_power_off.append(vm)
        else:
            to_delete.append(vm)

    print(f"Inactive VMs:")
    print(f"  To power off (has tags) : {len(to_power_off)}")
    print(f"  To delete   (no tags)   : {len(to_delete)}\n")

    # 4. Execute
    powered_off, deleted, errors = [], [], []

    for vm in to_power_off:
        label = f"{vm['name']} [{vm['cluster_name']}]"
        print(f"  {prefix}Powering off: {label}")
        if power_off_vm(vm, args.dry_run):
            powered_off.append(vm)
        else:
            errors.append(f"Failed to power off {label}")
            print(f"    ERROR: failed to power off {label}")

    for vm in to_delete:
        label = f"{vm['name']} [{vm['cluster_name']}]"
        print(f"  {prefix}Deleting: {label}")
        if delete_vm(vm, args.dry_run):
            deleted.append(vm)
        else:
            errors.append(f"Failed to delete {label}")
            print(f"    ERROR: failed to delete {label}")

    # 5. Send report
    print(f"\nSending report to {EMAIL_TO} …")
    try:
        send_report(powered_off, deleted, errors, args.dry_run)
        print("  Sent.")
    except Exception as e:
        print(f"  Email failed: {e}")

    print(f"\nDone — powered off: {len(powered_off)}, deleted: {len(deleted)}, errors: {len(errors)}")


if __name__ == "__main__":
    main()
