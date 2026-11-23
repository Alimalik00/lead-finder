"""Google PageSpeed Insights (Lighthouse) client.

Fetches performance data for each lead's website: category scores, Core Web
Vitals, and a curated set of Lighthouse audits that the UX heuristics in
:mod:`website_audit` and the rules in :mod:`lead_scoring` consume.

Two practical notes drive the design here:

* **PSI is slow.** A single URL takes 10-30 seconds, and each strategy
  (mobile/desktop) is its own request. Work is therefore spread over a small
  thread pool, with a shared rate limiter keeping us inside quota.
* **INP has no lab equivalent.** Lighthouse cannot simulate real interactions,
  so INP is read from CrUX field data when Google has enough traffic for the
  origin, and reported as unavailable otherwise. Total Blocking Time is
  captured alongside it as the lab-side proxy.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

from cache import JsonCache
from config import PageSpeedSettings
from http_client import HttpClient, HttpError
from logging_setup import get_logger
from models import PageSpeedResult, StrategyMetrics

logger = get_logger("pagespeed")

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

#: Lighthouse audits kept as pass/fail booleans. These map directly onto the
#: technical and UX checks the lead score cares about.
TRACKED_AUDITS: Sequence[str] = (
    "viewport",              # mobile responsive meta tag
    "font-size",             # legible font sizes (tiny text)
    "tap-targets",           # touch targets are sized/spaced sensibly
    "color-contrast",        # readable contrast
    "is-on-https",           # secure transport
    "meta-description",      # SEO metadata
    "document-title",        # page title
    "image-alt",             # accessible images
    "link-text",             # descriptive link labels (navigation quality)
    "crawlable-anchors",     # navigation is real links, not JS handlers
    "render-blocking-resources",
    "unminified-css",
    "unminified-javascript",
    "uses-responsive-images",
    "uses-optimized-images",
    "uses-text-compression",
    "legacy-javascript",
    "errors-in-console",
    "server-response-time",
)

#: A Lighthouse audit counts as "passing" at or above this score, matching the
#: green threshold Lighthouse itself uses in its report UI.
_PASS_THRESHOLD = 0.9


class PageSpeedClient:
    """Runs PageSpeed Insights for a set of URLs."""

    def __init__(
        self,
        settings: PageSpeedSettings,
        http: HttpClient,
        cache: Optional[JsonCache] = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self.cache = cache
        if not settings.api_key:
            logger.warning(
                "No PageSpeed API key configured. PSI still answers without one, "
                "but at a very low quota -- expect throttling on larger runs."
            )

    # ------------------------------------------------------------------
    # Single URL
    # ------------------------------------------------------------------

    def analyze(self, url: str) -> PageSpeedResult:
        """Run every configured strategy against *url*."""
        result = PageSpeedResult(url=url)
        errors: List[str] = []

        for strategy in self.settings.strategies:
            metrics = self._analyze_strategy(url, strategy)
            result.strategies[strategy] = metrics
            if metrics.error:
                errors.append(f"{strategy}: {metrics.error}")

        # Only treat the whole result as failed if *every* strategy failed.
        if errors and len(errors) == len(self.settings.strategies):
            result.error = "; ".join(errors)

        return result

    def _analyze_strategy(self, url: str, strategy: str) -> StrategyMetrics:
        cache_key = f"{url}|{strategy}|{','.join(sorted(self.settings.categories))}"
        if self.cache is not None:
            cached = self.cache.get("pagespeed", cache_key)
            if cached is not None:
                logger.debug("PSI cache hit: %s (%s)", url, strategy)
                return _parse_psi_payload(cached, strategy)

        params: List[tuple] = [
            ("url", url),
            ("strategy", strategy),
        ]
        for category in self.settings.categories:
            params.append(("category", category))
        if self.settings.api_key:
            params.append(("key", self.settings.api_key))

        try:
            payload = self.http.get_json(
                PSI_ENDPOINT,
                params=params,
                timeout=self.settings.timeout_seconds,
            )
        except HttpError as exc:
            message = _friendly_psi_error(exc)
            logger.warning("PageSpeed (%s) failed for %s: %s", strategy, url, message)
            return StrategyMetrics(strategy=strategy, error=message)

        if self.cache is not None:
            self.cache.set("pagespeed", cache_key, payload)

        return _parse_psi_payload(payload, strategy)

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def analyze_many(
        self,
        urls: Sequence[str],
        progress: Any = None,
    ) -> Dict[str, PageSpeedResult]:
        """Analyse many URLs concurrently.

        Returns a mapping of URL -> :class:`PageSpeedResult`. A URL that fails
        outright still gets an entry, carrying the error message, so callers
        never have to guard against missing keys.
        """
        results: Dict[str, PageSpeedResult] = {}
        unique_urls = list(dict.fromkeys(u for u in urls if u))
        if not unique_urls:
            return results

        workers = max(1, min(self.settings.max_workers, len(unique_urls)))
        logger.info(
            "Running PageSpeed Insights on %d site(s) x %d strateg(ies) "
            "with %d worker(s)",
            len(unique_urls), len(self.settings.strategies), workers,
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.analyze, url): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("Unexpected PageSpeed failure for %s: %s", url, exc)
                    results[url] = PageSpeedResult(url=url, error=str(exc))
                finally:
                    if progress is not None:
                        progress.update(1)

        return results


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_psi_payload(payload: Dict[str, Any], strategy: str) -> StrategyMetrics:
    """Turn a raw PSI response into :class:`StrategyMetrics`."""
    metrics = StrategyMetrics(strategy=strategy)

    lighthouse = payload.get("lighthouseResult") or {}
    if not lighthouse:
        error = (payload.get("error") or {}).get("message", "empty PSI response")
        metrics.error = str(error)
        return metrics

    # --- Category scores (0.0-1.0 -> 0-100) ---
    categories = lighthouse.get("categories") or {}
    metrics.performance_score = _score_to_int(categories.get("performance"))
    metrics.accessibility_score = _score_to_int(categories.get("accessibility"))
    metrics.best_practices_score = _score_to_int(categories.get("best-practices"))
    metrics.seo_score = _score_to_int(categories.get("seo"))

    # --- Lab metrics ---
    audits = lighthouse.get("audits") or {}
    metrics.fcp_ms = _numeric(audits.get("first-contentful-paint"))
    metrics.lcp_ms = _numeric(audits.get("largest-contentful-paint"))
    metrics.cls = _numeric(audits.get("cumulative-layout-shift"))
    metrics.tbt_ms = _numeric(audits.get("total-blocking-time"))
    metrics.speed_index_ms = _numeric(audits.get("speed-index"))
    metrics.tti_ms = _numeric(audits.get("interactive"))
    metrics.total_byte_weight = _numeric(audits.get("total-byte-weight"))
    metrics.dom_size = _numeric(audits.get("dom-size"))

    # --- INP ---
    # Lighthouse has no lab INP, so prefer CrUX field data for the page, then
    # the origin, and only then the experimental lab audit if it is present.
    inp = _field_metric(payload.get("loadingExperience"), "INTERACTION_TO_NEXT_PAINT")
    if inp is None:
        inp = _field_metric(
            payload.get("originLoadingExperience"), "INTERACTION_TO_NEXT_PAINT"
        )
    if inp is not None:
        metrics.inp_ms = inp
        metrics.inp_from_field_data = True
    else:
        metrics.inp_ms = _numeric(audits.get("experimental-interaction-to-next-paint"))

    # --- Fall back to field data for any missing Core Web Vital ---
    if metrics.lcp_ms is None:
        metrics.lcp_ms = _field_metric(
            payload.get("loadingExperience"), "LARGEST_CONTENTFUL_PAINT_MS"
        )
    if metrics.fcp_ms is None:
        metrics.fcp_ms = _field_metric(
            payload.get("loadingExperience"), "FIRST_CONTENTFUL_PAINT_MS"
        )
    if metrics.cls is None:
        raw_cls = _field_metric(
            payload.get("loadingExperience"), "CUMULATIVE_LAYOUT_SHIFT_SCORE"
        )
        # CrUX reports CLS multiplied by 100.
        metrics.cls = raw_cls / 100.0 if raw_cls is not None else None

    # --- Tracked pass/fail audits ---
    for audit_id in TRACKED_AUDITS:
        metrics.audits[audit_id] = _audit_passed(audits.get(audit_id))

    return metrics


def _score_to_int(category: Any) -> Optional[int]:
    if not isinstance(category, dict):
        return None
    score = category.get("score")
    if score is None:
        return None
    try:
        return int(round(float(score) * 100))
    except (TypeError, ValueError):
        return None


def _numeric(audit: Any) -> Optional[float]:
    if not isinstance(audit, dict):
        return None
    value = audit.get("numericValue")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _audit_passed(audit: Any) -> Optional[bool]:
    """``True``/``False`` for a scored audit, ``None`` when not applicable.

    Informative audits (and audits Lighthouse skipped for this strategy)
    carry a ``null`` score; those stay ``None`` so downstream rules can tell
    "did not apply" apart from "failed".
    """
    if not isinstance(audit, dict):
        return None
    if audit.get("scoreDisplayMode") in {"notApplicable", "manual", "informative"}:
        return None
    score = audit.get("score")
    if score is None:
        return None
    try:
        return float(score) >= _PASS_THRESHOLD
    except (TypeError, ValueError):
        return None


def _field_metric(experience: Any, metric_name: str) -> Optional[float]:
    """Read a CrUX percentile from a ``loadingExperience`` block."""
    if not isinstance(experience, dict):
        return None
    metric = (experience.get("metrics") or {}).get(metric_name)
    if not isinstance(metric, dict):
        return None
    value = metric.get("percentile")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _friendly_psi_error(exc: HttpError) -> str:
    """Translate common PSI failures into something actionable."""
    if exc.status_code == 429:
        return "quota exceeded (HTTP 429) -- lower pagespeed.requests_per_second"
    if exc.status_code == 400:
        return "PSI could not load the page (HTTP 400) -- it may block bots or be down"
    if exc.status_code == 403:
        return "HTTP 403 -- check that the PageSpeed Insights API is enabled for your key"
    if exc.status_code == 500:
        return "PSI internal error (HTTP 500) -- usually a page Lighthouse cannot render"
    return str(exc)
