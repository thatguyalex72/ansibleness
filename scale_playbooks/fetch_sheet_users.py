#!/usr/bin/env python3
"""
Reads the Scale demo lab user request Google Sheet and writes users.yml
for use by create_users.yml.

Rows already marked italic in the sheet are skipped (already created).
After Ansible finishes, run with --mode mark-done to italicize the newly
created rows automatically, replacing the manual italicize step.

Requirements:
    pip install google-api-python-client google-auth pyyaml

Setup:
    1. Create a Google Cloud service account, enable the Google Sheets API,
       and download the JSON key file.
    2. Share the Google Sheet with the service account email (Editor access).
    3. Export these env vars (e.g. in ~/.scale_env):
         export GSHEET_SERVICE_ACCOUNT_FILE="/path/to/service_account.json"
         export GSHEET_SPREADSHEET_ID="sheet_id_from_url"

    The sheet ID is the long string in the Google Sheet URL:
      https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit

Column config:
    Update the *_COL constants below to match the exact headers in your sheet.
    Run with --mode fetch once and it will print available columns if any are missing.
"""

import argparse
import os
import sys

import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = os.environ.get("GSHEET_SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID = os.environ.get("GSHEET_SPREADSHEET_ID", "")
SHEET_NAME = os.environ.get("GSHEET_SHEET_NAME", "")

# Update these to match the exact column headers in your Google Sheet
USERNAME_COL = "Username"
FULL_NAME_COL = "Prospect's Name (Ex: Alex Smith)"
PASSWORD_COL = "Password"

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.yml")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


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
        username_idx = headers.index(USERNAME_COL)
        full_name_idx = headers.index(FULL_NAME_COL)
        password_idx = headers.index(PASSWORD_COL)
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

        username = get_col(username_idx)
        full_name = get_col(full_name_idx)
        password = get_col(password_idx)

        if not username or not password:
            skipped_empty += 1
            continue

        users.append({
            "username": username,
            "full_name": full_name,
            "password": password,
            "sheet_row": i + 1,  # 1-indexed sheet row number (accounts for header)
        })

    with open(OUTPUT_FILE, "w") as f:
        yaml.dump({"users": users}, f, default_flow_style=False, allow_unicode=True)

    print(
        f"Wrote {len(users)} new users to {OUTPUT_FILE} "
        f"({skipped_italic} already done, {skipped_empty} empty rows skipped)"
    )
    return len(headers)


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
        choices=["fetch", "mark-done"],
        default="fetch",
        help="fetch: read sheet, write users.yml  |  mark-done: mark processed rows italic",
    )
    args = parser.parse_args()

    service = build_service()

    if args.mode == "fetch":
        fetch_users(service)
    else:
        mark_done(service)


if __name__ == "__main__":
    main()
