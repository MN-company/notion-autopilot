#!/usr/bin/env python3
import csv
import json
import os
import sys
import datetime
import urllib.request
import urllib.error
import re

NOTION_API_BASE = "https://api.notion.com"
DEFAULT_NOTION_VERSION = "2022-06-28"


def env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        print(f"Missing required env var: {name}")
        sys.exit(1)
    return value


def request_json(method, path, token, notion_version, body=None):
    url = f"{NOTION_API_BASE}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": notion_version,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Notion API error {e.code}: {error_body}")


def normalize_notion_id(value):
    if not value:
        return value
    raw = value.strip()
    # If a full URL is provided, extract the first 32-hex identifier.
    match = re.search(r"[0-9a-fA-F]{32}", raw)
    if match:
        return match.group(0)
    return raw


def parse_bool(value):
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "y"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    return None


def parse_number(value):
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def build_properties(row, last_seen):
    props = {
        "Key": {"title": [{"text": {"content": row["Key"]}}]},
    }

    value_text = row.get("Value", "")
    if value_text:
        props["Value"] = {"rich_text": [{"text": {"content": value_text}}]}
    else:
        props["Value"] = {"rich_text": []}

    if row.get("Type"):
        props["Type"] = {"select": {"name": row["Type"]}}
    if row.get("Scope"):
        props["Scope"] = {"select": {"name": row["Scope"]}}
    if row.get("Applies_to"):
        props["Applies_to"] = {"select": {"name": row["Applies_to"]}}

    inferred = parse_bool(row.get("Inferred"))
    if inferred is not None:
        props["Inferred"] = {"checkbox": inferred}

    confidence = parse_number(row.get("Confidence"))
    if confidence is not None:
        props["Confidence"] = {"number": confidence}

    source_text = row.get("Source", "")
    if source_text:
        props["Source"] = {"rich_text": [{"text": {"content": source_text}}]}

    if last_seen:
        props["Last_seen"] = {"date": {"start": last_seen}}

    return props


def extract_rich_text(prop):
    if not prop:
        return ""
    rt = prop.get("rich_text") or []
    return "".join([t.get("plain_text", "") for t in rt])


def extract_title(prop):
    if not prop:
        return ""
    rt = prop.get("title") or []
    return "".join([t.get("plain_text", "") for t in rt])


def extract_select(prop):
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name") if sel else ""


def extract_checkbox(prop):
    if not prop:
        return None
    return prop.get("checkbox")


def extract_number(prop):
    if not prop:
        return None
    return prop.get("number")


def load_existing_page(db_id, key, token, notion_version):
    body = {
        "filter": {
            "property": "Key",
            "title": {"equals": key},
        },
        "page_size": 1,
    }
    resp = request_json("POST", f"/v1/databases/{db_id}/query", token, notion_version, body)
    results = resp.get("results", [])
    return results[0] if results else None


def should_overwrite(existing_page, mode):
    if mode == "overwrite":
        return True

    props = existing_page.get("properties", {})
    source = extract_rich_text(props.get("Source"))
    inferred = extract_checkbox(props.get("Inferred"))

    if inferred is True:
        return True
    if source in ("explicit", "seed", "config"):
        return True

    return False


def sync_row(db_id, row, token, notion_version, mode, last_seen):
    key = row["Key"]
    existing = load_existing_page(db_id, key, token, notion_version)
    props = build_properties(row, last_seen)

    if existing:
        if not should_overwrite(existing, mode):
            print(f"SKIP {key} (preserving user-managed row)")
            return
        page_id = existing["id"]
        body = {"properties": props}
        request_json("PATCH", f"/v1/pages/{page_id}", token, notion_version, body)
        print(f"UPDATE {key}")
    else:
        body = {
            "parent": {"database_id": db_id},
            "properties": props,
        }
        request_json("POST", "/v1/pages", token, notion_version, body)
        print(f"CREATE {key}")


def main():
    token = env("NOTION_TOKEN", required=True)
    db_id_raw = env("NOTION_DATABASE_ID", required=True)
    csv_path = env("PREFERENCES_CSV", "seed_preferences.csv")
    notion_version = env("NOTION_VERSION", DEFAULT_NOTION_VERSION)
    mode = env("SYNC_MODE", "overwrite").strip().lower()

    db_id = normalize_notion_id(db_id_raw)
    if not db_id or len(db_id) < 32:
        print("NOTION_DATABASE_ID must be a database id or a URL containing one.")
        sys.exit(1)

    if mode not in ("overwrite", "merge"):
        print("SYNC_MODE must be 'overwrite' or 'merge'")
        sys.exit(1)

    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    today = datetime.date.today().isoformat()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"Key", "Value", "Type", "Scope", "Applies_to", "Inferred", "Confidence", "Source", "Last_seen"}
        if not required.issubset(reader.fieldnames or []):
            print("CSV missing required headers")
            sys.exit(1)

        for row in reader:
            if not row.get("Key"):
                continue
            last_seen = row.get("Last_seen") or today
            sync_row(db_id, row, token, notion_version, mode, last_seen)


if __name__ == "__main__":
    main()
