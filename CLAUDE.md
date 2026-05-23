# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

A Playwright-based automation service that logs into Flipkart via OTP (fetched
automatically from Gmail using the Gmail API), scrapes the last 10 orders from the
order history, expands each order into its constituent items (via "See all items"),
and produces a per-product report containing:

1. Purchase date — may be the same for all products in a single order.
2. Number of times that product appears across the last 10 orders.

After each successful scrape, the report is also pushed to **Salesforce**: each
unique product title is matched against `Grocery_Product__c.title__c`, and
matching records get `number_of_times_purchased__c` and `last_ordered_date__c`
updated. **No new records are ever created** — non-matching titles are skipped.

The project runs as a **Flask web service** — scraping is triggered via HTTP
endpoints, and an interactive **Swagger UI** is served at `/docs`. It is designed
for local development and cloud deployment on **Render** (Docker-based).

## Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11 |
| Browser automation | Playwright (async) + Chromium |
| OTP retrieval | Gmail API (OAuth 2.0 Desktop app) — no email password needed |
| Salesforce sync | REST API + OAuth 2.0 client_credentials (Connected App) |
| Web service | Flask 3 |
| API docs | Swagger UI (CDN) backed by OpenAPI 3.0 spec at `/openapi.json` |
| Deployment | Render (Docker) |
| Config | `.env` file locally; Render environment variables in production |

## File Layout

```
.
├── CLAUDE.md
├── README.md
├── Dockerfile               # Docker image for Render deployment
├── render.yaml              # Render service configuration
├── .env.example             # Template — no real values
├── .gitignore
├── .dockerignore
├── requirements.txt
├── app.py                   # Flask web service (entry point) + Swagger UI at /docs
├── scrape_flipkart_orders.py  # Core scraping logic; calls salesforce_sync at end
├── salesforce_sync.py       # OAuth + PATCH Grocery_Product__c.title__c matches
└── test_gmail_auth.py       # Standalone Gmail API auth test
```

## Environment Variables

### Required (local `.env` and Render dashboard)

| Variable | Description |
|---|---|
| `FLIPKART_USERNAME` | Flipkart login email (same Gmail that receives OTP) |
| `GMAIL_CLIENT_ID` | OAuth 2.0 Desktop app Client ID from Google Cloud Console |
| `GMAIL_CLIENT_SECRET` | OAuth 2.0 Desktop app Client Secret |

### Salesforce sync (all four required; sync is skipped if any are missing)

| Variable | Description |
|---|---|
| `SF_TOKEN_URL` | OAuth token endpoint, e.g. `https://<domain>.my.salesforce.com/services/oauth2/token` |
| `SF_CLIENT_ID` | Connected App consumer key |
| `SF_CLIENT_SECRET` | Connected App consumer secret |
| `SF_API_ENDPOINT` | `https://<domain>.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/` |

### Cloud-only (Render dashboard — populated after first local run)

| Variable | Description |
|---|---|
| `GMAIL_TOKEN_JSON` | Full contents of `token.json` after local OAuth consent |
| `FLIPKART_AUTH_STATE` | Full contents of `auth_state.json` after first successful scrape |

### Optional overrides

| Variable | Default |
|---|---|
| `GMAIL_TOKEN_FILE` | `token.json` |
| `HEADLESS` | `false` locally, `true` in Docker |
| `PORT` | `10000` (Render sets this automatically) |

## Environment Setup (Local)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in `FLIPKART_USERNAME`, `GMAIL_CLIENT_ID`,
`GMAIL_CLIENT_SECRET`.

### Gmail OAuth — one-time setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → Enable **Gmail API**
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app** (NOT Web application)
4. Copy Client ID and Client Secret into `.env`
5. Run `python test_gmail_auth.py` — a browser opens for one-time Google consent
6. After approval, `token.json` is saved — all future runs are silent

> **Why Desktop app?** Desktop app OAuth clients automatically allow
> `http://localhost:{any_port}` redirects. Web application clients require every
> redirect URI to be explicitly registered, which breaks when the OAuth library
> picks a random port.

