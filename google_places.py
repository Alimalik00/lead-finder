"""Google Places API client.

This module discovers businesses and enriches them with contact details. It
uses the **official Places API** -- Google Maps is never scraped.

Two API generations are supported, selected by ``google.api_version``:

``legacy``
    ``maps.googleapis.com/maps/api/place/*``. Text Search and Nearby Search
    return 20 results per page and up to 3 pages (60 results) via
    ``next_page_token``. This is the default because its pagination behaviour
    is stable and well documented.

``v1``
    ``places.googleapis.com/v1/*`` (the "New" Places API). Uses field masks,
    which makes responses smaller and billing more predictable. Text Search
    supports ``pageToken``; Nearby Search does not paginate.

Both paths normalise into the same :class:`models.Place`, so nothing
downstream needs to know which one ran.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cache import JsonCache
from config import GoogleSettings, SearchConfig
from http_client import HttpClient, HttpError
from logging_setup import get_logger
from models import Place

logger = get_logger("places")

# --- Legacy endpoints ------------------------------------------------------
LEGACY_BASE = "https://maps.googleapis.com/maps/api"
LEGACY_TEXT_SEARCH = f"{LEGACY_BASE}/place/textsearch/json"
LEGACY_NEARBY_SEARCH = f"{LEGACY_BASE}/place/nearbysearch/json"
LEGACY_DETAILS = f"{LEGACY_BASE}/place/details/json"
LEGACY_GEOCODE = f"{LEGACY_BASE}/geocode/json"

#: Detail fields we request. Keep this list tight -- Places Details is billed
#: by field group, so asking for everything is needlessly expensive.
LEGACY_DETAIL_FIELDS: Sequence[str] = (
    "place_id",
    "name",
    "formatted_address",
    "formatted_phone_number",
    "international_phone_number",
    "website",
    "rating",
    "user_ratings_total",
    "business_status",
    "opening_hours",
    "current_opening_hours",
    "geometry/location",
    "types",
    "url",
)

# --- New (v1) endpoints ----------------------------------------------------
V1_BASE = "https://places.googleapis.com/v1"
V1_TEXT_SEARCH = f"{V1_BASE}/places:searchText"
V1_NEARBY_SEARCH = f"{V1_BASE}/places:searchNearby"

V1_SEARCH_FIELDS: Sequence[str] = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.rating",
    "places.userRatingCount",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "nextPageToken",
)

V1_DETAIL_FIELDS: Sequence[str] = (
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "types",
    "primaryType",
    "businessStatus",
    "currentOpeningHours.openNow",
    "location",
    "googleMapsUri",
)

#: Places statuses that mean "the request was fine, there is just nothing here".
_BENIGN_STATUSES = frozenset({"OK", "ZERO_RESULTS"})


class PlacesError(Exception):
    """Raised for unrecoverable Places API problems (bad key, quota, ...)."""


class GooglePlacesClient:
    """Discovers businesses and fetches their contact details.

    Args:
        settings: Google API configuration.
        http: Shared, rate-limited HTTP client.
        cache: Response cache. Only Details responses are cached -- search
            results change often enough that caching them hides new leads.
    """

    def __init__(
        self,
        settings: GoogleSettings,
        http: HttpClient,
        cache: Optional[JsonCache] = None,
    ) -> None:
        if not settings.api_key:
            raise PlacesError(
                "No Google API key configured. Set GOOGLE_MAPS_API_KEY in .env "
                "(see .env.example)."
            )
        self.settings = settings
        self.http = http
        self.cache = cache

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def geocode(self, location: str) -> Optional[Tuple[float, float]]:
        """Resolve a free-form location to ``(latitude, longitude)``.

        Returns ``None`` when the location cannot be resolved; callers fall
        back to an unbiased text search rather than failing the whole run.
        """
        params = {
            "address": location,
            "key": self.settings.api_key,
            "language": self.settings.language,
        }
        try:
            payload = self.http.get_json(LEGACY_GEOCODE, params=params)
        except HttpError as exc:
            logger.warning("Geocoding failed for %r: %s", location, exc)
            return None

        status = payload.get("status", "")
        if status != "OK":
            logger.warning(
                "Geocoding returned %s for %r%s",
                status, location,
                f" ({payload['error_message']})" if payload.get("error_message") else "",
            )
            return None

        results = payload.get("results") or []
        if not results:
            return None
        loc = results[0].get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            return None
        logger.info("Geocoded %r -> (%.5f, %.5f)", location, lat, lng)
        return float(lat), float(lng)

    # ------------------------------------------------------------------
    # Search (public entry point)
    # ------------------------------------------------------------------

    def search(self, search: SearchConfig) -> List[Place]:
        """Find businesses matching *search*, handling pagination internally.

        Returns partially-populated :class:`Place` objects -- search responses
        do not include phone numbers or websites. Call :meth:`enrich` (or
        :meth:`fetch_details`) to fill those in.
        """
        centre = self.geocode(search.location_string)
        if centre is None and search.search_mode == "nearby":
            raise PlacesError(
                f"Could not geocode {search.location_string!r}, which Nearby "
                "Search requires. Check the spelling, or use --search-mode text."
            )

        if self.settings.is_v1:
            places = self._search_v1(search, centre)
        else:
            places = self._search_legacy(search, centre)

        deduped = _dedupe(places)
        logger.info(
            "Search returned %d place(s)%s",
            len(deduped),
            f" ({len(places) - len(deduped)} duplicate(s) removed)"
            if len(places) != len(deduped) else "",
        )
        return deduped[: search.max_results]

    # ------------------------------------------------------------------
    # Search -- legacy implementation
    # ------------------------------------------------------------------

    def _search_legacy(
        self,
        search: SearchConfig,
        centre: Optional[Tuple[float, float]],
    ) -> List[Place]:
        if search.search_mode == "nearby":
            url = LEGACY_NEARBY_SEARCH
            assert centre is not None  # guaranteed by search()
            params: Dict[str, Any] = {
                "location": f"{centre[0]},{centre[1]}",
                "radius": search.radius_m,
                "keyword": search.keyword or search.category,
                "key": self.settings.api_key,
                "language": self.settings.language,
            }
            place_type = search.resolved_place_type()
            if place_type:
                params["type"] = place_type
        else:
            url = LEGACY_TEXT_SEARCH
            params = {
                "query": search.text_query,
                "key": self.settings.api_key,
                "language": self.settings.language,
            }
            if centre is not None:
                params["location"] = f"{centre[0]},{centre[1]}"
                params["radius"] = search.radius_m
            if search.country_code:
                params["region"] = search.country_code.lower()

        collected: List[Place] = []
        page_token: Optional[str] = None

        for page in range(self.settings.max_pages):
            if page_token:
                # A fresh next_page_token is not immediately valid; Google
                # documents a short delay before it becomes usable.
                time.sleep(self.settings.page_delay_seconds)
                params = {"pagetoken": page_token, "key": self.settings.api_key}

            payload = self._legacy_call_with_token_retry(url, params)
            if payload is None:
                break

            results = payload.get("results") or []
            for raw in results:
                collected.append(_place_from_legacy_search(raw, search.category))

            logger.info(
                "Page %d: %d result(s) (running total %d)",
                page + 1, len(results), len(collected),
            )

            if len(collected) >= search.max_results:
                break

            page_token = payload.get("next_page_token")
            if not page_token:
                break

        return collected

    def _legacy_call_with_token_retry(
        self,
        url: str,
        params: Dict[str, Any],
        max_token_retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Call a legacy endpoint, retrying the "token not ready yet" case."""
        for attempt in range(max_token_retries):
            try:
                payload = self.http.get_json(url, params=params)
            except HttpError as exc:
                logger.error("Places request failed: %s", exc)
                return None

            status = payload.get("status", "")
            if status in _BENIGN_STATUSES:
                return payload

            message = payload.get("error_message", "")

            # A page token that is not yet active reports INVALID_REQUEST.
            if status == "INVALID_REQUEST" and "pagetoken" in params:
                wait = self.settings.page_delay_seconds * (attempt + 2)
                logger.debug("Page token not ready; waiting %.1fs", wait)
                time.sleep(wait)
                continue

            if status in {"REQUEST_DENIED", "OVER_QUERY_LIMIT"}:
                raise PlacesError(
                    f"Google Places returned {status}: {message or 'no detail provided'}. "
                    "Check that the API key is valid, billing is enabled, and the "
                    "Places API is turned on for the project."
                )

            logger.warning("Places returned %s: %s", status, message)
            return None

        logger.warning("Gave up waiting for a valid next_page_token")
        return None

    # ------------------------------------------------------------------
    # Search -- v1 implementation
    # ------------------------------------------------------------------

    def _search_v1(
        self,
        search: SearchConfig,
        centre: Optional[Tuple[float, float]],
    ) -> List[Place]:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.settings.api_key,
            "X-Goog-FieldMask": ",".join(V1_SEARCH_FIELDS),
        }

        if search.search_mode == "nearby":
            assert centre is not None  # guaranteed by search()
            body: Dict[str, Any] = {
                "maxResultCount": min(search.max_results, 20),
                "languageCode": self.settings.language,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": centre[0], "longitude": centre[1]},
                        "radius": float(search.radius_m),
                    }
                },
            }
            place_type = search.resolved_place_type()
            if place_type:
                body["includedTypes"] = [place_type]
            url = V1_NEARBY_SEARCH
        else:
            body = {
                "textQuery": search.text_query,
                "languageCode": self.settings.language,
                "pageSize": 20,
            }
            if centre is not None:
                body["locationBias"] = {
                    "circle": {
                        "center": {"latitude": centre[0], "longitude": centre[1]},
                        "radius": float(search.radius_m),
                    }
                }
            if search.country_code:
                body["regionCode"] = search.country_code.upper()
            url = V1_TEXT_SEARCH

        collected: List[Place] = []
        page_token: Optional[str] = None

        for page in range(self.settings.max_pages):
            if page_token:
                body["pageToken"] = page_token

            try:
                payload = self.http.post_json(url, json_body=body, headers=headers)
            except HttpError as exc:
                if exc.status_code in (401, 403):
                    raise PlacesError(
                        f"Places API (v1) rejected the request ({exc.status_code}). "
                        "Confirm 'Places API (New)' is enabled and the key is unrestricted "
                        f"for this host. Response: {exc.body[:300]}"
                    ) from exc
                logger.error("Places v1 request failed: %s", exc)
                break

            results = payload.get("places") or []
            for raw in results:
                collected.append(_place_from_v1(raw, search.category))

            logger.info(
                "Page %d: %d result(s) (running total %d)",
                page + 1, len(results), len(collected),
            )

            if len(collected) >= search.max_results:
                break

            page_token = payload.get("nextPageToken")
            if not page_token or search.search_mode == "nearby":
                # searchNearby does not paginate.
                break

        return collected

    # ------------------------------------------------------------------
    # Details
    # ------------------------------------------------------------------

    def fetch_details(self, place: Place) -> Place:
        """Enrich one :class:`Place` with phone, website and opening status.

        Returns the place unchanged if the lookup fails, so a single bad
        response never removes a business from the run.
        """
        cache_key = f"{self.settings.api_version}:{place.place_id}"
        if self.cache is not None:
            cached = self.cache.get("place_details", cache_key)
            if cached is not None:
                return _merge_details(place, cached, self.settings.is_v1)

        try:
            if self.settings.is_v1:
                raw = self._fetch_details_v1(place.place_id)
            else:
                raw = self._fetch_details_legacy(place.place_id)
        except PlacesError:
            raise
        except HttpError as exc:
            logger.warning("Details lookup failed for %s: %s", place.name, exc)
            return place

        if raw is None:
            return place

        if self.cache is not None:
            self.cache.set("place_details", cache_key, raw)
        return _merge_details(place, raw, self.settings.is_v1)

    def _fetch_details_legacy(self, place_id: str) -> Optional[Dict[str, Any]]:
        params = {
            "place_id": place_id,
            "fields": ",".join(LEGACY_DETAIL_FIELDS),
            "key": self.settings.api_key,
            "language": self.settings.language,
        }
        payload = self.http.get_json(LEGACY_DETAILS, params=params)
        status = payload.get("status", "")
        if status == "OK":
            return payload.get("result") or {}
        if status in {"REQUEST_DENIED", "OVER_QUERY_LIMIT"}:
            raise PlacesError(
                f"Google Places Details returned {status}: "
                f"{payload.get('error_message', 'no detail provided')}"
            )
        logger.debug("Details for %s returned %s", place_id, status)
        return None

    def _fetch_details_v1(self, place_id: str) -> Optional[Dict[str, Any]]:
        headers = {
            "X-Goog-Api-Key": self.settings.api_key,
            "X-Goog-FieldMask": ",".join(V1_DETAIL_FIELDS),
        }
        # v1 IDs are already bare; legacy IDs work here too.
        url = f"{V1_BASE}/places/{place_id}"
        payload = self.http.get_json(
            url, headers=headers, params={"languageCode": self.settings.language}
        )
        return payload or None

    def enrich(self, places: Iterable[Place], progress: Any = None) -> List[Place]:
        """Fetch details for every place, updating *progress* if supplied.

        Details lookups are issued serially: the shared
        :class:`http_client.RateLimiter` already paces them, and Places Details
        is fast enough that concurrency buys little while raising the risk of
        tripping the per-second quota.
        """
        enriched: List[Place] = []
        for place in places:
            enriched.append(self.fetch_details(place))
            if progress is not None:
                progress.update(1)
        return enriched


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _place_from_legacy_search(raw: Dict[str, Any], category: str) -> Place:
    location = (raw.get("geometry") or {}).get("location") or {}
    opening = raw.get("opening_hours") or {}
    return Place(
        place_id=raw.get("place_id", ""),
        name=raw.get("name", ""),
        category=category,
        types=list(raw.get("types") or []),
        address=raw.get("formatted_address") or raw.get("vicinity") or "",
        rating=_as_float(raw.get("rating")),
        review_count=int(raw.get("user_ratings_total") or 0),
        business_status=raw.get("business_status", ""),
        open_now=opening.get("open_now"),
        latitude=_as_float(location.get("lat")),
        longitude=_as_float(location.get("lng")),
    )


