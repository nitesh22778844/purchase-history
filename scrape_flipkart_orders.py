"""
Flipkart order history scraper.
Login: email → Request OTP on Flipkart → fetch OTP via Gmail API → complete login.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Selectors — update here when Flipkart changes its markup
# ---------------------------------------------------------------------------
SELECTORS = {
    # Flipkart: landing page modal
    "modal_close": "button[class*='close'], [class*='_2KpZ6l'], [class*='close-button']",
    # Flipkart: login form input.
    # The login input has NO placeholder — the visible "Enter Email/Mobile
    # number" is a separate <label> element. We identify it by:
    #  • The hashed class names Flipkart currently uses (most specific)
    #  • As a fallback, any visible text input that is NOT the search bar
    #    (search bar has name='q')
    "username_input": (
        "input.xkp9Hl, "
        "input.ZvCKfk, "
        "input[type='text']:not([name='q']):not([type='hidden'])"
    ),
    "request_otp_button": "button:has-text('Request OTP'), button:has-text('Continue'), button:has-text('GET OTP')",
    # OTP entry — single field variant
    "otp_input_single": (
        "input[placeholder*='OTP'], input[placeholder*='otp'], "
        "input[placeholder*='one time'], input[class*='otp'], "
        "input[type='number'][maxlength='6']"
    ),
    # OTP entry — 6 individual digit-box variant
    "otp_digit_inputs": "input[maxlength='1']",
    "otp_verify_button": (
        "button:has-text('Verify'), button:has-text('Submit'), "
        "button:has-text('Confirm'), button[type='submit']"
    ),
    "logged_in_indicator": (
        "a:has-text('My Account'), span:has-text('Account'), "
        "[class*='account'][href*='wishlist'], [class*='profileIcon']"
    ),
    # Flipkart: orders page — outer wrapper used by BOTH regular orders and
    # Flipkart Minutes Basket cards. Regular orders contain /order_details
    # anchors directly; Minutes Basket cards have no anchors and must be
    # clicked to open their detail page (URL includes &grocery=true).
    # TODO: replace hashed class if Flipkart changes it
    "order_card": "div.ZcgLRi",
    # Product-page link on an order_details page — `/p/itm…` is Flipkart's
    # canonical product URL shape.
    "product_link_on_order_detail": "a[href*='/p/itm']",
    # On the product page itself — prefer attribute/text matches; hashed
    # classes are a last-resort fallback.
    # TODO: replace hashed class if Flipkart changes it
    "product_price": "div.Nx9bqj, div._30jeq3, div[class*='price']",
    "product_image": "img[src*='rukmini'], img[src*='flixcart']",
    # ---- Flipkart Minutes "add to cart" feature (see flipkart_minutes_cart.py) ----
    # Minutes results are React-Native-Web rendered (hashed css-* classes, no
    # stable ids). Verified shape: each search result is an `a[href*="/p/itm"]`
    # whose visible text is the product title and whose href is the product page.
    # Search is reached by URL (…/search?q=<q>&marketplace=HYPERLOCAL); see
    # MINUTES_SEARCH_URL. We add from each product's DETAIL page, because the
    # in-grid "Add" buttons sit inside horizontal carousels whose RN-web scroll
    # views swallow the click — the detail-page button is reliable.
    "minutes_product_anchor": "a[href*='/p/itm']",
    # The detail-page "Add to Cart" control (a <div>, matched on its own text).
    "detail_add_to_cart_text": r"^(add to cart|add item)$",
}

# ---------------------------------------------------------------------------
# Gmail API config
# ---------------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

load_dotenv()
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))

AUTH_STATE_FILE = Path("auth_state.json")
ORDERS_REPORT_FILE = Path("orders_report.json")
FLIPKART_HOME = "https://www.flipkart.com"
FLIPKART_ORDERS = "https://www.flipkart.com/account/orders"


def default_orders_to_scrape() -> int:
    """Default number of orders to scrape. Reads ORDERS_TO_SCRAPE from the
    environment (.env) and falls back to 10 when unset or invalid."""
    raw = (os.getenv("ORDERS_TO_SCRAPE") or "").strip()
    if not raw:
        return 10
    try:
        n = int(raw)
        return n if n > 0 else 10
    except ValueError:
        print(f"[config] ORDERS_TO_SCRAPE={raw!r} is not a valid integer; using 10.")
        return 10


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def mask(value: str) -> str:
    return f"***{value[-4:]}" if value and len(value) > 4 else "****"


def _today_iso() -> str:
    """Local-date ISO string (YYYY-MM-DD) for 'Delivered Today' orders."""
    return datetime.now(tz=timezone.utc).astimezone().date().isoformat()


def parse_date(raw: str) -> str:
    if not raw:
        return "unknown"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
    cleaned = re.sub(r"'(\d{2})\b", lambda m: f"20{m.group(1)}", cleaned)
    # Flipkart anchor text shows "Delivered on Apr 06" — no year. If dateutil
    # has to fall back to its default (current year), the date can land in the
    # future for orders that actually happened last year. Detect a missing
    # year ourselves and roll back 12 months when needed.
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", cleaned))
    try:
        parsed = dateutil_parser.parse(cleaned, fuzzy=True).date()
        if not has_year and parsed > datetime.now(tz=timezone.utc).date():
            parsed = parsed.replace(year=parsed.year - 1)
        return parsed.isoformat()
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if m:
            return m.group(0)
        m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", raw)
        if m:
            d, mo, y = m.groups()
            y = f"20{y}" if len(y) == 2 else y
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return "unknown"


# ---------------------------------------------------------------------------
# Gmail API — authentication and OTP retrieval
# ---------------------------------------------------------------------------

def _is_server_environment() -> bool:
    """Detect whether we're running on a headless server (Render, Docker, etc.)."""
    return os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")