## Running Locally

### Start the web service

```powershell
$env:PORT="3000"; $env:HEADLESS="false"; .venv\Scripts\python.exe app.py
```

### API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/docs` | Interactive Swagger UI |
| `GET` | `/openapi.json` | OpenAPI 3.0 spec |
| `GET` | `/api/products` | Latest scrape output, `{product_name, date, number_of_times_purchased}` shape |
| `POST` | `/api/products` | Start a scrape (runs in background thread). Body: `{"orders": <int>}`, default 10 |

Open `http://localhost:3000/docs` for the interactive playground (the `/` route
redirects there). From there, every endpoint can be exercised with the
**Try it out** button.

```powershell
# Trigger scrape
Invoke-RestMethod -Method POST -Uri http://localhost:3000/api/products `
  -ContentType "application/json" -Body '{"orders": 10}'

# Poll for results (scrape takes ~2–5 minutes)
Invoke-RestMethod http://localhost:3000/api/products
```

### Run the scraper directly (without Flask)

```powershell
.venv\Scripts\python.exe scrape_flipkart_orders.py           # headed, 10 orders
.venv\Scripts\python.exe scrape_flipkart_orders.py --orders=5
.venv\Scripts\python.exe scrape_flipkart_orders.py --headed=false  # headless
```

### Test Gmail API auth

```powershell
.venv\Scripts\python.exe test_gmail_auth.py        # steps 1–3
.venv\Scripts\python.exe test_gmail_auth.py --otp  # also search for OTP email
```

## High-Level Scraping Flow

1. **Gmail API authenticates** silently using `token.json` (or prompts browser
   consent on first run).
2. **Launch Chromium** (headed locally, headless in Docker) with a persistent
   browser profile for session reuse.
3. **Navigate** to `https://www.flipkart.com`.
4. **Login via OTP** (fully automated):
   - Detect and open the login form (modal auto-open, or click Login in nav).
   - Enter `FLIPKART_USERNAME`.
   - Click "Request OTP".
   - Poll Gmail API every 6 s (up to 90 s) for a Flipkart email received after
     the OTP was requested; extract the 6-digit OTP.
   - Fill OTP (single input or 6 digit-box layout) → submit.
5. **Save session** to `auth_state.json` (skips login on subsequent runs).
6. **Navigate** to `https://www.flipkart.com/account/orders`.
7. **Scroll** until 10 order cards are loaded (lazy-load aware).
8. **For each order**: click "See all items" / "View all items" if present.
9. **Extract** product title + purchase date from each card.
10. **Aggregate** by product title; write `orders_report.json`; print table.
11. **Sync to Salesforce** (best-effort):
    - Authenticate via `client_credentials` against `SF_TOKEN_URL`.
    - For each unique title, SOQL-query `Grocery_Product__c` by `title__c`.
    - On match → `PATCH` `number_of_times_purchased__c` + `last_ordered_date__c`.
    - On miss → log `[not found]` and skip. **Never insert new records.**
    - Any Salesforce error is logged but does not fail the scrape.

## Selector Strategy

Flipkart's DOM uses hashed CSS class names that change frequently.
**Never rely on those class names alone.** Use this priority order:

1. `placeholder*=` attribute matching (most stable for inputs)
2. Role + accessible name: `page.get_by_role("button", name="Login")`
3. Visible text: `page.get_by_text("See all items", exact=False)`
4. Stable attributes if present (`data-testid`, `data-tkid`)
5. Structural selectors anchored on visible text as last resort
6. Hashed class names — only as fallback, with a `# TODO: replace` comment

All selectors are centralised in the `SELECTORS` dict at the top of
`scrape_flipkart_orders.py`. Update there and nowhere else.

When a selector fails:
- Save a screenshot (`login_debug.png` or `otp_debug.png`) for diagnosis.
- Log the current URL and a 600-char HTML snippet.
- Exit non-zero. Do not silently continue with empty data.

## Expected Output Shape

