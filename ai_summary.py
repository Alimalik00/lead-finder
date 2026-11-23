"""AI-written lead reasoning and outreach copy.

Every lead gets four pieces of sales copy:

``reason``              why this business is worth contacting
``suggested_services``  what to pitch
``email``               a short cold email (subject + body)
``linkedin_message``    a LinkedIn connection note

Two generators produce them, behind a common interface:

:class:`ClaudeSummaryGenerator`
    Calls the Anthropic API. Used when ``ANTHROPIC_API_KEY`` is set and the
    ``anthropic`` package is installed.

:class:`TemplateSummaryGenerator`
    Deterministic, offline, zero-cost. Used when Claude is unavailable, and as
    the per-lead fallback whenever an API call fails or is declined -- so the
    export always has copy in every row.

The model is only ever given facts this pipeline actually measured. The prompt
forbids inventing details, because a cold email that misstates a prospect's
website is worse than no email at all.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

from config import AISettings
from logging_setup import get_logger
from models import AISummary, Lead

logger = get_logger("ai")

#: Beta flag for server-side refusal fallbacks. Claude Opus 5 ships with
#: safety classifiers that can decline a request; with this enabled the API
#: transparently re-runs a declined request on a fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Schema the model must fill in. Structured output removes all response
#: parsing guesswork -- there is no prose to strip and no markdown fence.
SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Two or three sentences explaining why this business needs help "
                "and what to fix first. Reference the specific findings given."
            ),
        },
        "suggested_services": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three to five concrete services to pitch.",
        },
        "email_subject": {
            "type": "string",
            "description": "Cold email subject line, under 60 characters, no clickbait.",
        },
        "email_body": {
            "type": "string",
            "description": (
                "Cold email body, 90-140 words, plain text. Opens with a specific "
                "observation about their online presence, states the business impact, "
                "ends with one low-friction question. Sign off as '[Your Name]'."
            ),
        },
        "linkedin_message": {
            "type": "string",
            "description": "LinkedIn connection note under 300 characters.",
        },
    },
    "required": [
        "reason",
        "suggested_services",
        "email_subject",
        "email_body",
        "linkedin_message",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You write lead-qualification notes and cold outreach for a web design and \
software agency that sells to local businesses.

Rules:
- Use ONLY the facts in the lead brief. Never invent metrics, technologies, \
competitors, names, or claims about the business. If a fact is missing, write \
around it rather than guessing.
- Be specific and concrete. "Your site takes 6 seconds to load on mobile" beats \
"your site could be faster".
- Lead with the business consequence (lost bookings, lost calls, lost search \
traffic), not with technical jargon.
- Plain, direct, professional. No hype, no exclamation marks, no "I hope this \
email finds you well", no em dashes, no emoji.
- The email must be something a busy owner reads in fifteen seconds and can \
reply to with one line.
"""


class AIGenerationError(Exception):
    """Raised when the model call fails or is declined."""


# ---------------------------------------------------------------------------
# Template generator (always available)
# ---------------------------------------------------------------------------