def get_gmail_service(login_hint: str = ""):
    """
    Return an authenticated Gmail API service object.

    Auth resolution order:
      1. Load token.json if present → if expired, refresh via refresh_token
      2. If no valid token and we're NOT on a server, run interactive OAuth flow
      3. If no valid token and we ARE on a server, fail with a clear error
         (servers cannot open browsers; the GMAIL_TOKEN_JSON env var must be set)
    """
    creds = None

    # Step 1: try loading from token.json
    if GMAIL_TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
            print(f"[gmail] Loaded credentials from {GMAIL_TOKEN_FILE.name}.")
        except Exception as exc:
            print(f"[gmail] Could not parse {GMAIL_TOKEN_FILE.name}: {exc}")
            creds = None
    else:
        print(f"[gmail] {GMAIL_TOKEN_FILE.name} not found.")

    # Step 2: refresh expired access token if we have a refresh_token
    if creds and creds.expired and creds.refresh_token:
        try:
            print("[gmail] Access token expired — refreshing…")
            creds.refresh(Request())
            GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            print("[gmail] Token refreshed and saved.")
        except Exception as exc:
            print(f"[gmail] Token refresh failed: {exc}")
            creds = None

    # Done — we have valid creds
    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds)

    # Step 3: no valid creds. On a server we cannot open a browser, so bail out clearly.
    if _is_server_environment():
        print(
            "\n[error] Gmail credentials missing or invalid, and the server cannot open a browser.\n"
            "  This usually means the GMAIL_TOKEN_JSON environment variable is not set or is malformed.\n\n"
            "  How to fix:\n"
            "    1. On your local machine, run:  python test_gmail_auth.py\n"
            "       (This produces a valid token.json after one-time browser consent.)\n"
            "    2. Open token.json and copy its ENTIRE contents (including the { } braces).\n"
            "    3. On Render → Service → Environment → set GMAIL_TOKEN_JSON to that value.\n"
            "    4. Restart / redeploy the service.\n"
        )
        raise RuntimeError(
            "Gmail OAuth cannot run on a headless server. "
            "Set the GMAIL_TOKEN_JSON env var with a valid token.json content."
        )

    # Local mode — run interactive OAuth flow
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "\n[error] GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env\n\n"
            "  How to get them:\n"
            "  1. https://console.cloud.google.com → select your project\n"
            "  2. APIs & Services → Credentials\n"
            "  3. Find your OAuth 2.0 Client ID → click the pencil/edit icon\n"
            "  4. Copy 'Client ID' and 'Client secret' into .env\n"
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    print(
        f"[gmail] Opening browser for Gmail OAuth consent (one-time setup)…\n"
        f"        Account: {login_hint or '(not specified)'}"
    )
    flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, login_hint=login_hint or None)
    GMAIL_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print("[gmail] OAuth token saved to token.json.")
    return build("gmail", "v1", credentials=creds)


def _decode_gmail_body(message: dict) -> str:
    """Extract plain-text (or de-tagged HTML) body from a Gmail API message."""
    payload = message.get("payload", {})

    def b64decode(data: str) -> str:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore") if data else ""

    def walk_parts(parts) -> tuple[str, str]:
        plain = html = ""
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and data:
                plain = plain or b64decode(data)
            elif mime == "text/html" and data:
                html = html or re.sub(r"<[^>]+>", " ", b64decode(data))
            if part.get("parts"):
                p2, h2 = walk_parts(part["parts"])
                plain = plain or p2
                html = html or h2
        return plain, html

    parts = payload.get("parts", [])
    if parts:
        plain, html = walk_parts(parts)
        return plain or html

    # Non-multipart message
    return b64decode(payload.get("body", {}).get("data", ""))


def _extract_otp_from_message(msg: dict) -> str | None:
    """
    Return the 6-digit OTP from a Gmail message.
    Flipkart puts the OTP in the SUBJECT (e.g. "215567 is your verification code"),
    so we check subject first and only fall back to body.
    """
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")

    m = re.search(r"\b(\d{6})\b", subject)
    if m:
        return m.group(1)

    body = _decode_gmail_body(msg)
    m = re.search(r"\b(\d{6})\b", body)
    if m:
        return m.group(1)

    return None


def fetch_otp_via_gmail_api(
    service, after_timestamp: int, max_wait_secs: int = 180
) -> str | None:
    """
    Synchronous function (runs in a thread).
    Polls Gmail every 6 s for a Flipkart verification email that arrived after
    `after_timestamp` (Unix seconds); returns the 6-digit OTP from subject or body.
    """
    interval = 6
    attempts = max_wait_secs // interval
    # Match Flipkart's actual sender domain (noreply@rmo.flipkart.com) and subject
    query = (
        f"(from:flipkart OR subject:verification OR subject:code) "
        f"after:{after_timestamp}"
    )

    print(f"[gmail] Polling Gmail API for Flipkart OTP (up to {max_wait_secs}s)…")

    for attempt in range(attempts):
        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=5)
                .execute()
            )
            messages = result.get("messages", [])

            for stub in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=stub["id"], format="full")
                    .execute()
                )
                otp = _extract_otp_from_message(msg)
                if otp:
                    print("[gmail] 6-digit OTP extracted from Flipkart email.")
                    return otp

            remaining = max_wait_secs - (attempt + 1) * interval
            if messages:
                print(f"[gmail] Flipkart email found but no OTP yet ({remaining}s remaining)…")
            else:
                print(f"[gmail] No Flipkart OTP email yet ({remaining}s remaining)…")

        except HttpError as exc:
            print(f"[gmail] API error on attempt {attempt + 1}: {exc}")

        time.sleep(interval)

    return None


# ---------------------------------------------------------------------------
# Flipkart: auth helpers
# ---------------------------------------------------------------------------

async def dismiss_modal(page) -> None:
    try:
        close = page.locator(SELECTORS["modal_close"]).first
        await close.wait_for(state="visible", timeout=4_000)
        await close.click()
        print("[nav] Dismissed login modal.")
    except PlaywrightTimeoutError:
        pass


async def is_logged_in(page) -> bool:
    try:
        await page.wait_for_selector(SELECTORS["logged_in_indicator"], timeout=4_000)
        return True
    except PlaywrightTimeoutError:
        return False


async def enter_otp_on_flipkart(page, otp: str) -> None:
    """Fill OTP into Flipkart's input — handles single field and 6-digit-box layouts."""
    # Single input
    single = page.locator(SELECTORS["otp_input_single"]).first
    try:
        await single.wait_for(state="visible", timeout=5_000)
        await single.fill(otp)
        print("[auth] OTP entered (single input).")
        return
    except PlaywrightTimeoutError:
        pass

    # 6 separate digit inputs
    digit_inputs = page.locator(SELECTORS["otp_digit_inputs"])
    if await digit_inputs.count() >= 6:
        for i, digit in enumerate(otp):
            await digit_inputs.nth(i).fill(digit)
        print("[auth] OTP entered (digit-box inputs).")
        return

    print("[error] Could not locate OTP input on Flipkart page.")
    print(f"  URL: {page.url}")
    sys.exit(1)


async def save_auth(context) -> None:
    storage = await context.storage_state()
    AUTH_STATE_FILE.write_text(json.dumps(storage, indent=2), encoding="utf-8")
    print("[auth] Flipkart session saved to auth_state.json.")


