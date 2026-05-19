#!/usr/bin/env python3
"""
Reads the Scale demo lab user request Google Sheet and writes users.yml
for use by create_users.yml.

Rows already marked italic in the sheet are skipped (already created).
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

    The sheet ID is the long string in the Google Sheet URL:
      https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit

Column config:
    Update the *_COL constants below to match the exact headers in your sheet.
    Run with --mode fetch once and it will print available columns if any are missing.
"""

import argparse
import os
import smtplib
import sys
from email.mime.text import MIMEText

import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = os.environ.get("GSHEET_SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID = os.environ.get("GSHEET_SPREADSHEET_ID", "")
SHEET_NAME = os.environ.get("GSHEET_SHEET_NAME", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Update these to match the exact column headers in your Google Sheet
USERNAME_COL = "Username"
FULL_NAME_COL = "Prospect's Name (Ex: Alex Smith)"
PASSWORD_COL  = "Password"
EMAIL_COL     = "Email (alex@acme_it.com)"
VMTAG_COL     = "Templates/Tags"

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.yml")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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


def build_service():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_sheet_info(service):
    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    props = metadata["sheets"][0]["properties"]
    name = SHEET_NAME or props["title"]
    print(f"Using sheet tab: {name}")
    return name, props["sheetId"]


def fetch_users(service):
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
        username_idx  = headers.index(USERNAME_COL)
        full_name_idx = headers.index(FULL_NAME_COL)
        password_idx  = headers.index(PASSWORD_COL)
        email_idx     = headers.index(EMAIL_COL)
        vmtag_idx     = headers.index(VMTAG_COL)
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

    users = []
    skipped_italic = 0
    skipped_empty = 0

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

        if not username or not password:
            skipped_empty += 1
            continue

        users.append({
            "username":  username,
            "full_name": full_name,
            "password":  password,
            "email":     email,
            "vmtag":     vmtag,
            "sheet_row": i + 1,
        })

    with open(OUTPUT_FILE, "w") as f:
        yaml.dump({"users": users}, f, default_flow_style=False, allow_unicode=True)

    print(
        f"Wrote {len(users)} new users to {OUTPUT_FILE} "
        f"({skipped_italic} already done, {skipped_empty} empty rows skipped)"
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

    users = data.get("users", [])
    if not users:
        print("No users to email.")
        return

    sent = 0
    skipped = 0

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

        for user in users:
            to_email = user.get("email", "").strip()
            if not to_email:
                print(f"  skipping {user['username']} — no email address in sheet")
                skipped += 1
                continue

            body = EMAIL_TEMPLATE.format(
                username=user["username"],
                password=user["password"],
                vmtag=user.get("vmtag", ""),
            )

            msg = MIMEText(body)
            msg["Subject"] = EMAIL_SUBJECT
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = to_email

            smtp.send_message(msg)
            print(f"  sent to {user['username']} <{to_email}>")
            sent += 1

    print(f"Emails sent: {sent}" + (f" ({skipped} skipped — no email in sheet)" if skipped else ""))


def mark_done(service, num_cols=None):
    if not os.path.exists(OUTPUT_FILE):
        print("users.yml not found — nothing to mark.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_FILE) as f:
        data = yaml.safe_load(f)

    users = data.get("users", [])
    row_numbers = [u["sheet_row"] for u in users if "sheet_row" in u]

    if not row_numbers:
        print("No rows to mark.")
        return

    sheet_name, sheet_id = get_sheet_info(service)
    if num_cols is None:
        header_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet_name}!1:1",
        ).execute()
        num_cols = len(header_result.get("values", [[]])[0])

    requests = [
        {
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
        }
        for row_num in row_numbers
    ]

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests},
    ).execute()

    print(f"Marked {len(row_numbers)} rows as italic in the sheet.")


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
            "fetch: read sheet, write users.yml  |  "
            "send-emails: email credentials to each user  |  "
            "mark-done: mark processed rows italic"
        ),
    )
    args = parser.parse_args()

    if args.mode == "send-emails":
        send_emails()
        return

    service = build_service()

    if args.mode == "fetch":
        fetch_users(service)
    else:
        mark_done(service)


if __name__ == "__main__":
    main()
