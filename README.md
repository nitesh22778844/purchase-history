# Flipkart Purchase History → Salesforce

A Flask web service that logs into Flipkart via OTP (fetched automatically from
Gmail), scrapes the last 10 orders (including Flipkart **Minutes Basket**
grocery orders), and syncs each product to Salesforce `Grocery_Product__c`
records.

- **OTP login is fully automated** via the Gmail API — no email password needed.
- **Salesforce sync is update-only** — existing `Grocery_Product__c` records are
  matched by `title__c` and have `number_of_times_purchased__c` and
  `last_ordered_date__c` patched. New records are **never** created.
- **Interactive Swagger UI** is served at `/docs`.
- **Deployable to Render** out of the box (Docker, headless Chromium).

---

## API

| Method | Path             | Description                                                                 |
|--------|------------------|-----------------------------------------------------------------------------|
| `GET`  | `/health`        | Liveness probe.                                                             |
| `GET`  | `/docs`          | Swagger UI playground (the root `/` redirects here).                        |
| `GET`  | `/openapi.json`  | OpenAPI 3.0 spec.                                                           |
| `GET`  | `/api/products`  | Latest scrape output: `{product_name, date, number_of_times_purchased}[]`.  |
| `POST` | `/api/products`  | Start a scrape in a background thread. Body: `{"orders": <int>}` (default 10). |

A scrape takes 2–5 minutes. Poll `GET /api/products` until `status` flips from
`running` to results.

---

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in:

- `FLIPKART_USERNAME` — your Flipkart login email (same Gmail that receives the OTP)
- `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` — see **Gmail OAuth** below
- (optional) `SF_TOKEN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_API_ENDPOINT`

### Gmail OAuth — one-time setup

1. <https://console.cloud.google.com> → create a project → enable **Gmail API**.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   Application type must be **Desktop app** (not Web application).
3. Copy the Client ID + Secret into `.env`.
4. Run the auth check — a browser opens once for Google consent, then
   `token.json` is saved and all future runs are silent:
   ```powershell
   .venv\Scripts\python.exe test_gmail_auth.py
   ```

> Why **Desktop app**? Desktop OAuth clients allow `http://localhost:<any-port>`
> redirects automatically. Web application clients require every redirect URI
> to be registered, which breaks when the OAuth library picks a random port.

### Salesforce Connected App (optional)

If you want sync, create a Connected App with:

- **OAuth flow:** Client Credentials
- **Scopes:** `api`, `refresh_token`
- **Run-as user** with read/update access to `Grocery_Product__c`

Then fill the four `SF_*` env vars in `.env`. If any are missing, sync is
silently skipped and the scrape still completes.

---

## Running

### Start the web service

```powershell
$env:PORT="3000"; $env:HEADLESS="false"; .venv\Scripts\python.exe app.py
```

Open <http://localhost:3000/docs> for the interactive playground.

```powershell
# Trigger a scrape
Invoke-RestMethod -Method POST -Uri http://localhost:3000/api/products `
  -ContentType "application/json" -Body '{"orders": 10}'

# Poll for results
Invoke-RestMethod http://localhost:3000/api/products
```

### Run the scraper directly (no Flask)

```powershell
.venv\Scripts\python.exe scrape_flipkart_orders.py             # headed, 10 orders
.venv\Scripts\python.exe scrape_flipkart_orders.py --orders=5
.venv\Scripts\python.exe scrape_flipkart_orders.py --headed=false
```

### Re-sync the existing report to Salesforce (no re-scrape)

```powershell
.venv\Scripts\python.exe salesforce_sync.py
```

---

## Deploying to Render

The repo is Docker-based and ready for Render's "New Web Service → connect repo"
flow. `render.yaml` declares every env var the service expects.

### One-time steps

1. **Complete Gmail OAuth locally** so you have a valid `token.json`:
   ```powershell
   .venv\Scripts\python.exe test_gmail_auth.py
   ```
2. **Push to GitHub.**
3. **Render → New → Web Service → connect repo.** Render auto-detects
   `Dockerfile` and `render.yaml`.
4. **Set environment variables** in the Render dashboard
   (Service → Environment):

   | Variable               | Value                                                      |
   |------------------------|------------------------------------------------------------|
   | `FLIPKART_USERNAME`    | your Flipkart login email                                  |
   | `GMAIL_CLIENT_ID`      | from Google Cloud Console                                  |
   | `GMAIL_CLIENT_SECRET`  | from Google Cloud Console                                  |
   | `GMAIL_TOKEN_JSON`     | **paste the entire contents of your local `token.json`**   |
   | `FLIPKART_AUTH_STATE`  | leave blank for now (filled after first scrape — see below)|
   | `SF_TOKEN_URL`         | (optional) Salesforce OAuth token endpoint                 |
   | `SF_CLIENT_ID`         | (optional) Connected App consumer key                      |
   | `SF_CLIENT_SECRET`     | (optional) Connected App consumer secret                   |
   | `SF_API_ENDPOINT`      | (optional) `…/services/data/v57.0/sobjects/Grocery_Product__c/` |
   | `HEADLESS`             | `true` (already set in `render.yaml`)                      |

5. **Deploy.** Trigger one scrape via `POST /api/products`. After it finishes,
   Render logs print the new `auth_state.json` content. Copy that value into
   the `FLIPKART_AUTH_STATE` env var so future restarts skip the OTP login.

### How session persistence works on Render

Render's filesystem is ephemeral — `token.json` and `auth_state.json` are wiped
on every restart. `app.py` rehydrates them from `GMAIL_TOKEN_JSON` and
`FLIPKART_AUTH_STATE` on container startup, so the scraper finds them
exactly where it expects.

---

## Output shape

`orders_report.json`:

```json
{
  "scraped_at": "2026-05-24T19:27:53+05:30",
  "orders_scanned": 10,
  "products": [
    {
      "title": "Nandini Homogenised Cow Milk",
      "purchase_date": "2026-05-22",
      "purchase_count_in_last_10_orders": 3
    }
  ]
}
```

`purchase_count_in_last_10_orders` is a **per-product aggregate** across all
scraped orders, repeated on every row of the same title. Titles are matched
**exactly** — slight variations from Flipkart (e.g. `SUPERPLUM Mango Sindhura`
vs `SUPERPLUM Sindhura Mango`) are kept as separate products.

The `/api/products` endpoint reshapes this to
`{product_name, date, number_of_times_purchased}`.

---

## Notes & constraints

- **No write actions on Flipkart** — read-only browsing of order history.
- **No credential logging** — OTPs never reach stdout; the username is masked.
- **No new Salesforce records** — matches by `title__c` only; misses are logged
  and skipped.
- **Captchas are not bypassed** — if one appears, the scrape stops with a clear
  error.
- **Single tenant** — one Flipkart account per deployment.
- Never commit `.env`, `auth_state.json`, or `token.json`. Already in
  `.gitignore`.
