"""
Sync scraped Flipkart purchase history to Salesforce Grocery_Product__c records.

For each unique product title in the scrape result:
  - Find the Grocery_Product__c record whose title__c matches the product title.
  - If found, PATCH number_of_times_purchased__c and last_ordered_date__c.
  - If not found, log and skip — new records are never created.

Salesforce auth uses the OAuth 2.0 client_credentials flow against the Connected
App identified by SF_CLIENT_ID / SF_CLIENT_SECRET.

Required env vars:
    SF_TOKEN_URL       e.g. https://<domain>.my.salesforce.com/services/oauth2/token
    SF_CLIENT_ID
    SF_CLIENT_SECRET
    SF_API_ENDPOINT    e.g. https://<domain>.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/

If any of those are missing, sync_products() returns a "skipped" stats dict
without raising — the scraper still completes successfully.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable
from urllib.parse import quote, urlparse

import requests

# Force UTF-8 stdout/stderr so unicode characters print on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TITLE_FIELD = "title__c"
COUNT_FIELD = "number_of_times_purchased__c"
DATE_FIELD = "last_ordered_date__c"

_REQUIRED_ENV = ("SF_TOKEN_URL", "SF_CLIENT_ID", "SF_CLIENT_SECRET", "SF_API_ENDPOINT")
_TOKEN_CACHE: dict[str, str] = {}


class SalesforceError(RuntimeError):
    pass


def _config_present() -> bool:
    return all((os.getenv(k) or "").strip() for k in _REQUIRED_ENV)


def _env(name: str) -> str:
    val = (os.getenv(name) or "").strip()
    if not val:
        raise SalesforceError(f"Environment variable {name} is required for Salesforce sync.")
    return val


def _sobject_base() -> str:
    base = _env("SF_API_ENDPOINT")
    return base if base.endswith("/") else base + "/"


def _instance_root() -> str:
    parsed = urlparse(_sobject_base())
    return f"{parsed.scheme}://{parsed.netloc}"


def _api_version_path() -> str:
    m = re.search(r"/services/data/v\d+\.\d+", _sobject_base())
    if not m:
        raise SalesforceError(
            "SF_API_ENDPOINT must include '/services/data/v<XX.X>/' (e.g. v57.0)."
        )
    return m.group(0)


def get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh and _TOKEN_CACHE.get("access_token"):
        return _TOKEN_CACHE["access_token"]

    resp = requests.post(
        _env("SF_TOKEN_URL"),
        data={
            "grant_type": "client_credentials",
            "client_id": _env("SF_CLIENT_ID"),
            "client_secret": _env("SF_CLIENT_SECRET"),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SalesforceError(
            f"Salesforce OAuth failed: {resp.status_code} {resp.text[:300]}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise SalesforceError(f"Salesforce OAuth response missing access_token: {resp.text[:300]}")
    _TOKEN_CACHE["access_token"] = token
    print("[salesforce] Obtained access token via client_credentials grant.")
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Wrap requests so that a 401 triggers one token refresh + retry."""
    resp = requests.request(method, url, headers=_auth_headers(), timeout=30, **kwargs)
    if resp.status_code == 401:
        get_access_token(force_refresh=True)
        resp = requests.request(method, url, headers=_auth_headers(), timeout=30, **kwargs)
    return resp


def _query(soql: str) -> list[dict]:
    url = f"{_instance_root()}{_api_version_path()}/query/?q={quote(soql)}"
    resp = _request("GET", url)
    if resp.status_code != 200:
        raise SalesforceError(f"SOQL query failed: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("records", [])


def _find_by_title(title: str) -> dict | None:
    safe = title.replace("\\", "\\\\").replace("'", "\\'")
    soql = (
        f"SELECT Id, {TITLE_FIELD}, {COUNT_FIELD}, {DATE_FIELD} "
        f"FROM Grocery_Product__c "
        f"WHERE {TITLE_FIELD} = '{safe}' LIMIT 1"
    )
    records = _query(soql)
    return records[0] if records else None


def _patch(record_id: str, payload: dict) -> None:
    url = f"{_sobject_base()}{record_id}"
    resp = _request("PATCH", url, json=payload)
    if resp.status_code not in (200, 204):
        raise SalesforceError(
            f"Update {record_id} failed: {resp.status_code} {resp.text[:300]}"
        )


def _dedupe(products: Iterable[dict]) -> list[dict]:
    """One entry per title; keep the latest known date and the report's count."""
    grouped: dict[str, dict] = {}
    for p in products:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        raw_date = p.get("purchase_date")
        date = raw_date if raw_date and raw_date != "unknown" else None
        try:
            count = int(p.get("purchase_count_in_last_10_orders") or 0)
        except (TypeError, ValueError):
            count = 0

        cur = grouped.get(title)
        if cur is None:
            grouped[title] = {"title": title, "last_ordered_date": date, "count": count}
            continue
        if date and (cur["last_ordered_date"] is None or date > cur["last_ordered_date"]):
            cur["last_ordered_date"] = date
        cur["count"] = max(cur["count"], count)
    return list(grouped.values())


def sync_products(products: Iterable[dict]) -> dict:
    """
    Sync `products` (rows from orders_report.json) to Salesforce.

    Each row should expose: title, purchase_date, purchase_count_in_last_10_orders.
    Returns a stats dict; never raises — errors are caught and logged so the
    scrape pipeline does not fail if Salesforce is temporarily unavailable.
    """
    stats = {"updated": 0, "not_found": 0, "errors": 0, "skipped": 0}

    if not _config_present():
        missing = [k for k in _REQUIRED_ENV if not (os.getenv(k) or "").strip()]
        print(f"[salesforce] Sync skipped — missing env vars: {', '.join(missing)}")
        stats["skipped"] = 1
        return stats

    deduped = _dedupe(products)
    if not deduped:
        print("[salesforce] No products to sync.")
        return stats

    print(f"[salesforce] Syncing {len(deduped)} unique product(s) to Grocery_Product__c …")

    for entry in deduped:
        title = entry["title"]
        body: dict = {COUNT_FIELD: entry["count"]}
        if entry["last_ordered_date"]:
            body[DATE_FIELD] = entry["last_ordered_date"]

        try:
            existing = _find_by_title(title)
            if existing:
                _patch(existing["Id"], body)
                stats["updated"] += 1
                print(
                    f"  [updated]   {title[:60]:<60}  "
                    f"count={entry['count']}  date={entry['last_ordered_date']}"
                )
            else:
                stats["not_found"] += 1
                print(f"  [not found] {title[:60]:<60}  skipped (no matching title__c)")
        except SalesforceError as exc:
            stats["errors"] += 1
            print(f"  [error]     {title[:60]} → {exc}")
        except Exception as exc:
            stats["errors"] += 1
            print(f"  [error]     {title[:60]} → unexpected: {exc}")

    print(
        f"[salesforce] Sync complete: "
        f"{stats['updated']} updated, {stats['not_found']} not found, "
        f"{stats['errors']} errors."
    )
    return stats


def _cli() -> None:
    """Run sync against the local orders_report.json — handy for ad-hoc reruns."""
    import json
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv()
    path = Path("orders_report.json")
    if not path.exists():
        print("[salesforce] orders_report.json not found — run the scraper first.")
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    sync_products(report.get("products", []))


if __name__ == "__main__":
    _cli()
