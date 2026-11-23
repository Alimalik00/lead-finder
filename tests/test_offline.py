"""Offline test suite -- no network, no API keys, no extra dependencies.

Run from the project root::

    python -m unittest discover -s tests -v

Everything here uses fixture HTML and a stub HTTP client, so the suite is fast
and safe to run in CI.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ai_summary import (  # noqa: E402
    TemplateSummaryGenerator,
    _parse_json_object,
    build_generator,
    build_lead_brief,
)
from cache import JsonCache  # noqa: E402
from config import AISettings, AppConfig, load_config  # noqa: E402
from export import build_output_path, export_leads, leads_to_dataframe  # noqa: E402
from http_client import HttpClient, HttpError, RateLimiter  # noqa: E402
from lead_scoring import LeadScorer  # noqa: E402
from models import Lead, PageSpeedResult, Place, WebsiteAudit  # noqa: E402
from pagespeed import _parse_psi_payload  # noqa: E402
from website_audit import (  # noqa: E402
    WebsiteAuditor,
    merge_pagespeed_signals,
    normalize_url,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OUTDATED_HTML = """
<html>
<head><title>Bobs Plumbing</title></head>
<body bgcolor="#ffffff">
<center><font size="4">Welcome to Bobs Plumbing</font></center>
<table><tr><td>a</td></tr></table>
<table><tr><td>b</td></tr></table>
<table><tr><td>c</td></tr></table>
<img src="/logo.gif">
<img src="/broken.gif">
<a href="/services.html">Services</a>
<a href="https://facebook.com/bobs">FB</a>
<p style="font-size:9px">Call 555-1234</p>
<p style="font-size:8px">tiny</p>
<p style="font-size:10px">also tiny</p>
<script src="/js/jquery-1.7.2.min.js"></script>
<p>&copy; 2011 Bobs Plumbing</p>
</body></html>
"""

MODERN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<title>Sunrise Dental - Austin TX</title>
<meta name="description" content="Family dentistry in Austin. Book your appointment online today.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.ico">
<script src="https://www.googletagmanager.com/gtag/js"></script>
</head>
<body>
<header><nav><a href="/">Home</a><a href="/team">Team</a><a href="/contact">Contact</a></nav></header>
<main><section>
  <h1>Sunrise Dental</h1>
  <a class="btn" href="https://calendly.com/sunrise/checkup">Book an appointment</a>
  <a href="tel:+15125550123">Call us</a>
  <img src="/hero.jpg" alt="Our clinic">
  <form action="/send"><input type="email" name="email"><textarea name="msg"></textarea>
  <input type="submit" value="Contact us"></form>
</section></main>
<footer>
  <a href="https://www.instagram.com/sunrise">Instagram</a>
  <a href="https://wa.me/15125550123">WhatsApp</a>
  <a href="mailto:info@sunrisedental.com">info@sunrisedental.com</a>
  <p>&copy; 2026 Sunrise Dental</p>
</footer>
<script src="https://widget.intercom.io/widget/abc"></script>
</body></html>
"""

PSI_PAYLOAD = {
    "loadingExperience": {
        "metrics": {
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 340},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 18},
        }
    },
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.34},
            "accessibility": {"score": 0.71},
            "best-practices": {"score": 0.90},
            "seo": {"score": 0.83},
        },
        "audits": {
            "first-contentful-paint": {"numericValue": 2500.4},
            "largest-contentful-paint": {"numericValue": 6100.9},
            "cumulative-layout-shift": {"numericValue": 0.31},
            "total-blocking-time": {"numericValue": 890},
            "dom-size": {"numericValue": 1800},
            "viewport": {"score": 0, "scoreDisplayMode": "binary"},
            "font-size": {"score": 0, "scoreDisplayMode": "binary"},
            "tap-targets": {"score": 0, "scoreDisplayMode": "binary"},
            "is-on-https": {"score": 1, "scoreDisplayMode": "binary"},
            "meta-description": {"score": 1, "scoreDisplayMode": "binary"},
            "color-contrast": {"score": None, "scoreDisplayMode": "notApplicable"},
            "legacy-javascript": {"score": 0.2, "scoreDisplayMode": "numeric"},
        },
    },
}


class _FakeResponse:
    def __init__(self, text: str, url: str, status: int = 200) -> None:
        self.text = text
        self.content = text.encode()
        self.url = url
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.elapsed = timedelta(milliseconds=420)


