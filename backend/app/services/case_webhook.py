"""Avisa a unicas cuando un caso ya tiene decisión, para que marque al socio."""

from __future__ import annotations

import ipaddress
import json
import logging
from urllib.parse import urlparse

import httpx

from app.security import get_webhook_secret, sign_payload

logger = logging.getLogger("deepguard.case_webhook")


def automatic_verdict(document_verdict: str, face_match: bool | None, liveness_passed: bool) -> tuple[str, int]:
    """El documento falsificado, la selfie distinta o la prueba de vida fallida rechazan.

    Si la selfie coincide y el documento no está falsificado, se aprueba sin revisión manual.
    """
    verdict = (document_verdict or "").strip().lower()
    if face_match is False or not liveness_passed or verdict == "forge":
        return "forge", 0
    if face_match is True:
        return "authentique", 100
    return "suspect", 70


def notify_case_completed(case, *, document_verdict: str, face_match: bool | None, liveness_passed: bool) -> None:
    url = (getattr(case, "webhook_url", "") or "").strip()
    if not url or not _allowed(url):
        logger.info("Case %s finished without a reachable webhook", getattr(case, "id", None))
        return

    verdict, score = automatic_verdict(document_verdict, face_match, liveness_passed)
    payload = {
        "event": "case.completed",
        "case_id": case.id,
        "reference": case.reference,
        "verdict": verdict,
        "score": score,
        "face_match": face_match,
        "liveness_passed": liveness_passed,
        "document_number": case.document_number or None,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        signature = sign_payload(get_webhook_secret(), body)
    except RuntimeError:
        logger.warning("Webhook secret missing; case %s was not notified", case.id)
        return

    headers = {
        "Content-Type": "application/json",
        "X-DeepGuard-Signature": signature,
        "User-Agent": "DeepGuard/1.0 (+webhooks)",
    }
    host = (getattr(case, "webhook_host", "") or "").strip()
    if host:
        headers["Host"] = host

    try:
        response = httpx.post(url, content=body, headers=headers, timeout=8.0, follow_redirects=False)
    except httpx.HTTPError:
        logger.exception("Webhook delivery failed for case %s", case.id)
        return

    if 200 <= response.status_code < 300:
        logger.info("Case %s notified (%s)", case.id, response.status_code)
    else:
        logger.warning("Case %s webhook returned HTTP %s", case.id, response.status_code)


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or parsed.username:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "metadata.google.internal"}:
        return False
    if host == "host.docker.internal":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
