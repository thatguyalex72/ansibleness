#!/usr/bin/env python3
"""
Reads the Scale demo lab user request Google Sheet and writes users.yml
for use by create_users.yml.

Rows already marked italic in the sheet are skipped (already created).
For rows with empty Username/Password, credentials are auto-generated and
written back to the sheet. Conflicts are checked against both the sheet
and all publab clusters before writing anything.

After Ansible finishes, run with --mode send-emails to notify users,
then --mode mark-done to italicize the newly created rows.

Requirements:
    pip install google-api-python-client google-auth pyyaml

Setup:
    1. Create a Google Cloud service account, enable the Google Sheets API,
       and download the JSON key file.
    2. Share the Google Sheet with the service account email (Editor access).
    3. Export these env vars (e.g. in ~/.scale_env):
         export GSHEET_SERVICE_ACCOUNT_FILE="/path/to/service_account.json"
         export GSHEET_SPREADSHEET_ID="sheet_id_from_url"
         export GMAIL_ADDRESS="you@gmail.com"
         export GMAIL_APP_PASSWORD="your16charapppassword"
         export SC_USERNAME="alexapi"
         export SC_PASSWORD="yourpassword"

    The sheet ID is the long string in the Google Sheet URL:
      https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit

Column config:
    Update the *_COL constants below to match the exact headers in your sheet.
    Run with --mode fetch once and it will print available columns if any are missing.
"""

import argparse
import base64
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from email.mime.text import MIMEText

import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = os.environ.get("GSHEET_SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID       = os.environ.get("GSHEET_SPREADSHEET_ID", "")
SHEET_NAME           = os.environ.get("GSHEET_SHEET_NAME", "")
GMAIL_ADDRESS        = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD   = os.environ.get("GMAIL_APP_PASSWORD", "")
SC_USERNAME          = os.environ.get("SC_USERNAME", "")
SC_PASSWORD          = os.environ.get("SC_PASSWORD", "")

# Publab clusters to check for existing users
PUBLAB_CLUSTERS = [
    "https://10.5.11.80",
    "https://10.5.11.110",
    "https://10.5.11.100",
]

# Update these to match the exact column headers in your Google Sheet
USERNAME_COL = "Username"
FULL_NAME_COL = "Prospect's Name (Ex: Alex Smith)"
PASSWORD_COL  = "Password"
EMAIL_COL     = "Email (alex@acme_it.com)"
VMTAG_COL     = "Templates/Tags"
COMPANY_COL   = "Company/Partner Name (Acme IT)"
CC_COL        = "SC//Sales Team Affiliation"

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.yml")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TIMESTAMP_COL = "Timestamp"

EMAIL_SUBJECT = "SC//Showcase - Public Cluster Info"
EMAIL_TEMPLATE = """\
Hey there!

You should receive a separate email directly from our Fleet Manager Platform — a \
web portal that provides access to our lab clusters. It should come from \
noreply@scalecomputing.com titled "Welcome to Public Lab in SC//Fleet Manager". \
Once you click that link you'll want to create an account, either with Microsoft \
or Google's SSO or by creating a username and password. From there you'll have \
the ability to navigate to the Clusters tab.

I've created a separate login for you to our Sandbox Clusters. You can access \
each cluster by clicking on the Clusters tab, clicking on the cluster name, and \
then clicking the "Go To Cluster" button in the top right corner. The clusters \
you'll have access to are:

    PUB1-3250DFz
    PUB2-5250D-DR
    PUB3-1250DF

Cluster Credentials:

Username: {username}
Password: {password}
VMTag:    {vmtag}

Your VMTag is a label you'll assign to any VMs you create so they can be \
identified as yours.

Here are a few guidelines to keep in mind while using the clusters:

    - Try to use sensible names. "Test" and "Demo" get used a lot.
    - Make sure any VMs you create are tagged with your VMTag so they can be \
identified as yours.
    - Don't create a VM that uses all the available RAM on a node. Other Scale \
staff and resellers will be using these clusters for demos.
    - Once you have finished any demo, delete any VMs you created. You can keep \
VMs running on the cluster if you like, but make sure memory usage is low so \
there is room for others. Or you can just keep them powered off.
    - Don't delete other people's VMs or power them off/on. You're welcome to \
live-migrate them between nodes.
    - This is a 1GbE environment with other users on the cluster at any time, so \
performance may not be representative of an actual production environment.
    - Any VMs left untagged will be deleted every Friday, so make sure anything \
you create is assigned to your VMTag.

If you have any questions please let me know!"""

DUPLICATE_SUBJECT = "SC//Demo Lab — Duplicate Submission: {full_name}"
DUPLICATE_TEMPLATE = """\
Hi,

This is an automated notification from the SC//Demo Lab provisioning system.

{full_name} from {company} has already been submitted for access to the SC//Demo \
Lab and their account is active. Please verify they are able to connect to the \
lab, or engage your territory SA and Alex as needed.

If you have any questions, please reach out directly."""


def build_service():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_sheet_info(service):
    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    props = metadata["sheets"][0]["properties"]
    name = SHEET_NAME or props["title"]
    print(f"Using sheet tab: {name}")
    return name, props["sheetId"]


def extract_email(value):
    """Pull a bare email address out of strings like 'EMEA - emea@scalecomputing.com'."""
    match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]+", value)
    return match.group(0) if match else ""


