"""Configuration objects and loading for Lead Finder.

Configuration is resolved from four layers, later layers winning:

1. Dataclass defaults in this module.
2. ``.env`` / process environment (API keys and a few common toggles).
3. An optional YAML file (``--config config.yaml``).
4. Command-line flags parsed in :mod:`main`.

Everything the application needs is reachable from a single :class:`AppConfig`
instance, which is passed down to the individual modules. No module reads
``os.environ`` directly -- that keeps the pipeline testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

try:  # PyYAML is required only when a YAML config file is used.
    import yaml
except ImportError:  # pragma: no cover - optional at runtime
    yaml = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_USER_AGENT = (
    "LeadFinderBot/1.0 (+https://example.com/bot; contact@example.com)"
)

#: Maps a human-friendly business category onto a Google Places ``type``.
#: Used by Nearby Search and to bias Text Search. Unknown categories simply
#: fall through to a plain keyword search, so this table is a convenience,
#: not a whitelist.
CATEGORY_TO_PLACE_TYPE: Dict[str, str] = {
    "dentists": "dentist",
    "dentist": "dentist",
    "dental clinics": "dentist",
    "restaurants": "restaurant",
    "restaurant": "restaurant",
    "cafes": "cafe",
    "bars": "bar",
    "lawyers": "lawyer",
    "law firms": "lawyer",
    "attorneys": "lawyer",
    "roofing": "roofing_contractor",
    "roofers": "roofing_contractor",
    "real estate": "real_estate_agency",
    "real estate agents": "real_estate_agency",
    "hvac": "hvac_contractor",
    "plumbers": "plumber",
    "plumbing": "plumber",
    "electricians": "electrician",
    "gyms": "gym",
    "gym": "gym",
    "fitness": "gym",
    "medical clinics": "doctor",
    "doctors": "doctor",
    "clinics": "doctor",
    "physiotherapists": "physiotherapist",
    "veterinarians": "veterinary_care",
    "vets": "veterinary_care",
    "salons": "beauty_salon",
    "hair salons": "hair_care",
    "spas": "spa",
    "accountants": "accounting",
    "insurance": "insurance_agency",
    "car repair": "car_repair",
    "auto repair": "car_repair",
    "landscaping": "general_contractor",
    "contractors": "general_contractor",
    "pest control": "general_contractor",
    "hotels": "lodging",
}


# ---------------------------------------------------------------------------
# Individual configuration sections
# ---------------------------------------------------------------------------


@dataclass
class HttpSettings:
    """Shared HTTP behaviour for every outbound request."""

    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_factor: float = 0.8
    backoff_max_seconds: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT
    verify_ssl: bool = True


@dataclass
class GoogleSettings:
    """Google Places / Geocoding API settings."""

    api_key: str = ""
    api_version: str = "legacy"  # "legacy" | "v1"
    language: str = "en"
    requests_per_second: float = 8.0
    max_pages: int = 3
    page_delay_seconds: float = 2.0

    @property
    def is_v1(self) -> bool:
        return self.api_version.lower() in {"v1", "new"}


@dataclass
class PageSpeedSettings:
    """Google PageSpeed Insights settings."""

    api_key: str = ""
    enabled: bool = True
    strategies: List[str] = field(default_factory=lambda: ["mobile", "desktop"])
    categories: List[str] = field(
        default_factory=lambda: ["performance", "accessibility", "best-practices", "seo"]
    )
    requests_per_second: float = 0.5
    timeout_seconds: float = 120.0
    max_workers: int = 4


@dataclass
class AuditSettings:
    """Website-audit crawler settings."""

    enabled: bool = True
    max_workers: int = 8
    timeout_seconds: float = 15.0
    max_links_to_check: int = 25
    max_images_to_check: int = 20
    asset_check_workers: int = 6
    follow_contact_pages: bool = True
    respect_robots: bool = True
    max_html_bytes: int = 3_000_000
    contact_paths: List[str] = field(
        default_factory=lambda: [
            "/contact",
            "/contact-us",
            "/contactus",
            "/about",
            "/about-us",
            "/get-in-touch",
        ]
    )


@dataclass
class AISettings:
    """AI recommendation settings (Anthropic Claude, with template fallback)."""

    enabled: bool = True
    provider: str = "auto"  # "auto" | "claude" | "template"
    api_key: str = ""
    model: str = "claude-opus-5"
    effort: str = "low"
    max_tokens: int = 4096
    max_workers: int = 4


@dataclass
class ScoringWeights:
    """Point values and thresholds used by :mod:`lead_scoring`.

    Every field is data, not code -- tune the numbers in ``config.yaml`` to
    match how your agency actually qualifies leads.
    """

    # Rule weights
    no_website: int = 50
    site_unreachable: int = 30
    outdated_design: int = 15
    slow_performance: int = 15
    no_ssl: int = 10
    not_mobile_responsive: int = 10
    no_cta: int = 10
    no_contact_form: int = 10
    few_reviews: int = 5
    no_booking: int = 5
    missing_meta_description: int = 5
    missing_title: int = 3
    broken_assets: int = 4
    no_social_presence: int = 3
    poor_core_web_vitals: int = 5
    no_chat: int = 2
    high_demand_bonus: int = 8
    low_rating_penalty: int = 3

    # Thresholds
    performance_threshold: int = 50
    few_reviews_threshold: int = 20
    high_demand_reviews: int = 100
    low_rating_threshold: float = 4.0
    lcp_threshold_ms: float = 4000.0
    cls_threshold: float = 0.25

    # Priority buckets
    hot_threshold: int = 70
    warm_threshold: int = 40
    max_score: int = 100


@dataclass
class OutputSettings:
    """Where and how results are written."""

    directory: str = "output"
    filename_template: str = "leads_{city}_{category}_{timestamp}.xlsx"
    write_csv: bool = True
    write_json: bool = False


@dataclass
class CacheSettings:
    """On-disk response cache (keeps re-runs cheap and API bills low)."""

    enabled: bool = True
    directory: str = ".cache"
    ttl_hours: float = 168.0


@dataclass
class SearchConfig:
    """The user's actual query: where to look and what to look for."""

    country: str = ""
    state: Optional[str] = None
    city: str = ""
    category: str = ""
    country_code: Optional[str] = None
    search_mode: str = "text"  # "text" | "nearby"
    radius_m: int = 15_000
    max_results: int = 60
    min_reviews: int = 0
    min_rating: float = 0.0
    require_no_website: bool = False
    keyword: Optional[str] = None
    place_type: Optional[str] = None

    # -- helpers ------------------------------------------------------------

    @property
    def location_string(self) -> str:
        """``"Austin, Texas, United States"`` -- used for geocoding."""
        parts = [p for p in (self.city, self.state, self.country) if p]
        return ", ".join(parts)

    @property
    def text_query(self) -> str:
        """The free-form query handed to Places Text Search."""
        parts = [self.category]
        if self.keyword:
            parts.append(self.keyword)
        parts.append("in")
        parts.append(self.location_string)
        return " ".join(p for p in parts if p)

    def resolved_place_type(self) -> Optional[str]:
        """Explicit ``place_type`` if given, else inferred from the category."""
        if self.place_type:
            return self.place_type
        return CATEGORY_TO_PLACE_TYPE.get(self.category.strip().lower())

    def validate(self) -> List[str]:
        """Return a list of human-readable problems (empty means valid)."""
        problems: List[str] = []
        if not self.country:
            problems.append("country is required")
        if not self.city:
            problems.append("city is required")
        if not self.category:
            problems.append("category is required")
        if self.radius_m <= 0:
            problems.append("radius must be positive")
        if self.max_results <= 0:
            problems.append("max_results must be positive")
        if not 0.0 <= self.min_rating <= 5.0:
            problems.append("min_rating must be between 0 and 5")
        if self.search_mode not in {"text", "nearby"}:
            problems.append("search_mode must be 'text' or 'nearby'")
        return problems


