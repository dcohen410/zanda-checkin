"""Check-in portal: the iPad signs in once with the practice account, then shows the kiosk.

The iPad holds only a kiosk device token (an HttpOnly, Secure cookie scoped to /checkin).
That token is also what unlocks the practice's key for this iPad: the server keeps only a
hash of it, so the server can't use the key without the iPad.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ..accounts import device_by_token
from ..app import audit, check_csrf, render, touch
from ..checkin import parse_dob
from ..db import CheckinLog, Device, Organisation, Site
from ..security import new_device_token
from ..zanda import ZandaError
from .deps import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/checkin")
ONE_YEAR = 365 * 24 * 60 * 60


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def kiosk_token(request: Request) -> str | None:
    return request.cookies.get(request.app.state.kiosk_cookie)


def kiosk_device(request: Request, db) -> Device | None:
    device = device_by_token(db, kiosk_token(request), "kiosk")
    if device is not None:
        touch(db, device)
    return device


def log_outcome(db, site_id: int, outcome: str) -> None:
    db.add(CheckinLog(site_id=site_id, outcome=outcome[:24]))
    db.commit()


def login_page(request, status=200, **kw):
    kw.setdefault("error", None)
    kw.setdefault("email", "")
    return render(request, "login.html", status, action="/checkin/login", title="Set up this check-in screen",
                  intro="Sign in with your practice's Zanda Checkin account. You only need to do this once per iPad.",
                  show_code=True, **kw)


@router.get("")
def kiosk(request: Request, db=Depends(get_db)):
    device = kiosk_device(request, db)
    if device is None or device.site is None:
        return see_other("/checkin/login")
    site = device.site
    if not site.is_ready:
        return render(request, "not_ready.html", site=site)
    welcome = site.welcome_name or db.get(Organisation, site.organisation_id).name
    return render(request, "kiosk.html", site=site, welcome=welcome)


@router.get("/login")
def login_form(request: Request):
    return login_page(request)


@router.post("/login")
def login(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
          password: str = Form(""), code: str = Form("")):
    check_csrf(request, csrf)
    accounts = request.app.state.accounts
    result = accounts.authenticate(request, db, email, password, code, require_mfa=True)
    if result.error:
        return login_page(request, 400, error=result.error, email=email)
    accounts.start_session(request, db, result.user, result.omk, purpose="kiosk_setup")
    return see_other("/checkin/choose-site")


@router.get("/choose-site")
def choose_site_form(request: Request, db=Depends(get_db)):
    ws = request.app.state.accounts.session(request, db, "kiosk_setup")
    if ws is None:
        return see_other("/checkin/login")
    sites = db.query(Site).filter_by(organisation_id=ws.organisation_id).order_by(Site.name).all()
    return render(request, "choose_site.html", sites=sites)


@router.post("/choose-site")
def choose_site(request: Request, db=Depends(get_db), csrf: str = Form(""), site_id: int = Form(0),
                device_name: str = Form("")):
    check_csrf(request, csrf)
    accounts = request.app.state.accounts
    ws = accounts.session(request, db, "kiosk_setup")
    site = db.get(Site, site_id)
    if ws is None or site is None or site.organisation_id != ws.organisation_id:
        return see_other("/checkin/login")
    omk = accounts.session_omk(request, db, ws)
    if omk is None:
        return see_other("/checkin/login")
    token, token_hash = new_device_token()
    name = device_name.strip()[:120] or "Check-in iPad"
    device = Device(organisation_id=ws.organisation_id, user_id=ws.user_id, site_id=site.id, kind="kiosk",
                    name=name, token_hash=token_hash)
    db.add(device)
    db.flush()
    accounts.wrap_for_device(db, device, token, omk)
    audit(db, ws.organisation_id, ws.user_id, "device.kiosk_added", f"{name} at {site.name}")
    db.commit()
    accounts.end_session(request, db)  # the setup session (and its key copy) is no longer needed
    resp = see_other("/checkin")
    resp.set_cookie(request.app.state.kiosk_cookie, token, max_age=ONE_YEAR, path="/checkin", httponly=True,
                    secure=request.app.state.settings.secure_cookies, samesite="strict")
    return resp


@router.get("/exit")
def exit_form(request: Request, db=Depends(get_db)):
    if kiosk_device(request, db) is None:
        return see_other("/checkin/login")
    return render(request, "login.html", error=None, email="", action="/checkin/exit",
                  title="Sign this iPad out", show_code=True,
                  intro="Staff only. Sign in with your practice account to stop this iPad being a check-in screen.",
                  back="/checkin")


@router.post("/exit")
def exit_kiosk(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
               password: str = Form(""), code: str = Form("")):
    check_csrf(request, csrf)
    device = kiosk_device(request, db)
    if device is None:
        return see_other("/checkin/login")
    result = request.app.state.accounts.authenticate(request, db, email, password, code, require_mfa=True,
                                                     scope=f"exit:{device.id}")
    error = result.error
    if not error and result.user.organisation_id != device.organisation_id:
        error = "That account isn't for this practice."
    if error:
        return render(request, "login.html", 400, error=error, email=email, action="/checkin/exit",
                      title="Sign this iPad out", intro="Staff only.", back="/checkin", show_code=True)
    request.app.state.accounts.forget_device(db, device)
    audit(db, device.organisation_id, result.user.id, "device.signed_out", f"kiosk: {device.name}")
    db.commit()
    request.session.clear()
    resp = see_other("/checkin/login")
    resp.delete_cookie(request.app.state.kiosk_cookie, path="/checkin",
                       secure=request.app.state.settings.secure_cookies, httponly=True, samesite="strict")
    return resp


@router.get("/manifest.webmanifest")
def manifest(request: Request):
    return JSONResponse({
        "name": "Check in", "short_name": "Check in",
        "start_url": "/checkin", "scope": "/checkin", "display": "standalone",
        "orientation": "any", "background_color": "#f6f3ee", "theme_color": "#f6f3ee",
        "icons": [{"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


# -- kiosk JSON API -------------------------------------------------------------
async def _json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(400)
    if int(request.headers.get("content-length") or 0) > 4096:
        raise HTTPException(413)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400)
    return body if isinstance(body, dict) else {}


def _service(request: Request, db):
    """The site's check-in service, using the key unlocked by this iPad's own token."""
    device = kiosk_device(request, db)
    if device is None or device.site is None:
        raise HTTPException(401)
    site = device.site
    if not site.is_ready:
        raise HTTPException(409)
    accounts = request.app.state.accounts
    omk = accounts.device_omk(db, device, kiosk_token(request))
    if omk is None:
        raise HTTPException(401)
    key = accounts.open_api_key(omk, site)
    return site, request.app.state.registry.service_for(site, key)


def _log_failure(site_id: int, what: str, exc: Exception) -> None:
    detail = f"Zanda {exc.status}" if isinstance(exc, ZandaError) else type(exc).__name__
    log.error("%s failed for site %s: %s", what, site_id, detail)


@router.post("/api/lookup")
async def api_lookup(request: Request):
    body = await _json_body(request)
    return await run_in_threadpool(_lookup, request, body)


def _lookup(request: Request, body: dict):
    db = request.app.state.Session()
    try:
        site, service = _service(request, db)
        dob = parse_dob(str(body.get("dob", ""))[:16])
        initial = str(body.get("initial", ""))[:1]
        if dob is None:
            return JSONResponse({"status": "invalid"})
        if site.require_surname_initial and not initial.isalpha():
            return JSONResponse({"status": "need_initial"})
        try:
            result = service.lookup(dob, initial or None)
        except Exception as exc:
            _log_failure(site.id, "Lookup", exc)
            log_outcome(db, site.id, "error")
            return JSONResponse({"status": "reception", "reason": "error"})
        if result.status != "match":
            log_outcome(db, site.id, f"reception_{result.reason}")
            return JSONResponse({"status": "reception"})
        person = result.candidate.public()
        if not site.show_clinician:
            person["practitioner"] = ""
        return JSONResponse({"status": "match", "token": result.token, "person": person})
    finally:
        db.close()


@router.post("/api/confirm")
async def api_confirm(request: Request):
    body = await _json_body(request)
    return await run_in_threadpool(_confirm, request, body)


def _confirm(request: Request, body: dict):
    db = request.app.state.Session()
    try:
        site, service = _service(request, db)
        try:
            cand = service.confirm(str(body.get("token", ""))[:64])
        except Exception as exc:
            _log_failure(site.id, "Check-in", exc)
            log_outcome(db, site.id, "error")
            return JSONResponse({"status": "reception", "reason": "error"})
        if cand is None:
            log_outcome(db, site.id, "expired")
            return JSONResponse({"status": "reception"})
        log_outcome(db, site.id, "checked_in")
        return JSONResponse({"status": "done", "firstName": cand.first_name, "seeReception": cand.see_reception})
    finally:
        db.close()


@router.post("/api/cancel")
async def api_cancel(request: Request):
    body = await _json_body(request)

    def cancel():
        db = request.app.state.Session()
        try:
            _site, service = _service(request, db)
            service.cancel(str(body.get("token", ""))[:64])
        except HTTPException:
            pass
        finally:
            db.close()

    await run_in_threadpool(cancel)
    return JSONResponse({"status": "ok"})