`orders_report.json`:
```json
{
  "scraped_at": "2026-05-23T10:15:00+05:30",
  "orders_scanned": 10,
  "products": [
    {
      "title": "Boat Airdopes 141 Bluetooth Headset",
      "purchase_date": "2026-04-12",
      "purchase_count_in_last_10_orders": 2
    }
  ]
}
```

`purchase_count_in_last_10_orders` is the same for every row of the same product
title — it is a per-product aggregate repeated on each occurrence row.

## Render Deployment

### How it works

- Render builds the `Dockerfile` (Python 3.11-slim + Playwright Chromium).
- On container start, `app.py` reads `GMAIL_TOKEN_JSON` and `FLIPKART_AUTH_STATE`
  env vars and writes them to `token.json` / `auth_state.json` (Render's filesystem
  is ephemeral — files reset on every restart).
- Scraping runs headless inside the container.

### Deploy steps

1. Complete Gmail OAuth locally (`python test_gmail_auth.py`) → get `token.json`.
2. Push code to GitHub.
3. Render → **New Web Service** → connect repo → auto-detects `Dockerfile`.
4. Add environment variables in Render dashboard (see table above).
5. Set `GMAIL_TOKEN_JSON` = full contents of local `token.json`.
6. After first successful scrape, Render logs print `auth_state.json` contents —
   copy that value into `FLIPKART_AUTH_STATE` env var.

### Session persistence on Render

Since the filesystem is ephemeral, both credentials are stored as env vars:

```
Container starts
  └─ app.py writes GMAIL_TOKEN_JSON  → token.json
  └─ app.py writes FLIPKART_AUTH_STATE → auth_state.json  (if set)
  └─ Scraper runs using those files normally
  └─ After scrape: logs new auth_state.json so user can update env var
```

## Important Constraints & Pitfalls

- **No credential logging.** Never print OTP values. Mask the username
  (show only last 4 chars).
- **Respect Flipkart.** Single browser context, default Playwright user-agent,
  human-paced clicks (`wait_for_load_state("networkidle")` between navigations,
  small `wait_for_timeout` between scrolls). Do not parallelise requests.
- **Captcha.** If a captcha appears, stop and surface a clear message. Never
  attempt to solve a captcha automatically.
- **Lazy loading.** Scroll until 10 order cards are in the DOM, or stop after
  2 scroll attempts with no new cards.
- **"See all items" wording varies.** Match on `(see|view).+item` (case-insensitive).
- **Date parsing.** Flipkart shows dates like "Delivered on Mon, Apr 12th '26".
  Use `dateutil.parser` with `fuzzy=True`; fall back to regex extraction.
- **OTP timing.** Record a Unix timestamp just before clicking "Request OTP".
  Pass it as the `after:` filter to Gmail API so stale OTP emails are ignored.
- **Desktop app OAuth only.** The Gmail OAuth client MUST be type "Desktop app"
  in Google Cloud Console. Web application clients require exact redirect URI
  registration and break with random localhost ports.
- **`.gitignore` must include**: `.env`, `auth_state.json`, `token.json`,
  `browser_profile/`, `__pycache__/`, `.venv/`, `orders_report.json`,
  `login_debug.png`, `otp_debug.png`.

## Salesforce sync notes

- Auth uses OAuth 2.0 `client_credentials` flow (Connected App with the "Run As"
  user set). Tokens are cached in-process and refreshed on a 401.
- Field mapping (hard-coded constants at the top of `salesforce_sync.py`):
  - Match field: `title__c`
  - Updated fields: `number_of_times_purchased__c`, `last_ordered_date__c`
- The Connected App must grant access to the `Grocery_Product__c` sObject and
  the `api` scope. `Name` is auto-number on this object and **must not** be sent
  in POST/PATCH bodies.
- Running `python salesforce_sync.py` re-syncs the current `orders_report.json`
  on demand, without re-running the scraper.

## Out of Scope

- No purchase, cancel, return, or any write action on the Flipkart account.
- No scraping beyond the 10 most recent orders unless `--orders` is explicitly raised.
- No creation of new Salesforce records — sync only updates titles that already exist.
- No multi-user support — the service is single-tenant (one Flipkart account).
