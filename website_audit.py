"""Website auditing: technical, UX and business-capability checks.

Given a URL this module fetches the homepage (and optionally a couple of
contact pages), parses it with BeautifulSoup/lxml, and produces a
:class:`models.WebsiteAudit` describing:

* **Technical** -- HTTPS, certificate validity, title/meta/favicon, modern
  markup, broken images and links.
* **UX** -- responsive viewport, tiny text, typography sprawl, clutter,
  navigation quality, whether there is any clear call to action.
* **Business** -- online booking, ordering, live chat, WhatsApp, appointment
  and contact forms, social profiles.
* **Contact** -- discoverable email addresses, for outreach.

Two deliberate design choices:

1. **Heuristics are explicit and cheap.** No headless browser is used, so an
   audit is one HTTP request plus a handful of sampled asset probes. Where a
   heuristic is necessarily approximate (spacing, clutter) the field is named
   so and can be overridden by real Lighthouse data -- see
   :func:`merge_pagespeed_signals`.
2. **Everything is best-effort.** A site that times out yields an audit with
   ``reachable=False`` rather than an exception, because "their site is down"
   is itself an extremely strong sales signal.
"""

from __future__ import annotations

import re
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from config import AuditSettings
from http_client import HttpClient, HttpError, SSLVerificationError
from logging_setup import get_logger
from models import PageSpeedResult, WebsiteAudit

logger = get_logger("audit")


# ---------------------------------------------------------------------------
# Signature tables
#
# Each entry maps a fingerprint that appears in page source onto a
# human-readable provider name. They are intentionally data, not code, so new
# vendors can be added without touching the detection logic.
# ---------------------------------------------------------------------------

BOOKING_SIGNATURES: Dict[str, str] = {
    "calendly.com": "Calendly",
    "acuityscheduling.com": "Acuity Scheduling",
    "squareup.com/appointments": "Square Appointments",
    "setmore.com": "Setmore",
    "simplybook.": "SimplyBook.me",
    "mindbodyonline.com": "Mindbody",
    "vagaro.com": "Vagaro",
    "booksy.com": "Booksy",
    "zocdoc.com": "Zocdoc",
    "opentable.com": "OpenTable",
    "resy.com": "Resy",
    "sevenrooms.com": "SevenRooms",
    "housecallpro.com": "Housecall Pro",
    "getjobber.com": "Jobber",
    "servicetitan.com": "ServiceTitan",
    "appointmentcore": "AppointmentCore",
    "youcanbook.me": "YouCanBook.me",
    "cal.com": "Cal.com",
    "bookeo.com": "Bookeo",
    "schedulicity.com": "Schedulicity",
    "timify.com": "Timify",
    "janeapp.com": "Jane",
    "dentrixascend": "Dentrix",
}

ORDERING_SIGNATURES: Dict[str, str] = {
    "doordash.com": "DoorDash",
    "ubereats.com": "Uber Eats",
    "grubhub.com": "Grubhub",
    "postmates.com": "Postmates",
    "toasttab.com": "Toast",
    "chownow.com": "ChowNow",
    "slicelife.com": "Slice",
    "olo.com": "Olo",
    "square.site": "Square Online",
    "menufy.com": "Menufy",
    "clover.com/online-ordering": "Clover",
    "shopify.com": "Shopify",
    "woocommerce": "WooCommerce",
    "snapfinger": "Snapfinger",
    "deliveroo.": "Deliveroo",
    "justeat.": "Just Eat",
}

CHAT_SIGNATURES: Dict[str, str] = {
    "intercom.io": "Intercom",
    "widget.intercom": "Intercom",
    "drift.com": "Drift",
    "tawk.to": "Tawk.to",
    "crisp.chat": "Crisp",
    "livechatinc.com": "LiveChat",
    "zdassets.com": "Zendesk Chat",
    "zopim.com": "Zendesk Chat",
    "hs-scripts.com": "HubSpot",
    "tidio.co": "Tidio",
    "freshchat.com": "Freshchat",
    "olark.com": "Olark",
    "chatra.io": "Chatra",
    "gorgias.chat": "Gorgias",
    "podium.com": "Podium",
    "smartsupp.com": "Smartsupp",
    "jivosite.com": "JivoChat",
    "tiledesk": "Tiledesk",
}

SOCIAL_DOMAINS: Dict[str, str] = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest",
    "yelp.com": "yelp",
    "threads.net": "threads",
    "nextdoor.com": "nextdoor",
}

