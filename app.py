"""
Flask web service wrapping the Flipkart scraper.
Render (and any cloud platform) needs an HTTP port — this provides it.

Endpoints:
  GET  /health   → liveness check
  POST /scrape   → start a scrape (runs in background thread)
  GET  /results  → return the latest scrape output
"""

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr so unicode characters print on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Startup: hydrate ephemeral files from environment variables.
# Render's filesystem resets on every deploy/restart, so credentials that
# were obtained locally are stored as env vars and written back here.
# ---------------------------------------------------------------------------

def _hydrate_file(env_key: str, file_path: Path) -> None:
    """Write env_key's value to file_path. Verbose logging so Render logs show status."""
    value = os.getenv(env_key, "").strip()
    if not value:
        print(f"[init] {env_key}: NOT SET — {file_path.name} will not be created.")
        return
    if file_path.exists():
        print(f"[init] {env_key}: skipping — {file_path.name} already exists.")
        return
    try:
        file_path.write_text(value, encoding="utf-8")
        # Validate it's parseable JSON since both files are JSON
        try:
            parsed = json.loads(value)
            keys = list(parsed.keys()) if isinstance(parsed, dict) else []
            print(
                f"[init] {env_key}: wrote {file_path.name} "
                f"({len(value)} chars, JSON keys: {keys})"
            )
        except json.JSONDecodeError as je:
            print(
                f"[init] {env_key}: wrote {file_path.name} but content is NOT valid JSON: {je}\n"
                f"        First 80 chars: {value[:80]!r}"
            )
    except Exception as exc:
        print(f"[init] {env_key}: ERROR writing {file_path.name}: {exc}")


print("=" * 60)
print("[init] Hydrating credentials from environment variables…")
_hydrate_file("GMAIL_TOKEN_JSON", Path("token.json"))
_hydrate_file("FLIPKART_AUTH_STATE", Path("auth_state.json"))
print("=" * 60)

# ---------------------------------------------------------------------------
# Scrape state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "running": False,
    "last_result": None,          # dict from orders_report.json
    "last_run_at": None,          # ISO timestamp
    "error": None,
}


def _run_scrape(num_orders: int) -> None:
    """Blocking function executed in a background thread."""
    global _state
    headless = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
    try:
        from scrape_flipkart_orders import run
        asyncio.run(run(num_orders=num_orders, headless=headless))

        report_path = Path("orders_report.json")
        if report_path.exists():
            _state["last_result"] = json.loads(report_path.read_text())
            _state["error"] = None

            # Log auth_state.json content so the user can update FLIPKART_AUTH_STATE
            auth_path = Path("auth_state.json")
            if auth_path.exists():
                print("\n[deploy] Copy the value below into the FLIPKART_AUTH_STATE "
                      "environment variable on Render to persist the Flipkart session:\n")
                print(auth_path.read_text())
                print()
        else:
            _state["error"] = "Scrape finished but orders_report.json was not created."

    except Exception as exc:
        _state["error"] = str(exc)
        print(f"[scrape] Error: {exc}")
    finally:
        _state["running"] = False
        _state["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()})


def _shape_products() -> list[dict]:
    """Return the latest scrape result in {product_name, date, number_of_times_purchased} format."""
    result = _state.get("last_result") or {}
    return [
        {
            "product_name": p["title"],
            "date": p["purchase_date"],
            "number_of_times_purchased": p["purchase_count_in_last_10_orders"],
        }
        for p in result.get("products", [])
    ]


@app.route("/api/products", methods=["GET"])
def api_get_products():
    """
    Returns the products from the last 10 Flipkart orders in clean JSON.

    Response (200) when data is available:
      {
        "scraped_at": "...",
        "orders_scanned": 10,
        "products": [
          { "product_name": "...", "date": "YYYY-MM-DD", "number_of_times_purchased": 1 },
          ...
        ]
      }

    Response (202) if a scrape is currently running.
    Response (404) if no scrape has been run yet — call POST /api/products to start one.
    """
    if _state["running"]:
        return jsonify({
            "status": "running",
            "message": "A scrape is in progress. Try again in 2-5 minutes.",
        }), 202

    if _state["error"]:
        return jsonify({
            "status": "error",
            "error": _state["error"],
            "last_run_at": _state["last_run_at"],
        }), 500

    if _state["last_result"] is None:
        return jsonify({
            "status": "no_data",
            "message": "No scrape has been run yet. POST /api/products to start one.",
        }), 404

    result = _state["last_result"]
    return jsonify({
        "scraped_at": result.get("scraped_at"),
        "orders_scanned": result.get("orders_scanned", 0),
        "products": _shape_products(),
    }), 200


@app.route("/api/products", methods=["POST"])
def api_refresh_products():
    """
    Trigger a fresh scrape of the last 10 Flipkart orders.

    Optional JSON body: { "orders": <int> }   (default: 10)

    Returns immediately with status 202.
    Poll GET /api/products until status switches from "running" to having data.
    """
    with _lock:
        if _state["running"]:
            return jsonify({
                "status": "running",
                "message": "A scrape is already in progress.",
            }), 409

        body = request.get_json(silent=True) or {}
        num_orders = int(body.get("orders", 10))

        _state["running"] = True
        _state["error"] = None

    thread = threading.Thread(target=_run_scrape, args=(num_orders,), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "orders_requested": num_orders,
        "message": "Scrape started. Poll GET /api/products until results appear.",
    }), 202


