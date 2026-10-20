"""Informe de verificación con la estructura del reporte de referencia.

Incluye portada, documento, prueba de vida, rostro, dispositivo y los
controles que este motor sí ejecutó. No afirma revisiones que no existen.
"""

from __future__ import annotations

import os
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app import kyc

NAVY = HexColor("#0B1F3A")
GREEN = HexColor("#0E8A4A")
RED = HexColor("#C62828")
AMBER = HexColor("#8A6A12")
MUTED = HexColor("#5C6B7A")
LINE = HexColor("#E4E7EC")
PAPER = HexColor("#F7F8FA")

_STATUS_COLOR = {
    "APPROVED": GREEN,
    "DECLINED": RED,
    "IN REVIEW": AMBER,
}


def generate(case: kyc.KycCase, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = SimpleDocTemplate(
        path,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=24 * mm,
        bottomMargin=18 * mm,
        title=f"Verificación {case.id}",
        author="DeepGuard",
    )
    status = _status(case)
    fields = _identity_fields(case)
    story = []
    story.extend(_cover(case, status))
    story.append(PageBreak())
    story.extend(_summary(case, status, fields))
    story.append(PageBreak())
    story.extend(_document_page(case, fields))
    story.append(PageBreak())
    story.extend(_liveness_page(case))
    story.append(PageBreak())
    story.extend(_face_page(case))
    story.append(PageBreak())
    story.extend(_device_page(case))
    story.append(PageBreak())
    story.extend(_checks_page(case, fields))
    document.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return path


def _decorate(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 16 * mm, width, 16 * mm, fill=1, stroke=0)
    canvas.setFillColor(white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(16 * mm, height - 10 * mm, "DeepGuard")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(width - 16 * mm, height - 10 * mm, "Informe de verificación")
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(16 * mm, 10 * mm, "Generado con los controles ejecutados en esta sesión")
    canvas.drawRightString(width - 16 * mm, 10 * mm, str(canvas.getPageNumber()))
    canvas.restoreState()


def _status(case: kyc.KycCase) -> str:
    face = case.signals.get("face_match")
    liveness = case.signals.get("liveness") or {}
    live = liveness.get("is_live")
    document_verdict = str(case.signals.get("document_verdict") or "").lower()
    if document_verdict == "forge" or face is False or live is False or case.status == "rejected":
        return "DECLINED"
    if face is True and live is True:
        return "APPROVED"
    if case.status == "approved":
        return "APPROVED"
    return "IN REVIEW"


def _identity_fields(case: kyc.KycCase) -> dict:
    cached = case.signals.get("ocr_fields")
    if isinstance(cached, dict):
        return cached
    fields: dict = {}
    path = case.image_path
    if path and os.path.exists(path) and os.path.getsize(path) >= 15_000:
        try:
            from app.services.ocr import extract_fields

            fields = dict(extract_fields(path).get("fields") or {})
        except Exception:
            fields = {}
    if not fields.get("document_number") and case.document_number:
        fields["document_number"] = case.document_number
    case.signals["ocr_fields"] = fields
    return fields


def _cover(case: kyc.KycCase, status: str) -> list:
    sections = [
        "Resumen",
        "Documento de identidad",
        "Prueba de vida",
        "Coincidencia de rostro",
        "Dispositivo",
        "Controles",
    ]
    return [
        Spacer(1, 18 * mm),
        _pill(status),
        Spacer(1, 8 * mm),
        Paragraph(_text(case.applicant or "Solicitante"), _style("name", 22, NAVY, "Helvetica-Bold")),
        Spacer(1, 4 * mm),
        Paragraph("Informe de verificación", _style("kicker", 11, MUTED)),
        Spacer(1, 10 * mm),
        _fields([("Sección", section) for section in sections]),
        Spacer(1, 8 * mm),
        Paragraph(f"Sesión {case.id}", _style("session", 9, MUTED)),
    ]


def _summary(case: kyc.KycCase, status: str, fields: dict) -> list:
    country = case.country
    if not country or country == "Unknown":
        country = fields.get("nationality") or "—"
    return [
        _heading("Resumen"),
        Paragraph(_text(case.applicant or "Solicitante"), _style("person", 16, NAVY, "Helvetica-Bold")),
        Spacer(1, 2 * mm),
        Paragraph(f"Enviado el {_shown_date(case.submitted_at)}", _style("since", 9, MUTED)),
        Spacer(1, 4 * mm),
        _pill(status),
        Spacer(1, 6 * mm),
        _fields([
            ("Sesión", str(case.id)),
            ("Referencia", case.reference or "—"),
            ("Creado", _shown_date(case.submitted_at)),
            ("Tipo de documento", _doc_type(case.doc_type)),
            ("País", country),
            ("Dispositivo", _device_label(case)),
        ]),
    ]


def _document_page(case: kyc.KycCase, fields: dict) -> list:
    rows = [
        ("Nombres", fields.get("first_name")),
        ("Apellidos", fields.get("last_name")),
        ("Sexo", fields.get("sex")),
        ("Nacimiento", fields.get("birth_date")),
        ("Tipo", _doc_type(case.doc_type)),
        ("Número", fields.get("document_number")),
        ("Nacionalidad", fields.get("nationality")),
        ("Vencimiento", fields.get("expiry_date")),
    ]
    story = [_heading("Documento de identidad"), _photo(case.image_path, 150 * mm, 78 * mm), Spacer(1, 4 * mm)]
    story.append(_fields([(label, value or "—") for label, value in rows]))
    return story


def _liveness_page(case: kyc.KycCase) -> list:
    liveness = case.signals.get("liveness") or {}
    score = liveness.get("score")
    checks = liveness.get("checks") or liveness.get("signals") or {}
    story = [
        _heading("Prueba de vida"),
        _photo(case.selfie_path, 90 * mm, 110 * mm),
        Spacer(1, 4 * mm),
        _fields([
            ("Puntaje", _percent(score)),
            ("Resultado", "Superada" if liveness.get("is_live") else "No superada" if liveness else "—"),
        ]),
        Spacer(1, 4 * mm),
    ]
    if isinstance(checks, dict) and checks:
        story.append(_fields([
            (_check_label(name), _check_result(value))
            for name, value in checks.items()
        ]))
    return story


def _face_page(case: kyc.KycCase) -> list:
    match = case.signals.get("face_match")
    score = case.signals.get("face_score")
    if match is True:
        result = "La selfie coincide con la foto del documento."
    elif match is False:
        result = "La selfie no coincide con la foto del documento."
    else:
        result = "No se pudo comparar la selfie con el documento."
    return [
        _heading("Coincidencia de rostro"),
        _side_by_side(case.image_path, case.selfie_path),
        Spacer(1, 4 * mm),
        _fields([
            ("Similitud", _similarity(score, case.signals.get("face_distance"))),
            ("Resultado", result),
        ]),
    ]


def _device_page(case: kyc.KycCase) -> list:
    return [
        _heading("Dispositivo"),
        _fields([
            ("Tipo", _device_label(case)),
            ("Navegador", case.user_agent or "—"),
            ("IP", case.client_ip or "—"),
        ]),
        Spacer(1, 4 * mm),
        Paragraph(
            "Esta sesión no calcula ciudad, operador ni uso de VPN.",
            _style("note", 9, MUTED),
        ),
    ]


def _checks_page(case: kyc.KycCase, fields: dict) -> list:
    liveness = case.signals.get("liveness") or {}
    document_verdict = str(case.signals.get("document_verdict") or "").lower()
    forensic_passed = False if document_verdict == "forge" else True if document_verdict else None
    checks = [
        ("Documento recibido", bool(case.image_path and os.path.exists(case.image_path))),
        ("El análisis forense no marcó el documento como falsificado", forensic_passed),
        ("Número de documento disponible", bool(fields.get("document_number"))),
        ("Prueba de vida superada", liveness.get("is_live") if "is_live" in liveness else None),
        ("Selfie recibida", bool(case.selfie_path and os.path.exists(case.selfie_path))),
        (
            "La selfie coincide con el documento",
            case.signals.get("face_match") if "face_match" in case.signals else None,
        ),
    ]
    return [
        _heading("Controles"),
        _fields([(label, _passed(value)) for label, value in checks]),
    ]


def _heading(text: str):
    return Paragraph(text, _style(f"h-{text}", 16, NAVY, "Helvetica-Bold", space_after=8))


def _pill(status: str):
    color = _STATUS_COLOR.get(status, AMBER)
    table = Table([[status]], colWidths=[42 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("TEXTCOLOR", (0, 0), (-1, -1), white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _fields(rows: list[tuple[str, str]]):
    data = [
        [
            Paragraph(_text(label), _style("label", 8, MUTED)),
            Paragraph(_text(value), _style("value", 10, NAVY)),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[58 * mm, 112 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAPER),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _photo(path: str, max_width: float, max_height: float):
    image = _image(path, max_width, max_height)
    if image is not None:
        return image
    return Paragraph("Foto no disponible", _style("missing-photo", 9, MUTED))


def _side_by_side(document_path: str, selfie_path: str):
    left = _image(document_path, 80 * mm, 55 * mm) or Paragraph("Documento no disponible", _style("d", 8, MUTED))
    right = _image(selfie_path, 55 * mm, 70 * mm) or Paragraph("Selfie no disponible", _style("s", 8, MUTED))
    table = Table([[left, right]], colWidths=[90 * mm, 70 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _image(path: str, max_width: float, max_height: float):
    if not path or not os.path.exists(path):
        return None
    try:
        image = Image(path)
        scale = min(max_width / float(image.drawWidth), max_height / float(image.drawHeight), 1)
        image.drawWidth *= scale
        image.drawHeight *= scale
        return image
    except Exception:
        return None


def _text(value) -> str:
    return escape(str(value)).replace("\n", "<br/>")


def _style(name: str, size: int, color, font: str = "Helvetica", space_after: int = 0) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        fontName=font,
        fontSize=size,
        leading=size + 3,
        textColor=color,
        alignment=TA_LEFT,
        spaceAfter=space_after,
    )


def _shown_date(value: str) -> str:
    return value.replace("T", " ").replace("Z", " UTC") if value else "—"


def _doc_type(value: str) -> str:
    labels = {
        "cni": "Documento de identidad",
        "passeport": "Pasaporte",
        "passport": "Pasaporte",
        "selfie": "Selfie",
    }
    return labels.get((value or "").lower(), value or "—")


def _device_label(case: kyc.KycCase) -> str:
    device = str(case.signals.get("capture_device") or "")
    if device == "mobile":
        return "Teléfono"
    if device == "web":
        return "Computadora"
    return "—"


def _similarity(score, distance) -> str:
    """La similitud sale de la distancia de SFace: 0 es la misma foto."""
    try:
        gap = float(distance)
    except (TypeError, ValueError):
        return _percent(score)
    return f"{max(0.0, min(1.0, 1.0 - gap)) * 100:.2f}%"


def _percent(score) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "—"
    if value <= 1:
        value *= 100
    return f"{value:.2f}%"


def _check_label(name: str) -> str:
    labels = {
        "eye_shine": "Brillo de ojos",
        "skin_texture": "Textura de la piel",
        "symmetry": "Simetría",
        "borders": "Bordes",
    }
    return labels.get(name, name.replace("_", " ").capitalize())


def _check_result(value) -> str:
    if isinstance(value, dict):
        score = value.get("score")
        note = value.get("note") or ""
        mark = _passed(_score_passed(score))
        return f"{mark}. {note}".strip()
    return _passed(bool(value))


def _score_passed(score) -> bool | None:
    try:
        return float(score) >= 0.55
    except (TypeError, ValueError):
        return None


def _passed(value: bool | None) -> str:
    if value is True:
        return "Superado"
    if value is False:
        return "No superado"
    return "Sin dato"
