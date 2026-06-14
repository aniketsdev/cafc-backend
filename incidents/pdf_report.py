"""
Server-side PDF generator for incident completion reports.

Used by the completion email path to attach a PDF of the report to the
notification sent to Guardian and Agent. Uses ReportLab; the output is
intentionally simple (one page, plain layout) - pixel-perfect parity
with the frontend PDF is out of scope.
"""

import io
import logging
from urllib.request import urlopen

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    PageBreak,
)
from reportlab.lib.enums import TA_LEFT

logger = logging.getLogger(__name__)


def _safe(value, fallback="-"):
    if value is None or value == "":
        return fallback
    return str(value)


def _fetch_image(url, max_height_inches=1.0):
    """Download a remote image (e.g., S3 presigned URL) and return a
    flowable Image. Returns None on any failure (network, parse, etc.)."""
    if not url:
        return None
    try:
        with urlopen(url, timeout=10) as resp:
            data = resp.read()
        img = Image(io.BytesIO(data), height=max_height_inches * inch)
        img.hAlign = "LEFT"
        # Scale width proportionally
        if img.imageWidth and img.imageHeight:
            ratio = img.imageWidth / img.imageHeight
            img.drawWidth = max_height_inches * inch * ratio
            img.drawHeight = max_height_inches * inch
        return img
    except Exception as e:
        logger.warning(f"[pdf] failed to fetch signature image url={url} err={e}")
        return None


def build_incident_pdf(incident) -> bytes:
    """
    Build a one/two-page PDF report for the incident. Returns the raw bytes.
    Caller is responsible for attaching to email / saving to disk.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Incident Report {incident.uuid}",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, alignment=TA_LEFT)
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold")

    flow = []
    flow.append(Paragraph("Incident Report", h1))
    flow.append(Spacer(1, 4))

    resident_name = "-"
    if incident.resident_id:
        r = incident.resident
        resident_name = f"{r.first_name or ''} {r.last_name or ''}".strip() or r.email

    rows = [
        ["Resident", _safe(resident_name)],
        ["Incident Date/Time", _safe(incident.incident_datetime)],
        ["Location", _safe(incident.location)],
        ["Group Home", _safe(getattr(incident.group_home, "name", None) if incident.group_home_id else None)],
        ["Region", _safe(incident.region)],
        ["Agency", _safe(incident.agency_name)],
        ["Status", _safe(incident.get_status_display() if hasattr(incident, "get_status_display") else incident.status)],
        ["Completed At", _safe(incident.completed_at)],
    ]
    t = Table(rows, colWidths=[1.6 * inch, 5.0 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(t)
    flow.append(Spacer(1, 10))

    # Narrative blocks
    for title, value in [
        ("Pre-Incident Notes", getattr(incident, "pre_incident_notes", None)),
        ("Incident Description", getattr(incident, "incident_description", None)),
        ("Response / Action Taken", getattr(incident, "response_action", None)),
    ]:
        flow.append(Paragraph(title, h2))
        flow.append(Paragraph(_safe(value).replace("\n", "<br/>"), body))
        flow.append(Spacer(1, 6))

    # Flags
    def _flag_list(qs, attr):
        return ", ".join(getattr(f, attr) for f in qs) or "-"

    flow.append(Paragraph("Flags", h2))
    flag_rows = [
        ["Medical", _flag_list(incident.medical_flags.all(), "medical_type")],
        ["Legal", _flag_list(incident.legal_flags.all(), "legal_type")],
        ["Social", _flag_list(incident.social_flags.all(), "social_type")],
        ["Victim", _flag_list(incident.victim_flags.all(), "victim_type")],
    ]
    ft = Table(flag_rows, colWidths=[1.0 * inch, 5.6 * inch])
    ft.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(ft)
    flow.append(Spacer(1, 10))

    # PM Review/Follow-up
    flow.append(Paragraph("Program Manager / Coordinator Review/Follow-up", h2))
    flow.append(Paragraph(_safe(getattr(incident, "pm_review_notes", None)).replace("\n", "<br/>"), body))
    flow.append(Spacer(1, 4))
    pm_rows = [
        ["Program Type", _safe(getattr(incident, "pm_program_type", None))],
        ["Service Transition (past 6 months)", _safe(getattr(incident, "get_pm_service_transition_display", lambda: getattr(incident, "pm_service_transition", None))())],
        ["Service Transition Description", _safe(getattr(incident, "pm_service_transition_description", None))],
        ["Behavior Plan Followed?", _safe(getattr(incident, "get_pm_behavior_plan_followed_display", lambda: getattr(incident, "pm_behavior_plan_followed", None))())],
    ]
    pmt = Table(pm_rows, colWidths=[2.4 * inch, 4.2 * inch])
    pmt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(pmt)
    flow.append(Spacer(1, 10))

    # Signatures (images via S3 presigned URL on the Media model)
    flow.append(Paragraph("Signatures", h2))

    def _get_media_url(media):
        if not media:
            return None
        # Reuse whatever the Media model exposes for presigned URL - we try a
        # few common entry points.
        for attr in ("get_file_url", "presigned_url", "url"):
            fn = getattr(media, attr, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        return val
                except Exception:
                    continue
            elif isinstance(fn, str) and fn:
                return fn
        f = getattr(media, "file", None)
        try:
            return f.url if f else None
        except Exception:
            return None

    reporter_url = _get_media_url(incident.reporter_signature) if incident.reporter_signature_id else None
    pm_url = _get_media_url(incident.pm_signature) if incident.pm_signature_id else None

    sig_data = []
    sig_data.append(["Reporter", "Program Manager"])
    sig_data.append([
        _fetch_image(reporter_url) or Paragraph("-", body),
        _fetch_image(pm_url) or Paragraph("-", body),
    ])
    sig_data.append([
        Paragraph(
            (f"{incident.reported_by.first_name} {incident.reported_by.last_name}".strip()
             if incident.reported_by_id else "-"),
            body,
        ),
        Paragraph(
            (f"{incident.assigned_program_manager.first_name} {incident.assigned_program_manager.last_name}".strip()
             if incident.assigned_program_manager_id else "-"),
            body,
        ),
    ])
    sig_data.append([
        Paragraph(_safe(getattr(incident, "started_at", None)), body),
        Paragraph(_safe(incident.completed_at), body),
    ])
    sig_t = Table(sig_data, colWidths=[3.3 * inch, 3.3 * inch])
    sig_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(sig_t)

    doc.build(flow)
    return buf.getvalue()