def generate_username(full_name):
    parts = full_name.strip().split()
    if not parts:
        return ""
    first_initial = parts[0][0].lower()
    last_name = re.sub(r"[^a-z0-9]", "", parts[-1].lower())
    return first_initial + last_name


def generate_password(company_name):
    first_word = company_name.strip().split()[0] if company_name.strip() else "Scale"
    clean_word = re.sub(r"[^a-zA-Z0-9]", "", first_word)
    return f"{clean_word}##Scale2026"


def generate_vmtag(company_name):
    words = company_name.strip().split()
    if not words:
        return ""
    if len(words) == 1:
        return re.sub(r"[^a-zA-Z0-9]", "", words[0])
    return "".join(re.sub(r"[^a-zA-Z0-9]", "", w)[0] for w in words if re.sub(r"[^a-zA-Z0-9]", "", w))


def get_cluster_usernames():
    """Returns set of usernames already on any publab cluster."""
    if not SC_USERNAME or not SC_PASSWORD:
        print("  WARNING: SC_USERNAME/SC_PASSWORD not set — skipping cluster conflict check")
        return set()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    credentials = base64.b64encode(f"{SC_USERNAME}:{SC_PASSWORD}".encode()).decode()
    usernames = set()

    for cluster_url in PUBLAB_CLUSTERS:
        try:
            req = urllib.request.Request(
                f"{cluster_url}/rest/v1/ClusterMember",
                headers={"Authorization": f"Basic {credentials}"},
            )
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                data = json.loads(resp.read())
                cluster_users = {u.get("username", "").lower() for u in data}
                usernames.update(cluster_users)
                print(f"  {cluster_url}: {len(cluster_users)} users found")
        except Exception as e:
            print(f"  WARNING: could not reach {cluster_url}: {e}")

    return usernames