@dataclass
class AppConfig:
    """Root configuration object passed through the whole pipeline."""

    search: SearchConfig = field(default_factory=SearchConfig)
    google: GoogleSettings = field(default_factory=GoogleSettings)
    pagespeed: PageSpeedSettings = field(default_factory=PageSpeedSettings)
    audit: AuditSettings = field(default_factory=AuditSettings)
    ai: AISettings = field(default_factory=AISettings)
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    output: OutputSettings = field(default_factory=OutputSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    http: HttpSettings = field(default_factory=HttpSettings)

    log_level: str = "INFO"
    log_file: Optional[str] = "logs/lead_finder.log"

    # -- helpers ------------------------------------------------------------

    def output_dir(self) -> Path:
        return _resolve_path(self.output.directory)

    def cache_dir(self) -> Path:
        return _resolve_path(self.cache.directory)

    def log_path(self) -> Optional[Path]:
        return _resolve_path(self.log_file) if self.log_file else None


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _resolve_path(value: str) -> Path:
    """Resolve *value* relative to the project directory unless absolute."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def _env_bool(name: str, default: Optional[bool] = None) -> Optional[bool]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort coercion of a YAML scalar to the dataclass field's type.

    ``from __future__ import annotations`` turns every annotation into a
    string, so we compare against the textual form rather than the type object.
    """
    if value is None:
        return None
    text = str(target_type)
    if "bool" in text and not isinstance(value, bool):
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if "int" in text and "float" not in text and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if "float" in text:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


def _apply_mapping(target: Any, data: Dict[str, Any]) -> None:
    """Recursively copy known keys from *data* onto the dataclass *target*."""
    if not isinstance(data, dict):
        return
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            continue  # Unknown keys are ignored rather than fatal.
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_mapping(current, value)
        else:
            setattr(target, key, _coerce(value, spec.type))


def load_yaml_config(path: Path) -> Dict[str, Any]:
    """Read a YAML config file into a plain dict."""
    if yaml is None:
        raise RuntimeError(
            "PyYAML is not installed but a --config file was supplied. "
            "Run: pip install PyYAML"
        )
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping")
    return loaded


def load_config(
    config_path: Optional[str] = None,
    env_file: Optional[str] = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from ``.env`` plus an optional YAML file.

    Command-line overrides are applied afterwards by :mod:`main`.
    """
    # 1. Environment (.env next to this file unless told otherwise).
    dotenv_path = Path(env_file) if env_file else (BASE_DIR / ".env")
    load_dotenv(dotenv_path=dotenv_path if dotenv_path.exists() else None)

    config = AppConfig()

    # 2. YAML file (structure mirrors the dataclasses).
    if config_path:
        _apply_mapping(config, load_yaml_config(Path(config_path).expanduser()))

    # 3. Secrets and a handful of common toggles always come from the env,
    #    so that a shared config.yaml never has to contain a key.
    google_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if google_key:
        config.google.api_key = google_key

    psi_key = os.getenv("PAGESPEED_API_KEY", "").strip()
    config.pagespeed.api_key = psi_key or config.google.api_key

    api_version = os.getenv("GOOGLE_PLACES_API_VERSION", "").strip()
    if api_version:
        config.google.api_version = api_version

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        config.ai.api_key = anthropic_key

    ai_model = os.getenv("LEAD_FINDER_AI_MODEL", "").strip()
    if ai_model:
        config.ai.model = ai_model

    ai_effort = os.getenv("LEAD_FINDER_AI_EFFORT", "").strip()
    if ai_effort:
        config.ai.effort = ai_effort

    log_level = os.getenv("LEAD_FINDER_LOG_LEVEL", "").strip()
    if log_level:
        config.log_level = log_level.upper()

    output_dir = os.getenv("LEAD_FINDER_OUTPUT_DIR", "").strip()
    if output_dir:
        config.output.directory = output_dir

    cache_dir = os.getenv("LEAD_FINDER_CACHE_DIR", "").strip()
    if cache_dir:
        config.cache.directory = cache_dir

    cache_enabled = _env_bool("LEAD_FINDER_CACHE_ENABLED")
    if cache_enabled is not None:
        config.cache.enabled = cache_enabled

    user_agent = os.getenv("LEAD_FINDER_USER_AGENT", "").strip()
    if user_agent:
        config.http.user_agent = user_agent

    return config