CMS_SIGNATURES: Dict[str, str] = {
    "wp-content": "WordPress",
    "wp-includes": "WordPress",
    "wix.com": "Wix",
    "wixstatic.com": "Wix",
    "squarespace.com": "Squarespace",
    "static1.squarespace": "Squarespace",
    "shopify": "Shopify",
    "cdn.shopify.com": "Shopify",
    "godaddysites.com": "GoDaddy Website Builder",
    "weebly.com": "Weebly",
    "webflow.com": "Webflow",
    "joomla": "Joomla",
    "drupal": "Drupal",
    "duda.co": "Duda",
    "bigcommerce": "BigCommerce",
    "hubspot": "HubSpot CMS",
    "framer.website": "Framer",
    "carrd.co": "Carrd",
}

ANALYTICS_SIGNATURES: Tuple[str, ...] = (
    "googletagmanager.com",
    "google-analytics.com",
    "gtag(",
    "connect.facebook.net",
    "clarity.ms",
    "hotjar.com",
    "plausible.io",
    "matomo",
    "segment.com",
)

FORM_EMBED_SIGNATURES: Tuple[str, ...] = (
    "typeform.com",
    "jotform.com",
    "wufoo.com",
    "gravityforms",
    "wpcf7",
    "contact-form-7",
    "formspree.io",
    "hsforms.net",
    "google.com/forms",
    "cognitoforms.com",
    "formstack.com",
    "ninja-forms",
)

#: Markup that dates a site. Presence of any of these is a strong "outdated"
#: signal that also gives the sales pitch something concrete to point at.
LEGACY_TAGS: Tuple[str, ...] = (
    "font",
    "center",
    "marquee",
    "blink",
    "frameset",
    "frame",
    "applet",
    "big",
    "strike",
    "tt",
)

LEGACY_ATTRIBUTES: Tuple[str, ...] = (
    "bgcolor",
    "cellpadding",
    "cellspacing",
    "valign",
    "hspace",
    "vspace",
    "background",
)

SEMANTIC_TAGS: Tuple[str, ...] = (
    "header",
    "nav",
    "main",
    "section",
    "article",
    "aside",
    "footer",
    "figure",
)

CTA_PATTERNS: Tuple[str, ...] = (
    "book", "booking", "schedule", "appointment", "reserve", "reservation",
    "contact us", "get in touch", "get a quote", "request a quote", "free quote",
    "free estimate", "get started", "sign up", "signup", "start now",
    "call now", "call us", "order now", "order online", "buy now", "shop now",
    "learn more", "request info", "request a demo", "book a demo", "enquire",
    "inquire", "consultation", "apply now", "join now", "subscribe",
    "make an appointment", "talk to us", "message us",
)

APPOINTMENT_KEYWORDS: Tuple[str, ...] = (
    "appointment", "booking", "schedule", "reservation", "consultation",
    "preferred date", "preferred time",
)

EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

#: Substrings that mark an "email" as a false positive (asset filenames,
#: vendor placeholders, tracking addresses).
EMAIL_BLOCKLIST: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js",
    "example.com", "domain.com", "yourdomain", "email.com", "sentry.io",
    "wixpress.com", "godaddy.com", "@2x", "@3x", "sentry-next",
    ".wpengine", "core-js", "npmjs",
)

