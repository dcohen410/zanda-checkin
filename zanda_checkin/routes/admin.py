"""Admin portal: practice sign-up, sites, Zanda API keys, devices."""
from __future__ import annotations

import hmac
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from ..app import audit, check_csrf, render
from ..db import AuditEvent, Device, Organisation, Site, User
from ..security import MIN_PASSWORD, hash_password, verify_password
from ..zanda import REGIONS, ZandaError
from .deps import authenticate, current_user, get_db

log = logging.getLogger(__name__)
router = APIRouter()

REGION_NAMES = {"au": "Australia", "us": "United States", "uk": "United Kingdom"}
TIME_ZONES = ["Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane", "Australia/Adelaide",
              "Australia/Darwin", "Australia/Perth", "Australia/Hobart", "Pacific/Auckland",
              "Europe/London", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def own_site(db, user: User, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None or site.organisation_id != user.organisation_id:
        raise HTTPException(404)
    return site


def zanda_error_message(exc: Exception) -> str:
    if isinstance(exc, ZandaError):
        if exc.is_auth_problem:
            return "Zanda didn't accept that API key. Check it was copied in full and hasn't been revoked."
        return f"Zanda returned an error ({exc.status}). Try again shortly."
    return "Couldn't reach Zanda. Try again shortly."


def pick_location(locations: list[dict]):
    in_person = [loc for loc in locations if loc.get("serviceModality") == "In Person"] or locations
    return in_person[0] if len(in_person) == 1 else None


# -- accounts -------------------------------------------------------------------
@router.get("/")
def home(request: Request):
    return see_other("/admin" if request.session.get("user_id") else "/login")


@router.get("/signup")
def signup_form(request: Request):
    return render(request, "signup.html", form={}, error=None,
                  needs_code=bool(request.app.state.settings.signup_code))


@router.post("/signup")
def signup(request: Request, db=Depends(get_db), csrf: str = Form(""), practice_name: str = Form(""),
           name: str = Form(""), email: str = Form(""), password: str = Form(""),
           password2: str = Form(""), signup_code: str = Form("")):
    check_csrf(request, csrf)
    settings = request.app.state.settings
    form = {"practice_name": practice_name, "name": name, "email": email}
    email = email.strip().lower()
    error = None
    if settings.signup_code and not hmac.compare_digest(signup_code.strip(), settings.signup_code):
        error = "That sign-up code isn't right. Ask us for one."
    elif not practice_name.strip():
        error = "Enter your practice's name."
    elif "@" not in email or len(email) > 254:
        error = "Enter a valid email address."
    elif len(password) < MIN_PASSWORD:
        error = f"Use a password of at least {MIN_PASSWORD} characters."
    elif password != password2:
        error = "The two passwords don't match."
    elif db.query(User).filter_by(email=email).first():
        error = "There's already an account with that email. Sign in instead."
    if error:
        return render(request, "signup.html", 400, form=form, error=error, needs_code=bool(settings.signup_code))
    org = Organisation(name=practice_name.strip()[:120])
    user = User(organisation=org, email=email, name=name.strip()[:120], password_hash=hash_password(password))
    db.add_all([org, user])
    try:
        db.flush()
        audit(db, org.id, user.id, "account.created", email)
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(request, "signup.html", 400, form=form, needs_code=bool(settings.signup_code),
                      error="There's already an account with that email. Sign in instead.")
    request.session.clear()
    request.session["user_id"] = user.id
    return see_other("/admin")


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html", error=None, email="", action="/login", title="Sign in",
                  intro="Sign in to manage your check-in sites.")


@router.post("/login")
def login(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
          password: str = Form("")):
    check_csrf(request, csrf)
    user, error = authenticate(request, db, email, password)
    if error:
        return render(request, "login.html", 400, error=error, email=email, action="/login", title="Sign in",
                      intro="Sign in to manage your check-in sites.")
    request.session.clear()
    request.session["user_id"] = user.id
    return see_other("/admin")


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return see_other("/login")


@router.get("/admin/account")
def account_form(request: Request, user=Depends(current_user)):
    return render(request, "account.html", user=user, message=None, error=None)


@router.post("/admin/account")
def account(request: Request, db=Depends(get_db), user=Depends(current_user), csrf: str = Form(""),
            current: str = Form(""), new: str = Form(""), new2: str = Form("")):
    check_csrf(request, csrf)
    error = None
    if not verify_password(current, user.password_hash):
        error = "Your current password isn't right."
    elif len(new) < MIN_PASSWORD:
        error = f"Use a password of at least {MIN_PASSWORD} characters."
    elif new != new2:
        error = "The two new passwords don't match."
    if error:
        return render(request, "account.html", 400, user=user, message=None, error=error)
    user = db.merge(user)
    user.password_hash = hash_password(new)
    audit(db, user.organisation_id, user.id, "account.password_changed")
    db.commit()
    return render(request, "account.html", user=user, message="Password changed.", error=None)


# -- dashboard ------------------------------------------------------------------
@router.get("/admin")
def dashboard(request: Request, db=Depends(get_db), user=Depends(current_user)):
    org = db.get(Organisation, user.organisation_id)
    devices = (db.query(Device).filter_by(organisation_id=org.id, revoked=False)
               .order_by(Device.kind, Device.created_at.desc()).all())
    events = (db.query(AuditEvent).filter_by(organisation_id=org.id)
              .order_by(AuditEvent.created_at.desc()).limit(15).all())
    live = {s.id: request.app.state.broadcaster.subscriber_count(s.id) for s in org.sites}
    return render(request, "dashboard.html", user=user, org=org, devices=devices, events=events, live=live)


@router.post("/admin/devices/{device_id}/revoke")
def revoke_device(device_id: int, request: Request, db=Depends(get_db), user=Depends(current_user),
                  csrf: str = Form("")):
    check_csrf(request, csrf)
    device = db.get(Device, device_id)
    if device is None or device.organisation_id != user.organisation_id:
        raise HTTPException(404)
    device.revoked = True
    audit(db, user.organisation_id, user.id, "device.signed_out", f"{device.kind}: {device.name}")
    db.commit()
    return see_other("/admin")


# -- sites ------------------------------------------------------------------------
@router.get("/admin/sites/new")
def new_site_form(request: Request, user=Depends(current_user)):
    return render(request, "site_new.html", user=user, error=None, form={"region": "au"}, regions=REGION_NAMES)


@router.post("/admin/sites/new")
def new_site(request: Request, db=Depends(get_db), user=Depends(current_user), csrf: str = Form(""),
             name: str = Form(""), region: str = Form("au"), api_key: str = Form("")):
    check_csrf(request, csrf)
    form = {"name": name, "region": region}
    key = api_key.strip()
    error = None
    if not name.strip():
        error = "Give the site a name, such as the clinic's suburb."
    elif region not in REGIONS:
        error = "Choose your Zanda region."
    elif not key:
        error = "Paste the Zanda API key for this site."
    locations = []
    if not error:
        probe = Site(zanda_region=region, time_zone="Australia/Sydney")
        try:
            locations = request.app.state.registry.client_for(probe, api_key=key).list_locations()
        except Exception as exc:
            if not isinstance(exc, ZandaError):
                log.exception("Zanda connection failed during site setup")
            error = zanda_error_message(exc)
    if error:
        return render(request, "site_new.html", 400, user=user, error=error, form=form, regions=REGION_NAMES)
    only = pick_location(locations)
    site = Site(organisation_id=user.organisation_id, name=name.strip()[:120], zanda_region=region,
                zanda_key_encrypted=request.app.state.keybox.encrypt(key), zanda_key_hint=key[-4:],
                location_id=int(only["id"]) if only else None, location_name=only["name"] if only else "")
    db.add(site)
    db.flush()
    audit(db, user.organisation_id, user.id, "site.created", site.name)
    db.commit()
    return see_other(f"/admin/sites/{site.id}?created=1")


def _site_page(request, db, user, site, *, message=None, error=None, status=200):
    locations, flags = [], []
    try:
        client = request.app.state.registry.client_for(site)
        locations = client.list_locations()
        flags = client.list_appointment_flags()
    except Exception as exc:
        if not isinstance(exc, ZandaError):
            log.exception("Loading site settings from Zanda failed")
        error = error or zanda_error_message(exc)
    notifiers = request.app.state.broadcaster.subscriber_count(site.id)
    return render(request, "site.html", status, user=user, site=site, locations=locations, flags=flags,
                  time_zones=TIME_ZONES, message=message, error=error, notifiers=notifiers)


@router.get("/admin/sites/{site_id}")
def site_page(site_id: int, request: Request, db=Depends(get_db), user=Depends(current_user)):
    site = own_site(db, user, site_id)
    message = "Site created. Check the settings below." if request.query_params.get("created") else None
    return _site_page(request, db, user, site, message=message)


@router.post("/admin/sites/{site_id}")
def save_site(site_id: int, request: Request, db=Depends(get_db), user=Depends(current_user),
              csrf: str = Form(""), name: str = Form(""), welcome_name: str = Form(""),
              location_id: str = Form(""), location_name: str = Form(""), time_zone: str = Form(""),
              minutes_early: int = Form(90), minutes_late: int = Form(30), reception_flag: str = Form(""),
              require_surname_initial: str = Form(""), show_clinician: str = Form(""),
              mark_arrived: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, user, site_id)
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return _site_page(request, db, user, site, error="Choose a valid time zone.", status=400)
    site.name = name.strip()[:120] or site.name
    site.welcome_name = welcome_name.strip()[:120]
    site.location_id = int(location_id) if location_id.strip().isdigit() else None
    site.location_name = ""
    if site.location_id:
        try:
            names = {int(loc["id"]): loc["name"] for loc in
                     request.app.state.registry.client_for(site).list_locations()}
            site.location_name = names.get(site.location_id, "")[:120]
        except Exception:
            site.location_name = location_name.strip()[:120]
    site.time_zone = time_zone
    site.minutes_early = max(0, min(240, minutes_early))
    site.minutes_late = max(0, min(120, minutes_late))
    site.reception_flag = reception_flag.strip()[:120]
    site.require_surname_initial = require_surname_initial == "on"
    site.show_clinician = show_clinician == "on"
    site.mark_arrived = mark_arrived == "on"
    audit(db, user.organisation_id, user.id, "site.updated", site.name)
    db.commit()
    request.app.state.registry.forget(site.id)
    return _site_page(request, db, user, site, message="Saved.")


@router.post("/admin/sites/{site_id}/key")
def replace_key(site_id: int, request: Request, db=Depends(get_db), user=Depends(current_user),
                csrf: str = Form(""), api_key: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, user, site_id)
    key = api_key.strip()
    if not key:
        return _site_page(request, db, user, site, error="Paste the new API key.", status=400)
    try:
        request.app.state.registry.client_for(site, api_key=key).list_locations()
    except Exception as exc:
        return _site_page(request, db, user, site, error=zanda_error_message(exc), status=400)
    site.zanda_key_encrypted = request.app.state.keybox.encrypt(key)
    site.zanda_key_hint = key[-4:]
    audit(db, user.organisation_id, user.id, "site.key_replaced", site.name)
    db.commit()
    request.app.state.registry.forget(site.id)
    return _site_page(request, db, user, site, message="API key replaced. Revoke the old key in Zanda.")


@router.post("/admin/sites/{site_id}/delete")
def delete_site(site_id: int, request: Request, db=Depends(get_db), user=Depends(current_user),
                csrf: str = Form(""), confirm_name: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, user, site_id)
    if confirm_name.strip() != site.name:
        return _site_page(request, db, user, site, error="Type the site's name exactly to delete it.", status=400)
    name = site.name
    db.query(Device).filter_by(site_id=site.id).update({"revoked": True})
    db.delete(site)
    audit(db, user.organisation_id, user.id, "site.deleted", name)
    db.commit()
    request.app.state.registry.forget(site_id)
    return see_other("/admin")