class _FakeHttp:
    """Serves one canned page; probes report anything named "broken" as 404."""

    def __init__(self, html: str, url: str) -> None:
        self.html = html
        self.url = url
        self.session = types.SimpleNamespace(headers={"User-Agent": "LeadFinderBot/1.0"})

    def get(self, url, **kwargs):
        if url.rstrip("/") == self.url.rstrip("/"):
            return _FakeResponse(self.html, self.url)
        return _FakeResponse("<html><body>nope</body></html>", url, status=404)

    def head(self, url, **kwargs):
        return _FakeResponse("", url, status=200)

    def probe(self, url, timeout=None):
        return (404, "") if "broken" in url else (200, "")


def _audit(html: str, url: str) -> WebsiteAudit:
    settings = AppConfig().audit
    settings.follow_contact_pages = False
    return WebsiteAuditor(settings, _FakeHttp(html, url)).audit(url)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_yaml_config_is_applied(self):
        config = load_config(str(PROJECT_ROOT / "config.example.yaml"))
        self.assertEqual(config.search.city, "Austin")
        self.assertEqual(config.search.radius_m, 15000)
        self.assertEqual(config.scoring.no_website, 50)
        self.assertTrue(config.pagespeed.enabled)
        self.assertEqual(config.pagespeed.strategies, ["mobile", "desktop"])

    def test_derived_search_fields(self):
        config = load_config(str(PROJECT_ROOT / "config.example.yaml"))
        self.assertEqual(config.search.location_string, "Austin, Texas, United States")
        self.assertEqual(
            config.search.text_query, "Dentists in Austin, Texas, United States"
        )
        self.assertEqual(config.search.resolved_place_type(), "dentist")

    def test_validation_reports_missing_fields(self):
        problems = AppConfig().search.validate()
        self.assertIn("country is required", problems)
        self.assertIn("city is required", problems)
        self.assertIn("category is required", problems)


class TestUrlNormalisation(unittest.TestCase):
    def test_scheme_is_added(self):
        self.assertEqual(normalize_url("example.com"), "https://example.com")

    def test_existing_scheme_is_kept(self):
        self.assertEqual(normalize_url("http://a.com/x"), "http://a.com/x")

    def test_fragment_is_stripped(self):
        self.assertEqual(normalize_url("https://a.com/x#y"), "https://a.com/x")

    def test_empty_input(self):
        self.assertEqual(normalize_url(""), "")


class TestOutdatedSiteAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = _audit(OUTDATED_HTML, "https://bobsplumbing.com")

    def test_basic_metadata(self):
        self.assertTrue(self.audit.reachable)
        self.assertEqual(self.audit.title, "Bobs Plumbing")
        self.assertFalse(self.audit.has_meta_description)
        self.assertFalse(self.audit.mobile_responsive)
        self.assertFalse(self.audit.html5_doctype)

    def test_legacy_markup_is_detected(self):
        markers = " ".join(self.audit.legacy_markers)
        self.assertIn("table-based layout", markers)
        self.assertIn("jQuery 1", markers)
        self.assertGreaterEqual(len(self.audit.legacy_markers), 3)

    def test_verdicts(self):
        self.assertTrue(self.audit.tiny_text)
        self.assertTrue(self.audit.looks_outdated)
        self.assertEqual(self.audit.copyright_year, 2011)
        self.assertEqual(self.audit.broken_images, 1)

    def test_missing_conversion_surface(self):
        self.assertFalse(self.audit.has_cta)
        self.assertFalse(self.audit.has_contact_form)
        self.assertFalse(self.audit.has_booking)
        self.assertFalse(self.audit.has_analytics)

    def test_social_link_found(self):
        self.assertIn("facebook", self.audit.social_links)

    def test_issue_list_is_populated(self):
        self.assertGreater(len(self.audit.issues), 8)
        self.assertIn("Missing meta description", self.audit.issues)


class TestModernSiteAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = _audit(MODERN_HTML, "https://sunrisedental.com")

    def test_technical_checks_pass(self):
        self.assertTrue(self.audit.https)
        self.assertTrue(self.audit.mobile_responsive)
        self.assertTrue(self.audit.html5_doctype)
        self.assertTrue(self.audit.has_meta_description)
        self.assertTrue(self.audit.has_favicon)
        self.assertTrue(self.audit.modern_html)
        self.assertFalse(self.audit.looks_outdated)

    def test_conversion_surface_detected(self):
        self.assertTrue(self.audit.has_cta)
        self.assertTrue(self.audit.has_contact_form)
        self.assertTrue(self.audit.has_phone_link)
        self.assertFalse(self.audit.poor_navigation)

    def test_vendors_identified(self):
        self.assertEqual(self.audit.booking_providers, ["Calendly"])
        self.assertEqual(self.audit.chat_providers, ["Intercom"])
        self.assertTrue(self.audit.has_whatsapp)
        self.assertTrue(self.audit.has_analytics)

    def test_email_discovered(self):
        self.assertEqual(self.audit.primary_email, "info@sunrisedental.com")


class TestPageSpeedParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = _parse_psi_payload(PSI_PAYLOAD, "mobile")

    def test_category_scores(self):
        self.assertEqual(self.metrics.performance_score, 34)
        self.assertEqual(self.metrics.seo_score, 83)
        self.assertEqual(self.metrics.accessibility_score, 71)

    def test_lab_metrics_win_over_field_data(self):
        self.assertAlmostEqual(self.metrics.lcp_ms, 6100.9, places=1)
        self.assertAlmostEqual(self.metrics.cls, 0.31, places=3)

    def test_inp_comes_from_field_data(self):
        self.assertEqual(self.metrics.inp_ms, 340)
        self.assertTrue(self.metrics.inp_from_field_data)

    def test_audit_pass_fail_tri_state(self):
        self.assertIs(self.metrics.audits["viewport"], False)
        self.assertIs(self.metrics.audits["is-on-https"], True)
        self.assertIsNone(self.metrics.audits["color-contrast"])  # not applicable
        self.assertIsNone(self.metrics.audits["link-text"])  # absent from payload

    def test_error_payload(self):
        result = _parse_psi_payload({"error": {"message": "boom"}}, "mobile")
        self.assertEqual(result.error, "boom")


class TestPageSpeedOverridesHeuristics(unittest.TestCase):
    def test_lighthouse_verdicts_are_authoritative(self):
        audit = _audit(MODERN_HTML, "https://sunrisedental.com")
        # The HTML says the site is responsive with legible text...
        self.assertTrue(audit.mobile_responsive)
        self.assertFalse(audit.tiny_text)

        metrics = _parse_psi_payload(PSI_PAYLOAD, "mobile")
        result = PageSpeedResult(url=audit.url, strategies={"mobile": metrics})
        merge_pagespeed_signals(audit, result)

        # ...but Lighthouse disagrees, and Lighthouse wins.
        self.assertFalse(audit.mobile_responsive)
        self.assertTrue(audit.tiny_text)
        self.assertTrue(audit.bad_spacing)  # from the tap-targets audit
        self.assertIn(
            "Not mobile responsive (no viewport meta tag)", audit.issues
        )


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.scorer = LeadScorer()

    def test_no_website_is_hot(self):
        place = Place(
            place_id="p1", name="Acme Roofing", category="Roofing",
            rating=4.8, review_count=214,
        )
        score = self.scorer.score(place, None, None)
        self.assertTrue(score.needs_website)
        self.assertEqual(score.priority, "Hot")
        self.assertGreaterEqual(score.score, 70)
        rules = {reason.rule for reason in score.reasons}
        self.assertIn("no_website", rules)
        self.assertIn("high_demand_bonus", rules)

    def test_social_page_does_not_count_as_a_website(self):
        place = Place(
            place_id="p2", name="Joe Barber", category="Salons",
            website="https://www.facebook.com/joebarber", review_count=40,
        )
        score = self.scorer.score(place, None, None)
        self.assertTrue(score.needs_website)
        self.assertGreaterEqual(score.score, 50)

    def test_healthy_site_scores_cold(self):
        place = Place(
            place_id="p3", name="Sunrise Dental", category="Dentists",
            website="https://sunrisedental.com", rating=4.9, review_count=300,
        )
        audit = _audit(MODERN_HTML, "https://sunrisedental.com")
        score = self.scorer.score(place, audit, None)
        self.assertEqual(score.priority, "Cold")
        self.assertLessEqual(score.score, 25)

    def test_unreachable_site_fires_its_own_rule(self):
        place = Place(
            place_id="p4", name="Ghost Cafe", category="Restaurants",
            website="https://ghostcafe.example", review_count=88,
        )
        audit = WebsiteAudit(url=place.website, reachable=False, error="timeout")
        score = self.scorer.score(place, audit, None)
        self.assertIn("site_unreachable", {r.rule for r in score.reasons})

    def test_performance_rules_need_pagespeed_data(self):
        place = Place(
            place_id="p5", name="Bobs Plumbing", category="Plumbers",
            website="https://bobsplumbing.com", rating=3.6, review_count=12,
        )
        audit = _audit(OUTDATED_HTML, place.website)
        metrics = _parse_psi_payload(PSI_PAYLOAD, "mobile")
        psi = PageSpeedResult(url=place.website, strategies={"mobile": metrics})
        score = self.scorer.score(place, audit, psi)
        rules = {reason.rule for reason in score.reasons}
        self.assertIn("slow_performance", rules)
        self.assertIn("poor_core_web_vitals", rules)
        self.assertIn("few_reviews", rules)
        self.assertIn("low_rating_penalty", rules)

    def test_score_is_capped(self):
        place = Place(
            place_id="p6", name="Worst Site", category="Plumbers",
            website="https://bobsplumbing.com", rating=1.0, review_count=1,
        )
        audit = _audit(OUTDATED_HTML, place.website)
        metrics = _parse_psi_payload(PSI_PAYLOAD, "mobile")
        psi = PageSpeedResult(url=place.website, strategies={"mobile": metrics})
        score = self.scorer.score(place, audit, psi)
        self.assertLessEqual(score.score, 100)
        self.assertGreaterEqual(score.raw_score, score.score)


class TestTemplateSummaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scorer = LeadScorer()
        generator = TemplateSummaryGenerator()

        no_site = Place(
            place_id="t1", name="Acme Roofing", category="Roofing",
            address="1 Main St, Austin, TX 78701, USA", rating=4.8, review_count=214,
        )
        dead = Place(
            place_id="t2", name="Ghost Cafe", category="Restaurants",
            website="https://ghostcafe.example", review_count=88,
        )
        old = Place(
            place_id="t3", name="Bobs Plumbing", category="Plumbers",
            website="https://bobsplumbing.com", rating=3.6, review_count=12,
        )

        cls.leads = [
            Lead(place=no_site, score=scorer.score(no_site, None, None)),
            Lead(
                place=dead,
                audit=WebsiteAudit(url=dead.website, reachable=False, error="timeout"),
            ),
            Lead(place=old, audit=_audit(OUTDATED_HTML, old.website)),
        ]
        cls.leads[1].score = scorer.score(cls.leads[1].place, cls.leads[1].audit, None)
        cls.leads[2].score = scorer.score(cls.leads[2].place, cls.leads[2].audit, None)
        for lead in cls.leads:
            lead.summary = generator.generate(lead)

    def test_every_field_is_populated(self):
        for lead in self.leads:
            self.assertGreater(len(lead.summary.reason), 60, lead.name)
            self.assertTrue(lead.summary.email_subject, lead.name)
            self.assertGreater(len(lead.summary.email_body), 80, lead.name)
            self.assertTrue(lead.summary.linkedin_message, lead.name)

    def test_linkedin_note_respects_length_limit(self):
        for lead in self.leads:
            self.assertLessEqual(len(lead.summary.linkedin_message), 300)

    def test_copy_references_real_findings(self):
        self.assertIn("214", self.leads[0].summary.reason)
        self.assertIn("does not load", self.leads[1].summary.reason)

    def test_brief_only_contains_measured_facts(self):
        brief = build_lead_brief(self.leads[2])
        self.assertIn("Website:", brief)
        self.assertIn("Lead score:", brief)
        self.assertIn("Issues found:", brief)


class TestGeneratorSelection(unittest.TestCase):
    def test_template_used_without_a_key(self):
        self.assertIsInstance(
            build_generator(AISettings(api_key="")), TemplateSummaryGenerator
        )

    def test_template_used_when_disabled(self):
        self.assertIsInstance(
            build_generator(AISettings(api_key="k", enabled=False)),
            TemplateSummaryGenerator,
        )

    def test_template_used_when_requested(self):
        self.assertIsInstance(
            build_generator(AISettings(api_key="k", provider="template")),
            TemplateSummaryGenerator,
        )


