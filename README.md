# Lead Finder

Find local businesses that are likely to need a new website or other software
services — automatically.

Instead of manually trawling Google Maps, Lead Finder discovers businesses in a
city and category via the **official Google Places API**, audits whatever web
presence they have, scores each one as a sales lead, writes the outreach copy,
and exports a formatted Excel workbook.

```
python main.py --country "United States" --state Texas --city Austin --category Dentists
```

```
==============================================================================
  47 lead(s) scored
  Hot: 12   Warm: 19   Cold: 16   |   No website: 9   Emails found: 23
==============================================================================

  Top 10 lead(s):

  Score  Priority  Business                            Why
  -----  --------  ----------------------------------  ------------------------------------
     91  Hot       Lakeview Family Dental              No website listed on Google
     83  Hot       Congress Ave Orthodontics           No website listed on Google
     74  Hot       Barton Springs Dental Care          Website design looks outdated
     ...
```

---

## Contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Getting a Google API key](#getting-a-google-api-key)
- [Usage](#usage)
- [Configuration](#configuration)
- [What gets checked](#what-gets-checked)
- [Lead scoring](#lead-scoring)
- [AI recommendations](#ai-recommendations)
- [Output](#output)
- [Cost and quota](#cost-and-quota)
- [Extending it](#extending-it)
- [Troubleshooting](#troubleshooting)
- [Responsible use](#responsible-use)

---

## How it works

```
   ┌──────────────┐
   │  Your input  │  country / state / city / category / filters
   └──────┬───────┘
          ▼
   ┌──────────────────────┐
   │ 1. Geocoding API     │  "Austin, Texas, US" → lat/lng
   ├──────────────────────┤
   │ 2. Places Search     │  Text or Nearby search, auto-paginated
   │ 3. Places Details    │  phone, website, rating, reviews, hours
   ├──────────────────────┤
   │ 4. Filters           │  min reviews, min rating, still trading
   ├──────────────────────┤
   │ 5. Website audit     │  HTTPS, markup, UX, CTA, booking, email…
   │ 6. PageSpeed API     │  mobile/desktop scores, LCP, CLS, INP, FCP
   ├──────────────────────┤
   │ 7. Lead scoring      │  0–100, Hot / Warm / Cold, with reasons
   │ 8. Outreach copy     │  Claude, or deterministic templates
   ├──────────────────────┤
   │ 9. Excel export      │  Leads + Summary + Issues sheets
   └──────────────────────┘
```

Google Maps is **never scraped** — everything comes from the official APIs.
Business websites are fetched exactly once each, the way a browser or
Lighthouse would.

---

## Project structure

```
lead_finder/
├── main.py               CLI + pipeline orchestration
├── config.py             Dataclass config, .env + YAML loading
├── models.py             Typed data structures shared across stages
├── google_places.py      Places search / details / geocoding (legacy + v1)
├── website_audit.py      Technical, UX and business-capability checks
├── pagespeed.py          PageSpeed Insights / Lighthouse client
├── lead_scoring.py       Rule-based 0–100 scoring and prioritisation
├── ai_summary.py         Claude + template outreach copy generators
├── export.py             Formatted Excel / CSV / JSON export
├── http_client.py        Shared retries, back-off and rate limiting
├── cache.py              On-disk response cache
├── logging_setup.py      Console + rotating file logging
├── tests/
│   └── test_offline.py   47 tests, no network or API keys required
├── requirements.txt
├── config.example.yaml   Sample configuration (copy to config.yaml)
├── .env.example          Sample secrets file (copy to .env)
└── README.md
```

The modules in the second block (`http_client`, `cache`, `logging_setup`,
`models`) are shared infrastructure — every stage depends on them, and nothing
in them knows about leads.

---

## Setup

**Requirements:** Python 3.9 or newer (3.11+ recommended).

```bash
# 1. Get the code and enter the directory
cd lead_finder

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Add your API keys
cp .env.example .env
# then edit .env and set GOOGLE_MAPS_API_KEY

# 5. Run it
python main.py --country "United States" --city Austin --category Dentists
```

On Windows use the launcher:

```
py main.py --country "United States" --city Austin --category Dentists
```

Optionally copy the sample config for repeatable runs:

```bash
cp config.example.yaml config.yaml
python main.py --config config.yaml
```

---

## Getting a Google API key

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. Enable billing. Google's monthly free credit covers a substantial number of
   requests, but the APIs will not answer at all without billing enabled.
3. Under **APIs & Services → Library**, enable:
   - **Places API** *(or "Places API (New)" if you set `api_version: v1`)*
   - **Geocoding API**
   - **PageSpeed Insights API**
4. Under **APIs & Services → Credentials**, create an API key.
5. Restrict the key to those three APIs (**API restrictions**). Leave
   application restrictions off, or use an IP restriction — an HTTP-referrer
   restriction will block server-side calls.
6. Paste it into `.env` as `GOOGLE_MAPS_API_KEY`.

---

## Usage

### Common runs

```bash
# Basic
python main.py --country "United States" --city Austin --category Dentists

# Only busy businesses that have no website at all — the hottest leads
python main.py --country UAE --city Dubai --category Restaurants \
               --min-reviews 50 --no-website-only

# Wider net, more results, skip the slow PageSpeed stage
python main.py --country Canada --state Ontario --city Toronto \
               --category "Roofing" --radius 25000 --max-results 60 --no-pagespeed

# See who is out there before spending any audit time or quota
python main.py --country UK --city Manchester --category Gyms --dry-run

# Answer prompts instead of remembering flags
python main.py --interactive
```

### All options

| Flag | Description |
| --- | --- |
| `--country` | Country name (required) |
| `--state` | State or province (optional) |
| `--city` | City name (required) |
| `--category` | Business category, e.g. `Dentists`, `HVAC`, `Real Estate` (required) |
| `--keyword` | Extra keyword appended to the search query |
| `--country-code` | Two-letter code used to bias results, e.g. `us` |
| `--place-type` | Google Places type override, e.g. `dentist` |
| `--search-mode` | `text` (default, best recall) or `nearby` (strict radius) |
| `--radius` | Search radius in metres (default 15000) |
| `--max-results` | Maximum businesses to fetch (default 60) |
| `--min-reviews` | Drop businesses below this review count |
| `--min-rating` | Drop businesses below this rating |
| `--no-website-only` | Keep only businesses with no website |
| `--no-audit` | Skip the website audit stage |
| `--no-pagespeed` | Skip PageSpeed Insights (much faster) |
| `--no-ai` | Use template copy instead of calling Claude |
| `--dry-run` | List discovered businesses and stop |
| `--output` | Explicit `.xlsx` output path |
| `--output-dir` | Directory for generated files |
| `--csv` / `--json` | Also write a CSV / JSON copy |
| `--config` | Path to a YAML config file |
| `--env-file` | Path to a `.env` file |
| `--api-version` | `legacy` (default) or `v1` Places API |
| `--no-cache` | Bypass the on-disk response cache |
| `--clear-cache` | Delete cached responses and exit |
| `--interactive` | Prompt for the search parameters |
| `--log-level` / `-v` / `-q` | Logging verbosity |

### Categories

Anything works as a free-text category. These are mapped to a Google Places
`type` automatically for better precision:

`Dentists` · `Restaurants` · `Lawyers` · `Roofing` · `Real Estate` · `HVAC` ·
`Gyms` · `Medical Clinics` · `Plumbers` · `Electricians` · `Salons` · `Spas` ·
`Accountants` · `Veterinarians` · `Car Repair` · `Hotels` · `Insurance` …

Add your own in `CATEGORY_TO_PLACE_TYPE` in `config.py`.

---

## Configuration

Settings resolve in this order, each layer overriding the one before it:

1. Defaults in `config.py`
2. `.env` (secrets and a few common toggles)
3. `--config config.yaml`
4. Command-line flags

Secrets belong in `.env` only. `config.yaml` is safe to commit and share.
See `config.example.yaml` for every available key, fully commented.

---

## What gets checked

### No website

If Google lists no website — or only a Facebook/Instagram page, which is not a
website — the lead is flagged **`Needs Website = Yes`** and scored very highly.

### Performance (PageSpeed Insights)

Mobile and desktop performance scores, plus **LCP**, **CLS**, **INP** and
**FCP**. Accessibility, best-practices and SEO scores are captured too.

> **INP** has no Lighthouse lab equivalent, so it is read from real-user CrUX
> field data when Google has enough traffic for that origin, and left blank
> otherwise. Total Blocking Time is captured as the lab-side proxy.

### Technical

HTTPS · SSL certificate errors · HTTP→HTTPS redirect · page title · meta
description · favicon · viewport meta tag · HTML5 doctype · semantic markup ·
legacy markup (`<font>`, `<center>`, table layouts, Flash, end-of-life jQuery) ·
broken images · broken links · missing alt text · CMS/framework fingerprint ·
analytics presence.

### UX

Outdated vs modern design (a 0–100 composite) · mobile responsiveness · tiny
text · typography consistency · spacing · layout clutter · navigation quality ·
presence of a clear call to action.

Heuristics are computed from the HTML, then **overridden by real Lighthouse
audits** (`viewport`, `font-size`, `tap-targets`, `color-contrast`,
`link-text`, `crawlable-anchors`) whenever PageSpeed data is available.

### Business capabilities

Online booking (Calendly, Acuity, Zocdoc, OpenTable, Mindbody, Jobber,
ServiceTitan and ~20 more) · online ordering (DoorDash, Toast, ChowNow,
Shopify…) · live chat (Intercom, Drift, Tawk.to, Crisp…) · WhatsApp button ·
appointment form · contact form · social media profiles.

### Contact discovery

`mailto:` links and inline addresses on the homepage, then the contact/about
pages, with light de-obfuscation of `name (at) domain.com`. Role addresses
(`info@`, `contact@`) are ranked first.

---

## Lead scoring

Every lead gets a **0–100 score**, a **priority**, and a full breakdown of the
rules that fired. Weights and thresholds live in `config.yaml` under `scoring`.

| Rule | Points |
| --- | ---: |
| No website | **+50** |
| Website listed but does not load | +30 |
| Design looks outdated | +15 |
| Mobile PageSpeed below 50 | +15 |
| No SSL / certificate error | +10 |
| Not mobile responsive | +10 |
| No clear call to action | +10 |
| No contact form | +10 |
| Failing Core Web Vitals (LCP / CLS) | +5 |
| No online booking | +5 |
| Missing meta description | +5 |
| Few reviews (under 20) | +5 |
| Broken images or links | +4 |
| No social media links | +3 |
| Low rating (under 4.0) | +3 |
| Missing page title | +3 |
| No live chat or WhatsApp | +2 |
| **Bonus:** 100+ reviews with a weak web presence | +8 |

**Priority:** `Hot` ≥ 70 · `Warm` ≥ 40 · `Cold` below 40.

Two deliberate design decisions:

- **A business with no website also fires the "no CTA", "no contact form" and
  "no booking" rules**, because it demonstrably has none of them. Without that,
  a no-website lead could never reach Hot — every other rule needs a site to
  inspect.
- **The `high_demand_bonus` is the sharpest signal in the model.** Lots of
  reviews plus a weak web presence means demand is already proven and only the
  conversion path is missing. Those are the calls to make first.

The `Score Breakdown` column shows exactly how each total was reached, e.g.
`No website listed on Google (+50); No online booking (+5); 214 reviews but a
weak web presence (+8)`.

---

## AI recommendations

Every lead gets a plain-English reason, a service list, a cold email and a
LinkedIn message.

**With `ANTHROPIC_API_KEY` set**, these are written by Claude
(`claude-opus-5` by default) using structured output, and the prompt is
constrained to the facts this pipeline actually measured — the model is
explicitly told not to invent metrics or claims.

**Without a key** — or with `--no-ai` — the same four fields are produced by
the deterministic template generator. That is not a placeholder: the copy is
specific, factual and sendable, it just does not vary its phrasing. The
template generator is also the per-lead fallback when an API call fails, so
every row in the export always has copy.

Example output:

> *This dental clinic has no website despite 214 Google reviews at 4.8 stars.
> Customers searching for them online have nowhere to land, so enquiries go to
> competitors who do have a site. A professional website with clear contact and
> booking would capture demand they are already generating.*

Emails are signed `[Your Name]` — replace it before sending.

---

## Output

`output/leads_<city>_<category>_<timestamp>.xlsx`, with three sheets.

**Leads** — one row per business, sorted hottest first, with frozen headers, an
autofilter, colour-coded priority, a colour scale on the score, and clickable
website / email / Maps links. Columns, in order:

`Business Name` · `Category` · `Phone` · `Website` · `Email` · `Rating` ·
`Reviews` · `Lead Score` · `Priority` · `Website Issues` · `PageSpeed Mobile` ·
`PageSpeed Desktop` · `Reason` · `Suggested Services` · `Generated Email` ·
`Generated LinkedIn Message`

…followed by diagnostics: `Needs Website`, `Address`, `Open Now`,
`Business Status`, `Score Breakdown`, `Site Reachable`, `HTTPS`,
`Mobile Friendly`, `Looks Outdated`, `Has CTA`, `Contact Form`,
`Online Booking`, `Online Ordering`, `Live Chat`, `WhatsApp`,
`Social Profiles`, `Platform / CMS`, `Broken Images`, `Broken Links`,
`LCP (mobile)`, `CLS (mobile)`, `INP (mobile)`, `FCP (mobile)`, `SEO Score`,
`Accessibility Score`, `Copy Source`, `Google Place ID`, `Latitude`,
`Longitude`, `Google Maps URL`.

**Summary** — the search parameters and aggregate statistics for the run.

**Issues** — long-format `business × issue` rows, for pivoting or for filtering
the list down to, say, everyone with no booking system.

CSV and JSON copies are written when `--csv` / `--json` are passed (CSV is on
by default in `config.example.yaml`).

---

## Cost and quota

| API | Notes |
| --- | --- |
| Geocoding | One call per run. |
| Places Text/Nearby Search | One call per page; 20 results per page, 3 pages max. |
| Places Details | **One billable call per business** — the expensive part. |
| PageSpeed Insights | Free, but slow (10–30 s per URL per strategy) and rate limited. |

Three things keep the bill and the wall-clock down:

- **Response caching.** Places Details and PageSpeed responses are cached on
  disk for 7 days (`.cache/`), so re-runs and scoring tweaks cost nothing.
  Clear it with `--clear-cache`.
- **Tight field masks.** Only the Details fields actually used are requested;
  Places bills per field group.
- **PageSpeed is skipped for unreachable sites**, and can be turned off
  entirely with `--no-pagespeed` for a much faster run.

Rough timings for 60 businesses: ~30 s without PageSpeed, ~4–6 minutes with it.

---

## Tests

The suite is fully offline — fixture HTML, a stub HTTP client, no API keys, no
extra dependencies beyond `requirements.txt`:

```bash
python -m unittest discover -s tests -v
```

It covers config layering, URL normalisation, both website-audit paths
(outdated and modern), PageSpeed parsing, the Lighthouse-overrides-heuristics
logic, every scoring branch, the template copy generator, Excel/CSV/JSON export
formatting, the cache, and the HTTP client's rate limiting and retries.

---

## Extending it

**Detect another booking/chat/ordering vendor** — add one line to the relevant
table at the top of `website_audit.py`:

```python
BOOKING_SIGNATURES = {
    ...,
    "mynewvendor.com": "MyNewVendor",
}
```

**Add a scoring rule** — add a weight to `ScoringWeights` in `config.py`, then
a small function in `LeadScorer._site_rules()`:

```python
def _no_pricing_page(audit: WebsiteAudit) -> RuleResult:
    if audit.has_pricing_page:
        return None
    return (weights.no_pricing_page, "No pricing page -- visitors bounce to compare")
```

**Add an export column** — append a `(header, extractor)` pair to `COLUMNS` in
`export.py`. Nothing else needs to change.

**Swap the AI provider** — implement `generate(lead) -> AISummary` and return
it from `build_generator()` in `ai_summary.py`.

**Add a category shortcut** — extend `CATEGORY_TO_PLACE_TYPE` in `config.py`.

---

## Troubleshooting

**`REQUEST_DENIED` from Places**
The API is not enabled for the project, billing is off, or the key has an
HTTP-referrer restriction (which blocks server-side calls). Check all three.

**No results**
Try `--search-mode text` (the default; `nearby` is much stricter), widen
`--radius`, or use a more common category term. `--dry-run` shows what the
search returns before anything else runs.

**PageSpeed returns HTTP 429**
You are over quota. Lower `pagespeed.requests_per_second` in `config.yaml`, or
add a `PAGESPEED_API_KEY` — the key-less quota is very small.

**PageSpeed returns HTTP 400 for a specific site**
Lighthouse could not render that page. This is normal for sites that block
bots; the run continues and the lead is scored without performance data.

**Audits are slow**
Lower `audit.max_links_to_check` / `audit.max_images_to_check`, or raise
`audit.max_workers`.

**`NotOpenSSLWarning` on macOS**
The system Python 3.9 links against LibreSSL. Harmless. Use a Python 3.11+
build (e.g. from python.org or Homebrew) to silence it.

**Excel column looks truncated**
Cells are capped at Excel's 32,000-character limit. The full data is in the
JSON export (`--json`).

---

## Responsible use

- **Google Maps is not scraped.** All business data comes from the official
  Places API, per its terms of service.
- **Websites are fetched once each**, with an identifying user agent you should
  set in `.env` (`LEAD_FINDER_USER_AGENT`). The homepage is loaded the way any
  browser or Lighthouse run would; `robots.txt` is honoured for any sub-page
  crawling (`audit.respect_robots`).
- **Email addresses are only collected from pages where the business published
  them publicly.** Before you use them, check what applies to you: GDPR and
  PECR in the EU/UK, CAN-SPAM in the US, CASL in Canada. Several of these
  require a lawful basis, accurate sender identification and a working opt-out.
  This tool finds leads; complying with the rules for contacting them is on you.
- **Read the generated copy before sending it.** Everything is derived from
  automated checks, and an audit can be wrong. A cold email that misstates a
  prospect's website is worse than no email at all.