FONT_SIZE_PATTERN = re.compile(
    r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem)", re.IGNORECASE
)
FONT_FAMILY_PATTERN = re.compile(r"font-family\s*:\s*([^;{}\"']+)", re.IGNORECASE)
JQUERY_VERSION_PATTERN = re.compile(r"jquery[.-](\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
COPYRIGHT_YEAR_PATTERN = re.compile(r"(?:©|&copy;|copyright)[^0-9]{0,20}(\d{4})", re.IGNORECASE)

#: Sites are considered "stale" if the newest year in the footer is this many
#: years behind the current one.
STALE_COPYRIGHT_YEARS = 2

#: Above this composite score (0-100) a site is reported as looking outdated.
OUTDATED_THRESHOLD = 30


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Add a scheme when missing and strip fragments/whitespace."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _bare_host(url: str) -> str:
    """Hostname without the ``www.`` prefix, lowercased."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _same_host(a: str, b: str) -> bool:
    try:
        return _bare_host(a) == _bare_host(b)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# The auditor
# ---------------------------------------------------------------------------


class WebsiteAuditor:
    """Audits websites. Safe to share across threads."""

    def __init__(self, settings: AuditSettings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http
        self._robots_cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._robots_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def audit(self, url: str) -> WebsiteAudit:
        """Audit a single website. Never raises."""
        normalized = normalize_url(url)
        audit = WebsiteAudit(url=normalized)
        if not normalized:
            audit.error = "no URL supplied"
            return audit

        try:
            self._audit_inner(audit)
        except Exception as exc:  # pragma: no cover - defensive catch-all
            logger.error("Unexpected error auditing %s: %s", normalized, exc, exc_info=True)
            audit.error = audit.error or f"{type(exc).__name__}: {exc}"

        _finalize(audit)
        return audit

    def audit_many(
        self,
        urls: Sequence[str],
        progress: Any = None,
    ) -> Dict[str, WebsiteAudit]:
        """Audit many websites concurrently, keyed by the original URL."""
        results: Dict[str, WebsiteAudit] = {}
        unique = list(dict.fromkeys(u for u in urls if u))
        if not unique:
            return results

        workers = max(1, min(self.settings.max_workers, len(unique)))
        logger.info("Auditing %d website(s) with %d worker(s)", len(unique), workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.audit, url): url for url in unique}
            for future in as_completed(futures):
                original = futures[future]
                try:
                    results[original] = future.result()
                except Exception as exc:  # pragma: no cover - audit() swallows
                    logger.error("Audit worker failed for %s: %s", original, exc)
                    results[original] = WebsiteAudit(url=original, error=str(exc))
                finally:
                    if progress is not None:
                        progress.update(1)

        return results

    # ------------------------------------------------------------------
    # Fetch + parse
    # ------------------------------------------------------------------

    def _audit_inner(self, audit: WebsiteAudit) -> None:
        response = self._fetch_homepage(audit)
        if response is None:
            return

        html = response.text or ""
        audit.page_size_kb = round(len(response.content or b"") / 1024.0, 1)
        soup = BeautifulSoup(html[: self.settings.max_html_bytes], "lxml")

        self._check_transport(audit, response)
        self._check_head(audit, soup)
        self._check_markup(audit, soup, html)
        self._check_typography(audit, soup, html)
        self._check_navigation_and_cta(audit, soup)
        self._check_forms(audit, soup, html)
        self._check_business_capabilities(audit, soup, html)
        self._check_technology(audit, html)
        self._collect_emails(audit, soup, html)
        self._check_assets(audit, soup)

        if self.settings.follow_contact_pages and not audit.emails:
            self._scan_contact_pages(audit, soup)

    def _fetch_homepage(self, audit: WebsiteAudit) -> Optional[Any]:
        """Fetch the homepage, tolerating a broken certificate.

        On a TLS failure we record the certificate problem (a genuine finding)
        and retry once with verification disabled so the rest of the audit can
        still run. Nothing is submitted to the site, so the reduced trust only
        affects data we are reading.
        """
        try:
            response = self.http.get(audit.url, timeout=self.settings.timeout_seconds)
        except SSLVerificationError as exc:
            audit.ssl_error = True
            audit.ssl_error_detail = str(exc)[:300]
            logger.debug("SSL problem on %s; retrying without verification", audit.url)
            try:
                response = self.http.get(
                    audit.url,
                    timeout=self.settings.timeout_seconds,
                    verify=False,
                )
            except HttpError as retry_exc:
                audit.error = f"SSL error and retry failed: {retry_exc}"
                return None
        except HttpError as exc:
            audit.error = str(exc)[:300]
            audit.status_code = exc.status_code
            return None

        audit.status_code = response.status_code
        audit.final_url = response.url or audit.url
        elapsed = getattr(response, "elapsed", None)
        if elapsed is not None:
            audit.response_time_ms = round(elapsed.total_seconds() * 1000.0, 1)

        if response.status_code >= 400:
            audit.error = f"HTTP {response.status_code}"
            return None

        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type:
            audit.error = f"non-HTML content type: {content_type}"
            return None

        audit.reachable = True
        return response

    # ------------------------------------------------------------------
    # Individual check groups
    # ------------------------------------------------------------------

    def _check_transport(self, audit: WebsiteAudit, response: Any) -> None:
        """HTTPS availability and http -> https redirection."""
        final = audit.final_url or audit.url
        audit.https = final.lower().startswith("https://") and not audit.ssl_error

        if not audit.https:
            return

        # Confirm the plain-http origin redirects to https. We only claim
        # "no redirect" when the check actually succeeded -- an unreachable
        # http origin is not evidence of a misconfiguration.
        parsed = urlparse(final)
        http_origin = urlunparse(("http", parsed.netloc, parsed.path or "/", "", "", ""))
        try:
            probe = self.http.head(
                http_origin,
                timeout=min(self.settings.timeout_seconds, 8.0),
                allow_redirects=True,
                verify=False,
            )
        except Exception:
            # An unreachable http:// origin tells us nothing either way.
            return
        if probe.status_code < 400:
            audit.redirects_http_to_https = str(probe.url).lower().startswith("https://")
            if not audit.redirects_http_to_https:
                audit.issues.append("HTTP traffic is not redirected to HTTPS")

    def _check_head(self, audit: WebsiteAudit, soup: BeautifulSoup) -> None:
        """Title, meta description, favicon, viewport, Open Graph, language."""
        title_tag = soup.find("title")
        audit.title = (title_tag.get_text(strip=True) if title_tag else "")[:300]
        audit.has_title = bool(audit.title)

        description = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if description is None:
            description = soup.find("meta", attrs={"property": "og:description"})
        content = (description.get("content") if description else "") or ""
        audit.meta_description = content.strip()[:500]
        audit.has_meta_description = len(audit.meta_description) >= 20

        audit.has_favicon = bool(
            soup.find("link", rel=lambda v: bool(v) and "icon" in " ".join(
                v if isinstance(v, list) else [v]
            ).lower())
        )
        if not audit.has_favicon:
            # Many sites ship /favicon.ico without declaring a <link>.
            favicon_url = urljoin(audit.final_url or audit.url, "/favicon.ico")
            status, _ = self.http.probe(favicon_url, timeout=6.0)
            audit.has_favicon = status is not None and status < 400

        viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
        audit.has_viewport_meta = viewport is not None
        audit.mobile_responsive = audit.has_viewport_meta

        audit.has_open_graph = soup.find("meta", attrs={"property": "og:title"}) is not None

        html_tag = soup.find("html")
        if html_tag is not None:
            audit.language = (html_tag.get("lang") or "")[:16]

    def _check_markup(self, audit: WebsiteAudit, soup: BeautifulSoup, html: str) -> None:
        """HTML5 doctype, semantic structure and legacy markup detection."""
        head = html[:2000].lower()
        audit.html5_doctype = bool(re.search(r"<!doctype\s+html\s*>", head))

        audit.semantic_tag_count = sum(len(soup.find_all(tag)) for tag in SEMANTIC_TAGS)

        markers: List[str] = []
        for tag in LEGACY_TAGS:
            if soup.find(tag) is not None:
                markers.append(f"<{tag}> tag")
        for attribute in LEGACY_ATTRIBUTES:
            if soup.find(attrs={attribute: True}) is not None:
                markers.append(f"{attribute} attribute")

        # Table-based layout: several nested tables and no semantic landmarks.
        tables = soup.find_all("table")
        if len(tables) >= 3 and audit.semantic_tag_count == 0:
            markers.append("table-based layout")
        if soup.find("object") is not None or soup.find("embed") is not None:
            if re.search(r"\.swf|shockwave|x-shockwave-flash", html, re.IGNORECASE):
                markers.append("Flash content")

        audit.legacy_markers = markers
        audit.modern_html = (
            audit.html5_doctype
            and audit.semantic_tag_count >= 2
            and len(markers) <= 1
        )

        text_length = len(soup.get_text(" ", strip=True))
        audit.dom_node_count = len(soup.find_all(True))
        audit.text_to_html_ratio = round(text_length / max(len(html), 1), 4)

    def _check_typography(self, audit: WebsiteAudit, soup: BeautifulSoup, html: str) -> None:
        """Approximate typography quality from inline styles and <style> blocks.

        This only sees CSS present in the HTML document -- external stylesheets
        are not fetched. It is therefore a *lower bound*: real Lighthouse data
        from :func:`merge_pagespeed_signals` overrides it when available.
        """
        style_text = " ".join(tag.get_text(" ") for tag in soup.find_all("style"))
        inline_styles = [
            str(tag.get("style")) for tag in soup.find_all(style=True)
        ]
        audit.inline_style_count = len(inline_styles)
        css_blob = style_text + " " + " ".join(inline_styles)

        tiny = 0
        for value, unit in FONT_SIZE_PATTERN.findall(css_blob):
            try:
                size = float(value)
            except ValueError:
                continue
            unit = unit.lower()
            if (unit == "px" and size < 12) or (unit == "pt" and size < 9):
                tiny += 1
            elif unit in {"em", "rem"} and size < 0.75:
                tiny += 1
        audit.tiny_text_declarations = tiny
        audit.tiny_text = tiny >= 3

        families = set()
        for match in FONT_FAMILY_PATTERN.findall(css_blob):
            first = match.split(",")[0].strip().strip("\"'").lower()
            if first and not first.startswith("var("):
                families.add(first)
        audit.font_family_count = len(families)

    def _check_navigation_and_cta(self, audit: WebsiteAudit, soup: BeautifulSoup) -> None:
        """Navigation structure and presence of a clear call to action."""
        nav_elements = soup.find_all("nav")
        nav_links: List[Any] = []
        for nav in nav_elements:
            nav_links.extend(nav.find_all("a"))
        if not nav_elements:
            # Fall back to a header/menu container when <nav> is not used.
            for container in soup.find_all(
                attrs={"class": re.compile(r"(nav|menu|header)", re.I)}
            ):
                nav_links.extend(container.find_all("a"))

        audit.has_nav = bool(nav_elements) or bool(nav_links)
        audit.nav_link_count = len(nav_links)
        # Too few links means nothing to navigate; too many means a wall of
        # links that visitors have to read rather than scan.
        audit.poor_navigation = (not audit.has_nav) or audit.nav_link_count > 25

        candidates: List[str] = []
        for tag in soup.find_all(["a", "button"]):
            text = tag.get_text(" ", strip=True)
            if text:
                candidates.append(text)
        for tag in soup.find_all("input", attrs={"type": re.compile("^(submit|button)$", re.I)}):
            value = tag.get("value")
            if value:
                candidates.append(str(value))

        matches: List[str] = []
        for text in candidates:
            lowered = text.lower()
            if len(lowered) > 60:
                continue
            if any(pattern in lowered for pattern in CTA_PATTERNS):
                matches.append(text.strip())

        audit.has_cta = bool(matches)
        # Keep a few examples so the outreach email can quote them (or note
        # their absence) without re-fetching the page.
        audit.cta_samples = list(dict.fromkeys(matches))[:5]

        audit.has_phone_link = soup.find(
            "a", href=lambda v: bool(v) and str(v).lower().startswith("tel:")
        ) is not None

    def _check_forms(self, audit: WebsiteAudit, soup: BeautifulSoup, html: str) -> None:
        """Contact and appointment form detection."""
        lowered_html = html.lower()

        has_form = False
        appointment_form = False
        for form in soup.find_all("form"):
            inputs = form.find_all(["input", "textarea", "select"])
            has_email_field = any(
                (inp.get("type") or "").lower() == "email"
                or "email" in (inp.get("name") or "").lower()
                or "email" in (inp.get("id") or "").lower()
                for inp in inputs
            )
            has_textarea = form.find("textarea") is not None
            if has_email_field or has_textarea:
                has_form = True

            form_text = form.get_text(" ", strip=True).lower()
            has_date_field = any(
                (inp.get("type") or "").lower() in {"date", "datetime-local", "time"}
                for inp in inputs
            )
            if has_date_field or any(k in form_text for k in APPOINTMENT_KEYWORDS):
                appointment_form = True

        if not has_form:
            has_form = any(sig in lowered_html for sig in FORM_EMBED_SIGNATURES)

        audit.has_contact_form = has_form
        audit.has_appointment_form = appointment_form

    def _check_business_capabilities(
        self, audit: WebsiteAudit, soup: BeautifulSoup, html: str
    ) -> None:
        """Booking, ordering, chat, WhatsApp and social profiles."""
        lowered = html.lower()

        audit.booking_providers = _match_signatures(lowered, BOOKING_SIGNATURES)
        if not audit.booking_providers:
            # Self-hosted booking pages are common; look for the route too.
            if re.search(r'href=["\'][^"\']*/(book|booking|appointments?|schedule)\b', lowered):
                audit.booking_providers = ["on-site booking page"]
        audit.has_booking = bool(audit.booking_providers) or audit.has_appointment_form

        audit.ordering_providers = _match_signatures(lowered, ORDERING_SIGNATURES)
        if not audit.ordering_providers:
            if re.search(r'href=["\'][^"\']*/(order-online|order|cart|checkout)\b', lowered):
                audit.ordering_providers = ["on-site ordering page"]
        audit.has_online_ordering = bool(audit.ordering_providers)

        audit.chat_providers = _match_signatures(lowered, CHAT_SIGNATURES)
        audit.has_chat = bool(audit.chat_providers)

        audit.has_whatsapp = bool(
            re.search(r"(wa\.me/|api\.whatsapp\.com|whatsapp://)", lowered)
        )

        social: Dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            host = urlparse(href).netloc.lower()
            if not host:
                continue
            for domain, network in SOCIAL_DOMAINS.items():
                if domain in host and network not in social:
                    social[network] = href
                    break
        audit.social_links = social

    def _check_technology(self, audit: WebsiteAudit, html: str) -> None:
        """CMS, framework and analytics fingerprinting."""
        lowered = html.lower()

        for signature, name in CMS_SIGNATURES.items():
            if signature in lowered:
                audit.cms = name
                break

        technologies: List[str] = []
        if audit.cms:
            technologies.append(audit.cms)

        jquery = JQUERY_VERSION_PATTERN.search(lowered)
        if jquery:
            major = int(jquery.group(1))
            version = ".".join(part for part in jquery.groups() if part)
            technologies.append(f"jQuery {version}")
            if major < 3:
                audit.legacy_markers.append(f"jQuery {version} (end of life)")
        elif "jquery" in lowered:
            technologies.append("jQuery")

        for marker, label in (
            ("bootstrap", "Bootstrap"),
            ("react", "React"),
            ("vue", "Vue"),
            ("angular", "Angular"),
            ("next/static", "Next.js"),
            ("_nuxt/", "Nuxt"),
            ("tailwind", "Tailwind CSS"),
            ("elementor", "Elementor"),
            ("divi", "Divi"),
        ):
            if marker in lowered and label not in technologies:
                technologies.append(label)

        audit.technologies = technologies
        audit.has_analytics = any(sig in lowered for sig in ANALYTICS_SIGNATURES)

        years = [int(y) for y in COPYRIGHT_YEAR_PATTERN.findall(html) if y.isdigit()]
        plausible = [y for y in years if 1995 <= y <= datetime.now().year + 1]
        if plausible:
            audit.copyright_year = max(plausible)

    def _collect_emails(self, audit: WebsiteAudit, soup: BeautifulSoup, html: str) -> None:
        """Harvest publicly-listed contact email addresses."""
        found: List[str] = []

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if href.lower().startswith("mailto:"):
                address = href[7:].split("?")[0].strip()
                if address:
                    found.append(address)

        # Lightly de-obfuscate the common "name (at) domain dot com" pattern.
        deobfuscated = re.sub(r"\s*[\(\[]\s*at\s*[\)\]]\s*", "@", html, flags=re.IGNORECASE)
        deobfuscated = re.sub(
            r"\s*[\(\[]\s*dot\s*[\)\]]\s*", ".", deobfuscated, flags=re.IGNORECASE
        )
        found.extend(EMAIL_PATTERN.findall(deobfuscated))

        audit.emails = _clean_emails(found)

    def _check_assets(self, audit: WebsiteAudit, soup: BeautifulSoup) -> None:
        """Sample images and links, probing them for 4xx/5xx responses."""
        base = audit.final_url or audit.url

        images = soup.find_all("img")
        audit.images_total = len(images)
        audit.images_missing_alt = sum(
            1 for img in images if not (img.get("alt") or "").strip()
        )

        image_urls: List[str] = []
        for img in images:
            src = img.get("src") or img.get("data-src") or ""
            src = str(src).strip()
            if not src or src.startswith("data:"):
                continue
            image_urls.append(urljoin(base, src))
        image_urls = list(dict.fromkeys(image_urls))[: self.settings.max_images_to_check]

        anchors = soup.find_all("a", href=True)
        audit.links_total = len(anchors)
        link_urls: List[str] = []
        for anchor in anchors:
            href = str(anchor["href"]).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            absolute = urljoin(base, href)
            if not absolute.lower().startswith(("http://", "https://")):
                continue
            link_urls.append(absolute)
        link_urls = list(dict.fromkeys(link_urls))[: self.settings.max_links_to_check]

        audit.images_checked = len(image_urls)
        audit.links_checked = len(link_urls)
        if not image_urls and not link_urls:
            return

        targets = [("image", u) for u in image_urls] + [("link", u) for u in link_urls]
        workers = max(1, min(self.settings.asset_check_workers, len(targets)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.http.probe, url, 8.0): (kind, url)
                for kind, url in targets
            }
            for future in as_completed(futures):
                kind, url = futures[future]
                try:
                    status, _ = future.result()
                except Exception:  # pragma: no cover - probe swallows its own
                    status = None
                # A probe that could not complete is inconclusive (the host may
                # simply block HEAD from unknown clients), so only a definite
                # 4xx/5xx counts as broken.
                if status is not None and status >= 400:
                    if kind == "image":
                        audit.broken_images += 1
                    else:
                        audit.broken_links += 1

    def _scan_contact_pages(self, audit: WebsiteAudit, soup: BeautifulSoup) -> None:
        """Look for an email address on the site's contact/about pages."""
        base = audit.final_url or audit.url
        candidates: List[str] = []

        # Prefer links the site itself labels as contact pages.
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True).lower()
            href = str(anchor["href"])
            if any(word in text for word in ("contact", "get in touch", "about")):
                absolute = urljoin(base, href)
                if _same_host(absolute, base):
                    candidates.append(absolute)

        candidates.extend(urljoin(base, path) for path in self.settings.contact_paths)
        candidates = list(dict.fromkeys(candidates))[:3]

        for url in candidates:
            if not self._robots_allows(url):
                logger.debug("robots.txt disallows %s; skipping", url)
                continue
            try:
                response = self.http.get(url, timeout=min(self.settings.timeout_seconds, 10.0))
            except HttpError:
                continue
            if response.status_code >= 400:
                continue
            page = BeautifulSoup(response.text[: self.settings.max_html_bytes], "lxml")
            found: List[str] = []
            for anchor in page.find_all("a", href=True):
                href = str(anchor["href"])
                if href.lower().startswith("mailto:"):
                    found.append(href[7:].split("?")[0].strip())
            found.extend(EMAIL_PATTERN.findall(response.text))
            emails = _clean_emails(found)
            if emails:
                audit.emails = emails
                if not audit.has_contact_form:
                    audit.has_contact_form = page.find("form") is not None
                return

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------

    def _robots_allows(self, url: str) -> bool:
        """Whether ``robots.txt`` permits fetching *url*.

        Only applied to sub-page crawling. The homepage is always fetched:
        loading a business's public front page once is equivalent to a single
        browser visit, which is exactly what Lighthouse and PageSpeed do.
        """
        if not self.settings.respect_robots:
            return True

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        with self._robots_lock:
            if origin in self._robots_cache:
                parser = self._robots_cache[origin]
            else:
                parser = self._load_robots(origin)
                self._robots_cache[origin] = parser

        if parser is None:
            return True  # No usable robots.txt means no restrictions.
        user_agent = self.http.session.headers.get("User-Agent", "*")
        try:
            return parser.can_fetch(str(user_agent), url)
        except Exception:  # pragma: no cover - malformed robots data
            return True

    def _load_robots(self, origin: str) -> Optional[urllib.robotparser.RobotFileParser]:
        try:
            response = self.http.get(
                f"{origin}/robots.txt", timeout=6.0, retry_on_status=False
            )
        except Exception:
            return None
        if response.status_code >= 400:
            return None
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(response.text.splitlines())
        except Exception:  # pragma: no cover - malformed robots data
            return None
        return parser