def fetch_users(service, dry_run=False, override_usernames=None):
    sheet_name, _ = get_sheet_info(service)
    values_result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=sheet_name,
        valueRenderOption="FORMATTED_VALUE",
    ).execute()

    rows = values_result.get("values", [])
    if not rows:
        print("No data found in sheet.")
        return 0

    headers = rows[0]

    try:
        timestamp_idx = headers.index(TIMESTAMP_COL)
        username_idx  = headers.index(USERNAME_COL)
        full_name_idx = headers.index(FULL_NAME_COL)
        password_idx  = headers.index(PASSWORD_COL)
        email_idx     = headers.index(EMAIL_COL)
        vmtag_idx     = headers.index(VMTAG_COL)
        company_idx   = headers.index(COMPANY_COL)
        cc_idx        = headers.index(CC_COL)
    except ValueError as e:
        print(f"ERROR: Column not found: {e}", file=sys.stderr)
        print(f"Available columns: {headers}", file=sys.stderr)
        sys.exit(1)

    format_result = service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        includeGridData=True,
        ranges=sheet_name,
        fields="sheets.data.rowData.values.userEnteredFormat.textFormat.italic",
    ).execute()

    format_rows = (
        format_result.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [])
    )

    def is_italic(row_idx):
        if row_idx >= len(format_rows):
            return False
        values = format_rows[row_idx].get("values", [])
        if not values:
            return False
        return values[0].get("userEnteredFormat", {}).get("textFormat", {}).get("italic", False)

    # Build existing username and email sets from italic (already processed) rows.
    print("Checking for existing usernames and duplicate submissions...")
    sheet_usernames = {}  # username_lower -> sheet_row
    seen_emails = {}  # email -> {full_name, company, timestamp}

    for i, row in enumerate(rows[1:], start=1):
        if not is_italic(i):
            continue
        def get_italic_col(idx):
            return row[idx].strip() if idx < len(row) else ""
        uname = get_italic_col(username_idx)
        email = get_italic_col(email_idx)
        if uname:
            sheet_usernames[uname.lower()] = i + 1
        raw_email = get_italic_col(email_idx)
        clean_email = extract_email(raw_email) if raw_email else ""
        if clean_email:
            seen_emails[clean_email.lower()] = {
                "full_name": get_italic_col(full_name_idx),
                "company":   get_italic_col(company_idx),
                "timestamp": get_italic_col(timestamp_idx),
            }

    print(f"  sheet: {len(sheet_usernames)} existing usernames, {len(seen_emails)} processed emails (italic rows only)")
    cluster_usernames = get_cluster_usernames()
    existing_usernames = set(sheet_usernames) | cluster_usernames

    users = []
    conflicts = []
    duplicates = []
    skipped_italic = 0
    skipped_empty = 0
    writeback_updates = []

    # Track usernames and emails seen this run to catch same-batch duplicates
    generated_this_run = set()
    emails_this_run = set()

    for i, row in enumerate(rows[1:], start=1):
        if is_italic(i):
            skipped_italic += 1
            continue

        def get_col(idx):
            return row[idx].strip() if idx < len(row) else ""

        username  = get_col(username_idx)
        full_name = get_col(full_name_idx)
        password  = get_col(password_idx)
        email     = get_col(email_idx)
        vmtag     = get_col(vmtag_idx)
        company   = get_col(company_idx)
        cc        = extract_email(get_col(cc_idx))
        timestamp = get_col(timestamp_idx)

        # Duplicate submission check — same email already processed or in this batch
        email_lower = email.lower() if email else ""
        if email_lower and email_lower in seen_emails:
            duplicates.append({
                "full_name":          full_name,
                "company":            company,
                "cc":                 cc,
                "sheet_row":          i + 1,
                "original_timestamp": seen_emails[email_lower]["timestamp"],
                "duplicate_timestamp": timestamp,
            })
            continue
        if email_lower and email_lower in emails_this_run:
            duplicates.append({
                "full_name":          full_name,
                "company":            company,
                "cc":                 cc,
                "sheet_row":          i + 1,
                "original_timestamp": timestamp,
                "duplicate_timestamp": timestamp,
            })
            continue
        if email_lower:
            emails_this_run.add(email_lower)

        # Auto-generate username if missing
        if not username:
            if not full_name:
                skipped_empty += 1
                continue
            username = generate_username(full_name)
            writeback_updates.append({
                "range": f"{sheet_name}!{chr(65 + username_idx)}{i + 1}",
                "values": [[username]],
            })

        # Auto-generate password if missing
        if not password:
            if not company:
                skipped_empty += 1
                continue
            password = generate_password(company)
            writeback_updates.append({
                "range": f"{sheet_name}!{chr(65 + password_idx)}{i + 1}",
                "values": [[password]],
            })

        # Auto-generate vmtag if missing
        if not vmtag and company:
            vmtag = generate_vmtag(company)
            writeback_updates.append({
                "range": f"{sheet_name}!{chr(65 + vmtag_idx)}{i + 1}",
                "values": [[vmtag]],
            })

        # Conflict check
        username_lower = username.lower()
        conflict_source = None
        original_row = None
        if username_lower in (override_usernames or set()):
            pass  # force-create despite conflict
        elif username_lower in generated_this_run:
            conflict_source = "same batch"
        elif username_lower in cluster_usernames:
            conflict_source = "cluster"
        elif username_lower in sheet_usernames:
            conflict_source = "sheet"
            original_row = sheet_usernames[username_lower]

        if conflict_source:
            conflict = {
                "username":  username,
                "full_name": full_name,
                "sheet_row": i + 1,
                "source":    conflict_source,
            }
            if original_row is not None:
                conflict["original_row"] = original_row
            conflicts.append(conflict)
            continue

        generated_this_run.add(username_lower)

        users.append({
            "username":  username,
            "full_name": full_name,
            "password":  password,
            "email":     email,
            "cc":        cc,
            "vmtag":     vmtag,
            "sheet_row": i + 1,
        })

    # Write generated credentials back to sheet
    if writeback_updates:
        if dry_run:
            print(f"[dry-run] Would write {len(writeback_updates)} generated credential(s) back to sheet")
        else:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "RAW", "data": writeback_updates},
            ).execute()
            print(f"Wrote {len(writeback_updates)} generated credential(s) back to sheet")

    with open(OUTPUT_FILE, "w") as f:
        yaml.dump(
            {"users": users, "conflicts": conflicts, "duplicates": duplicates},
            f,
            default_flow_style=False,
            allow_unicode=True,
        )

    print(
        f"Wrote {len(users)} new users to {OUTPUT_FILE} "
        f"({skipped_italic} already done"
        + (f", {len(duplicates)} duplicate(s)" if duplicates else "")
        + (f", {len(conflicts)} conflict(s)" if conflicts else "")
        + (f", {skipped_empty} empty rows" if skipped_empty else "")
        + ")"
    )
    return len(headers)


