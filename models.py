"""Typed data structures shared across the pipeline.

The pipeline is a straight line of transformations, and each stage owns one
dataclass:

``google_places``  -> :class:`Place`
``website_audit``  -> :class:`WebsiteAudit`
``pagespeed``      -> :class:`PageSpeedResult`
``lead_scoring``   -> :class:`LeadScore`
``ai_summary``     -> :class:`AISummary`

All of them are collected onto a :class:`Lead`, which is what :mod:`export`
flattens into spreadsheet rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Stage 1 -- Google Places
# ---------------------------------------------------------------------------


@dataclass
class Place:
    """A single business as returned by the Google Places API."""

    place_id: str
    name: str
    category: str = ""
    types: List[str] = field(default_factory=list)
    address: str = ""
    phone: str = ""
    international_phone: str = ""
    website: str = ""
    rating: Optional[float] = None
    review_count: int = 0
    business_status: str = ""
    open_now: Optional[bool] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    maps_url: str = ""

    @property
    def has_website(self) -> bool:
        """True when Google lists a website that is not a social profile.

        A Facebook page or an Instagram bio link is a *social presence*, not a
        website -- treating it as one would hide some of the best leads.
        """
        if not self.website:
            return False
        lowered = self.website.lower()
        social_hosts = (
            "facebook.com",
            "fb.com",
            "instagram.com",
            "linkedin.com",
            "twitter.com",
            "x.com",
            "tiktok.com",
            "yelp.com",
            "linktr.ee",
            "business.site",  # Google Business "website" builder
            "sites.google.com",
        )
        return not any(host in lowered for host in social_hosts)

    @property
    def website_is_social_only(self) -> bool:
        """True when the only listed "website" is a social/profile page."""
        return bool(self.website) and not self.has_website

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Stage 2 -- PageSpeed Insights
# ---------------------------------------------------------------------------


@dataclass
class StrategyMetrics:
    """Lighthouse + CrUX metrics for one strategy (mobile or desktop)."""

    strategy: str
    performance_score: Optional[int] = None
    accessibility_score: Optional[int] = None
    best_practices_score: Optional[int] = None
    seo_score: Optional[int] = None

    # Core Web Vitals / lab metrics
    lcp_ms: Optional[float] = None
    cls: Optional[float] = None
    inp_ms: Optional[float] = None
    fcp_ms: Optional[float] = None
    tbt_ms: Optional[float] = None
    speed_index_ms: Optional[float] = None
    tti_ms: Optional[float] = None

    #: Whether INP came from real-user (CrUX) field data rather than the lab.
    inp_from_field_data: bool = False

    #: Selected pass/fail Lighthouse audits that feed the UX heuristics,
    #: e.g. ``{"viewport": True, "font-size": False}``.
    audits: Dict[str, Optional[bool]] = field(default_factory=dict)

    total_byte_weight: Optional[float] = None
    dom_size: Optional[float] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PageSpeedResult:
    """PageSpeed Insights results for one website, across all strategies."""

    url: str
    strategies: Dict[str, StrategyMetrics] = field(default_factory=dict)
    error: str = ""

    # -- convenience accessors used by scoring/export ------------------------

    @property
    def mobile(self) -> Optional[StrategyMetrics]:
        return self.strategies.get("mobile")

    @property
    def desktop(self) -> Optional[StrategyMetrics]:
        return self.strategies.get("desktop")

    @property
    def mobile_score(self) -> Optional[int]:
        return self.mobile.performance_score if self.mobile else None

    @property
    def desktop_score(self) -> Optional[int]:
        return self.desktop.performance_score if self.desktop else None

    @property
    def primary(self) -> Optional[StrategyMetrics]:
        """Mobile if available, otherwise whatever strategy we do have."""
        if self.mobile is not None:
            return self.mobile
        return next(iter(self.strategies.values()), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "error": self.error,
            "strategies": {k: v.to_dict() for k, v in self.strategies.items()},
        }


# ---------------------------------------------------------------------------
# Stage 3 -- Website audit
# ---------------------------------------------------------------------------


@dataclass
class WebsiteAudit:
    """Everything we can learn about a website without a headless browser."""

    url: str
    final_url: str = ""
    reachable: bool = False
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error: str = ""

    # --- Security / transport ---
    https: bool = False
    redirects_http_to_https: bool = False
    ssl_error: bool = False
    ssl_error_detail: str = ""

    # --- Head / metadata ---
    title: str = ""
    has_title: bool = False
    meta_description: str = ""
    has_meta_description: bool = False
    has_favicon: bool = False
    has_viewport_meta: bool = False
    has_open_graph: bool = False
    language: str = ""

    # --- Markup quality ---
    html5_doctype: bool = False
    semantic_tag_count: int = 0
    legacy_markers: List[str] = field(default_factory=list)
    modern_html: bool = False
    mobile_responsive: bool = False

    # --- Assets ---
    images_total: int = 0
    images_checked: int = 0
    broken_images: int = 0
    images_missing_alt: int = 0
    links_total: int = 0
    links_checked: int = 0
    broken_links: int = 0

    # --- Layout / typography heuristics ---
    dom_node_count: int = 0
    page_size_kb: float = 0.0
    text_to_html_ratio: float = 0.0
    tiny_text_declarations: int = 0
    font_family_count: int = 0
    inline_style_count: int = 0
    poor_typography: bool = False
    tiny_text: bool = False
    bad_spacing: bool = False
    cluttered_layout: bool = False

    # --- Conversion surface ---
    has_cta: bool = False
    cta_samples: List[str] = field(default_factory=list)
    has_contact_form: bool = False
    has_appointment_form: bool = False
    has_nav: bool = False
    nav_link_count: int = 0
    poor_navigation: bool = False
    has_phone_link: bool = False

    # --- Business capabilities ---
    has_booking: bool = False
    booking_providers: List[str] = field(default_factory=list)
    has_online_ordering: bool = False
    ordering_providers: List[str] = field(default_factory=list)
    has_chat: bool = False
    chat_providers: List[str] = field(default_factory=list)
    has_whatsapp: bool = False
    social_links: Dict[str, str] = field(default_factory=dict)

    # --- Contact discovery ---
    emails: List[str] = field(default_factory=list)

    # --- Tech fingerprint ---
    cms: str = ""
    technologies: List[str] = field(default_factory=list)
    has_analytics: bool = False
    copyright_year: Optional[int] = None

    # --- Verdicts ---
    outdated_score: int = 0
    looks_outdated: bool = False
    issues: List[str] = field(default_factory=list)

    @property
    def has_social_presence(self) -> bool:
        return bool(self.social_links)

    @property
    def primary_email(self) -> str:
        return self.emails[0] if self.emails else ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Stage 4 -- Scoring
# ---------------------------------------------------------------------------


@dataclass
class ScoreReason:
    """One scoring rule that fired, with its point value and explanation."""

    rule: str
    points: int
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LeadScore:
    """The 0-100 score for a lead plus the rules that produced it."""

    score: int = 0
    raw_score: int = 0
    priority: str = "Cold"
    reasons: List[ScoreReason] = field(default_factory=list)
    suggested_services: List[str] = field(default_factory=list)
    needs_website: bool = False

    @property
    def reason_summary(self) -> str:
        """Semicolon-joined explanations, ready for a spreadsheet cell."""
        return "; ".join(reason.detail for reason in self.reasons)

    @property
    def breakdown(self) -> str:
        """``"No website (+50); Few reviews (+5)"`` -- an auditable trail."""
        return "; ".join(f"{r.detail} (+{r.points})" for r in self.reasons)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "raw_score": self.raw_score,
            "priority": self.priority,
            "needs_website": self.needs_website,
            "suggested_services": list(self.suggested_services),
            "reasons": [r.to_dict() for r in self.reasons],
        }


# ---------------------------------------------------------------------------
# Stage 5 -- AI summary
# ---------------------------------------------------------------------------


@dataclass
class AISummary:
    """Sales-ready copy for one lead."""

    reason: str = ""
    suggested_services: List[str] = field(default_factory=list)
    email_subject: str = ""
    email_body: str = ""
    linkedin_message: str = ""
    #: ``"claude"`` or ``"template"`` -- useful when reviewing output quality.
    generator: str = "template"

    @property
    def email(self) -> str:
        """Subject + body as a single cell value."""
        if not self.email_body:
            return ""
        if self.email_subject:
            return f"Subject: {self.email_subject}\n\n{self.email_body}"
        return self.email_body

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The assembled lead
# ---------------------------------------------------------------------------


@dataclass
class Lead:
    """A business plus everything we discovered about it."""

    place: Place
    audit: Optional[WebsiteAudit] = None
    pagespeed: Optional[PageSpeedResult] = None
    score: LeadScore = field(default_factory=LeadScore)
    summary: AISummary = field(default_factory=AISummary)

    # -- convenience accessors ----------------------------------------------

    @property
    def name(self) -> str:
        return self.place.name

    @property
    def website(self) -> str:
        return self.place.website

    @property
    def email(self) -> str:
        """Best discoverable email address, if the audit found one."""
        return self.audit.primary_email if self.audit else ""

    @property
    def issues(self) -> List[str]:
        return list(self.audit.issues) if self.audit else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "place": self.place.to_dict(),
            "audit": self.audit.to_dict() if self.audit else None,
            "pagespeed": self.pagespeed.to_dict() if self.pagespeed else None,
            "score": self.score.to_dict(),
            "summary": self.summary.to_dict(),
        }