class TemplateSummaryGenerator:
    """Builds outreach copy deterministically from the audit findings.

    This is not a placeholder: the copy it produces is specific, factual and
    genuinely sendable. It exists so the tool is fully useful without any AI
    credentials, and so a failed API call never leaves a blank row.
    """

    name = "template"

    def generate(self, lead: Lead) -> AISummary:
        place = lead.place
        audit = lead.audit
        score = lead.score
        business = place.name or "this business"
        category = (place.category or "local business").rstrip("s").lower()

        reason = self._reason(lead, business, category)
        services = score.suggested_services or ["Website performance review"]

        subject, body = self._email(lead, business, services)
        linkedin = self._linkedin(lead, business, category)

        return AISummary(
            reason=reason,
            suggested_services=list(services),
            email_subject=subject,
            email_body=body,
            linkedin_message=linkedin,
            generator=self.name,
        )

    # -- pieces -------------------------------------------------------------

    def _reason(self, lead: Lead, business: str, category: str) -> str:
        place = lead.place
        audit = lead.audit
        pagespeed = lead.pagespeed

        credibility = ""
        if place.review_count and place.rating:
            credibility = (
                f" despite {place.review_count} Google reviews at {place.rating:.1f} stars"
            )
        elif place.review_count:
            credibility = f" despite {place.review_count} Google reviews"

        if not place.has_website:
            what = "only a social media page" if place.website_is_social_only else "no website"
            return (
                f"This {category} has {what}{credibility}. Customers searching for "
                f"them online have nowhere to land, so enquiries go to competitors "
                f"who do have a site. A professional website with clear contact and "
                f"booking would capture demand they are already generating."
            )

        if audit is not None and not audit.reachable:
            return (
                f"{business} lists a website that does not load "
                f"({audit.error or 'unreachable'}){credibility}. Anyone who clicks "
                f"through from Google hits a dead end. Recovering or rebuilding the "
                f"site is urgent and an easy first conversation."
            )

        problems: List[str] = []
        if audit is not None:
            if audit.looks_outdated:
                problems.append("the design looks dated")
            if not audit.mobile_responsive:
                problems.append("it is not mobile responsive")
            if not audit.https:
                problems.append("it has no HTTPS, so browsers flag it as not secure")
            if not audit.has_cta:
                problems.append("there is no clear call to action")
            if not audit.has_contact_form:
                problems.append("there is no contact form")
            if not audit.has_booking:
                problems.append("there is no online booking")

        mobile_score = pagespeed.mobile_score if pagespeed else None
        if mobile_score is not None and mobile_score < 50:
            problems.append(f"mobile PageSpeed scores {mobile_score}/100")

        if not problems:
            issue_text = "there are still measurable gaps in the site's conversion path"
        elif len(problems) == 1:
            issue_text = problems[0]
        else:
            issue_text = ", ".join(problems[:-1]) + f", and {problems[-1]}"

        return (
            f"{business} has a website, but {issue_text}{credibility}. "
            f"That combination means visits are arriving and not converting. "
            f"A focused redesign around speed, mobile and a clear booking path is "
            f"the highest-leverage fix."
        )

    def _email(self, lead: Lead, business: str, services: Sequence[str]) -> tuple:
        place = lead.place
        audit = lead.audit
        pagespeed = lead.pagespeed
        city = _city_from_address(place.address)
        where = f" in {city}" if city else ""

        if not place.has_website:
            subject = f"{business} is missing from search results"
            observation = (
                f"I was looking at {(place.category or 'local businesses').lower()}"
                f"{where} and noticed {business} does not have a website listed on "
                f"Google."
            )
            if place.review_count >= 20:
                observation += (
                    f" With {place.review_count} reviews, you clearly have the demand "
                    f"already. Right now that demand has nowhere to land online."
                )
            ask = (
                "Would it be useful if I put together a one-page outline of what a "
                "site for you would need to cover? No cost, no obligation."
            )
        elif audit is not None and not audit.reachable:
            subject = f"{business}'s website is not loading"
            observation = (
                f"I tried to open {place.website} from your Google listing and it did "
                f"not load. Anyone who clicks through from search is hitting the same "
                f"dead end."
            )
            ask = "Happy to take a quick look and tell you what is wrong. Want me to?"
        else:
            findings = _top_findings(audit, pagespeed, limit=3)
            subject = _email_subject_for(business, findings)
            observation = (
                f"I had a look at {_display_domain(place.website)} and noticed a few "
                f"things worth fixing:"
            )
            if findings:
                observation += "\n\n" + "\n".join(f"- {f}" for f in findings)
            ask = (
                "Each of these costs you enquiries every month. Want me to send over "
                "a short breakdown of what I would change first?"
            )

        service_line = ""
        if services:
            service_line = f"\n\nWhere I would start: {', '.join(list(services)[:3])}."

        # We never have a contact name from Places, so address the business
        # rather than guessing at a person.
        greeting = f"Hi {business} team," if len(business) < 40 else "Hi there,"
        body = (
            f"{greeting}\n\n"
            f"{observation}"
            f"{service_line}\n\n"
            f"{ask}\n\n"
            f"[Your Name]"
        )
        return subject, body

    def _linkedin(self, lead: Lead, business: str, category: str) -> str:
        place = lead.place
        if not place.has_website:
            note = (
                f"Hi, I work with {category} businesses on their web presence. I "
                f"noticed {business} does not have a website listed on Google yet, "
                f"which is a lot of search traffic going elsewhere. Happy to share a "
                f"few ideas if useful."
            )
        else:
            note = (
                f"Hi, I ran a quick audit of {_display_domain(place.website)} and "
                f"found a few fixable issues holding back enquiries. Happy to send "
                f"the findings over if that is useful."
            )
        return note[:300]


# ---------------------------------------------------------------------------
# Claude generator
# ---------------------------------------------------------------------------


