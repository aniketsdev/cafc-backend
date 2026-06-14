"""
residents/pdf_templates.py
--------------------------
HTML templates for Care Plan PDF generation.
- generate_care_plan_report_html: Residential Progress Note (matches CAFC format)
"""

import base64
import calendar
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_logo_data_uri():
    """
    Load the CAFC logo and return a base64 data URI string.
    Checks backend static first, then falls back to frontend assets.
    Returns empty string if logo is not found.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    candidates = [
        backend_dir / "static" / "images" / "logo.png",
        backend_dir / "static" / "images" / "logo.svg",
        backend_dir.parent / "cafc-frontend" / "src" / "assets" / "images" / "logo.svg",
    ]

    for logo_path in candidates:
        if logo_path.exists():
            try:
                raw = logo_path.read_bytes()
                b64 = base64.b64encode(raw).decode("ascii")
                ext = logo_path.suffix.lower()
                if ext == ".svg":
                    mime = "image/svg+xml"
                elif ext == ".png":
                    mime = "image/png"
                elif ext in (".jpg", ".jpeg"):
                    mime = "image/jpeg"
                else:
                    mime = "application/octet-stream"
                return f"data:{mime};base64,{b64}"
            except Exception:
                logger.warning("Failed to read logo at %s", logo_path)
                continue

    return ""


def generate_care_plan_report_html(report, lead):
    """
    Build a full HTML string for the Residential Progress Note PDF.

    Matches the official CAFC "Common Actions for Change — Residential Progress Note"
    document format with:
    - Centered logo + org name + document title
    - Client info (name, report date, from/thru dates)
    - Numbered goals in italic
    - Methodology section
    - Report on each goal
    - Supportive Service Note

    Args:
        report: CarePlanReport instance
        lead: Lead instance (for resident personal info)

    Returns:
        str: Complete HTML document string
    """
    # Resident info
    resident = report.resident
    first_name = resident.first_name or ""
    last_name = resident.last_name or ""
    resident_name = f"{first_name} {last_name}".strip() or "N/A"

    # Report period
    month = report.report_month
    year = report.report_year
    month_name = calendar.month_name[month]
    _, last_day = calendar.monthrange(year, month)
    from_date = f"{month_name} {1}, {year}"
    thru_date = f"{month_name} {last_day}, {year}"

    # Report date (when the report was created)
    report_date_str = ""
    if report.created_at:
        report_date_str = report.created_at.strftime("%B %d, %Y")

    # Report data
    report_data = report.report_data or {}
    goals = report_data.get("goals", [])
    methodology = report_data.get("methodology", "")
    supportive_service_note = report_data.get("supportive_service_note", "")

    # Build goals listing (italic descriptions)
    goals_listing_html = ""
    for idx, goal in enumerate(goals, start=1):
        description = goal.get("description", "")
        goals_listing_html += f"""
        <p class="goal-item">
            <strong><em>Goal {idx}:</em></strong>
            <em>{_esc(description)}</em>
        </p>
        """

    # Build methodology section
    methodology_html = ""
    if methodology:
        methodology_html = f"""
        <p class="section-block">
            <strong>Methodology:</strong> {_esc(methodology)}
        </p>
        """

    # Build report-on-goal sections
    reports_html = ""
    for idx, goal in enumerate(goals, start=1):
        report_text = goal.get("report_text", "")
        reports_html += f"""
        <p class="section-block">
            <strong>Report on Goal {idx}:</strong> {_esc(report_text)}
        </p>
        """

    # Build supportive service note section
    supportive_html = ""
    if supportive_service_note:
        supportive_html = f"""
        <p class="section-block supportive-note">
            <strong>Supportive Service Note:</strong> {_esc(supportive_service_note)}
        </p>
        """

    # Logo
    logo_uri = _get_logo_data_uri()
    logo_html = ""
    if logo_uri:
        logo_html = f'<img class="logo" src="{logo_uri}" alt="CAFC Logo" />'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
    @page {{
        size: letter;
        margin: 1in 1in 1in 1in;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: "Times New Roman", Times, Georgia, serif;
        font-size: 12px;
        color: #000;
        line-height: 1.5;
    }}

    /* --- Header / Letterhead --- */
    .header {{
        text-align: center;
        margin-bottom: 24px;
    }}
    .logo {{
        width: 60px;
        height: 60px;
        margin-bottom: 6px;
    }}
    .org-name {{
        font-size: 16px;
        font-weight: normal;
        letter-spacing: 0.5px;
        margin-bottom: 2px;
    }}
    .doc-title {{
        font-size: 13px;
        font-weight: normal;
    }}

    /* --- Client Info Block --- */
    .client-info {{
        margin-bottom: 20px;
    }}
    .client-info .row {{
        margin-bottom: 2px;
        font-size: 12px;
    }}
    .client-info .row strong {{
        font-weight: 700;
    }}

    /* --- Goal Items (italic descriptions) --- */
    .goal-item {{
        margin-bottom: 10px;
        font-size: 12px;
        line-height: 1.5;
    }}

    /* --- Section blocks (methodology, reports, supportive note) --- */
    .section-block {{
        margin-bottom: 14px;
        font-size: 12px;
        line-height: 1.5;
    }}
    .section-block strong {{
        font-weight: 700;
    }}

    /* Extra top spacing before methodology and supportive note */
    .supportive-note {{
        margin-top: 24px;
    }}
</style>
</head>
<body>
    <div class="header">
        {logo_html}
        <div class="org-name">Common Actions for Change</div>
        <div class="doc-title">Residential Progress Note</div>
    </div>

    <div class="client-info">
        <div class="row"><strong>Client:</strong> {_esc(resident_name)}</div>
        <div class="row">
            <strong>Report Date:</strong> {_esc(report_date_str)}
            &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
            <strong>Report Month/Year-</strong> {_esc(month_name)} {year}
        </div>
        <div class="row">
            <strong>From Date:</strong> {_esc(from_date)}
            &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
            <strong>Thru Date:</strong> {_esc(thru_date)}
        </div>
    </div>

    {goals_listing_html}

    {methodology_html}

    {reports_html}

    {supportive_html}
</body>
</html>"""

    return html


def _esc(value):
    """Escape HTML special characters."""
    if not value:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
