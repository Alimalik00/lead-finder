"""Export leads to Excel (and optionally CSV / JSON).

The Excel workbook is the deliverable a salesperson actually opens, so it is
formatted for use rather than just dumped:

* **Leads** -- one row per business, sorted hottest first, with frozen headers,
  an autofilter, sensible column widths, colour-coded priority, a colour scale
  on the score, and clickable website / email / Maps links.
* **Summary** -- run parameters and aggregate statistics.
* **Issues** -- long-format ``business x issue`` rows, handy for pivoting or
  for filtering the list down to "everyone who has no booking system".

Column order matches the brief; extra diagnostic columns follow after it.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from config import AppConfig
from logging_setup import get_logger
from models import Lead

logger = get_logger("export")

# --- Styling ---------------------------------------------------------------

HEADER_FILL = PatternFill("solid", start_color="1F3864", end_color="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)

PRIORITY_FILLS: Dict[str, PatternFill] = {
    "Hot": PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE"),
    "Warm": PatternFill("solid", start_color="FFEB9C", end_color="FFEB9C"),
    "Cold": PatternFill("solid", start_color="DDEBF7", end_color="DDEBF7"),
}
PRIORITY_FONTS: Dict[str, Font] = {
    "Hot": Font(color="9C0006", bold=True),
    "Warm": Font(color="9C6500", bold=True),
    "Cold": Font(color="1F4E78"),
}

LINK_FONT = Font(color="0563C1", underline="single")

#: Columns that hold long prose and get a wrapped, wide cell.
WIDE_COLUMNS = {
    "Website Issues",
    "Reason",
    "Suggested Services",
    "Generated Email",
    "Generated LinkedIn Message",
    "Score Breakdown",
}

MAX_COLUMN_WIDTH = 60
MIN_COLUMN_WIDTH = 10

#: Excel refuses to store a cell longer than this.
EXCEL_CELL_LIMIT = 32_000


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _fmt_bool(value: Optional[bool]) -> str:
    if value is None:
        return ""
    return "Yes" if value else "No"


def _fmt_ms(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value / 1000:.2f}s"


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


#: ``(header, extractor)`` pairs. The first block is the column set requested
#: in the brief; everything after it is supplementary diagnostic data.
COLUMNS: Sequence[Tuple[str, Callable[[Lead], Any]]] = (
    ("Business Name", lambda l: l.place.name),
    ("Category", lambda l: l.place.category),
    ("Phone", lambda l: l.place.phone or l.place.international_phone),
    ("Website", lambda l: l.place.website),
    ("Email", lambda l: l.email),
    ("Rating", lambda l: l.place.rating),
    ("Reviews", lambda l: l.place.review_count),
    ("Lead Score", lambda l: l.score.score),
    ("Priority", lambda l: l.score.priority),
    ("Website Issues", lambda l: "; ".join(l.issues)),
    ("PageSpeed Mobile", lambda l: l.pagespeed.mobile_score if l.pagespeed else None),
    ("PageSpeed Desktop", lambda l: l.pagespeed.desktop_score if l.pagespeed else None),
    ("Reason", lambda l: l.summary.reason),
    ("Suggested Services", lambda l: ", ".join(
        l.summary.suggested_services or l.score.suggested_services)),
    ("Generated Email", lambda l: l.summary.email),
    ("Generated LinkedIn Message", lambda l: l.summary.linkedin_message),

    # --- Supplementary ---
    ("Needs Website", lambda l: _fmt_bool(l.score.needs_website)),
    ("Address", lambda l: l.place.address),
    ("Open Now", lambda l: _fmt_bool(l.place.open_now)),
    ("Business Status", lambda l: l.place.business_status),
    ("Score Breakdown", lambda l: l.score.breakdown),
    ("Site Reachable", lambda l: _fmt_bool(l.audit.reachable) if l.audit else ""),
    ("HTTPS", lambda l: _fmt_bool(l.audit.https) if l.audit else ""),
    ("Mobile Friendly", lambda l: _fmt_bool(l.audit.mobile_responsive) if l.audit else ""),
    ("Looks Outdated", lambda l: _fmt_bool(l.audit.looks_outdated) if l.audit else ""),
    ("Has CTA", lambda l: _fmt_bool(l.audit.has_cta) if l.audit else ""),
    ("Contact Form", lambda l: _fmt_bool(l.audit.has_contact_form) if l.audit else ""),
    ("Online Booking", lambda l: _fmt_bool(l.audit.has_booking) if l.audit else ""),
    ("Online Ordering", lambda l: _fmt_bool(l.audit.has_online_ordering) if l.audit else ""),
    ("Live Chat", lambda l: _fmt_bool(l.audit.has_chat) if l.audit else ""),
    ("WhatsApp", lambda l: _fmt_bool(l.audit.has_whatsapp) if l.audit else ""),
    ("Social Profiles", lambda l: ", ".join(l.audit.social_links) if l.audit else ""),
    ("Platform / CMS", lambda l: l.audit.cms if l.audit else ""),
    ("Broken Images", lambda l: l.audit.broken_images if l.audit else None),
    ("Broken Links", lambda l: l.audit.broken_links if l.audit else None),
    ("LCP (mobile)", lambda l: _fmt_ms(
        l.pagespeed.mobile.lcp_ms if l.pagespeed and l.pagespeed.mobile else None)),
    ("CLS (mobile)", lambda l: _round(
        l.pagespeed.mobile.cls if l.pagespeed and l.pagespeed.mobile else None, 3)),
    ("INP (mobile)", lambda l: _fmt_ms(
        l.pagespeed.mobile.inp_ms if l.pagespeed and l.pagespeed.mobile else None)),
    ("FCP (mobile)", lambda l: _fmt_ms(
        l.pagespeed.mobile.fcp_ms if l.pagespeed and l.pagespeed.mobile else None)),
    ("SEO Score", lambda l: l.pagespeed.mobile.seo_score
        if l.pagespeed and l.pagespeed.mobile else None),
    ("Accessibility Score", lambda l: l.pagespeed.mobile.accessibility_score
        if l.pagespeed and l.pagespeed.mobile else None),
    ("Copy Source", lambda l: l.summary.generator),
    ("Google Place ID", lambda l: l.place.place_id),
    ("Latitude", lambda l: l.place.latitude),
    ("Longitude", lambda l: l.place.longitude),
    ("Google Maps URL", lambda l: l.place.maps_url),
)

HEADERS: List[str] = [header for header, _ in COLUMNS]


def leads_to_rows(leads: Sequence[Lead]) -> List[Dict[str, Any]]:
    """Flatten leads into spreadsheet rows."""
    rows: List[Dict[str, Any]] = []
    for lead in leads:
        row: Dict[str, Any] = {}
        for header, extract in COLUMNS:
            try:
                value = extract(lead)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Column %r failed for %s: %s", header, lead.name, exc)
                value = ""
            if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT:
                value = value[: EXCEL_CELL_LIMIT - 3] + "..."
            row[header] = value
        rows.append(row)
    return rows


def leads_to_dataframe(leads: Sequence[Lead]) -> pd.DataFrame:
    """Build the main leads DataFrame, sorted hottest first."""
    frame = pd.DataFrame(leads_to_rows(leads), columns=HEADERS)
    if not frame.empty:
        frame = frame.sort_values(
            by=["Lead Score", "Reviews"], ascending=[False, False]
        ).reset_index(drop=True)
    return frame


# ---------------------------------------------------------------------------
# Auxiliary sheets
# ---------------------------------------------------------------------------


def build_summary_rows(leads: Sequence[Lead], config: AppConfig) -> List[Dict[str, Any]]:
    """Run parameters and aggregate statistics for the Summary sheet."""
    total = len(leads)
    priorities = Counter(lead.score.priority for lead in leads)
    no_website = sum(1 for lead in leads if not lead.place.has_website)
    unreachable = sum(
        1 for lead in leads
        if lead.place.has_website and lead.audit is not None and not lead.audit.reachable
    )
    with_email = sum(1 for lead in leads if lead.email)
    outdated = sum(
        1 for lead in leads if lead.audit is not None and lead.audit.looks_outdated
    )
    no_booking = sum(
        1 for lead in leads
        if lead.audit is not None and lead.audit.reachable and not lead.audit.has_booking
    )
    scores = [lead.score.score for lead in leads]
    mobile_scores = [
        lead.pagespeed.mobile_score for lead in leads
        if lead.pagespeed and lead.pagespeed.mobile_score is not None
    ]

    search = config.search

    def pct(count: int) -> str:
        return f"{(100.0 * count / total):.0f}%" if total else "-"

    rows: List[Dict[str, Any]] = [
        {"Metric": "Generated at", "Value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"Metric": "Country", "Value": search.country},
        {"Metric": "State / Province", "Value": search.state or "-"},
        {"Metric": "City", "Value": search.city},
        {"Metric": "Category", "Value": search.category},
        {"Metric": "Search mode", "Value": search.search_mode},
        {"Metric": "Search radius (m)", "Value": search.radius_m},
        {"Metric": "Max results requested", "Value": search.max_results},
        {"Metric": "Min reviews filter", "Value": search.min_reviews},
        {"Metric": "Min rating filter", "Value": search.min_rating},
        {"Metric": "", "Value": ""},
        {"Metric": "Total leads", "Value": total},
        {"Metric": "Hot leads", "Value": f"{priorities.get('Hot', 0)} ({pct(priorities.get('Hot', 0))})"},
        {"Metric": "Warm leads", "Value": f"{priorities.get('Warm', 0)} ({pct(priorities.get('Warm', 0))})"},
        {"Metric": "Cold leads", "Value": f"{priorities.get('Cold', 0)} ({pct(priorities.get('Cold', 0))})"},
        {"Metric": "", "Value": ""},
        {"Metric": "No website at all", "Value": f"{no_website} ({pct(no_website)})"},
        {"Metric": "Website listed but not loading", "Value": unreachable},
        {"Metric": "Website looks outdated", "Value": outdated},
        {"Metric": "No online booking", "Value": no_booking},
        {"Metric": "Email address discovered", "Value": f"{with_email} ({pct(with_email)})"},
        {"Metric": "", "Value": ""},
        {
            "Metric": "Average lead score",
            "Value": f"{sum(scores) / len(scores):.1f}" if scores else "-",
        },
        {
            "Metric": "Average mobile PageSpeed",
            "Value": f"{sum(mobile_scores) / len(mobile_scores):.0f}"
            if mobile_scores else "not measured",
        },
    ]
    return rows


def build_issue_rows(leads: Sequence[Lead]) -> List[Dict[str, Any]]:
    """Long-format ``business x issue`` rows for pivoting and filtering."""
    rows: List[Dict[str, Any]] = []
    for lead in leads:
        if not lead.place.has_website:
            rows.append(
                {
                    "Business Name": lead.place.name,
                    "Website": "",
                    "Lead Score": lead.score.score,
                    "Priority": lead.score.priority,
                    "Issue": "No website",
                }
            )
            continue
        for issue in lead.issues:
            rows.append(
                {
                    "Business Name": lead.place.name,
                    "Website": lead.place.website,
                    "Lead Score": lead.score.score,
                    "Priority": lead.score.priority,
                    "Issue": issue,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Worksheet formatting
# ---------------------------------------------------------------------------


def _style_header(sheet: Worksheet, column_count: int) -> None:
    for column in range(1, column_count + 1):
        cell = sheet.cell(row=1, column=column)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=True)
    sheet.row_dimensions[1].height = 28


def _autosize(sheet: Worksheet, frame: pd.DataFrame) -> None:
    for index, header in enumerate(frame.columns, start=1):
        letter = get_column_letter(index)
        if header in WIDE_COLUMNS:
            sheet.column_dimensions[letter].width = MAX_COLUMN_WIDTH
            continue
        longest = len(str(header))
        # Sampling the first 200 rows keeps this fast on large exports while
        # still producing sensible widths.
        for value in frame[header].head(200):
            longest = max(longest, len(str(value)) if value is not None else 0)
        sheet.column_dimensions[letter].width = max(
            MIN_COLUMN_WIDTH, min(longest + 2, 40)
        )


def _format_leads_sheet(sheet: Worksheet, frame: pd.DataFrame) -> None:
    rows = len(frame)
    columns = len(frame.columns)
    if columns == 0:
        return

    _style_header(sheet, columns)
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(columns)}{max(rows + 1, 1)}"
    _autosize(sheet, frame)

    if rows == 0:
        return

    header_index = {name: i + 1 for i, name in enumerate(frame.columns)}

    # Wrap the prose columns and top-align every data row.
    for header in WIDE_COLUMNS & set(header_index):
        letter = get_column_letter(header_index[header])
        for row in range(2, rows + 2):
            sheet[f"{letter}{row}"].alignment = Alignment(wrap_text=True, vertical="top")

    # Colour-code the priority column.
    priority_col = header_index.get("Priority")
    if priority_col:
        letter = get_column_letter(priority_col)
        for row in range(2, rows + 2):
            cell = sheet[f"{letter}{row}"]
            fill = PRIORITY_FILLS.get(str(cell.value))
            if fill is not None:
                cell.fill = fill
                cell.font = PRIORITY_FONTS[str(cell.value)]
            cell.alignment = Alignment(horizontal="center", vertical="top")

    # Colour scale on the score so the eye lands on the best leads first.
    score_col = header_index.get("Lead Score")
    if score_col:
        letter = get_column_letter(score_col)
        span = f"{letter}2:{letter}{rows + 1}"
        sheet.conditional_formatting.add(
            span,
            ColorScaleRule(
                start_type="num", start_value=0, start_color="F8696B",
                mid_type="num", mid_value=50, mid_color="FFEB84",
                end_type="num", end_value=100, end_color="63BE7B",
            ),
        )

    # Flag low PageSpeed scores in red.
    for header in ("PageSpeed Mobile", "PageSpeed Desktop"):
        col = header_index.get(header)
        if not col:
            continue
        letter = get_column_letter(col)
        sheet.conditional_formatting.add(
            f"{letter}2:{letter}{rows + 1}",
            CellIsRule(
                operator="lessThan",
                formula=["50"],
                fill=PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE"),
                font=Font(color="9C0006"),
            ),
        )

    # Make links clickable.
    for header, prefix in (
        ("Website", ""),
        ("Google Maps URL", ""),
        ("Email", "mailto:"),
    ):
        col = header_index.get(header)
        if not col:
            continue
        letter = get_column_letter(col)
        for row in range(2, rows + 2):
            cell = sheet[f"{letter}{row}"]
            value = str(cell.value or "").strip()
            if not value:
                continue
            if prefix:
                target = prefix + value
            elif value.lower().startswith(("http://", "https://")):
                target = value
            else:
                continue
            try:
                cell.hyperlink = target
                cell.font = LINK_FONT
            except Exception:  # pragma: no cover - malformed URL
                continue


def _format_simple_sheet(sheet: Worksheet, frame: pd.DataFrame) -> None:
    columns = len(frame.columns)
    if columns == 0:
        return
    _style_header(sheet, columns)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(columns)}{max(len(frame) + 1, 1)}"
    _autosize(sheet, frame)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_output_path(config: AppConfig) -> Path:
    """Render the configured filename template into a concrete path."""
    directory = config.output_dir()
    directory.mkdir(parents=True, exist_ok=True)

    fields = {
        "city": _slug(config.search.city) or "all",
        "state": _slug(config.search.state or ""),
        "country": _slug(config.search.country),
        "category": _slug(config.search.category) or "leads",
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
    }
    try:
        name = config.output.filename_template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Invalid output.filename_template (%s); falling back to the default.", exc
        )
        name = "leads_{city}_{category}_{timestamp}.xlsx".format(**fields)
    if not name.lower().endswith((".xlsx", ".xlsm")):
        name += ".xlsx"
    return directory / name


def export_leads(
    leads: Sequence[Lead],
    config: AppConfig,
    output_path: Optional[Path] = None,
) -> Path:
    """Write the Excel workbook (plus any configured side files).

    Returns the path to the workbook.
    """
    path = output_path or build_output_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    leads_frame = leads_to_dataframe(leads)
    summary_frame = pd.DataFrame(build_summary_rows(leads, config))
    issues_frame = pd.DataFrame(
        build_issue_rows(leads),
        columns=["Business Name", "Website", "Lead Score", "Priority", "Issue"],
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        leads_frame.to_excel(writer, sheet_name="Leads", index=False)
        summary_frame.to_excel(writer, sheet_name="Summary", index=False)
        issues_frame.to_excel(writer, sheet_name="Issues", index=False)

        _format_leads_sheet(writer.sheets["Leads"], leads_frame)
        _format_simple_sheet(writer.sheets["Summary"], summary_frame)
        _format_simple_sheet(writer.sheets["Issues"], issues_frame)

    logger.info("Wrote %d lead(s) to %s", len(leads_frame), path)

    if config.output.write_csv:
        csv_path = path.with_suffix(".csv")
        leads_frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info("Wrote CSV copy to %s", csv_path)

    if config.output.write_json:
        json_path = path.with_suffix(".json")
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "search": {
                "country": config.search.country,
                "state": config.search.state,
                "city": config.search.city,
                "category": config.search.category,
                "radius_m": config.search.radius_m,
            },
            "leads": [lead.to_dict() for lead in leads],
        }
        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        logger.info("Wrote JSON copy to %s", json_path)

    return path


def _slug(value: str) -> str:
    """Filesystem-safe fragment for use in filenames."""
    cleaned = "".join(
        char.lower() if char.isalnum() else "_" for char in (value or "").strip()
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")
