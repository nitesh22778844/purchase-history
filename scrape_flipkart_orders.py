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
    # Flipkart: orders page — each order is a div.HetYBQ inside div.allJIf
    "order_card": (
        "div.HetYBQ, "
        "div:has(> div > div > a[href*='order_details']), "
        "div:has(> div > a[href*='order_details'])"
    ),
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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def mask(value: str) -> str:
    return f"***{value[-4:]}" if value and len(value) > 4 else "****"


def parse_date(raw: str) -> str:
    if not raw:
        return "unknown"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
    cleaned = re.sub(r"'(\d{2})\b", lambda m: f"20{m.group(1)}", cleaned)
    try:
        return dateutil_parser.parse(cleaned, fuzzy=True).date().isoformat()
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
        r"\s*(?:Color:|Size:|₹|\+\d+|Delivered\s+on|Refund|Return\s|Cancelled|Your\s+item)",
        title, maxsplit=1, flags=re.I,
    )[0]
    title = re.sub(r"\s+", " ", title).strip()
    # Drop trailing ellipsis/punctuation
    title = title.rstrip("…. ")
    return title


def _extract_date_from_text(text: str) -> str:
    """Find a 'Delivered/Ordered/Shipped on <Date>' inside text → ISO YYYY-MM-DD."""
    m = re.search(
        r"(?:Delivered|Ordered|Shipped|Cancelled|Return\s+completed|Refund\s+completed)"
        r"\s+on\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4})",
        text, re.I,
    )
    if m:
        return parse_date(m.group(1))
    return "unknown"


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

        if title:
            products.append({"item_id": item_id, "title": title, "date": date})

    print(f"[order {order_idx}/{total}] {len(products)} product(s)")
    return products


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
            slow_mo=400 if not headless else 0,
            args=browser_args,
        )

        # Reuse the saved Flipkart session if auth_state.json exists.
        # On Render, this file is written from FLIPKART_AUTH_STATE env var at startup.
        storage_state = str(AUTH_STATE_FILE) if AUTH_STATE_FILE.exists() else None
        if storage_state:
            print(f"[auth] Restoring session from {AUTH_STATE_FILE}")

        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1400, "height": 900},
        )
        page = await context.new_page()

        # ---- Login ----
        await login(page, flipkart_email, gmail_service)
        await save_auth(context)

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
        all_products: list[dict] = []
        seen_item_ids_global: set[str] = set()
        for idx, card in enumerate(cards, start=1):
            try:
                products = await expand_and_get_products(page, card, idx, actual_count)
                for p in products:
                    if p["item_id"] in seen_item_ids_global:
                        continue
                    seen_item_ids_global.add(p["item_id"])
                    all_products.append(p)
            except Exception as exc:
                print(f"[order {idx}/{actual_count}] Error: {exc}")

        await context.close()

    # ---- Aggregate ----
    title_counts = Counter(p["title"] for p in all_products)
    report_products = [
        {
            "title": p["title"],
            "purchase_date": p["date"],
            "purchase_count_in_last_10_orders": title_counts[p["title"]],
        }
        for p in all_products
    ]
    report = {
        "scraped_at": datetime.now(tz=timezone.utc).astimezone().isoformat(),
        "orders_scanned": actual_count,
        "products": report_products,
    }
    ORDERS_REPORT_FILE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[done] Report written to {ORDERS_REPORT_FILE}")

    print(f"\n{'#':<4}  {'Product Title':<60}  {'Date':<12}  {'Count'}")
    print("-" * 90)
    for i, p in enumerate(report_products, 1):
        title = p["title"][:58] + ".." if len(p["title"]) > 60 else p["title"]
        print(f"{i:<4}  {title:<60}  {p['purchase_date']:<12}  {p['purchase_count_in_last_10_orders']}")

    # ---- Push to Salesforce ----
    # Best-effort: any failure here is logged but does not fail the scrape.
    try:
        from salesforce_sync import sync_products
        sync_products(report_products)
    except Exception as exc:
        print(f"[salesforce] Sync failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Flipkart order history.")
    ap.add_argument("--orders", type=int, default=10, help="Number of orders to scrape (default: 10)")
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