# ---------------------------------------------------------------------------
# Derived verdicts
# ---------------------------------------------------------------------------


def _finalize(audit: WebsiteAudit) -> None:
    """Compute composite verdicts and the human-readable issue list.

    Safe to call more than once: the issue list is rebuilt from scratch each
    time, so :func:`merge_pagespeed_signals` can refresh it after adding real
    Lighthouse data.
    """
    preserved = [i for i in audit.issues if i == "HTTP traffic is not redirected to HTTPS"]
    audit.issues = []

    if not audit.reachable:
        audit.issues.append(
            f"Website could not be loaded ({audit.error or 'unknown error'})"
        )
        audit.looks_outdated = False
        audit.outdated_score = 0
        return

    # --- Composite UX verdicts ---
    #
    # These are OR-ed with their current value rather than assigned, because
    # merge_pagespeed_signals() sets some of them from real Lighthouse audits
    # before calling back into here. A verdict that Lighthouse has already
    # established must not be undone by the weaker HTML-only heuristic.
    audit.poor_typography = (
        audit.poor_typography or audit.tiny_text or audit.font_family_count > 4
    )
    audit.cluttered_layout = (
        audit.cluttered_layout
        or audit.dom_node_count > 2500
        or audit.links_total > 120
        or audit.images_total > 80
    )
    # Spacing cannot be measured without rendering. This proxy fires when the
    # page is markup-heavy relative to its content, or leans on inline styles
    # instead of a stylesheet -- both correlate strongly with cramped layouts.
    audit.bad_spacing = (
        audit.bad_spacing
        or (audit.text_to_html_ratio < 0.03 and audit.dom_node_count > 400)
        or audit.inline_style_count > 60
    )

    # --- Outdated score (0-100) ---
    score = 0
    current_year = datetime.now().year

    if not audit.https:
        score += 15
    if not audit.mobile_responsive:
        score += 20
    if not audit.html5_doctype:
        score += 10
    if audit.semantic_tag_count == 0:
        score += 10
    score += min(len(audit.legacy_markers) * 5, 20)
    if audit.copyright_year and audit.copyright_year < current_year - STALE_COPYRIGHT_YEARS:
        score += 10
    if audit.poor_typography:
        score += 5
    if audit.cluttered_layout:
        score += 5
    if audit.images_checked and audit.broken_images / audit.images_checked > 0.2:
        score += 5

    audit.outdated_score = min(score, 100)
    audit.looks_outdated = audit.outdated_score >= OUTDATED_THRESHOLD

    # --- Issue list (this is what lands in the spreadsheet) ---
    issues: List[str] = list(preserved)
    if not audit.https:
        issues.append("No HTTPS / insecure connection")
    if audit.ssl_error:
        issues.append("SSL certificate error")
    if not audit.mobile_responsive:
        issues.append("Not mobile responsive (no viewport meta tag)")
    if not audit.has_title:
        issues.append("Missing page title")
    if not audit.has_meta_description:
        issues.append("Missing meta description")
    if not audit.has_favicon:
        issues.append("Missing favicon")
    if not audit.modern_html:
        issues.append("Outdated HTML structure")
    if audit.legacy_markers:
        issues.append("Legacy markup: " + ", ".join(audit.legacy_markers[:3]))
    if audit.looks_outdated:
        issues.append(f"Design looks dated (score {audit.outdated_score}/100)")
    if audit.broken_images:
        issues.append(f"{audit.broken_images} broken image(s)")
    if audit.broken_links:
        issues.append(f"{audit.broken_links} broken link(s)")
    if audit.images_total and audit.images_missing_alt:
        issues.append(f"{audit.images_missing_alt} image(s) missing alt text")
    if audit.tiny_text:
        issues.append("Tiny text / poor legibility")
    if audit.poor_typography:
        issues.append("Inconsistent typography")
    if audit.bad_spacing:
        issues.append("Cramped spacing / layout")
    if audit.cluttered_layout:
        issues.append("Cluttered layout")
    if audit.poor_navigation:
        issues.append("Poor navigation structure")
    if not audit.has_cta:
        issues.append("No clear call to action")
    if not audit.has_contact_form:
        issues.append("No contact form")
    if not audit.has_booking:
        issues.append("No online booking")
    if not audit.has_chat:
        issues.append("No live chat")
    if not audit.has_whatsapp:
        issues.append("No WhatsApp contact")
    if not audit.social_links:
        issues.append("No social media links")
    if not audit.has_analytics:
        issues.append("No analytics tracking detected")
    if audit.copyright_year and audit.copyright_year < current_year - STALE_COPYRIGHT_YEARS:
        issues.append(f"Copyright notice stuck at {audit.copyright_year}")

    audit.issues = list(dict.fromkeys(issues))


