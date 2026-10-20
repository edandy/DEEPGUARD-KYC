import logging
import os
import stat
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import kyc, signals
from app.config import get_settings
from app.db import init_db
from app.routers import (
    analytics,
    batch,
    clients,
    export,
    plugins,
    reports,
    rules,
    screening,
    webhooks,
)
from app.schemas import DecisionIn
from app.security import current_client

logger = logging.getLogger("deepguard")

settings = get_settings()

app = FastAPI(title="DeepGuard API", version="0.1.0")

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — restrict to configured origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["X-API-Key", "Content-Type"],
)

init_db()


def _warm_face_engine() -> None:
    """Load DeepFace once so the first selfie does not pay the model startup."""
    try:
        from app.services.facematch import is_available

        if is_available():
            logger.info("Face matching engine ready")
        else:
            logger.warning("Face matching engine unavailable")
    except Exception:
        logger.exception("Face matching warmup failed")


threading.Thread(target=_warm_face_engine, daemon=True, name="face-warmup").start()


# Seed a default admin client so the dashboard can authenticate immediately.
# Use the API key "dg_demo" in the dashboard's Settings page to get started.
def _seed_default_client() -> None:
    """Create the demo client, but ONLY in explicit demo/sandbox mode.

    The demo key is publicly documented, so it must never exist on a deployment
    that is not running with DEEP_GUARD_ALLOW_ANON=true. An existing demo row is
    left untouched (its key is never silently reset on boot).
    """
    if os.getenv("DEEP_GUARD_ALLOW_ANON", "false").lower() != "true":
        return

    from app.db import SessionLocal
    from app.models import Client
    from app.security import hash_api_key

    demo_key = "dg_demo"
    db = SessionLocal()
    try:
        existing = db.query(Client).filter(Client.email == "demo@deepguard.local").first()
        if not existing:
            db.add(Client(
                name="Demo Client",
                email="demo@deepguard.local",
                api_key=hash_api_key(demo_key),
                plan="pro",
            ))
            db.commit()
            logger.info("Seeded demo client (sandbox mode only)")
    except Exception:
        db.rollback()
        logger.exception("Failed to seed default client")
    finally:
        db.close()


_seed_default_client()

app.include_router(clients.router)
app.include_router(webhooks.router)
app.include_router(screening.router)
app.include_router(reports.router)
app.include_router(batch.router)
app.include_router(rules.router)
app.include_router(analytics.router)
app.include_router(export.router)
app.include_router(plugins.router)

try:
    from app.websocket import router as ws_router
    app.include_router(ws_router)
except ImportError:
    pass

MAX_SIZE = 30 * 1024 * 1024  # 30 MB
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
@limiter.limit("20/minute")
async def analyze(
    request: Request,
    file: UploadFile = File(...),
    client=Depends(current_client),
):
    suffix = Path(file.filename or "image.png").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(status_code=415, detail=f"Unsupported format: {suffix}")
    if not file.content_type:
        raise HTTPException(status_code=400, detail="Missing Content-Type")

    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 30 MB).")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    tmp = Path(tempfile.gettempdir()) / "deepguard" / f"{uuid.uuid4().hex}{suffix}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        result = signals.analyze_image(str(tmp))
    except Exception:
        logger.exception("Image analysis failed")
        raise HTTPException(status_code=422, detail="Unable to analyze image.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    result["filename"] = file.filename
    result["size_bytes"] = len(data)
    return result


# --- KYC dashboard ------------------------------------------------------------


@app.get("/api/kyc/cases")
@limiter.limit("60/minute")
def list_cases(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(ge=1, default=1),
    page_size: int = Query(ge=1, le=100, default=35),
    client=Depends(current_client),
):
    return kyc.get_cases(status=status, q=q, page=page, page_size=page_size)


@app.post("/api/kyc/cases")
@limiter.limit("20/minute")
async def create_case(
    request: Request,
    file: UploadFile = File(...),
    applicant: str = Form(""),
    doc_type: str = Form("autre"),
    reference: str = Form(""),
    document_number: str = Form(""),
    webhook_url: str = Form(""),
    webhook_host: str = Form(""),
    client=Depends(current_client),
):
    """Submit an ID document for review."""
    suffix = Path(file.filename or "image.png").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(status_code=415, detail=f"Unsupported format: {suffix}")

    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 30 MB).")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    tmp = Path(tempfile.gettempdir()) / "deepguard" / f"{uuid.uuid4().hex}{suffix}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        case = kyc.create_case(
            str(tmp),
            applicant=applicant,
            doc_type=doc_type.strip().lower() or "autre",
            reference=reference.strip(),
            filename=file.filename,
            document_number=document_number,
            webhook_url=webhook_url,
            webhook_host=webhook_host,
        )
    except Exception:
        logger.exception("KYC case creation failed")
        raise HTTPException(status_code=422, detail="Unable to create KYC case.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return case.to_detail()


@app.get("/api/kyc/cases/{case_id}")
@limiter.limit("60/minute")
def case_detail(request: Request, case_id: int, client=Depends(current_client)):
    case = kyc.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="KYC case not found.")
    return case.to_detail()