def _place_from_v1(raw: Dict[str, Any], category: str) -> Place:
    location = raw.get("location") or {}
    opening = raw.get("currentOpeningHours") or {}
    return Place(
        place_id=raw.get("id", ""),
        name=(raw.get("displayName") or {}).get("text", ""),
        category=category,
        types=list(raw.get("types") or []),
        address=raw.get("formattedAddress", ""),
        phone=raw.get("nationalPhoneNumber", ""),
        international_phone=raw.get("internationalPhoneNumber", ""),
        website=raw.get("websiteUri", ""),
        rating=_as_float(raw.get("rating")),
        review_count=int(raw.get("userRatingCount") or 0),
        business_status=raw.get("businessStatus", ""),
        open_now=opening.get("openNow"),
        latitude=_as_float(location.get("latitude")),
        longitude=_as_float(location.get("longitude")),
        maps_url=raw.get("googleMapsUri", ""),
    )


def _merge_details(place: Place, raw: Dict[str, Any], is_v1: bool) -> Place:
    """Overlay a Details response onto an existing search-result Place."""
    if is_v1:
        detail = _place_from_v1(raw, place.category)
    else:
        location = (raw.get("geometry") or {}).get("location") or {}
        opening = raw.get("current_opening_hours") or raw.get("opening_hours") or {}
        detail = Place(
            place_id=raw.get("place_id", place.place_id),
            name=raw.get("name", place.name),
            category=place.category,
            types=list(raw.get("types") or []),
            address=raw.get("formatted_address", ""),
            phone=raw.get("formatted_phone_number", ""),
            international_phone=raw.get("international_phone_number", ""),
            website=raw.get("website", ""),
            rating=_as_float(raw.get("rating")),
            review_count=int(raw.get("user_ratings_total") or 0),
            business_status=raw.get("business_status", ""),
            open_now=opening.get("open_now"),
            latitude=_as_float(location.get("lat")),
            longitude=_as_float(location.get("lng")),
            maps_url=raw.get("url", ""),
        )

    # Prefer detail values, but never overwrite a good search value with a blank.
    place.name = detail.name or place.name
    place.types = detail.types or place.types
    place.address = detail.address or place.address
    place.phone = detail.phone or place.phone
    place.international_phone = detail.international_phone or place.international_phone
    place.website = detail.website or place.website
    place.rating = detail.rating if detail.rating is not None else place.rating
    place.review_count = detail.review_count or place.review_count
    place.business_status = detail.business_status or place.business_status
    place.open_now = detail.open_now if detail.open_now is not None else place.open_now
    place.latitude = detail.latitude if detail.latitude is not None else place.latitude
    place.longitude = detail.longitude if detail.longitude is not None else place.longitude
    place.maps_url = detail.maps_url or place.maps_url or (
        f"https://www.google.com/maps/place/?q=place_id:{place.place_id}"
        if place.place_id else ""
    )
    return place