class ClaudeSummaryGenerator:
    """Generates outreach copy with the Anthropic API.

    Falls back to :class:`TemplateSummaryGenerator` per-lead on any failure, so
    a transient API problem degrades the copy rather than breaking the run.
    """

    name = "claude"

    #: Request shapes tried in order. Each one is downgraded permanently the
    #: first time the installed SDK or the account rejects it, so an older
    #: ``anthropic`` release still works without per-call exception handling.
    _MODES = ("fallbacks", "structured", "plain")

    def __init__(self, settings: AISettings, fallback: TemplateSummaryGenerator) -> None:
        try:
            import anthropic  # noqa: F401  (imported for its side effects/types)
        except ImportError as exc:  # pragma: no cover - dependency check
            raise AIGenerationError(
                "The 'anthropic' package is not installed. "
                "Run: pip install anthropic"
            ) from exc

        self._anthropic = anthropic
        self.settings = settings
        self.fallback = fallback
        self.client = anthropic.Anthropic(api_key=settings.api_key or None)
        self._mode_index = 0
        self._mode_lock = threading.Lock()

    # -- public -------------------------------------------------------------

    def generate(self, lead: Lead) -> AISummary:
        brief = build_lead_brief(lead)
        try:
            payload = self._invoke(brief)
        except AIGenerationError as exc:
            logger.warning("Falling back to template copy for %s: %s", lead.name, exc)
            summary = self.fallback.generate(lead)
            summary.generator = "template (AI unavailable)"
            return summary

        services = payload.get("suggested_services") or []
        if not isinstance(services, list):
            services = [str(services)]

        return AISummary(
            reason=str(payload.get("reason", "")).strip(),
            suggested_services=[str(s).strip() for s in services if str(s).strip()],
            email_subject=str(payload.get("email_subject", "")).strip(),
            email_body=str(payload.get("email_body", "")).strip(),
            linkedin_message=str(payload.get("linkedin_message", "")).strip(),
            generator=self.name,
        )

    # -- internals ----------------------------------------------------------

    def _current_mode(self) -> str:
        with self._mode_lock:
            return self._MODES[self._mode_index]

    def _downgrade(self, reason: str) -> bool:
        """Step down to a simpler request shape. Returns False when exhausted."""
        with self._mode_lock:
            if self._mode_index >= len(self._MODES) - 1:
                return False
            self._mode_index += 1
            new_mode = self._MODES[self._mode_index]
        logger.info("Anthropic request downgraded to '%s' mode: %s", new_mode, reason)
        return True

    def _build_kwargs(self, mode: str, brief: str) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.settings.model,
            # Short, structured outreach copy: a few hundred tokens of output
            # at most. This ceiling leaves generous headroom for thinking
            # without risking an HTTP timeout on a non-streaming request.
            "max_tokens": self.settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": brief}],
        }
        if mode in ("fallbacks", "structured"):
            kwargs["output_config"] = {
                # Low effort is right for short copy generation and keeps both
                # latency and cost down across dozens of leads.
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA},
            }
        if mode == "fallbacks":
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _invoke(self, brief: str) -> Dict[str, Any]:
        anthropic = self._anthropic

        while True:
            mode = self._current_mode()
            kwargs = self._build_kwargs(mode, brief)
            create = (
                self.client.beta.messages.create
                if mode == "fallbacks"
                else self.client.messages.create
            )

            try:
                response = create(**kwargs)
            except TypeError as exc:
                # The installed SDK does not know one of these parameters.
                if self._downgrade(f"SDK rejected a parameter ({exc})"):
                    continue
                raise AIGenerationError(f"Incompatible anthropic SDK: {exc}") from exc
            except anthropic.BadRequestError as exc:
                if self._downgrade(f"API rejected the request ({exc})"):
                    continue
                raise AIGenerationError(f"Bad request: {exc}") from exc
            except anthropic.AuthenticationError as exc:
                raise AIGenerationError("Invalid ANTHROPIC_API_KEY") from exc
            except anthropic.PermissionDeniedError as exc:
                raise AIGenerationError(f"API key lacks permission: {exc}") from exc
            except anthropic.NotFoundError as exc:
                raise AIGenerationError(
                    f"Model {self.settings.model!r} not found for this key: {exc}"
                ) from exc
            except anthropic.RateLimitError as exc:
                # The SDK already retried with backoff before surfacing this.
                raise AIGenerationError(f"Rate limited: {exc}") from exc
            except anthropic.APIConnectionError as exc:
                raise AIGenerationError(f"Network error reaching the API: {exc}") from exc
            except anthropic.APIStatusError as exc:
                raise AIGenerationError(f"API error {exc.status_code}: {exc}") from exc

            # Safety classifiers can decline a request. Check before reading
            # content -- on a refusal `content` is empty or partial.
            if getattr(response, "stop_reason", None) == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise AIGenerationError(
                    f"Request declined by safety classifiers (category={category})"
                )

            text = _extract_text(response)
            if not text:
                raise AIGenerationError("Model returned no text content")

            parsed = _parse_json_object(text)
            if parsed is None:
                if mode != "plain" and self._downgrade("structured output not honoured"):
                    continue
                raise AIGenerationError("Could not parse the model response as JSON")
            return parsed


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_lead_brief(lead: Lead) -> str:
    """Render the measured facts about a lead as a compact prompt."""
    place = lead.place
    audit = lead.audit
    pagespeed = lead.pagespeed
    score = lead.score

    lines: List[str] = ["<lead>"]
    lines.append(f"Business: {place.name}")
    lines.append(f"Category: {place.category or 'unknown'}")
    if place.address:
        lines.append(f"Address: {place.address}")
    if place.rating is not None:
        lines.append(f"Google rating: {place.rating:.1f}/5 from {place.review_count} reviews")
    else:
        lines.append(f"Google reviews: {place.review_count}")
    if place.phone:
        lines.append(f"Phone: {place.phone}")

    if place.has_website:
        lines.append(f"Website: {place.website}")
    elif place.website:
        lines.append(f"Website: none (only a social page: {place.website})")
    else:
        lines.append("Website: NONE listed on Google")

    if audit is not None and place.has_website:
        if not audit.reachable:
            lines.append(f"Website status: DOES NOT LOAD ({audit.error or 'unreachable'})")
        else:
            lines.append("Website status: loads successfully")
            lines.append(f"HTTPS: {'yes' if audit.https else 'NO'}")
            lines.append(
                f"Mobile responsive: {'yes' if audit.mobile_responsive else 'NO'}"
            )
            lines.append(
                f"Design assessment: "
                f"{'looks outdated' if audit.looks_outdated else 'looks current'} "
                f"(outdated score {audit.outdated_score}/100)"
            )
            lines.append(f"Clear call to action: {'yes' if audit.has_cta else 'NO'}")
            lines.append(f"Contact form: {'yes' if audit.has_contact_form else 'NO'}")
            lines.append(
                f"Online booking: "
                f"{', '.join(audit.booking_providers) if audit.booking_providers else 'NO'}"
            )
            lines.append(f"Live chat: {'yes' if audit.has_chat else 'NO'}")
            lines.append(f"WhatsApp button: {'yes' if audit.has_whatsapp else 'NO'}")
            lines.append(
                f"Social links: "
                f"{', '.join(audit.social_links) if audit.social_links else 'NONE'}"
            )
            if audit.cms:
                lines.append(f"Platform: {audit.cms}")
            if audit.broken_images or audit.broken_links:
                lines.append(
                    f"Broken assets: {audit.broken_images} image(s), "
                    f"{audit.broken_links} link(s)"
                )
            if audit.issues:
                lines.append("Issues found: " + "; ".join(audit.issues[:12]))

    if pagespeed is not None:
        metrics = pagespeed.mobile
        if metrics is not None and not metrics.error:
            if metrics.performance_score is not None:
                lines.append(f"PageSpeed mobile score: {metrics.performance_score}/100")
            if metrics.lcp_ms is not None:
                lines.append(f"Largest Contentful Paint (mobile): {metrics.lcp_ms/1000:.1f}s")
            if metrics.cls is not None:
                lines.append(f"Cumulative Layout Shift: {metrics.cls:.2f}")
        desktop = pagespeed.desktop
        if desktop is not None and desktop.performance_score is not None:
            lines.append(f"PageSpeed desktop score: {desktop.performance_score}/100")

    lines.append(f"Lead score: {score.score}/100 ({score.priority} priority)")
    if score.reasons:
        lines.append("Scoring rationale: " + score.breakdown)
    if score.suggested_services:
        lines.append("Candidate services: " + ", ".join(score.suggested_services))
    lines.append("</lead>")

    lines.append("")
    lines.append(
        "Write the lead note and outreach copy for this business using only the "
        "facts above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_generator(settings: AISettings) -> Any:
    """Pick the best available generator for the current configuration."""
    template = TemplateSummaryGenerator()

    if not settings.enabled or settings.provider == "template":
        logger.info("Using template-based lead summaries")
        return template

    if not settings.api_key:
        if settings.provider == "claude":
            logger.warning(
                "AI provider is 'claude' but ANTHROPIC_API_KEY is not set; "
                "using template summaries instead."
            )
        else:
            logger.info("No ANTHROPIC_API_KEY set; using template summaries")
        return template

    try:
        generator = ClaudeSummaryGenerator(settings, template)
    except AIGenerationError as exc:
        logger.warning("%s -- using template summaries instead.", exc)
        return template

    logger.info("Using Claude (%s) for lead summaries", settings.model)
    return generator


def generate_summaries(
    leads: Sequence[Lead],
    settings: AISettings,
    progress: Any = None,
) -> None:
    """Populate ``lead.summary`` for every lead, in place."""
    if not leads:
        return

    generator = build_generator(settings)

    # The template generator is pure CPU work; run it inline. Only fan out
    # when there are real network calls to overlap.
    if isinstance(generator, TemplateSummaryGenerator):
        for lead in leads:
            lead.summary = generator.generate(lead)
            if progress is not None:
                progress.update(1)
        return

    workers = max(1, min(settings.max_workers, len(leads)))
    logger.info("Generating AI summaries for %d lead(s) with %d worker(s)",
                len(leads), workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(generator.generate, lead): lead for lead in leads}
        for future in as_completed(futures):
            lead = futures[future]
            try:
                lead.summary = future.result()
            except Exception as exc:  # pragma: no cover - generator handles its own
                logger.error("Summary generation failed for %s: %s", lead.name, exc)
                lead.summary = TemplateSummaryGenerator().generate(lead)
                lead.summary.generator = "template (AI error)"
            finally:
                if progress is not None:
                    progress.update(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response."""
    parts: List[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object out of *text*, tolerating fences and preamble."""
    candidate = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            return None
    return None


def _display_domain(url: str) -> str:
    """``https://www.example.com/x`` -> ``example.com``."""
    cleaned = re.sub(r"^https?://", "", (url or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^www\.", "", cleaned, flags=re.IGNORECASE)
    return cleaned.split("/")[0] or url


def _city_from_address(address: str) -> str:
    """Best-effort city extraction from a Google formatted address."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) == 2:
        return parts[0]
    return ""


def _top_findings(audit: Any, pagespeed: Any, limit: int = 3) -> List[str]:
    """The most sellable, most concrete findings, in priority order."""
    findings: List[str] = []

    mobile_score = pagespeed.mobile_score if pagespeed else None
    lcp = None
    if pagespeed is not None and pagespeed.mobile is not None:
        lcp = pagespeed.mobile.lcp_ms

    if audit is not None and not audit.mobile_responsive:
        findings.append("The site is not mobile responsive")
    if audit is not None and not audit.https:
        findings.append("There is no HTTPS, so browsers show a 'Not secure' warning")
    if lcp is not None and lcp > 4000:
        findings.append(f"The main content takes {lcp/1000:.1f} seconds to appear on mobile")
    elif mobile_score is not None and mobile_score < 50:
        findings.append(f"Mobile PageSpeed scores {mobile_score} out of 100")
    if audit is not None and not audit.has_cta:
        findings.append("There is no clear call to action on the homepage")
    if audit is not None and not audit.has_contact_form:
        findings.append("There is no contact form, so enquiries depend on a phone call")
    if audit is not None and not audit.has_booking:
        findings.append("There is no way to book online")
    if audit is not None and audit.looks_outdated:
        findings.append("The design looks several years out of date")
    if audit is not None and (audit.broken_images or audit.broken_links):
        findings.append("Some images or links on the homepage are broken")

    return findings[:limit]


def _email_subject_for(business: str, findings: Sequence[str]) -> str:
    """A short, specific subject line derived from the strongest finding."""
    if not findings:
        return f"A few notes on {business}'s website"
    first = findings[0].lower()
    if "mobile responsive" in first:
        return f"{business}'s site on mobile"
    if "https" in first or "not secure" in first:
        return f"{business}'s site is flagged 'Not secure'"
    if "seconds" in first or "pagespeed" in first:
        return f"{business}'s website load time"
    if "call to action" in first or "contact form" in first:
        return f"Quick note on {business}'s website enquiries"
    if "book online" in first:
        return f"Online booking for {business}"
    return f"A few fixable issues on {business}'s website"