class TestJsonExtraction(unittest.TestCase):
    def test_fenced_block(self):
        self.assertEqual(_parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_surrounding_prose(self):
        self.assertEqual(_parse_json_object('Sure: {"a": 2} done'), {"a": 2})

    def test_invalid_input(self):
        self.assertIsNone(_parse_json_object("not json at all"))


class TestExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scorer = LeadScorer()
        generator = TemplateSummaryGenerator()
        places = [
            Place(place_id="e1", name="Acme Roofing", category="Roofing",
                  rating=4.8, review_count=214),
            Place(place_id="e2", name="Sunrise Dental", category="Dentists",
                  website="https://sunrisedental.com", rating=4.9, review_count=300),
        ]
        audits = [None, _audit(MODERN_HTML, "https://sunrisedental.com")]
        cls.leads = []
        for place, audit in zip(places, audits):
            lead = Lead(place=place, audit=audit)
            lead.score = scorer.score(place, audit, None)
            lead.summary = generator.generate(lead)
            cls.leads.append(lead)

    def test_required_columns_come_first_and_rows_are_sorted(self):
        frame = leads_to_dataframe(self.leads)
        required = [
            "Business Name", "Category", "Phone", "Website", "Email", "Rating",
            "Reviews", "Lead Score", "Priority", "Website Issues",
            "PageSpeed Mobile", "PageSpeed Desktop", "Reason",
            "Suggested Services", "Generated Email", "Generated LinkedIn Message",
        ]
        self.assertEqual(list(frame.columns)[: len(required)], required)
        self.assertEqual(
            list(frame["Lead Score"]), sorted(frame["Lead Score"], reverse=True)
        )

    def test_workbook_is_written_and_formatted(self):
        import openpyxl

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.search.city = "Austin"
            config.search.category = "Dentists"
            config.output.directory = tmp
            config.output.write_csv = True
            config.output.write_json = True

            path = export_leads(self.leads, config)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_suffix(".csv").exists())

            workbook = openpyxl.load_workbook(path)
            self.assertEqual(workbook.sheetnames, ["Leads", "Summary", "Issues"])
            sheet = workbook["Leads"]
            self.assertEqual(sheet["A1"].value, "Business Name")
            self.assertEqual(sheet.freeze_panes, "C2")
            self.assertIsNotNone(sheet.auto_filter.ref)
            self.assertEqual(sheet.max_row, len(self.leads) + 1)

            payload = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload["leads"]), len(self.leads))

    def test_bad_filename_template_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.output.directory = tmp
            config.output.filename_template = "leads_{not_a_field}.xlsx"
            path = build_output_path(config)
            self.assertTrue(path.name.endswith(".xlsx"))
            self.assertNotIn("not_a_field", path.name)


class TestCache(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = JsonCache(Path(tmp), ttl_hours=1, enabled=True)
            self.assertIsNone(cache.get("ns", "key"))
            cache.set("ns", "key", {"value": 1})
            self.assertEqual(cache.get("ns", "key"), {"value": 1})
            self.assertIn("1 hits", cache.stats())

            # A near-zero TTL means every entry is stale on read.
            short = JsonCache(Path(tmp), ttl_hours=1e-9, enabled=True)
            short.set("ns2", "k", 1)
            self.assertIsNone(short.get("ns2", "k"))

    def test_disabled_cache_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = JsonCache(Path(tmp), enabled=False)
            cache.set("ns", "key", 1)
            self.assertIsNone(cache.get("ns", "key"))
            self.assertEqual(cache.stats(), "cache disabled")


class TestHttpClient(unittest.TestCase):
    def test_rate_limiter_spaces_requests(self):
        import time

        limiter = RateLimiter(20.0)  # 50 ms apart
        start = time.monotonic()
        for _ in range(4):
            limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - start, 0.10)

    def test_zero_rate_disables_limiting(self):
        self.assertEqual(RateLimiter(0)._min_interval, 0.0)

    def test_unreachable_host_raises_http_error(self):
        client = HttpClient(max_retries=1, backoff_factor=0.01)
        try:
            with self.assertRaises(HttpError):
                client.get("http://127.0.0.1:9/nothing", timeout=1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
