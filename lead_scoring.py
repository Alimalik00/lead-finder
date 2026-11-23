"""Lead scoring: turn audit findings into a 0-100 score and a priority.

The scorer is a list of small, independent rules. Each one inspects the place,
the website audit and the PageSpeed result, and either fires (contributing
points and an explanation) or does not. The total is capped at
``ScoringWeights.max_score`` and bucketed into Hot / Warm / Cold.

Two properties matter more than the exact numbers:

* **Auditability.** Every point is attributable to a named rule, so
  :attr:`models.LeadScore.breakdown` can be pasted into a spreadsheet cell and
  defended in a sales conversation.
* **Tunability.** Weights and thresholds live in :class:`config.ScoringWeights`
  and can be edited in ``config.yaml`` without touching this file.

Rule of thumb behind the default weights: the score should approximate *how
much revenue is being left on the table*. A busy business with no website
scores highest; a quiet business with a decent site scores lowest.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from config import ScoringWeights
from logging_setup import get_logger
from models import (
    Lead,
    LeadScore,
    PageSpeedResult,
    Place,
    ScoreReason,
    WebsiteAudit,
)

logger = get_logger("scoring")

#: A rule receives the full context and returns ``(points, detail)`` when it
#: fires, or ``None`` when it does not apply.
RuleResult = Optional[Tuple[int, str]]


class LeadScorer:
    """Applies the scoring rules to a business."""

    def __init__(self, weights: Optional[ScoringWeights] = None) -> None:
        self.weights = weights or ScoringWeights()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        place: Place,
        audit: Optional[WebsiteAudit] = None,
        pagespeed: Optional[PageSpeedResult] = None,
    ) -> LeadScore:
        """Score one business and explain the result."""
        weights = self.weights
        reasons: List[ScoreReason] = []

        def fire(rule: str, points: int, detail: str) -> None:
            if points <= 0:
                return
            reasons.append(ScoreReason(rule=rule, points=points, detail=detail))

        has_website = place.has_website
        site_up = bool(audit and audit.reachable)

        # ------------------------------------------------------------------
        # 1. The single biggest signal: no website at all.
        # ------------------------------------------------------------------
        if not has_website:
            if place.website_is_social_only:
                detail = "No real website -- only a social media page is listed"
            else:
                detail = "No website listed on Google"
            fire("no_website", weights.no_website, detail)

            # A business with no website is, by definition, missing every
            # capability a website provides. Crediting those absences keeps the
            # score proportional to the work involved -- and keeps the scale
            # honest: without them a no-website lead could never reach Hot,
            # because none of the site-quality rules below can fire.
            fire(
                "no_cta",
                weights.no_cta,
                "No call to action anywhere online for searchers to act on",
            )
            fire(
                "no_contact_form",
                weights.no_contact_form,
                "No way to submit an enquiry online",
            )
            fire(
                "no_booking",
                weights.no_booking,
                "No online booking or appointment scheduling",
            )

        # ------------------------------------------------------------------
        # 2. There is a website, but it does not load.
        # ------------------------------------------------------------------
        elif audit is not None and not audit.reachable:
            fire(
                "site_unreachable",
                weights.site_unreachable,
                f"Website is listed but does not load ({audit.error or 'unreachable'})",
            )

        # ------------------------------------------------------------------
        # 3. Website exists and loads -- grade it.
        # ------------------------------------------------------------------
        if site_up and audit is not None:
            for rule_fn in self._site_rules():
                result = rule_fn(audit)
                if result is not None:
                    points, detail = result
                    fire(rule_fn.__name__.lstrip("_"), points, detail)

        # ------------------------------------------------------------------
        # 4. Performance rules (need PageSpeed data).
        # ------------------------------------------------------------------
        if pagespeed is not None and has_website:
            for points, detail, rule in self._performance_reasons(pagespeed):
                fire(rule, points, detail)

        # ------------------------------------------------------------------
        # 5. Demand signals from the Google listing itself.
        # ------------------------------------------------------------------
        if 0 < place.review_count < weights.few_reviews_threshold:
            fire(
                "few_reviews",
                weights.few_reviews,
                f"Only {place.review_count} Google review(s) -- weak online visibility",
            )

        # A busy business with a weak web presence is the most valuable lead in
        # the list: demand is already proven, only the conversion path is missing.
        weak_web_presence = (not has_website) or (audit is not None and audit.looks_outdated)
        if place.review_count >= weights.high_demand_reviews and weak_web_presence:
            fire(
                "high_demand_bonus",
                weights.high_demand_bonus,
                f"{place.review_count} reviews but a weak web presence -- "
                "proven demand, no digital front door",
            )

        if place.rating is not None and 0 < place.rating < weights.low_rating_threshold:
            fire(
                "low_rating_penalty",
                weights.low_rating_penalty,
                f"Rated {place.rating:.1f}/5 -- reputation management opportunity",
            )

        # ------------------------------------------------------------------
        # Total, cap, priority.
        # ------------------------------------------------------------------
        raw = sum(reason.points for reason in reasons)
        capped = min(raw, weights.max_score)

        result = LeadScore(
            score=capped,
            raw_score=raw,
            priority=self._priority(capped),
            reasons=reasons,
            needs_website=not has_website,
        )
        result.suggested_services = self.suggest_services(place, audit, pagespeed, result)

        logger.debug(
            "Scored %s: %d/%d (%s) from %d rule(s)",
            place.name, capped, weights.max_score, result.priority, len(reasons),
        )
        return result

    # ------------------------------------------------------------------
    # Rule definitions
    # ------------------------------------------------------------------

    def _site_rules(self) -> List[Callable[[WebsiteAudit], RuleResult]]:
        """Rules that apply to a website we successfully loaded."""
        weights = self.weights

        def _outdated_design(audit: WebsiteAudit) -> RuleResult:
            if not audit.looks_outdated:
                return None
            markers = ", ".join(audit.legacy_markers[:2])
            suffix = f" ({markers})" if markers else ""
            return (
                weights.outdated_design,
                f"Website design looks outdated{suffix}",
            )

        def _no_ssl(audit: WebsiteAudit) -> RuleResult:
            if audit.https and not audit.ssl_error:
                return None
            if audit.ssl_error:
                return (weights.no_ssl, "SSL certificate error -- browsers warn visitors")
            return (weights.no_ssl, "No HTTPS -- browsers mark the site 'Not secure'")

        def _not_mobile_responsive(audit: WebsiteAudit) -> RuleResult:
            if audit.mobile_responsive:
                return None
            return (
                weights.not_mobile_responsive,
                "Not mobile responsive -- most local searches are on a phone",
            )

        def _no_cta(audit: WebsiteAudit) -> RuleResult:
            if audit.has_cta:
                return None
            return (weights.no_cta, "No clear call to action anywhere on the homepage")

        def _no_contact_form(audit: WebsiteAudit) -> RuleResult:
            if audit.has_contact_form:
                return None
            return (weights.no_contact_form, "No contact form -- visitors must phone or email")

        def _no_booking(audit: WebsiteAudit) -> RuleResult:
            if audit.has_booking:
                return None
            return (weights.no_booking, "No online booking or appointment scheduling")

        def _missing_meta_description(audit: WebsiteAudit) -> RuleResult:
            if audit.has_meta_description:
                return None
            return (
                weights.missing_meta_description,
                "Missing meta description -- weaker search-result click-through",
            )

        def _missing_title(audit: WebsiteAudit) -> RuleResult:
            if audit.has_title:
                return None
            return (weights.missing_title, "Missing page title tag")

        def _broken_assets(audit: WebsiteAudit) -> RuleResult:
            total = audit.broken_images + audit.broken_links
            if total == 0:
                return None
            parts = []
            if audit.broken_images:
                parts.append(f"{audit.broken_images} broken image(s)")
            if audit.broken_links:
                parts.append(f"{audit.broken_links} broken link(s)")
            return (weights.broken_assets, " and ".join(parts) + " on the homepage")

        def _no_social_presence(audit: WebsiteAudit) -> RuleResult:
            if audit.has_social_presence:
                return None
            return (weights.no_social_presence, "No social media links on the website")

        def _no_chat(audit: WebsiteAudit) -> RuleResult:
            if audit.has_chat or audit.has_whatsapp:
                return None
            return (weights.no_chat, "No live chat or WhatsApp contact option")

        return [
            _outdated_design,
            _no_ssl,
            _not_mobile_responsive,
            _no_cta,
            _no_contact_form,
            _no_booking,
            _missing_meta_description,
            _missing_title,
            _broken_assets,
            _no_social_presence,
            _no_chat,
        ]

    def _performance_reasons(
        self, pagespeed: PageSpeedResult
    ) -> List[Tuple[int, str, str]]:
        """Rules driven by PageSpeed Insights data."""
        weights = self.weights
        out: List[Tuple[int, str, str]] = []

        metrics = pagespeed.primary
        if metrics is None or metrics.error:
            return out

        label = "mobile" if metrics.strategy == "mobile" else metrics.strategy

        if (
            metrics.performance_score is not None
            and metrics.performance_score < weights.performance_threshold
        ):
            out.append(
                (
                    weights.slow_performance,
                    f"PageSpeed {label} score is {metrics.performance_score}/100 "
                    f"(below {weights.performance_threshold}) -- slow pages lose visitors",
                    "slow_performance",
                )
            )

        vitals: List[str] = []
        if metrics.lcp_ms is not None and metrics.lcp_ms > weights.lcp_threshold_ms:
            vitals.append(f"LCP {metrics.lcp_ms / 1000:.1f}s")
        if metrics.cls is not None and metrics.cls > weights.cls_threshold:
            vitals.append(f"CLS {metrics.cls:.2f}")
        if vitals:
            out.append(
                (
                    weights.poor_core_web_vitals,
                    "Failing Core Web Vitals: " + ", ".join(vitals),
                    "poor_core_web_vitals",
                )
            )

        return out

    # ------------------------------------------------------------------
    # Priority + service recommendations
    # ------------------------------------------------------------------

    def _priority(self, score: int) -> str:
        if score >= self.weights.hot_threshold:
            return "Hot"
        if score >= self.weights.warm_threshold:
            return "Warm"
        return "Cold"

    def suggest_services(
        self,
        place: Place,
        audit: Optional[WebsiteAudit],
        pagespeed: Optional[PageSpeedResult],
        score: LeadScore,
    ) -> List[str]:
        """Map findings onto the services you would actually pitch."""
        services: List[str] = []

        def add(service: str) -> None:
            if service not in services:
                services.append(service)

        if not place.has_website:
            add("New website design & build")
            add("Local SEO setup")
            add("Google Business Profile optimisation")
            if place.review_count >= self.weights.few_reviews_threshold:
                add("Online booking / lead capture")
            return services[:6]

        if audit is not None and not audit.reachable:
            add("Website recovery / rebuild")
            add("Hosting & uptime monitoring")
            return services[:6]

        if audit is not None:
            if audit.looks_outdated or not audit.modern_html:
                add("Website redesign")
            if not audit.mobile_responsive:
                add("Mobile-responsive rebuild")
            if not audit.https or audit.ssl_error:
                add("SSL / security fix")
            if not audit.has_cta or not audit.has_contact_form:
                add("Conversion-rate optimisation")
            if not audit.has_booking:
                add("Online booking system")
            if not audit.has_online_ordering and _is_food_business(place):
                add("Online ordering integration")
            if not audit.has_chat and not audit.has_whatsapp:
                add("Live chat / WhatsApp integration")
            if not audit.has_meta_description or not audit.has_title:
                add("Technical SEO cleanup")
            if not audit.has_social_presence:
                add("Social media integration")
            if not audit.has_analytics:
                add("Analytics & conversion tracking")
            if audit.broken_images or audit.broken_links:
                add("Website maintenance retainer")

        if pagespeed is not None:
            metrics = pagespeed.primary
            if metrics is not None and not metrics.error:
                if (
                    metrics.performance_score is not None
                    and metrics.performance_score < self.weights.performance_threshold
                ):
                    add("Performance optimisation")
                if metrics.seo_score is not None and metrics.seo_score < 80:
                    add("SEO audit & fixes")
                if metrics.accessibility_score is not None and metrics.accessibility_score < 80:
                    add("Accessibility remediation")

        if place.rating is not None and place.rating < self.weights.low_rating_threshold:
            add("Reputation management")

        if not services:
            add("Website performance review")

        return services[:6]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FOOD_TYPES = frozenset(
    {
        "restaurant", "cafe", "bakery", "bar", "meal_takeaway",
        "meal_delivery", "food", "pizza", "coffee_shop",
    }
)


def _is_food_business(place: Place) -> bool:
    """Whether online ordering is a plausible upsell for this business."""
    if any(t in _FOOD_TYPES for t in place.types):
        return True
    category = place.category.lower()
    return any(word in category for word in ("restaurant", "cafe", "bakery", "food", "pizza"))


def score_leads(
    leads: Sequence[Lead],
    weights: Optional[ScoringWeights] = None,
) -> None:
    """Score a sequence of :class:`models.Lead` objects in place.

    A convenience wrapper for callers that just want the whole list scored and
    do not care how :class:`LeadScorer` is constructed.
    """
    scorer = LeadScorer(weights)
    for lead in leads:
        lead.score = scorer.score(lead.place, lead.audit, lead.pagespeed)
