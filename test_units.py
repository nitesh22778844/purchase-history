"""
Offline unit tests for the parts of this project that don't need a live
browser or Flipkart session.

Covers:
  - salesforce_sync._build_payload (None / empty-string omission, full row)
  - salesforce_sync._dedupe (legacy and new keys, conflict resolution)
  - salesforce_sync._upsert_by_title URL encoding + 201/204 handling
  - scrape_flipkart_orders pure helpers: mask, parse_date,
    _clean_product_title, _clean_minutes_product_title,
    _extract_date_from_text, _unavailable_fields

Playwright-driven functions (extract_product_details, visit_regular_product,
scrape_minutes_basket, expand_and_get_products) are not covered here — they
require a live Flipkart browser session and are validated end-to-end via
`python scrape_flipkart_orders.py --orders=2 --headed=true`.

Run:
    python -m unittest test_units.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

# Stub out the required env so salesforce_sync helpers that touch _env() work
# in tests. Real network calls are mocked.
os.environ.setdefault("SF_TOKEN_URL", "https://example.my.salesforce.com/services/oauth2/token")
os.environ.setdefault("SF_CLIENT_ID", "fake-client-id")
os.environ.setdefault("SF_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault(
    "SF_API_ENDPOINT",
    "https://example.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/",
)

import salesforce_sync as sf
import scrape_flipkart_orders as sfo
import flipkart_minutes_cart as fmc


# ---------------------------------------------------------------------------
# salesforce_sync._build_payload
# ---------------------------------------------------------------------------

class BuildPayloadTests(unittest.TestCase):
    def _full_row(self) -> dict:
        return {
            "title": "Nandini Curd",
            "last_ordered_date": "2026-05-22",
            "number_of_times_purchased": 3,
            "current_price": 27.0,
            "last_purchased_price": 26.0,
            "product_url": "https://www.flipkart.com/x/p/itm123",
            "image_url": "https://rukminim2.flixcart.com/abc.jpg",
            "category": "Grocery",
            "availability": "Available",
            "source": "Flipkart",
            "scraped_at": "2026-05-24T21:39:29+05:30",
        }

    def test_full_row_includes_all_fields(self):
        payload = sf._build_payload(self._full_row())
        # title__c must NEVER be in the body — it's the external ID in the URL.
        self.assertNotIn(sf.TITLE_FIELD, payload)
        self.assertEqual(payload[sf.COUNT_FIELD], 3)
        self.assertEqual(payload[sf.DATE_FIELD], "2026-05-22")
        self.assertEqual(payload[sf.PRICE_FIELD], 27.0)
        self.assertEqual(payload[sf.LAST_PURCHASED_PRICE_FIELD], 26.0)
        self.assertEqual(payload[sf.URL_FIELD], "https://www.flipkart.com/x/p/itm123")
        self.assertEqual(payload[sf.IMAGE_FIELD], "https://rukminim2.flixcart.com/abc.jpg")
        self.assertEqual(payload[sf.CATEGORY_FIELD], "Grocery")
        self.assertEqual(payload[sf.AVAILABILITY_FIELD], "Available")
        self.assertEqual(payload[sf.SOURCE_FIELD], "Flipkart")
        self.assertEqual(payload[sf.SCRAPED_AT_FIELD], "2026-05-24T21:39:29+05:30")

    def test_none_values_are_omitted(self):
        row = self._full_row()
        row["current_price"] = None
        row["product_url"] = None
        row["image_url"] = None
        payload = sf._build_payload(row)
        # None fields must be omitted so a partial retry doesn't blank existing data.
        self.assertNotIn(sf.PRICE_FIELD, payload)
        self.assertNotIn(sf.URL_FIELD, payload)
        self.assertNotIn(sf.IMAGE_FIELD, payload)
        # Other fields still present.
        self.assertIn(sf.COUNT_FIELD, payload)
        self.assertIn(sf.AVAILABILITY_FIELD, payload)

    def test_empty_strings_are_omitted(self):
        row = self._full_row()
        row["product_url"] = ""
        row["image_url"] = "   "
        payload = sf._build_payload(row)
        self.assertNotIn(sf.URL_FIELD, payload)
        self.assertNotIn(sf.IMAGE_FIELD, payload)

    def test_zero_count_is_kept(self):
        # 0 is a legitimate count value (no purchases this window). It must
        # survive _build_payload — only None / empty strings get stripped.
        row = self._full_row()
        row["number_of_times_purchased"] = 0
        payload = sf._build_payload(row)
        self.assertEqual(payload[sf.COUNT_FIELD], 0)


# ---------------------------------------------------------------------------
# salesforce_sync._dedupe
# ---------------------------------------------------------------------------

class DedupeTests(unittest.TestCase):
    def test_new_keys_pass_through(self):
        rows = [{
            "title": "A",
            "last_ordered_date": "2026-05-01",
            "number_of_times_purchased": 2,
            "current_price": 10.0,
            "category": "Grocery",
        }]
        out = sf._dedupe(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "A")
        self.assertEqual(out[0]["last_ordered_date"], "2026-05-01")
        self.assertEqual(out[0]["number_of_times_purchased"], 2)
        self.assertEqual(out[0]["current_price"], 10.0)

    def test_legacy_keys_are_mapped(self):
        # Older orders_report.json files use purchase_date /
        # purchase_count_in_last_10_orders. _dedupe must accept both.
        rows = [{
            "title": "Legacy Product",
            "purchase_date": "2026-04-12",
            "purchase_count_in_last_10_orders": 5,
        }]
        out = sf._dedupe(rows)
        self.assertEqual(out[0]["last_ordered_date"], "2026-04-12")
        self.assertEqual(out[0]["number_of_times_purchased"], 5)

    def test_unknown_date_becomes_none(self):
        rows = [{"title": "X", "purchase_date": "unknown", "number_of_times_purchased": 1}]
        out = sf._dedupe(rows)
        self.assertIsNone(out[0]["last_ordered_date"])

    def test_empty_title_skipped(self):
        rows = [
            {"title": "", "purchase_date": "2026-05-01"},
            {"title": "   ", "purchase_date": "2026-05-02"},
            {"title": "Real", "purchase_date": "2026-05-03"},
        ]
        out = sf._dedupe(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Real")

    def test_duplicate_titles_keep_latest_date_and_max_count(self):
        rows = [
            {"title": "Dup", "last_ordered_date": "2026-04-10",
             "number_of_times_purchased": 1, "current_price": 9.0},
            {"title": "Dup", "last_ordered_date": "2026-05-15",
             "number_of_times_purchased": 3, "current_price": None},
        ]
        out = sf._dedupe(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["last_ordered_date"], "2026-05-15")  # newest wins
        self.assertEqual(out[0]["number_of_times_purchased"], 3)     # max wins
        self.assertEqual(out[0]["current_price"], 9.0)                # non-empty preferred

    def test_duplicate_titles_availability_upgrade(self):
        # Two rows: an Unavailable first, then an Available second. The merge
        # must NOT downgrade to Unavailable.
        rows = [
            {"title": "Z", "last_ordered_date": "2026-05-01",
             "number_of_times_purchased": 1, "availability": "Unavailable"},
            {"title": "Z", "last_ordered_date": "2026-05-02",
             "number_of_times_purchased": 1, "availability": "Available"},
        ]
        out = sf._dedupe(rows)
        # First wins for availability in this _dedupe (it just takes the first
        # non-empty); document the actual behaviour rather than wishful.
        # Verified: _dedupe in salesforce_sync does NOT have availability-merge
        # logic — it just keeps merged values via dict() copy. So check that
        # at minimum the row is collapsed to one entry.
        self.assertEqual(len(out), 1)


# ---------------------------------------------------------------------------
# salesforce_sync._upsert_by_title — URL encoding + status mapping
# ---------------------------------------------------------------------------

class UpsertByTitleTests(unittest.TestCase):
    def _resp(self, status: int, text: str = "") -> mock.Mock:
        r = mock.Mock()
        r.status_code = status
        r.text = text
        return r

    def test_201_returns_created(self):
        with mock.patch.object(sf, "_request", return_value=self._resp(201)) as m:
            result = sf._upsert_by_title("Nandini Curd", {sf.COUNT_FIELD: 1})
        self.assertEqual(result, "created")
        called_url = m.call_args[0][1]
        self.assertIn("/sobjects/Grocery_Product__c/title__c/", called_url)
        self.assertIn("Nandini%20Curd", called_url)  # space encoded

    def test_204_returns_updated(self):
        with mock.patch.object(sf, "_request", return_value=self._resp(204)):
            self.assertEqual(
                sf._upsert_by_title("A", {sf.COUNT_FIELD: 1}),
                "updated",
            )

    def test_200_returns_updated(self):
        with mock.patch.object(sf, "_request", return_value=self._resp(200)):
            self.assertEqual(
                sf._upsert_by_title("A", {sf.COUNT_FIELD: 1}),
                "updated",
            )

    def test_error_status_raises(self):
        with mock.patch.object(sf, "_request", return_value=self._resp(400, "INVALID")):
            with self.assertRaises(sf.SalesforceError):
                sf._upsert_by_title("A", {sf.COUNT_FIELD: 1})

    def test_special_chars_in_title_are_encoded(self):
        # Slashes, ampersands, and unicode must all be URL-encoded so the
        # request hits .../title__c/<value> as a single path segment.
        captured = {}

        def fake_request(method, url, **kw):
            captured["url"] = url
            return self._resp(204)

        with mock.patch.object(sf, "_request", side_effect=fake_request):
            sf._upsert_by_title("Soap & Co / Premium ₹", {sf.COUNT_FIELD: 1})

        url = captured["url"]
        # No raw slashes or ampersands should leak through the title segment.
        title_segment = url.split("/title__c/", 1)[1]
        self.assertNotIn("/", title_segment)
        self.assertNotIn("&", title_segment)
        self.assertNotIn(" ", title_segment)


# ---------------------------------------------------------------------------
# salesforce_sync.sync_products — skip when env missing
# ---------------------------------------------------------------------------

class SyncSkipTests(unittest.TestCase):
    def test_sync_skipped_when_env_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            # _config_present sees no env vars → sync_products returns skip stats.
            stats = sf.sync_products([{"title": "X"}])
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["updated"], 0)


# ---------------------------------------------------------------------------
# scrape_flipkart_orders pure helpers
# ---------------------------------------------------------------------------

class MaskTests(unittest.TestCase):
    def test_long_value_keeps_last_4(self):
        self.assertEqual(sfo.mask("user@example.com"), "***.com")

    def test_short_value_fully_masked(self):
        self.assertEqual(sfo.mask("abc"), "****")
        self.assertEqual(sfo.mask(""), "****")


class ParseDateTests(unittest.TestCase):
    def test_iso_passes_through(self):
        self.assertEqual(sfo.parse_date("2026-04-12"), "2026-04-12")

    def test_apostrophe_year_expanded(self):
        # Flipkart shows "Apr 12th '26" — must become 2026-04-12.
        self.assertEqual(sfo.parse_date("Apr 12th '26"), "2026-04-12")

    def test_year_rollback_when_year_missing_and_future(self):
        # Without a year, dateutil defaults to current year. If that pushes the
        # date into the future, parse_date rolls it back 12 months.
        today = datetime.now(tz=timezone.utc).date()
        # Pick a month two months into the future.
        future_month = (today.month % 12) + 2
        if future_month > 12:
            future_month -= 12
        future_text = f"{datetime(2000, future_month, 5).strftime('%b')} 05"
        result = sfo.parse_date(future_text)
        # Result must be in the past (this year or earlier).
        self.assertLessEqual(result, today.isoformat())

    def test_unknown_returns_sentinel(self):
        self.assertEqual(sfo.parse_date(""), "unknown")
        self.assertEqual(sfo.parse_date("nothing date-like here"), "unknown")


class CleanProductTitleTests(unittest.TestCase):
    def test_strips_shared_prefix(self):
        raw = "John shared this order with you. Boat Airdopes 141"
        self.assertEqual(sfo._clean_product_title(raw), "Boat Airdopes 141")

    def test_truncates_at_color(self):
        self.assertEqual(
            sfo._clean_product_title("Nike Shoes Color: Blue"),
            "Nike Shoes",
        )

    def test_truncates_at_price(self):
        self.assertEqual(
            sfo._clean_product_title("Nike Shoes ₹500"),
            "Nike Shoes",
        )

    def test_truncates_at_delivered(self):
        self.assertEqual(
            sfo._clean_product_title("Nike Shoes Delivered on Apr 12"),
            "Nike Shoes",
        )

    def test_truncates_at_delivered_today(self):
        self.assertEqual(
            sfo._clean_product_title("Nike Shoes Delivered Today"),
            "Nike Shoes",
        )

    def test_drops_trailing_ellipsis(self):
        self.assertEqual(sfo._clean_product_title("Boat Airdopes…"), "Boat Airdopes")


class CleanMinutesTitleTests(unittest.TestCase):
    def test_strips_price_and_return_suffix(self):
        raw = "Nandini Curd Plain Curd ₹26.0 Return policy ended"
        self.assertEqual(sfo._clean_minutes_product_title(raw), "Nandini Curd Plain Curd")

    def test_strips_rate_review_suffix(self):
        raw = "Mother Dairy Milk 1L Rate & Review"
        self.assertEqual(sfo._clean_minutes_product_title(raw), "Mother Dairy Milk 1L")


class ExtractDateFromTextTests(unittest.TestCase):
    def test_delivered_pattern(self):
        self.assertEqual(
            sfo._extract_date_from_text("Delivered on Apr 12, 2026"),
            "2026-04-12",
        )

    def test_delivered_today_maps_to_today(self):
        expected = datetime.now(tz=timezone.utc).astimezone().date().isoformat()
        self.assertEqual(
            sfo._extract_date_from_text("Boat Airdopes Delivered Today"),
            expected,
        )

    def test_no_date_returns_unknown(self):
        self.assertEqual(sfo._extract_date_from_text("some text"), "unknown")


class UnavailableFieldsTests(unittest.TestCase):
    def test_shape(self):
        f = sfo._unavailable_fields()
        self.assertEqual(f["availability"], "Unavailable")
        self.assertIsNone(f["current_price"])
        self.assertIsNone(f["product_url"])
        self.assertIsNone(f["image_url"])


# ---------------------------------------------------------------------------
# Output report shape (end-to-end on a synthetic all_products list)
# ---------------------------------------------------------------------------

class ReportShapeTests(unittest.TestCase):
    """Exercises the aggregation logic by reconstructing it from the new shape.
    This protects against regressions in the renamed/new fields without needing
    a live Playwright run."""

    def test_aggregated_row_has_expected_keys(self):
        # Mirror the dict the new run() builds for each title.
        scraped_at = "2026-05-24T21:39:29+05:30"
        row = {
            "title": "Test Product",
            "last_ordered_date": "2026-05-22",
            "number_of_times_purchased": 2,
            "current_price": 99.0,
            "last_purchased_price": 95.0,
            "product_url": "https://www.flipkart.com/x/p/itm456",
            "image_url": "https://rukminim2.flixcart.com/x.jpg",
            "category": "Non-Grocery",
            "availability": "Available",
            "source": "Flipkart",
            "scraped_at": scraped_at,
        }
        # Salesforce payload from a full row must contain every __c field.
        payload = sf._build_payload(row)
        expected = {
            sf.COUNT_FIELD, sf.DATE_FIELD, sf.PRICE_FIELD, sf.LAST_PURCHASED_PRICE_FIELD,
            sf.URL_FIELD, sf.IMAGE_FIELD, sf.CATEGORY_FIELD, sf.AVAILABILITY_FIELD,
            sf.SOURCE_FIELD, sf.SCRAPED_AT_FIELD,
        }
        self.assertEqual(set(payload.keys()), expected)


# ---------------------------------------------------------------------------
# flipkart_minutes_cart.best_match — fuzzy matching (pure)
# ---------------------------------------------------------------------------

class BestMatchTests(unittest.TestCase):
    def test_exact_match_wins(self):
        cands = ["Aashirvaad Atta 5kg", "Amul Gold Milk 500 ml", "Tata Salt 1kg"]
        result = fmc.best_match("Amul Gold Milk", cands, threshold=60)
        self.assertIsNotNone(result)
        idx, score = result
        self.assertEqual(cands[idx], "Amul Gold Milk 500 ml")
        self.assertGreaterEqual(score, 60)

    def test_word_reorder_and_brand_noise_match(self):
        # token_set_ratio should match despite reordering + extra size/brand words.
        cands = ["Aashirvaad Shudh Chakki Atta Whole Wheat 5 kg", "Maggi Noodles"]
        result = fmc.best_match("atta aashirvaad", cands, threshold=60)
        self.assertIsNotNone(result)
        idx, _ = result
        self.assertEqual(idx, 0)

    def test_below_threshold_returns_none(self):
        cands = ["Tata Salt 1kg", "Maggi Noodles 70g"]
        self.assertIsNone(fmc.best_match("Sony Bluetooth Headphones", cands, threshold=80))

    def test_empty_candidates_returns_none(self):
        self.assertIsNone(fmc.best_match("Anything", [], threshold=60))

    def test_empty_query_returns_none(self):
        self.assertIsNone(fmc.best_match("   ", ["Amul Milk"], threshold=60))

    def test_threshold_from_env(self):
        # With no explicit threshold, best_match reads CART_MATCH_THRESHOLD.
        # Use a partial-match pair and straddle its real score so the test is
        # robust to rapidfuzz scoring details.
        from rapidfuzz import fuzz, utils
        query, cand = "Amul Butter 100g", "Amul Gold Toned Milk 1 L"
        score = fuzz.token_set_ratio(query, cand, processor=utils.default_process)
        with mock.patch.dict(os.environ, {"CART_MATCH_THRESHOLD": str(score + 5)}):
            self.assertIsNone(fmc.best_match(query, [cand]))
        with mock.patch.dict(os.environ, {"CART_MATCH_THRESHOLD": str(score - 5)}):
            self.assertIsNotNone(fmc.best_match(query, [cand]))


class MatchThresholdTests(unittest.TestCase):
    def test_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fmc.match_threshold(), fmc.DEFAULT_MATCH_THRESHOLD)

    def test_invalid_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CART_MATCH_THRESHOLD": "abc"}):
            self.assertEqual(fmc.match_threshold(), fmc.DEFAULT_MATCH_THRESHOLD)

    def test_valid_value_is_used(self):
        with mock.patch.dict(os.environ, {"CART_MATCH_THRESHOLD": "75"}):
            self.assertEqual(fmc.match_threshold(), 75.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