@app.post("/api/kyc/cases/{case_id}/decision")
@limiter.limit("30/minute")
def case_decision(
    request: Request,
    case_id: int,
    payload: DecisionIn,
    client=Depends(current_client),
):
    case = kyc.decide(case_id, payload.action)
    if not case:
        raise HTTPException(status_code=404, detail="Invalid action or case.")
    return case.to_detail()


# --- Mobile face verification ------------------------------------------------


@app.post("/api/kyc/cases/{case_id}/face-session")
@limiter.limit("5/minute")
def create_face_session(
    request: Request,
    case_id: int,
    client=Depends(current_client),
):
    """Create a face verification session and return QR code for mobile scan."""
    case = kyc.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="KYC case not found.")

    from app.services.mobile_session import create_face_session as _create
    session = _create(case_id)
    return session


@app.get("/api/mobile/status/{token}")
@limiter.limit("30/minute")
def mobile_session_status(request: Request, token: str):
    """Poll face verification session status (used by dashboard)."""
    from app.services.mobile_session import get_session_status
    status = get_session_status(token)
    if not status:
        raise HTTPException(status_code=404, detail="Session not found or expired.")
    return status


@app.post("/api/mobile/verify")
@limiter.limit("10/minute")
async def mobile_verify(
    request: Request,
    token: str = Form(...),
    selfie: UploadFile = File(...),
):
    """Mobile app submits a selfie. Liveness, device and face checks run SERVER-SIDE.

    Client-supplied liveness or device claims are intentionally NOT accepted:
    anything the browser sends can be forged. Token is single-use and expires after TTL.
    """
    import tempfile
    import uuid as _uuid

    from app.services.anti_emulator import check_device
    from app.services.liveness import check_liveness
    from app.services.mobile_session import complete_session, verify_token

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    case_id = payload.get("case_id")

    selfie_data = await selfie.read()
    if not selfie_data:
        raise HTTPException(status_code=400, detail="Empty selfie.")

    # Device check from the request itself (User-Agent), not from form fields.
    user_agent = request.headers.get("user-agent", "")[:500]
    client_ip = _client_ip(request)
    device = check_device(user_agent=user_agent)
    if device.is_emulator or device.is_bot:
        raise HTTPException(
            status_code=403,
            detail="Emulator or bot detected. Please use a real mobile device.",
        )
    capture_device = "mobile" if device.is_mobile else "web"

    # Write the selfie once; both liveness and face matching read this file.
    selfie_tmp = os.path.join(tempfile.gettempdir(), "deepguard", f"selfie_{_uuid.uuid4().hex}.jpg")
    os.makedirs(os.path.dirname(selfie_tmp), exist_ok=True)
    try:
        with open(selfie_tmp, "wb") as f:
            f.write(selfie_data)
        os.chmod(selfie_tmp, 0o600)

        try:
            live = check_liveness(selfie_tmp)
        except Exception:
            logger.exception("Liveness check failed to run")
            live = {"score": 0.0, "passed": False, "signals": {}, "reasons": ["liveness_error"]}
        liveness_result = {
            "score": live["score"],
            "is_live": bool(live["passed"]),
            "checks": live.get("signals", {}),
        }
        if not liveness_result["is_live"]:
            kyc.remember_capture(
                case_id,
                selfie_path=selfie_tmp,
                user_agent=user_agent,
                client_ip=client_ip,
                liveness_result=liveness_result,
                capture_device=capture_device,
            )
            _notify_case(case_id, face_match=False, liveness_passed=False)
            raise HTTPException(
                status_code=403,
                detail="Liveness check failed. Please try again with a real face.",
            )

        face_result = {"match": None, "score": None, "distance": None}
        try:
            from app.services.facematch import compare_faces
            case = kyc.get_case(case_id)
            document_path = getattr(case, "image_path", "") if case else ""
            if document_path and os.path.exists(document_path):
                match_result = compare_faces(selfie_tmp, document_path)
                face_result = {
                    "match": match_result.get("match"),
                    "score": match_result.get("score"),
                    "distance": match_result.get("distance"),
                }
                if face_result["match"] is None:
                    logger.warning("Face matching unavailable: %s", match_result.get("message"))
            else:
                logger.warning("Face matching skipped: no document image for case %s", case_id)
        except Exception:
            logger.exception("Face match pipeline error")

        kyc.remember_capture(
            case_id,
            selfie_path=selfie_tmp,
            user_agent=user_agent,
            client_ip=client_ip,
            liveness_result=liveness_result,
            capture_device=capture_device,
        )
        session = complete_session(
            token=token,
            selfie_data=selfie_data,
            liveness_result=liveness_result,
            face_result=face_result,
            capture_device=capture_device,
        )
    finally:
        try:
            os.remove(selfie_tmp)
        except OSError:
            pass

    if not session:
        raise HTTPException(status_code=410, detail="Session expired or already used.")

    document_verdict = ""
    existing = kyc.get_case(case_id)
    if existing is not None:
        document_verdict = existing.verdict

    try:
        if face_result.get("match") is not None:
            kyc.update_face_verification(
                case_id=case_id,
                face_match=bool(face_result["match"]),
                face_score=float(face_result.get("score") or 0),
                liveness_result=liveness_result,
                capture_device=capture_device,
                face_distance=face_result.get("distance"),
            )
    except Exception:
        logger.exception("Failed to update KYC case with face verification")

    _notify_case(
        case_id,
        face_match=face_result.get("match") if isinstance(face_result.get("match"), bool) else None,
        liveness_passed=bool(liveness_result["is_live"]),
        document_verdict=document_verdict,
    )

    return _verify_response(face_result, liveness_result, device)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:80]
    if request.client and request.client.host:
        return request.client.host[:80]
    return ""


def _notify_case(case_id: int, *, face_match: bool | None, liveness_passed: bool, document_verdict: str | None = None) -> None:
    case = kyc.get_case(case_id)
    if case is None:
        return
    from app.services.case_webhook import notify_case_completed

    notify_case_completed(
        case,
        document_verdict=case.verdict if document_verdict is None else document_verdict,
        face_match=face_match,
        liveness_passed=liveness_passed,
    )


def _verify_response(face_result: dict, liveness_result: dict, device) -> dict:
    return {
        "status": "verified",
        "face_match": face_result.get("match"),
        "face_score": face_result.get("score"),
        "liveness_score": liveness_result["score"],
        "device": {
            "is_mobile": device.is_mobile,
            "confidence": device.confidence,
        },
    }