def _dedupe(places: Sequence[Place]) -> List[Place]:
    """Drop repeats by place_id, then by (name, address)."""
    seen_ids = set()
    seen_pairs = set()
    unique: List[Place] = []
    for place in places:
        if place.place_id and place.place_id in seen_ids:
            continue
        pair = (place.name.strip().lower(), place.address.strip().lower())
        if pair in seen_pairs and pair != ("", ""):
            continue
        if place.place_id:
            seen_ids.add(place.place_id)
        seen_pairs.add(pair)
        unique.append(place)
    return unique


def filter_places(places: Sequence[Place], search: SearchConfig) -> List[Place]:
    """Apply the user's quality filters, logging what each one removed."""
    kept: List[Place] = []
    dropped_reviews = dropped_rating = dropped_closed = dropped_website = 0

    for place in places:
        if place.business_status and place.business_status.upper() not in {
            "OPERATIONAL",
            "",
        }:
            dropped_closed += 1
            continue
        if place.review_count < search.min_reviews:
            dropped_reviews += 1
            continue
        if search.min_rating > 0 and (place.rating or 0.0) < search.min_rating:
            dropped_rating += 1
            continue
        if search.require_no_website and place.has_website:
            dropped_website += 1
            continue
        kept.append(place)

    for count, label in (
        (dropped_closed, "permanently/temporarily closed"),
        (dropped_reviews, f"fewer than {search.min_reviews} reviews"),
        (dropped_rating, f"rated below {search.min_rating}"),
        (dropped_website, "already have a website"),
    ):
        if count:
            logger.info("Filtered out %d business(es): %s", count, label)

    return kept


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
