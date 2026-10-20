from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import kyc
from ..models import Client
from ..schemas import ReportOut
from ..security import current_client

logger = logging.getLogger("deepguard.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])
PDF_DIR = "/tmp/deepguard_kyc"


def _generate_kyc_pdf(case: kyc.KycCase) -> str:
    from app.services.kyc_report import generate

    path = os.path.join(PDF_DIR, f"{case.id}_report.pdf")
    return generate(case, path)


@router.get("/{case_id}")
def download_report(
    case_id: int,
    client: Client = Depends(current_client),
) -> FileResponse:
    case = kyc.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    pdf_path = _generate_kyc_pdf(case)
    return FileResponse(pdf_path, media_type="application/pdf",
                        filename=f"report_kyc_{case_id}.pdf")


@router.get("", response_model=list[ReportOut])
def list_reports(
    client: Client = Depends(current_client),
) -> list[ReportOut]:
    reports = []
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for case in sorted(kyc._store.values(), key=lambda c: c.id):
        pdf_path = os.path.join(PDF_DIR, f"{case.id}_report.pdf")
        generated = now
        if os.path.exists(pdf_path):
            generated = datetime.fromtimestamp(
                os.path.getmtime(pdf_path), tz=UTC
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        reports.append(ReportOut(
            case_id=case.id, filename=f"{case.id}_report.pdf",
            engine="reportlab", generated_at=generated,
        ))
    return reports
