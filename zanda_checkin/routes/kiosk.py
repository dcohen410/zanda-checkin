"""Check-in portal: the iPad signs in once with the practice account, then shows the kiosk.

The iPad holds only a kiosk device token (an HttpOnly cookie scoped to /checkin).
It can look up and confirm check-ins for its one site, and nothing else. It
can't open the admin portal.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..app import KIOSK_COOKIE, audit, check_csrf, find_device, render, touch
from ..checkin import parse_dob
from ..db import CheckinLog, Device, Organisation, Site
from ..security import new_device_token
from ..zanda import ZandaError
from .deps import authenticate, get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/checkin")
ONE_YEAR = 365 * 24 * 60 * 60


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def kiosk_device(request: Request, db) -> Device | None:
    device = find_device(db, request.cookies.get(KIOSK_COOKIE), "kiosk")
    if device is not None:
        touch(db, device)
    return device


def log_outcome(db, site_id: int, outcome: str) -> None:
    db.add(CheckinLog(site_id=site_id, outcome=outcome))
    db.commit()


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
    return render(request, "login.html", error=None, email="", action="/checkin/login",
                  title="Set up this check-in screen",
                  intro="Sign in with your practice's Zanda Checkin account. You only need to do this once per iPad.")


@router.post("/login")
def login(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
          password: str = Form("")):
    check_csrf(request, csrf)
    user, error = authenticate(request, db, email, password)
    if error:
        return render(request, "login.html", 400, error=error, email=email, action="/checkin/login",
                      title="Set up this check-in screen",
                      intro="Sign in with your practice's Zanda Checkin account.")
    request.session["kiosk_user_id"] = user.id
    request.session["kiosk_org_id"] = user.organisation_id
    return see_other("/checkin/choose-site")


@router.get("/choose-site")
def choose_site_form(request: Request, db=Depends(get_db)):
    org_id = request.session.get("kiosk_org_id")
    if not org_id:
        return see_other("/checkin/login")
    sites = db.query(Site).filter_by(organisation_id=org_id).order_by(Site.name).all()
    return render(request, "choose_site.html", sites=sites)


@router.post("/choose-site")
def choose_site(request: Request, db=Depends(get_db), csrf: str = Form(""), site_id: int = Form(0),
                device_name: str = Form("")):
    check_csrf(request, csrf)
    org_id, user_id = request.session.get("kiosk_org_id"), request.session.get("kiosk_user_id")
    site = db.get(Site, site_id)
    if not org_id or site is None or site.organisation_id != org_id:
        return see_other("/checkin/login")
    token, token_hash = new_device_token()
    name = device_name.strip()[:120] or "Check-in iPad"
    db.add(Device(organisation_id=org_id, user_id=user_id, site_id=site.id, kind="kiosk",
                  name=name, token_hash=token_hash))
    audit(db, org_id, user_id, "device.kiosk_added", f"{name} at {site.name}")
    db.commit()
    request.session.pop("kiosk_user_id", None)
    request.session.pop("kiosk_org_id", None)
    resp = see_other("/checkin")
    resp.set_cookie(KIOSK_COOKIE, token, max_age=ONE_YEAR, path="/checkin", httponly=True,
                    secure=request.app.state.settings.secure_cookies, samesite="strict")
    return resp


@router.get("/exit")
def exit_form(request: Request, db=Depends(get_db)):
    if kiosk_device(request, db) is None:
        return see_other("/checkin/login")
    return render(request, "login.html", error=None, email="", action="/checkin/exit",
                  title="Sign this iPad out",
                  intro="Staff only. Sign in with your practice account to stop this iPad being a check-in screen.",
                  back="/checkin")


@router.post("/exit")
def exit_kiosk(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
               password: str = Form("")):
    check_csrf(request, csrf)
    device = kiosk_device(request, db)
    if device is None:
        return see_other("/checkin/login")
    user, error = authenticate(request, db, email, password)
    if user is not None and user.organisation_id != device.organisation_id:
        user, error = None, "That account isn't for this practice."
    if error:
        return render(request, "login.html", 400, error=error, email=email, action="/checkin/exit",
                      title="Sign this iPad out", intro="Staff only.", back="/checkin")
    device.revoked = True
    audit(db, device.organisation_id, user.id, "device.signed_out", f"kiosk: {device.name}")
    db.commit()
    resp = see_other("/checkin/login")
    resp.delete_cookie(KIOSK_COOKIE, path="/checkin")
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
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400)
    return body if isinstance(body, dict) else {}


def _ready_site(request: Request, db) -> Site:
    device = kiosk_device(request, db)
    if device is None or device.site is None:
        raise HTTPException(401)
    if not device.site.is_ready:
        raise HTTPException(409)
    return device.site


@router.post("/api/lookup")
async def api_lookup(request: Request):
    body = await _json_body(request)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_lookup, request, body)


def _lookup(request: Request, body: dict):
    db = request.app.state.Session()
    try:
        site = _ready_site(request, db)
        dob = parse_dob(str(body.get("dob", "")))
        initial = str(body.get("initial", ""))[:1]
        if dob is None:
            return JSONResponse({"status": "invalid"})
        if site.require_surname_initial and not initial.isalpha():
            return JSONResponse({"status": "need_initial"})
        try:
            result = request.app.state.registry.service_for(site).lookup(dob, initial or None)
        except Exception as exc:
            log.error("Lookup failed for site %s: %s", site.id, exc if isinstance(exc, ZandaError) else repr(exc))
            log_outcome(db, site.id, "error")
            return JSONResponse({"status": "reception", "reason": "error"})
        if result.status != "match":
            log_outcome(db, site.id, f"reception_{result.reason}"[:24])
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
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_confirm, request, body)


def _confirm(request: Request, body: dict):
    db = request.app.state.Session()
    try:
        site = _ready_site(request, db)
        try:
            cand = request.app.state.registry.service_for(site).confirm(str(body.get("token", "")))
        except Exception as exc:
            log.error("Check-in failed for site %s: %s", site.id, exc if isinstance(exc, ZandaError) else repr(exc))
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
    db = request.app.state.Session()
    try:
        device = kiosk_device(request, db)
        if device is not None and device.site is not None and device.site.is_ready:
            request.app.state.registry.service_for(device.site).cancel(str(body.get("token", "")))
    finally:
        db.close()
    return JSONResponse({"status": "ok"})