async def _open_login_form(page) -> bool:
    """
    Make sure the Flipkart login input is visible.
    Tries: use open modal → dismiss blocking overlay → click Login button.
    Returns True when the input is visible.
    """
    input_loc = page.locator(SELECTORS["username_input"]).first

    if await input_loc.is_visible():
        return True

    # Dismiss any overlay that isn't the login form itself
    await dismiss_modal(page)
    await page.wait_for_timeout(800)
    if await input_loc.is_visible():
        return True

    # Try every plausible Login trigger
    for sel in [
        "a:has-text('Login')",
        "button:has-text('Login')",
        "li:has-text('Login') a",
        "a[href*='login']",
        "[class*='login']",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(2_000)
                if await input_loc.is_visible():
                    return True
        except Exception:
            continue

    # Last chance: wait up to 10 s for any matching input
    try:
        await input_loc.wait_for(state="visible", timeout=10_000)
        return True
    except PlaywrightTimeoutError:
        return False


async def login(page, flipkart_email: str, gmail_service) -> None:
    """
    Flipkart OTP login:
      1. Try going to /account/orders directly. If we land there, we're logged in.
      2. Otherwise, run the OTP flow: open login form → enter email → request OTP
         → fetch OTP via Gmail API → enter OTP → verify.
    """
    print(f"[auth] Logging in to Flipkart as …{mask(flipkart_email)}")

    # Probe: try going straight to the orders page.
    # If the session cookie from auth_state.json is valid, Flipkart serves it.
    # If not, it redirects to a login page.
    await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1_500)

    if "/account/orders" in page.url and "login" not in page.url.lower():
        print("[auth] Already logged in via saved session.")
        return

    # Not logged in — go to home and run OTP flow
    await page.goto(FLIPKART_HOME, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1_500)

    if not await _open_login_form(page):
        screenshot_path = Path("login_debug.png")
        await page.screenshot(path=str(screenshot_path))
        # Dump every input on the page so we can fix the selector precisely
        inputs_info = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(i => ({
                type: i.type, name: i.name, placeholder: i.placeholder,
                id: i.id, autocomplete: i.autocomplete,
                cls: (i.className || '').slice(0, 60),
                visible: !!(i.offsetParent || i.getClientRects().length),
            }))
        """)
        print(
            f"[error] Could not locate Flipkart login input after all attempts.\n"
            f"  URL        : {page.url}\n"
            f"  Screenshot : {screenshot_path.resolve()}\n"
            f"  Inputs on page:"
        )
        for i, info in enumerate(inputs_info):
            print(f"    #{i}: {info}")
        sys.exit(1)

    # Enter email
    username_input = page.locator(SELECTORS["username_input"]).first
    await username_input.fill(flipkart_email)
    print("[auth] Email entered.")

    # Timestamp just before OTP request — filters out any older Flipkart emails
    otp_request_at = int(datetime.now(tz=timezone.utc).timestamp()) - 30

    # Click "Request OTP"
    otp_btn = page.locator(SELECTORS["request_otp_button"]).first
    await otp_btn.wait_for(state="visible", timeout=8_000)
    await otp_btn.click()
    await page.wait_for_timeout(3_000)
    print("[auth] OTP requested from Flipkart.")

    # Diagnostic: capture state right after the OTP click so we can tell
    # whether Flipkart actually sent the OTP, or showed a captcha / block /
    # silent error. On Render the screenshot file is ephemeral but visible
    # text + URL show up in the logs, which is what we'll use to diagnose.
    try:
        screenshot_path = Path("otp_request_debug.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        body_text = (await page.locator("body").inner_text())[:2000]
        page_html_len = len(await page.content())
        print(
            f"[diag] Post-OTP-click page state:\n"
            f"  URL          : {page.url}\n"
            f"  HTML length  : {page_html_len}\n"
            f"  Screenshot   : {screenshot_path.resolve()}\n"
            f"  Visible text : {body_text!r}"
        )
        lowered = body_text.lower()
        block_terms = [
            "captcha", "verify you are human", "are you human",
            "too many", "try again later", "blocked", "unusual activity",
            "robot", "rate limit", "suspicious", "unable to send",
            "could not send", "failed to send",
        ]
        hits = [t for t in block_terms if t in lowered]
        if hits:
            print(f"[diag] WARNING: Flipkart page contains block/captcha indicators: {hits}")
    except Exception as exc:
        print(f"[diag] Could not capture post-OTP diagnostics: {exc}")

    # Fetch OTP via Gmail API in a background thread
    otp = await asyncio.to_thread(
        fetch_otp_via_gmail_api, gmail_service, otp_request_at
    )

    if not otp:
        print(
            "\n[error] Could not retrieve OTP from Gmail.\n"
            "  • Check that the Flipkart OTP email landed in your inbox (not spam).\n"
            "  • Delete token.json and re-run to redo OAuth consent if needed.\n"
        )
        sys.exit(1)

    # Enter OTP on Flipkart
    await enter_otp_on_flipkart(page, otp)
    await page.wait_for_timeout(500)

    # Submit
    verify_btn = page.locator(SELECTORS["otp_verify_button"]).first
    await verify_btn.click()

    try:
        await page.wait_for_url(re.compile(r"flipkart\.com(?!/login)"), timeout=30_000)
    except PlaywrightTimeoutError:
        screenshot_path = Path("otp_debug.png")
        await page.screenshot(path=str(screenshot_path))
        print(
            f"[error] Login did not complete after OTP.\n"
            f"  Screenshot : {screenshot_path.resolve()}"
        )
        sys.exit(1)

    await page.wait_for_load_state("networkidle")
    print("[auth] Flipkart login successful.")


# ---------------------------------------------------------------------------
# Order scraping
# ---------------------------------------------------------------------------

async def scroll_until_n_orders(page, n: int) -> list:
    prev_count = 0
    stall_attempts = 0
    while True:
        cards = await page.query_selector_all(SELECTORS["order_card"])
        count = len(cards)
        print(f"[orders] Found {count} order card(s) so far…")
        if count >= n:
            break
        if count == prev_count:
            stall_attempts += 1
            if stall_attempts >= 2:
                print(f"[orders] No new cards after 2 scroll attempts; stopping with {count}.")
                break
        else:
            stall_attempts = 0
        prev_count = count
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        await page.wait_for_timeout(1_800)
    return await page.query_selector_all(SELECTORS["order_card"])


def _clean_product_title(raw: str) -> str:
    """Strip 'X shared this order with you.' prefix and truncate before price/color/etc."""
    title = re.sub(r"^[^.]+? shared this order with you\.\s*", "", raw, flags=re.I)
    # Cut at first occurrence of any of: Color:, Size:, ₹, +<digits>, Delivered, Refund, Return, Cancelled
    title = re.split(
        r"\s*(?:Color:|Size:|₹|\+\d+|Delivered\s+(?:on|Today)|Refund|Return\s|Cancelled|Your\s+item)",
        title, maxsplit=1, flags=re.I,
    )[0]
    title = re.sub(r"\s+", " ", title).strip()
    # Drop trailing ellipsis/punctuation
    title = title.rstrip("…. ")
    return title


def _extract_date_from_text(text: str) -> str:
    """Find a 'Delivered/Ordered/Shipped on <Date>' inside text → ISO YYYY-MM-DD.
    The year is optional — Flipkart anchor text often shows just 'Apr 06'."""
    # Same-day orders render as "Delivered Today" (no explicit date) — map to today.
    if re.search(r"Delivered\s+Today\b", text, re.I):
        return _today_iso()
    m = re.search(
        r"(?:Delivered|Ordered|Shipped|Cancelled|Return\s+completed|Refund\s+completed)"
        r"\s+on\s+([A-Za-z]{3,9}\s+\d{1,2}(?:,?\s+\d{2,4})?)",
        text, re.I,
    )
    if m:
        return parse_date(m.group(1))
    return "unknown"


def _clean_minutes_product_title(raw: str) -> str:
    """Strip price + status suffix from Minutes Basket modal item text.
    e.g. 'Nandini Curd Plain Curd ₹26.0 Return policy ended' → 'Nandini Curd Plain Curd'."""
    # Cut at first ₹ price
    t = re.split(r"\s*₹", raw, maxsplit=1)[0]
    # Strip trailing status keywords that may appear before the price
    t = re.sub(
        r"\s+(Return|Cancel|Refund|Replace|Rate\s+&\s+Review).*$", "",
        t, flags=re.I,
    )
    return re.sub(r"\s+", " ", t).strip()


_UNAVAILABLE_TERMS = (
    "sold out",
    "out of stock",
    "currently unavailable",
    "coming soon",
)


def _unavailable_fields() -> dict:
    return {
        "current_price": None,
        "product_url": None,
        "image_url": None,
        "availability": "Unavailable",
    }


async def _extract_current_price(page) -> float | None:
    """Pick the most prominent ₹ price from the product page."""
    for sel in (
        "div.Nx9bqj.CxhGGd",            # current selling price (hashed)
        SELECTORS["product_price"],     # broader fallback
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            text = await loc.inner_text(timeout=2_000)
            m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text)
            if m:
                return float(m.group(1).replace(",", ""))
        except Exception:
            continue
    try:
        body = await page.locator("body").inner_text()
        m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", body)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


async def _extract_main_image(page) -> str | None:
    """Return the largest Flipkart CDN image — usually the product hero shot."""
    try:
        return await page.evaluate("""
            () => {
              const imgs = Array.from(document.querySelectorAll(
                'img[src*="rukmini"], img[src*="flixcart"]'
              )).filter(img => img.src && !img.src.includes('promos') && !img.src.includes('logos'));
              if (!imgs.length) return null;
              imgs.sort((a, b) => (b.naturalWidth || 0) - (a.naturalWidth || 0));
              return imgs[0].src;
            }
        """)
    except Exception:
        return None


async def _extract_availability(page) -> str:
    """Two-state: 'Unavailable' if any sold-out marker is visible, else 'Available'."""
    try:
        body = (await page.locator("body").inner_text() or "").lower()
        for term in _UNAVAILABLE_TERMS:
            if term in body:
                return "Unavailable"
        return "Available"
    except Exception:
        return "Unavailable"


def _flipkart_pincode() -> str:
    """Pincode used only as a last-resort fallback when no saved address can
    be selected. Reads FLIPKART_PINCODE from .env; defaults to 560094."""
    return (os.getenv("FLIPKART_PINCODE") or "560094").strip()


def _flipkart_saved_address_prefix() -> str:
    """Text prefix that identifies the saved Flipkart address to prefer
    whenever a product page asks for a delivery location. Matched against
    the visible text of each saved-address card. Reads
    FLIPKART_SAVED_ADDRESS_PREFIX from .env; defaults to '82, flat No 6'."""
    return (os.getenv("FLIPKART_SAVED_ADDRESS_PREFIX") or "82, flat No 6").strip()


_PINCODE_PROMPT_SELECTORS = (
    "input[placeholder*='pincode' i]",
    "input[placeholder*='pin code' i]",
    "input[placeholder*='delivery pincode' i]",
    "input[placeholder*='enter pincode' i]",
    "input[name*='pincode' i]",
    "input[id*='pincode' i]",
)


async def _find_visible_pincode_input(page):
    """Return the first visible pincode input on the page, or None."""
    for sel in _PINCODE_PROMPT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            return loc
        except Exception:
            continue
    return None


async def _confirm_address_selection(page) -> None:
    """After clicking a saved-address card, click any 'Deliver here' /
    'Confirm' button that appears, then wait for the page to settle."""
    for sel in (
        "button:has-text('Deliver Here')",
        "button:has-text('Deliver here')",
        "button:has-text('Deliver to this address')",
        "button:has-text('Confirm')",
        "button:has-text('Apply')",
    ):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                break
        except Exception:
            continue
    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(600)


async def _click_saved_address_card(page, prefix: str) -> bool:
    """Find a visible element whose text starts with `prefix` and click it.
    Returns True iff something was clicked."""
    # Use the first ~15 chars so partial address differences ("flat No 6"
    # vs "Flat No.6", trailing comma vs no comma) still match. The prefix
    # is unique enough on its own that prefix-only matching is safe.
    snippet = prefix[:15]
    pattern = re.compile(re.escape(snippet), re.I)
    candidate = page.get_by_text(pattern).first
    try:
        if await candidate.count() == 0:
            return False
        try:
            await candidate.scroll_into_view_if_needed()
        except Exception:
            pass
        if not await candidate.is_visible():
            return False
        await candidate.click()
        await page.wait_for_timeout(500)
        await _confirm_address_selection(page)
        return True
    except Exception:
        return False


async def _select_saved_address_if_prompted(
    page, address_prefix: str | None = None
) -> bool:
    """If the current page shows a pincode / delivery-address prompt, click
    the saved address whose visible text starts with `address_prefix` rather
    than typing a pincode. Returns True iff a saved address was selected.

    Saved addresses are only available while logged in. Flipkart surfaces
    them in two layouts:
      1. Inline on the delivery-check widget — each saved address is its
         own clickable card.
      2. Behind a 'Saved Addresses' / 'Choose another address' link that
         opens a panel listing all addresses.
    This helper tries pass (1) first, then pass (2)."""
    prefix = (address_prefix or _flipkart_saved_address_prefix()).strip()
    if not prefix:
        return False

    if await _find_visible_pincode_input(page) is None:
        return False

    print(f"  [address] looking for saved address starting with {prefix[:15]!r}…")

    # Pass 1: address card may already be visible alongside the pincode input.
    if await _click_saved_address_card(page, prefix):
        print(f"  [address] selected saved address starting with {prefix[:15]!r}.")
        return True

    # Pass 2: open a 'Saved Addresses' / 'Choose another address' panel first.
    for trigger_sel in (
        "a:has-text('Saved Addresses')",
        "button:has-text('Saved Addresses')",
        "a:has-text('saved address')",
        "button:has-text('saved address')",
        "a:has-text('Choose another address')",
        "button:has-text('Choose another address')",
        "a:has-text('Choose Address')",
        "button:has-text('Choose Address')",
        "a:has-text('Select Address')",
        "button:has-text('Select Address')",
        "a:has-text('Change Address')",
        "button:has-text('Change Address')",
        "a:has-text('View other addresses')",
    ):
        try:
            trigger = page.locator(trigger_sel).first
            if await trigger.count() == 0 or not await trigger.is_visible():
                continue
            await trigger.click()
            await page.wait_for_timeout(800)
            if await _click_saved_address_card(page, prefix):
                print(f"  [address] selected saved address starting with {prefix[:15]!r}.")
                return True
        except Exception:
            continue

    return False


async def _set_pincode_if_prompted(page, pincode: str | None = None) -> bool:
    """Fallback for when no saved address can be picked. Fills the pincode
    input and submits. Returns True iff a pincode was entered."""
    pin = (pincode or _flipkart_pincode()).strip()
    if not pin:
        return False

    pincode_input = await _find_visible_pincode_input(page)
    if pincode_input is None:
        return False

    try:
        print(f"  [pincode] saved address unavailable — falling back to pincode {pin}")
        await pincode_input.scroll_into_view_if_needed()
        await pincode_input.fill(pin)
        await page.wait_for_timeout(400)

        # Try common submit buttons near the input; fall back to pressing Enter.
        clicked = False
        for sel in (
            "button:has-text('Check')",
            "button:has-text('Apply')",
            "button:has-text('Submit')",
            "button:has-text('Continue')",
            "button:has-text('Confirm')",
            "button[type='submit']",
        ):
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            await pincode_input.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(800)
        return True
    except Exception as exc:
        print(f"  [pincode] entry failed: {exc}")
        return False


async def _set_delivery_location_if_prompted(page) -> bool:
    """Resolve a Flipkart delivery-location prompt. Prefers selecting the
    configured saved address; falls back to typing FLIPKART_PINCODE only if
    the saved address cannot be located. Returns True iff either path
    handled the prompt."""
    if await _select_saved_address_if_prompted(page):
        return True
    return await _set_pincode_if_prompted(page)


def _unwrap_preview_url(url: str) -> str:
    """If `url` is a Flipkart hyperlocal-preview-page wrapper, return the
    decoded `originalUrl` (the canonical /<slug>/p/itm... URL). Otherwise
    return `url` unchanged. Used to convert basket-tile anchor hrefs into
    real product URLs without performing any navigation."""
    if not url or "hyperlocal-preview-page" not in url:
        return url or ""
    from urllib.parse import urlparse, parse_qs, unquote
    parsed = urlparse(url)
    original = unquote((parse_qs(parsed.query).get("originalUrl") or [""])[0])
    if not original:
        return url
    return original if original.startswith("http") else f"https://www.flipkart.com{original}"


async def _unwrap_hyperlocal_preview(page) -> None:
    """Flipkart routes Minutes product clicks through a 'hyperlocal-preview-page'
    interstitial that doesn't render a price. The canonical product URL is
    URL-encoded inside its `originalUrl` query param — follow it."""
    if "hyperlocal-preview-page" not in page.url:
        return
    from urllib.parse import urlparse, parse_qs, unquote
    parsed = urlparse(page.url)
    original = unquote((parse_qs(parsed.query).get("originalUrl") or [""])[0])
    if not original:
        return
    target = original if original.startswith("http") else f"https://www.flipkart.com{original}"
    try:
        await page.goto(target, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(600)
    except Exception as exc:
        print(f"  [unwrap] hyperlocal preview follow failed: {exc}")


async def extract_product_details(page) -> dict:
    """Capture price / image / url / availability from the currently-open product page."""
    # Some product pages defer their price/image render — small idle wait helps.
    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(600)

    # If we landed on the hyperlocal interstitial, resolve the delivery
    # prompt (saved address preferred, pincode fallback) — that often
    # navigates us straight to the real product page. If no prompt is
    # there, fall back to following originalUrl directly.
    if "hyperlocal-preview-page" in page.url:
        if await _set_delivery_location_if_prompted(page):
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(600)
        await _unwrap_hyperlocal_preview(page)

    # On the product page itself, the price + delivery availability are
    # sometimes gated by a per-product delivery check. Resolve it before extraction.
    if await _set_delivery_location_if_prompted(page):
        # Price/availability typically update in-place after the check.
        await page.wait_for_timeout(800)

    return {
        "current_price": await _extract_current_price(page),
        "product_url": page.url,
        "image_url": await _extract_main_image(page),
        "availability": await _extract_availability(page),
    }


async def _click_into_product_from_current_page(page) -> bool:
    """From an order_details (or Minutes detail) page, click into the product page.
    Tries multiple selectors and accepts either the canonical `/p/itm…` URL
    or Flipkart's `hyperlocal-preview-page` interstitial (the unwrapper
    resolves that later). Returns True if a navigation happened."""
    # Match the canonical path "<slug>/p/itm…" (path segment), OR the Minutes
    # interstitial. Anchored to flipkart.com so the encoded `/p/itm` sitting
    # inside an originalUrl query value does NOT match.
    landed_pattern = re.compile(
        r"flipkart\.com/[^?#]*?/p/itm|flipkart\.com/hyperlocal-preview-page"
    )

    for selector in (
        "a[href*='/p/itm']",                 # canonical product anchor
        "a[href*='hyperlocal-preview']",     # Minutes interstitial wrapper
        "a[href*='/p/'][href*='pid=']",      # alternate Flipkart product URL shape
    ):
        link = page.locator(selector).first
        try:
            # state="attached" so below-the-fold links are still found; we
            # scroll into view before clicking.
            await link.wait_for(state="attached", timeout=4_000)
        except PlaywrightTimeoutError:
            continue
        try:
            await link.scroll_into_view_if_needed()
            await link.click()
            await page.wait_for_url(landed_pattern, timeout=15_000)
            return True
        except Exception:
            continue
    return False


async def visit_regular_product(page, order_detail_url: str) -> dict:
    """For a regular (non-Minutes) order: navigate orders → order-detail → product page,
    extract per-product fields, then return to the orders list. Always returns a
    populated dict so callers can upsert with whatever we have."""
    if not order_detail_url:
        return _unavailable_fields()
    try:
        if not order_detail_url.startswith("http"):
            order_detail_url = FLIPKART_HOME.rstrip("/") + "/" + order_detail_url.lstrip("/")
        await page.goto(order_detail_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(700)

        if not await _click_into_product_from_current_page(page):
            return _unavailable_fields()

        return await extract_product_details(page)
    except Exception as exc:
        print(f"  [product] navigation failed: {exc}")
        return _unavailable_fields()


async def _return_to_basket_expanded(page, basket_url: str) -> None:
    """After visiting a product page, get back to the Minutes basket detail
    with the 'See all items' panel re-expanded. We try `go_back` first
    (cheap, sometimes preserves state); if that lands somewhere else or
    the expanded state is lost, fall back to a fresh `goto(basket_url)`
    and re-click 'See all items'."""
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=8_000)
        await page.wait_for_timeout(600)
    except Exception:
        pass

    if page.url != basket_url:
        try:
            await page.goto(basket_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(600)
        except Exception as exc:
            print(f"  [back] goto basket failed: {exc}")
            return

    # Re-trigger 'See all items' so subsequent tiles are clickable.
    try:
        see_all = page.get_by_text(re.compile(r"(see|view)\s+all\s+items", re.I)).first
        await see_all.wait_for(state="visible", timeout=4_000)
        await see_all.click()
        await page.wait_for_timeout(1_200)
    except PlaywrightTimeoutError:
        # Already expanded (no link present) or items rendered inline — fine.
        pass


async def scrape_minutes_basket(
    page, card, order_idx: int, total: int,
    details_cache: dict[str, dict] | None = None,
) -> list[dict]:
    """For a Flipkart Minutes Basket card: click into the detail page, expand
    'See all items', extract product titles + order date, then go back to the
    orders list.

    `details_cache` is a shared title→per-product-page-fields map. The same
    grocery item often appears across multiple Minutes baskets (weekly milk,
    curd, etc.) — caching the first successful page visit avoids re-clicking
    every basket. The cache is mutated in place."""
    cache: dict[str, dict] = details_cache if details_cache is not None else {}
    card_text = (await card.inner_text()) or ""
    fallback_date = _extract_date_from_text(card_text)

    await card.scroll_into_view_if_needed()
    await card.click()

    try:
        await page.wait_for_url(re.compile(r"order_details.*grocery=true"), timeout=15_000)
    except PlaywrightTimeoutError:
        print(f"[order {order_idx}/{total}] Minutes detail page did not open; skipping.")
        return []

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)

    detail_text = (await page.locator("body").inner_text()) or ""
    m = re.search(
        r"Order\s+Date\s+([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{2,4})",
        detail_text, re.I,
    )
    order_date = parse_date(m.group(1)) if m else fallback_date

    try:
        see_all = page.get_by_text(re.compile(r"(see|view)\s+all\s+items", re.I)).first
        await see_all.wait_for(state="visible", timeout=8_000)
        await see_all.click()
        await page.wait_for_timeout(1_500)
        await page.wait_for_load_state("networkidle")
    except PlaywrightTimeoutError:
        print(f"[order {order_idx}/{total}] 'See all items' not visible — using visible items.")

    # Remember the basket URL so we can return here between product-page clicks.
    basket_url = page.url

    items = await page.evaluate(r"""
        () => {
          const imgs = Array.from(document.querySelectorAll(
            'img[src*="rukmini"]:not([src*="promos"]):not([src*="logos"]):not([src*="banner"])'
          ));
          const out = [];
          for (const img of imgs) {
            let cur = img;
            let href = null;
            for (let i = 0; i < 10 && cur; i++) {
              // Capture the nearest ancestor anchor href, if any. Minutes
              // tiles wrap their image+text in a single <a> pointing to the
              // hyperlocal-preview-page URL (originalUrl param holds the
              // canonical /p/itm... product URL).
              if (!href && cur.tagName === 'A' && cur.href) {
                href = cur.href;
              }
              const t = (cur.innerText || '').replace(/\s+/g,' ').trim();
              if (t.length > 5 && t.length < 400 && t.includes('₹')) {
                out.push({ text: t, src: img.src, href: href });
                break;
              }
              cur = cur.parentElement;
            }
          }
          return out;
        }
    """)

    # Deduplicate items within this basket. Capture price+href from the
    # basket page itself — Minutes SKUs route through a hyperlocal-preview
    # interstitial that redirects back to itself even after we follow
    # originalUrl, so the product page is not a reliable source of price.
    # The basket already shows the price next to every item.
    unique_items: list[dict] = []
    seen_titles: set[str] = set()
    for it in items:
        title = _clean_minutes_product_title(it["text"])
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        price_match = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", it["text"])
        try:
            basket_price = float(price_match.group(1).replace(",", "")) if price_match else None
        except (TypeError, ValueError):
            basket_price = None
        unique_items.append({
            "title": title,
            "basket_image": it.get("src"),
            "basket_price": basket_price,
            "basket_href": _unwrap_preview_url(it.get("href") or ""),
        })

    products: list[dict] = []
    for item_idx, it in enumerate(unique_items):
        title = it["title"]

        # Cross-basket optimization: reuse details captured from any prior basket.
        cached = cache.get(title)
        if cached is not None:
            print(f"  [cache] reusing details for {title[:50]}")
            products.append({
                "item_id": f"minutes::{order_idx}::{title.lower()}",
                "title": title,
                "date": order_date,
                "category": "Grocery",
                "order_detail_url": None,
                **cached,
            })
            continue

        # Defaults from the basket page text — used only when the tile click
        # below cannot reach a real product page (captcha, preview gate that
        # refuses to lift, network error). basket_price is the purchase price,
        # NOT the current price; we'll overwrite it whenever the product page
        # itself yields a value.
        details = {
            "current_price": it.get("basket_price"),
            "last_purchased_price": it.get("basket_price"),
            "product_url": it.get("basket_href") or None,
            "image_url": it.get("basket_image"),
            "availability": "Unavailable",
        }

        # Make sure we're on the basket detail with 'See all items' expanded
        # before locating the next tile. After the first product visit the
        # DOM has been replaced — we need a fresh basket render.
        if item_idx > 0:
            await _return_to_basket_expanded(page, basket_url)

        # Resolve the tile to click. Prefer the anchor that wraps THE EXACT
        # image we captured in JS (stable identifier); fall back to title text.
        print(f"\n  [tile {item_idx + 1}/{len(unique_items)}] {title[:55]}")
        tile = None
        img_src = it.get("basket_image") or ""
        if img_src:
            escaped = img_src.replace('"', '\\"')
            cand = page.locator(f'a:has(img[src="{escaped}"])').first
            if await cand.count() > 0:
                tile = cand
                print(f"  [tile] resolved by image anchor")
        if tile is None:
            tile = page.get_by_text(title, exact=False).first
            print(f"  [tile] resolved by text (fallback)")

        try:
            await tile.scroll_into_view_if_needed()
            url_before = page.url
            print(f"  [click] dispatching; url before = {url_before[:90]}")
            await tile.click()

            # Wait for the redirect chain to complete on /p/itm. If we never
            # get there (Minutes preview gate, captcha, etc.), give the page
            # a few more seconds and proceed with whatever URL we landed on.
            try:
                await page.wait_for_url(
                    re.compile(r"flipkart\.com/[^?#]*?/p/itm"),
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(3_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(700)

            landed = page.url
            print(f"  [settle] landed: {landed[:90]}")

            if landed == url_before:
                print(f"  [warn] click did not navigate; keeping basket fallback")
            else:
                # extract_product_details also unwraps any lingering preview URL.
                page_details = await extract_product_details(page)

                px = page_details.get("current_price")
                if px is not None:
                    details["current_price"] = px
                    print(f"  [price] ₹{px} (from product page)")
                else:
                    print(
                        f"  [price] product page yielded no price; "
                        f"keeping basket ₹{details['current_price']}"
                    )
                if page_details.get("image_url"):
                    details["image_url"] = page_details["image_url"]
                # product_url := wherever we actually ended up — the URL a
                # user can click to reach the page we just scraped.
                if page_details.get("product_url"):
                    details["product_url"] = page_details["product_url"]
                    print(f"  [url] {details['product_url'][:90]}")
                details["availability"] = page_details.get("availability", "Unavailable")
        except Exception as exc:
            print(f"  [error] click for {title[:40]} failed: {exc}")

        cache[title] = dict(details)
        products.append({
            "item_id": f"minutes::{order_idx}::{title.lower()}",
            "title": title,
            "date": order_date,
            "category": "Grocery",
            "order_detail_url": None,
            **details,
        })

    print(f"[order {order_idx}/{total}] Minutes Basket: {len(products)} product(s)")

    # Return to the orders list — prefer an explicit goto over go_back, which
    # can land on an intermediate page after our per-item navigations.
    try:
        await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
    except Exception:
        await page.go_back()
        await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1_500)

    return products


async def expand_and_get_products(page, card, order_idx: int, total: int) -> list[dict]:
    # Each product inside an order has its own /order_details anchor.
    anchors = await card.query_selector_all("a[href*='order_details']")
    products: list[dict] = []
    seen_item_ids: set[str] = set()

    for anchor in anchors:
        href = (await anchor.get_attribute("href")) or ""
        m = re.search(r"item_id=([A-Z0-9]+)", href)
        item_id = m.group(1) if m else href
        if item_id in seen_item_ids:
            continue
        seen_item_ids.add(item_id)

        raw_text = (await anchor.inner_text()).strip()
        title = _clean_product_title(raw_text)
        date = _extract_date_from_text(raw_text)

        price_match = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", raw_text)
        try:
            last_purchased_price = float(price_match.group(1).replace(",", "")) if price_match else None
        except (TypeError, ValueError):
            last_purchased_price = None

        if title:
            products.append({
                "item_id": item_id,
                "title": title,
                "date": date,
                "category": "Non-Grocery",
                "order_detail_url": href,
                "last_purchased_price": last_purchased_price,
            })

    # Refunded / cancelled sub-items have no date in their anchor text. Fall
    # back to any sibling's date in the same order card, then to the card text.
    fallback = next((p["date"] for p in products if p["date"] != "unknown"), "unknown")
    if fallback == "unknown":
        fallback = _extract_date_from_text((await card.inner_text()) or "")
    if fallback != "unknown":
        for p in products:
            if p["date"] == "unknown":
                p["date"] = fallback

    print(f"[order {order_idx}/{total}] {len(products)} product(s)")
    return products


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def launch_logged_in_context(pw, headless: bool, flipkart_email: str, gmail_service):
    """Launch Chromium, build a geolocated context (restoring auth_state.json if
    present), open a page, log in via OTP, and persist the refreshed session.

    Returns (browser, context, page). Shared by the order scraper (`run`) and the
    Minutes cart feature so both use one auth/browser path."""
    # Browser args: --no-sandbox / --disable-dev-shm-usage are required when
    # Chromium runs as root inside a Docker container (e.g. on Render).
    browser_args = []
    if headless:
        browser_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]

    browser = await pw.chromium.launch(
        headless=headless,
        # In headed mode, slow down so you can watch each click. Headless
        # stays at 0 for production speed.
        slow_mo=900 if not headless else 0,
        args=browser_args,
    )

    # Reuse the saved Flipkart session if auth_state.json exists.
    # On Render, this file is written from FLIPKART_AUTH_STATE env var at startup.
    storage_state = str(AUTH_STATE_FILE) if AUTH_STATE_FILE.exists() else None
    if storage_state:
        print(f"[auth] Restoring session from {AUTH_STATE_FILE}")

    # Grant geolocation so Flipkart Minutes recognizes a known location
    # and routes us through to the real product page instead of stranding
    # us on the hyperlocal-preview interstitial. Override via env if your
    # delivery area is elsewhere — defaults to Bengaluru.
    try:
        lat = float(os.getenv("FLIPKART_LAT") or 12.9716)
        lng = float(os.getenv("FLIPKART_LNG") or 77.5946)
    except ValueError:
        lat, lng = 12.9716, 77.5946

    context = await browser.new_context(
        storage_state=storage_state,
        viewport={"width": 1400, "height": 900},
        geolocation={"latitude": lat, "longitude": lng},
        permissions=["geolocation"],
    )
    page = await context.new_page()

    # ---- Login ----
    await login(page, flipkart_email, gmail_service)
    await save_auth(context)

    return browser, context, page


async def run(num_orders: int, headless: bool) -> None:
    load_dotenv()
    flipkart_email = os.getenv("FLIPKART_USERNAME", "")

    if not flipkart_email:
        print("[error] FLIPKART_USERNAME must be set in .env")
        sys.exit(1)

    # Authenticate with Gmail API before opening the browser
    print("[gmail] Authenticating with Gmail API…")
    gmail_service = get_gmail_service(login_hint=flipkart_email)
    print("[gmail] Gmail API ready.")

    async with async_playwright() as pw:
        browser, context, page = await launch_logged_in_context(
            pw, headless, flipkart_email, gmail_service
        )

        # ---- Navigate to orders ----
        print("[nav] Navigating to orders page…")
        await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        if "login" in page.url.lower():
            print("[auth] Redirected to login; retrying…")
            await login(page, flipkart_email, gmail_service)
            await page.goto(FLIPKART_ORDERS, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
            if "login" in page.url.lower():
                print("[error] Still on login page after retry. Exiting.")
                await context.close()
                sys.exit(1)

        # ---- Collect order cards ----
        cards = await scroll_until_n_orders(page, num_orders)
        cards = cards[:num_orders]
        actual_count = len(cards)
        print(f"[orders] Processing {actual_count} order(s).")

        if actual_count == 0:
            screenshot_path = Path("orders_debug.png")
            await page.screenshot(path=str(screenshot_path), full_page=True)
            # Walk up from product anchors to find the order card container
            diagnostic = await page.evaluate("""
                () => {
                  // Order detail links (e.g. /dl/products/...?...orderId=OD123...)
                  const orderAnchors = Array.from(document.querySelectorAll(
                    'a[href*="orderId"], a[href*="/order"], a[href*="/dl/"]'
                  )).slice(0, 5);

                  const anchorInfo = orderAnchors.map(a => {
                    const ancestors = [];
                    let el = a;
                    for (let i = 0; i < 6 && el; i++) {
                      ancestors.push({
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 80),
                      });
                      el = el.parentElement;
                    }
                    return { href: a.href.slice(0, 100), ancestors };
                  });

                  // Product thumbnails (Flipkart CDN)
                  const productImgs = Array.from(document.querySelectorAll(
                    'img[src*="rukmini"], img[src*="flixcart"]'
                  )).slice(0, 3);

                  const imgInfo = productImgs.map(img => {
                    const ancestors = [];
                    let el = img;
                    for (let i = 0; i < 6 && el; i++) {
                      ancestors.push({
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 80),
                      });
                      el = el.parentElement;
                    }
                    return { src: img.src.slice(0, 60), ancestors };
                  });

                  return { anchorInfo, imgInfo, anchorCount: orderAnchors.length };
                }
            """)
            print(
                f"[error] No order cards found.\n"
                f"  URL        : {page.url}\n"
                f"  Screenshot : {screenshot_path.resolve()}\n"
                f"  Order anchors on page: {diagnostic['anchorCount']}"
            )
            print(f"\n  === Order anchor ancestors (anchor -> parent -> grandparent -> ...) ===")
            for i, info in enumerate(diagnostic["anchorInfo"]):
                print(f"\n    Anchor #{i}: {info['href']}")
                for level, a in enumerate(info["ancestors"]):
                    print(f"      L{level}: {a['tag']:<6}  cls='{a['cls']}'")

            print(f"\n  === Product image ancestors ===")
            for i, info in enumerate(diagnostic["imgInfo"]):
                print(f"\n    Image #{i}: {info['src']}...")
                for level, a in enumerate(info["ancestors"]):
                    print(f"      L{level}: {a['tag']:<6}  cls='{a['cls']}'")

            await context.close()
            sys.exit(1)

        # ---- Extract products, deduped globally by item_id ----
        # Flipkart's orders page sometimes shows the same items in multiple cards
        # (e.g. "All items" summary panels). Dedupe so each purchase is counted once.
        # Re-query cards each iteration because Minutes Basket scraping navigates
        # away and back, which detaches any previously captured ElementHandles.
        all_products: list[dict] = []
        seen_item_ids_global: set[str] = set()
        # Shared per-run cache: title → product-page fields. Populated by the
        # first basket that successfully extracts a given product, reused by
        # later baskets so repeat groceries aren't re-clicked.
        minutes_details_cache: dict[str, dict] = {}
        for idx in range(1, actual_count + 1):
            try:
                # Re-scroll after every Minutes back-navigation, which often
                # leaves the orders page with only the top few cards rendered.
                fresh_cards = await page.query_selector_all(SELECTORS["order_card"])
                if idx - 1 >= len(fresh_cards):
                    await scroll_until_n_orders(page, idx)
                    fresh_cards = await page.query_selector_all(SELECTORS["order_card"])
                if idx - 1 >= len(fresh_cards):
                    print(f"[order {idx}/{actual_count}] Card index out of range after reload; stopping.")
                    break
                card = fresh_cards[idx - 1]
                card_text = (await card.inner_text()) or ""

                if re.search(r"minutes\s*basket", card_text, re.I):
                    products = await scrape_minutes_basket(
                        page, card, idx, actual_count,
                        details_cache=minutes_details_cache,
                    )
                else:
                    products = await expand_and_get_products(page, card, idx, actual_count)

                for p in products:
                    if p["item_id"] in seen_item_ids_global:
                        continue
                    seen_item_ids_global.add(p["item_id"])
                    all_products.append(p)
            except Exception as exc:
                print(f"[order {idx}/{actual_count}] Error: {exc}")

        # ---- Aggregate by title BEFORE closing the context ----
        # Each unique title is visited once on its product page (regular orders
        # only — Minutes products already populated their per-product fields
        # inline during basket scraping).
        title_counts: Counter[str] = Counter(p["title"] for p in all_products)

        unique_by_title: dict[str, dict] = {}
        for p in all_products:
            t = p["title"]
            cur = unique_by_title.get(t)
            if cur is None:
                unique_by_title[t] = dict(p)
                continue
            # Newer date wins.
            if p.get("date") and p["date"] != "unknown" and (
                not cur.get("date") or cur["date"] == "unknown" or p["date"] > cur["date"]
            ):
                cur["date"] = p["date"]
                if "last_purchased_price" in p:
                    cur["last_purchased_price"] = p["last_purchased_price"]
            # If we don't yet have an order_detail_url, take this one.
            if not cur.get("order_detail_url") and p.get("order_detail_url"):
                cur["order_detail_url"] = p["order_detail_url"]
            # Prefer already-populated per-product fields from Minutes runs.
            for k in ("current_price", "last_purchased_price", "product_url", "image_url"):
                if not cur.get(k) and p.get(k):
                    cur[k] = p[k]
            if cur.get("availability") == "Unavailable" and p.get("availability") == "Available":
                cur["availability"] = "Available"

        # Per-product page visit for any title that still lacks page details
        # (i.e. regular-order products — Minutes are already filled in).
        regular_titles = [
            t for t, p in unique_by_title.items()
            if p.get("category") == "Non-Grocery" and p.get("product_url") is None
        ]
        print(f"\n[products] Visiting {len(regular_titles)} unique product page(s)…")
        for i, title in enumerate(regular_titles, 1):
            entry = unique_by_title[title]
            print(f"  [{i}/{len(regular_titles)}] {title[:70]}")
            details = await visit_regular_product(page, entry.get("order_detail_url"))
            entry.update(details)

        await context.close()

    # ---- Build report ----
    scraped_at = datetime.now(tz=timezone.utc).astimezone().isoformat()
    report_products = []
    for title, p in unique_by_title.items():
        date = p.get("date")
        report_products.append({
            "title": title,
            "last_ordered_date": None if not date or date == "unknown" else date,
            "number_of_times_purchased": title_counts[title],
            "current_price": p.get("current_price"),
            "last_purchased_price": p.get("last_purchased_price"),
            "product_url": p.get("product_url"),
            "image_url": p.get("image_url"),
            "category": p.get("category"),
            "availability": p.get("availability") or "Unavailable",
            "source": "Flipkart",
            "scraped_at": scraped_at,
        })

    report = {
        "scraped_at": scraped_at,
        "orders_scanned": actual_count,
        "products": report_products,
    }
    ORDERS_REPORT_FILE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[done] Report written to {ORDERS_REPORT_FILE}")

    print(
        f"\n{'#':<4}  {'Product Title':<50}  {'Date':<12}  {'Cnt':<4}  "
        f"{'Price':<8}  {'Cat':<12}  {'Avail'}"
    )
    print("-" * 110)
    for i, p in enumerate(report_products, 1):
        title = p["title"][:48] + ".." if len(p["title"]) > 50 else p["title"]
        price = "" if p["current_price"] is None else f"₹{p['current_price']}"
        print(
            f"{i:<4}  {title:<50}  {str(p['last_ordered_date']):<12}  "
            f"{p['number_of_times_purchased']:<4}  {price:<8}  "
            f"{str(p['category']):<12}  {p['availability']}"
        )

    # ---- Push to Salesforce ----
    # Best-effort: any failure here is logged but does not fail the scrape.
    try:
        from salesforce_sync import sync_products
        sync_products(report_products)
    except Exception as exc:
        print(f"[salesforce] Sync failed: {exc}")


def main() -> None:
    default_orders = default_orders_to_scrape()
    ap = argparse.ArgumentParser(description="Scrape Flipkart order history.")
    ap.add_argument(
        "--orders",
        type=int,
        default=default_orders,
        help=f"Number of orders to scrape (default: {default_orders}, from ORDERS_TO_SCRAPE in .env)",
    )
    ap.add_argument(
        "--headed",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        help="Run in headed mode (default: true)",
    )
    args = ap.parse_args()
    asyncio.run(run(num_orders=args.orders, headless=not args.headed))


if __name__ == "__main__":
    main()