def merge_pagespeed_signals(
    audit: WebsiteAudit,
    pagespeed: Optional[PageSpeedResult],
) -> WebsiteAudit:
    """Override heuristic UX verdicts with real Lighthouse audit results.

    The HTML-only heuristics in this module cannot see external stylesheets or
    rendered layout. When PageSpeed data is available it is authoritative, so
    this promotes the Lighthouse verdicts and recomputes the derived fields.

    Returns the same (mutated) ``audit`` for convenient chaining.
    """
    if pagespeed is None or not audit.reachable:
        return audit

    metrics = pagespeed.primary
    if metrics is None or metrics.error:
        return audit

    audits = metrics.audits

    def verdict(name: str) -> Optional[bool]:
        return audits.get(name)

    viewport = verdict("viewport")
    if viewport is not None:
        audit.mobile_responsive = viewport

    https_audit = verdict("is-on-https")
    if https_audit is not None and not audit.ssl_error:
        audit.https = https_audit

    font_size = verdict("font-size")
    if font_size is not None and font_size is False:
        audit.tiny_text = True

    contrast = verdict("color-contrast")
    if contrast is False:
        audit.poor_typography = True

    tap_targets = verdict("tap-targets")
    if tap_targets is False:
        audit.bad_spacing = True

    link_text = verdict("link-text")
    crawlable = verdict("crawlable-anchors")
    if link_text is False or crawlable is False:
        audit.poor_navigation = True

    if verdict("meta-description") is False:
        audit.has_meta_description = False
    if verdict("document-title") is False:
        audit.has_title = False

    if metrics.dom_size and metrics.dom_size > audit.dom_node_count:
        audit.dom_node_count = int(metrics.dom_size)

    if verdict("legacy-javascript") is False and "legacy JavaScript" not in audit.legacy_markers:
        audit.legacy_markers.append("legacy JavaScript bundles")

    _finalize(audit)
    return audit


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _match_signatures(haystack: str, table: Dict[str, str]) -> List[str]:
    """Return the distinct provider names whose signature appears in *haystack*."""
    found: List[str] = []
    for signature, name in table.items():
        if signature in haystack and name not in found:
            found.append(name)
    return found


def _clean_emails(candidates: Sequence[str]) -> List[str]:
    """De-duplicate, validate and rank discovered email addresses."""
    seen: Set[str] = set()
    cleaned: List[str] = []
    for raw in candidates:
        address = raw.strip().strip(".,;:<>()[]\"'").lower()
        if not address or "@" not in address:
            continue
        if any(bad in address for bad in EMAIL_BLOCKLIST):
            continue
        if len(address) > 100 or address.count("@") != 1:
            continue
        local, _, domain = address.partition("@")
        if not local or "." not in domain:
            continue
        if address in seen:
            continue
        seen.add(address)
        cleaned.append(address)

    # Prefer role addresses a business actually monitors.
    priority = ("info@", "contact@", "hello@", "office@", "enquiries@", "admin@", "sales@")

    def rank(address: str) -> int:
        for index, prefix in enumerate(priority):
            if address.startswith(prefix):
                return index
        return len(priority)

    cleaned.sort(key=rank)
    return cleaned[:5]