# ---------------------------------------------------------------------------
# OpenAPI 3.0 spec + Swagger UI served at /docs
# ---------------------------------------------------------------------------

_OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Purchase History API",
        "version": "1.0.0",
        "description": (
            "Flipkart order scraper + Salesforce `Grocery_Product__c` sync.\n\n"
            "After every successful scrape, each unique product title is matched against "
            "`Grocery_Product__c.title__c`. Matching records get `number_of_times_purchased__c` "
            "and `last_ordered_date__c` updated. **No new records are ever created.**\n\n"
            "A scrape runs in a background thread and typically takes 2–5 minutes. "
            "Poll `GET /api/products` until the status flips from `running` to `ok`."
        ),
    },
    "tags": [
        {"name": "system",   "description": "Health and liveness."},
        {"name": "scrape",   "description": "Trigger Flipkart scrapes."},
        {"name": "products", "description": "Read the latest scrape output."},
    ],
    "paths": {
        "/health": {
            "get": {
                "tags": ["system"],
                "summary": "Liveness probe",
                "description": "Returns `ok` and the current server timestamp.",
                "responses": {
                    "200": {
                        "description": "Server is up.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Health"},
                            "example": {"status": "ok", "timestamp": "2026-05-24T03:30:00+00:00"},
                        }},
                    }
                },
            }
        },
        "/api/products": {
            "get": {
                "tags": ["products"],
                "summary": "Get products from the last scrape (clean shape)",
                "description": (
                    "Returns the products in `{product_name, date, number_of_times_purchased}` shape — "
                    "easier to consume than `/results`."
                ),
                "responses": {
                    "200": {
                        "description": "Products available.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ProductsOk"},
                        }},
                    },
                    "202": {
                        "description": "A scrape is currently running.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Running"},
                        }},
                    },
                    "404": {
                        "description": "No scrape has been run yet.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "status": "no_data",
                                "message": "No scrape has been run yet. POST /api/products to start one.",
                            },
                        }},
                    },
                    "500": {
                        "description": "The last scrape errored.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapeError"},
                        }},
                    },
                },
            },
            "post": {
                "tags": ["scrape"],
                "summary": "Refresh products (start a scrape)",
                "description": "Starts a Flipkart scrape in a background thread. Returns `202 started` immediately.",
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/ScrapeRequest"},
                        "example": {"orders": 10},
                    }},
                },
                "responses": {
                    "202": {
                        "description": "Scrape started.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapeStarted"},
                        }},
                    },
                    "409": {
                        "description": "A scrape is already running.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                        }},
                    },
                },
            },
        },
    },
    "components": {
        "schemas": {
            "Health": {
                "type": "object",
                "properties": {
                    "status":    {"type": "string", "example": "ok"},
                    "timestamp": {"type": "string", "format": "date-time"},
                },
            },
            "ScrapeRequest": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "integer", "minimum": 1, "maximum": 50, "default": 10,
                        "description": "Number of recent Flipkart orders to scrape.",
                    },
                },
            },
            "ScrapeStarted": {
                "type": "object",
                "properties": {
                    "status":           {"type": "string", "example": "started"},
                    "orders_requested": {"type": "integer", "example": 10},
                    "message":          {"type": "string"},
                },
            },
            "Running": {
                "type": "object",
                "properties": {
                    "status":  {"type": "string", "example": "running"},
                    "message": {"type": "string"},
                },
            },
            "Error": {
                "type": "object",
                "properties": {
                    "status":  {"type": "string"},
                    "error":   {"type": "string"},
                    "message": {"type": "string"},
                },
            },
            "ScrapeError": {
                "type": "object",
                "properties": {
                    "status":      {"type": "string", "example": "error"},
                    "error":       {"type": "string"},
                    "last_run_at": {"type": "string", "format": "date-time"},
                },
            },
            "ProductsOk": {
                "type": "object",
                "properties": {
                    "scraped_at":     {"type": "string", "format": "date-time"},
                    "orders_scanned": {"type": "integer"},
                    "products": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/CleanProduct"},
                    },
                },
            },
            "CleanProduct": {
                "type": "object",
                "properties": {
                    "product_name":              {"type": "string"},
                    "date":                      {"type": "string", "example": "2026-04-12"},
                    "number_of_times_purchased": {"type": "integer"},
                },
            },
        }
    },
}


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Purchase History API — Docs</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css">
  <link rel="icon" type="image/svg+xml"
        href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><text y='52' font-size='52'>📦</text></svg>">
  <style>
    body { margin: 0; background: #fafafa; }
    .topbar { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
  <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-standalone-preset.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIStandalonePreset
      ],
      plugins: [SwaggerUIBundle.plugins.DownloadUrl],
      layout: "BaseLayout",
      tryItOutEnabled: true,
      persistAuthorization: true,
      defaultModelsExpandDepth: 0,
      docExpansion: "list"
    });
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return redirect("/docs", code=302)


@app.route("/docs", methods=["GET"])
def docs():
    return Response(_SWAGGER_HTML, mimetype="text/html; charset=utf-8")


@app.route("/openapi.json", methods=["GET"])
def openapi_spec():
    return jsonify(_OPENAPI_SPEC)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print(f"[server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