def send_emails():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD env vars must be set.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(OUTPUT_FILE):
        print("users.yml not found.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_FILE) as f:
        data = yaml.safe_load(f)

    users      = data.get("users", [])
    duplicates = data.get("duplicates", [])
    if not users and not duplicates:
        print("No emails to send.")
        return

    sent = 0
    skipped = 0
    dup_sent = 0

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

        for user in users:
            to_email = user.get("email", "").strip()
            if not to_email or "@" not in to_email:
                print(f"  skipping {user['username']} — missing or invalid email in sheet: '{to_email}'")
                skipped += 1
                continue

            body = EMAIL_TEMPLATE.format(
                username=user["username"],
                password=user["password"],
                vmtag=user.get("vmtag", ""),
            )

            cc_email = user.get("cc", "").strip()

            msg = MIMEText(body)
            msg["Subject"] = EMAIL_SUBJECT
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = to_email
            if cc_email and "@" in cc_email:
                msg["Cc"] = cc_email

            recipients = [to_email] + ([cc_email] if cc_email and "@" in cc_email else [])
            smtp.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
            print(f"  sent to {user['username']} <{to_email}>"
                  + (f" cc: {cc_email}" if cc_email and "@" in cc_email else ""))
            sent += 1

        for dup in duplicates:
            cc_email = dup.get("cc", "").strip()
            if not cc_email or "@" not in cc_email:
                print(f"  skipping duplicate notice for {dup['full_name']} — no territory email")
                continue

            body = DUPLICATE_TEMPLATE.format(
                full_name=dup["full_name"],
                company=dup["company"],
            )
            msg = MIMEText(body)
            msg["Subject"] = DUPLICATE_SUBJECT.format(full_name=dup["full_name"])
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = cc_email

            smtp.sendmail(GMAIL_ADDRESS, [cc_email], msg.as_string())
            print(f"  duplicate notice sent to territory team <{cc_email}> for {dup['full_name']}")
            dup_sent += 1

    print(f"Welcome emails sent: {sent}" + (f" ({skipped} skipped — no email in sheet)" if skipped else ""))
    if dup_sent:
        print(f"Duplicate notifications sent: {dup_sent}")


def mark_done(service, num_cols=None):
    if not os.path.exists(OUTPUT_FILE):
        print("users.yml not found — nothing to mark.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_FILE) as f:
        data = yaml.safe_load(f)

    users      = data.get("users", [])
    duplicates = data.get("duplicates", [])

    sheet_name, sheet_id = get_sheet_info(service)
    if num_cols is None:
        header_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet_name}!1:1",
        ).execute()
        num_cols = len(header_result.get("values", [[]])[0])

    user_rows = [u["sheet_row"] for u in users if "sheet_row" in u]
    dup_rows  = [d["sheet_row"] for d in duplicates if "sheet_row" in d]

    requests = []
    for row_num in user_rows:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                "fields": "userEnteredFormat.textFormat.italic",
            }
        })
    for row_num in dup_rows:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "strikethrough": True}}},
                "fields": "userEnteredFormat.textFormat.italic,userEnteredFormat.textFormat.strikethrough",
            }
        })

    if not requests:
        print("No rows to mark.")
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests},
    ).execute()

    if user_rows:
        print(f"Marked {len(user_rows)} row(s) as italic (created).")
    if dup_rows:
        print(f"Marked {len(dup_rows)} row(s) as italic + strikethrough (duplicate).")


def main():
    if not SPREADSHEET_ID:
        print("ERROR: GSHEET_SPREADSHEET_ID env var is not set.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"ERROR: Service account file not found: {SERVICE_ACCOUNT_FILE}", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["fetch", "send-emails", "mark-done"],
        default="fetch",
        help=(
            "fetch: read sheet, generate missing credentials, write users.yml  |  "
            "send-emails: email credentials to each user  |  "
            "mark-done: mark processed rows italic"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch mode only: skip writing generated credentials back to sheet",
    )
    parser.add_argument(
        "--override",
        default="",
        help="fetch mode only: comma-separated usernames to force-create despite conflicts",
    )
    args = parser.parse_args()

    if args.mode == "send-emails":
        send_emails()
        return

    service = build_service()

    if args.mode == "fetch":
        override_usernames = {u.strip().lower() for u in args.override.split(",") if u.strip()}
        if override_usernames:
            print(f"  overriding conflict check for: {', '.join(sorted(override_usernames))}")
        fetch_users(service, dry_run=args.dry_run, override_usernames=override_usernames)
    else:
        mark_done(service)


if __name__ == "__main__":
    main()
