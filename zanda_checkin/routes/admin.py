"""Admin portal: practice sign-up, two-step sign-in, sites, Zanda API keys, devices."""
from __future__ import annotations

import hmac
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import mfa
from ..app import audit, check_csrf, render
from ..db import AuditEvent, Device, Organisation, Site, User
from ..security import password_problem, verify_password
from ..zanda import REGIONS, ZandaError
from .deps import Admin, current_admin, get_db, org_key, signed_in

log = logging.getLogger(__name__)
router = APIRouter()

REGION_NAMES = {"au": "Australia", "us": "United States", "uk": "United Kingdom"}
TIME_ZONES = ["Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane", "Australia/Adelaide",
              "Australia/Darwin", "Australia/Perth", "Australia/Hobart", "Pacific/Auckland",
              "Europe/London", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def own_site(db, admin: Admin, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None or site.organisation_id != admin.org_id:
        raise HTTPException(404)
    return site


def zanda_error_message(exc: Exception) -> str:
    if isinstance(exc, ZandaError):
        if exc.is_auth_problem:
            return "Zanda didn't accept that API key. Check it was copied in full and hasn't been revoked."
        return f"Zanda returned an error ({exc.status}). Try again shortly."
    return "Couldn't reach Zanda. Try again shortly."


def log_unexpected(what: str, exc: Exception) -> None:
    # Type only: exception text from HTTP libraries can include URLs and IDs.
    if not isinstance(exc, ZandaError):
        log.error("%s failed: %s", what, type(exc).__name__)


def pick_location(locations: list[dict]):
    in_person = [loc for loc in locations if loc.get("serviceModality") == "In Person"] or locations
    return in_person[0] if len(in_person) == 1 else None


def login_page(request, status=200, **kw):
    kw.setdefault("error", None)
    kw.setdefault("email", "")
    return render(request, "login.html", status, action="/login", title="Sign in",
                  intro="Sign in to manage your check-in sites.", **kw)


# -- accounts -------------------------------------------------------------------
@router.get("/")
def home(request: Request):
    return see_other("/admin" if request.session.get("sid") else "/login")


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
    ip = request.client.host if request.client else "unknown"
    limiter = request.app.state.signup_limiter
    if limiter.blocked(ip):
        return render(request, "signup.html", 429, form=form, needs_code=bool(settings.signup_code),
                      error="Too many sign-up attempts from here. Try again in an hour.")
    limiter.hit(ip)
    if settings.signup_code and not hmac.compare_digest(signup_code.strip(), settings.signup_code):
        error = "That sign-up code isn't right. Ask us for one."
    elif not practice_name.strip():
        error = "Enter your practice's name."
    elif "@" not in email or len(email) > 254:
        error = "Enter a valid email address."
    elif password != password2:
        error = "The two passwords don't match."
    elif problem := password_problem(password, email, practice_name, name):
        error = problem
    elif db.query(User).filter_by(email=email).first():
        error = "There's already an account with that email. Sign in instead."
    if error:
        return render(request, "signup.html", 400, form=form, error=error, needs_code=bool(settings.signup_code))
    accounts = request.app.state.accounts
    user = accounts.create_practice(db, practice_name.strip()[:120], name.strip()[:120], email, password)
    audit(db, user.organisation_id, user.id, "account.created", email)
    db.commit()
    signin = accounts.authenticate(request, db, email, password, require_mfa=False)
    accounts.start_session(request, db, signin.user, signin.omk)
    return see_other("/admin/mfa")


@router.get("/login")
def login_form(request: Request):
    return login_page(request)


@router.post("/login")
def login(request: Request, db=Depends(get_db), csrf: str = Form(""), email: str = Form(""),
          password: str = Form(""), code: str = Form("")):
    check_csrf(request, csrf)
    accounts = request.app.state.accounts
    result = accounts.authenticate(request, db, email, password, code, require_mfa=False)
    if result.error:
        return login_page(request, 400, error=result.error, email=email)
    accounts.start_session(request, db, result.user, result.omk)
    audit(db, result.user.organisation_id, result.user.id, "account.signed_in")
    db.commit()
    return see_other("/admin" if result.user.totp_enabled else "/admin/mfa")


@router.post("/logout")
def logout(request: Request, db=Depends(get_db), csrf: str = Form("")):
    check_csrf(request, csrf)
    request.app.state.accounts.end_session(request, db)
    return see_other("/login")


# -- two-step sign-in -----------------------------------------------------------------
def _qr_svg(uri: str) -> str:
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#1f2a2e", light="#ffffff")


@router.get("/admin/mfa")
def mfa_page(request: Request, db=Depends(get_db), admin: Admin = Depends(signed_in)):
    user = admin.user
    if user.totp_enabled:
        left = len([h for h in user.recovery_code_hashes.split("\n") if h])
        return render(request, "mfa.html", user=user, enabled=True, recovery_left=left, error=None)
    pepper = request.app.state.pepper
    if not user.totp_secret_encrypted:
        user.totp_secret_encrypted = pepper.encrypt(mfa.new_secret())
        db.commit()
    secret = pepper.decrypt(user.totp_secret_encrypted)
    uri = mfa.provisioning_uri(secret, user.email)
    return render(request, "mfa.html", user=user, enabled=False, secret=secret, qr=_qr_svg(uri), error=None)


@router.post("/admin/mfa")
def mfa_enable(request: Request, db=Depends(get_db), admin: Admin = Depends(signed_in), csrf: str = Form(""),
               code: str = Form("")):
    check_csrf(request, csrf)
    user = admin.user
    if user.totp_enabled or not user.totp_secret_encrypted:
        return see_other("/admin/mfa")
    secret = request.app.state.pepper.decrypt(user.totp_secret_encrypted)
    step = mfa.verify(secret, code, user.totp_last_step)
    if step is None:
        return render(request, "mfa.html", 400, user=user, enabled=False, secret=secret,
                      qr=_qr_svg(mfa.provisioning_uri(secret, user.email)),
                      error="That code didn't match. Check the time on your phone is set automatically, then try again.")
    codes = mfa.new_recovery_codes()
    user.totp_enabled = True
    user.totp_last_step = step
    user.recovery_code_hashes = "\n".join(mfa.hash_recovery(c) for c in codes)
    audit(db, user.organisation_id, user.id, "account.mfa_enabled")
    db.commit()
    return render(request, "mfa_codes.html", user=user, codes=codes)


@router.post("/admin/mfa/recovery-codes")
def mfa_new_codes(request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin),
                  csrf: str = Form(""), password: str = Form("")):
    check_csrf(request, csrf)
    user = admin.user
    if not verify_password(password, user.password_hash):
        return render(request, "mfa.html", 400, user=user, enabled=True,
                      recovery_left=len([h for h in user.recovery_code_hashes.split("\n") if h]),
                      error="Your password isn't right.")
    codes = mfa.new_recovery_codes()
    user.recovery_code_hashes = "\n".join(mfa.hash_recovery(c) for c in codes)
    audit(db, user.organisation_id, user.id, "account.recovery_codes_replaced")
    db.commit()
    return render(request, "mfa_codes.html", user=user, codes=codes)


# -- account ----------------------------------------------------------------------------
@router.get("/admin/account")
def account_form(request: Request, admin: Admin = Depends(current_admin)):
    return render(request, "account.html", user=admin.user, message=None, error=None)


@router.post("/admin/account")
def account(request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin), csrf: str = Form(""),
            current: str = Form(""), new: str = Form(""), new2: str = Form("")):
    check_csrf(request, csrf)
    user = admin.user
    error = None
    if not verify_password(current, user.password_hash):
        error = "Your current password isn't right."
    elif new != new2:
        error = "The two new passwords don't match."
    elif problem := password_problem(new, user.email, user.name):
        error = problem
    elif not request.app.state.accounts.change_password(db, user, current, new, keep_sid=admin.session.id):
        error = "Your account's encryption key couldn't be unlocked. Contact support."
    if error:
        return render(request, "account.html", 400, user=user, message=None, error=error)
    audit(db, user.organisation_id, user.id, "account.password_changed")
    db.commit()
    return render(request, "account.html", user=user, error=None,
                  message="Password changed. You've been signed out everywhere else.")


# -- dashboard ------------------------------------------------------------------
@router.get("/admin")
def dashboard(request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin)):
    org = db.get(Organisation, admin.org_id)
    devices = (db.query(Device).filter_by(organisation_id=org.id, revoked=False)
               .order_by(Device.kind, Device.created_at.desc()).all())
    events = (db.query(AuditEvent).filter_by(organisation_id=org.id)
              .order_by(AuditEvent.created_at.desc()).limit(15).all())
    return render(request, "dashboard.html", user=admin.user, org=org, devices=devices, events=events)


@router.post("/admin/devices/{device_id}/revoke")
def revoke_device(device_id: int, request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin),
                  csrf: str = Form("")):
    check_csrf(request, csrf)
    device = db.get(Device, device_id)
    if device is None or device.organisation_id != admin.org_id:
        raise HTTPException(404)
    request.app.state.accounts.forget_device(db, device)
    audit(db, admin.org_id, admin.user.id, "device.signed_out", f"{device.kind}: {device.name}")
    db.commit()
    return see_other("/admin")


# -- sites ------------------------------------------------------------------------
@router.get("/admin/sites/new")
def new_site_form(request: Request, admin: Admin = Depends(current_admin)):
    return render(request, "site_new.html", user=admin.user, error=None, form={"region": "au"}, regions=REGION_NAMES)


@router.post("/admin/sites/new")
def new_site(request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin), csrf: str = Form(""),
             name: str = Form(""), region: str = Form("au"), api_key: str = Form("")):
    check_csrf(request, csrf)
    omk = org_key(request, admin, db)
    form = {"name": name, "region": region}
    key = api_key.strip()
    error = None
    if not name.strip():
        error = "Give the site a name, such as the clinic's suburb."
    elif region not in REGIONS:
        error = "Choose your Zanda region."
    elif not key or len(key) > 512:
        error = "Paste the Zanda API key for this site."
    locations = []
    if not error:
        probe = Site(zanda_region=region, time_zone="Australia/Sydney")
        try:
            locations = request.app.state.registry.client_for(probe, key).list_locations()
        except Exception as exc:
            log_unexpected("Zanda connection during site setup", exc)
            error = zanda_error_message(exc)
    if error:
        return render(request, "site_new.html", 400, user=admin.user, error=error, form=form, regions=REGION_NAMES)
    only = pick_location(locations)
    site = Site(organisation_id=admin.org_id, name=name.strip()[:120], zanda_region=region,
                location_id=int(only["id"]) if only else None, location_name=only["name"][:120] if only else "")
    db.add(site)
    db.flush()
    request.app.state.accounts.seal_api_key(omk, site, key)
    audit(db, admin.org_id, admin.user.id, "site.created", site.name)
    db.commit()
    return see_other(f"/admin/sites/{site.id}?created=1")


def _site_page(request, db, admin: Admin, site, *, message=None, error=None, status=200):
    locations, flags = [], []
    try:
        key = request.app.state.accounts.open_api_key(org_key(request, admin, db), site)
        client = request.app.state.registry.client_for(site, key)
        locations = client.list_locations()
        flags = client.list_appointment_flags()
    except Exception as exc:
        log_unexpected("Loading site settings from Zanda", exc)
        error = error or zanda_error_message(exc)
    notifiers = request.app.state.broadcaster.subscriber_count(site.id)
    return render(request, "site.html", status, user=admin.user, site=site, locations=locations, flags=flags,
                  time_zones=TIME_ZONES, message=message, error=error, notifiers=notifiers)


@router.get("/admin/sites/{site_id}")
def site_page(site_id: int, request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin)):
    site = own_site(db, admin, site_id)
    message = "Site created. Check the settings below." if request.query_params.get("created") else None
    return _site_page(request, db, admin, site, message=message)


@router.post("/admin/sites/{site_id}")
def save_site(site_id: int, request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin),
              csrf: str = Form(""), name: str = Form(""), welcome_name: str = Form(""),
              location_id: str = Form(""), time_zone: str = Form(""),
              minutes_early: int = Form(90), minutes_late: int = Form(30), reception_flag: str = Form(""),
              require_surname_initial: str = Form(""), show_clinician: str = Form(""),
              mark_arrived: str = Form(""), notify_names: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, admin, site_id)
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return _site_page(request, db, admin, site, error="Choose a valid time zone.", status=400)
    locations = {}
    try:
        key = request.app.state.accounts.open_api_key(org_key(request, admin, db), site)
        locations = {int(loc["id"]): loc["name"] for loc in
                     request.app.state.registry.client_for(site, key).list_locations()}
    except Exception as exc:
        log_unexpected("Loading locations", exc)
        return _site_page(request, db, admin, site, error=zanda_error_message(exc), status=502)
    chosen = int(location_id) if location_id.strip().isdigit() else None
    if chosen is not None and chosen not in locations:
        return _site_page(request, db, admin, site, error="Choose one of your Zanda locations.", status=400)
    site.name = name.strip()[:120] or site.name
    site.welcome_name = welcome_name.strip()[:120]
    site.location_id = chosen
    site.location_name = locations.get(chosen, "")[:120] if chosen else ""
    site.time_zone = time_zone
    site.minutes_early = max(0, min(240, minutes_early))
    site.minutes_late = max(0, min(120, minutes_late))
    site.reception_flag = reception_flag.strip()[:120]
    site.require_surname_initial = require_surname_initial == "on"
    site.show_clinician = show_clinician == "on"
    site.mark_arrived = mark_arrived == "on"
    site.notify_names = notify_names == "on"
    audit(db, admin.org_id, admin.user.id, "site.updated", site.name)
    db.commit()
    request.app.state.registry.forget(site.id)
    return _site_page(request, db, admin, site, message="Saved.")


@router.post("/admin/sites/{site_id}/key")
def replace_key(site_id: int, request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin),
                csrf: str = Form(""), api_key: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, admin, site_id)
    key = api_key.strip()
    if not key or len(key) > 512:
        return _site_page(request, db, admin, site, error="Paste the new API key.", status=400)
    try:
        request.app.state.registry.client_for(site, key).list_locations()
    except Exception as exc:
        log_unexpected("Checking a replacement key", exc)
        return _site_page(request, db, admin, site, error=zanda_error_message(exc), status=400)
    request.app.state.accounts.seal_api_key(org_key(request, admin, db), site, key)
    audit(db, admin.org_id, admin.user.id, "site.key_replaced", site.name)
    db.commit()
    request.app.state.registry.forget(site.id)
    return _site_page(request, db, admin, site, message="API key replaced. Now revoke the old key in Zanda.")


@router.post("/admin/sites/{site_id}/delete")
def delete_site(site_id: int, request: Request, db=Depends(get_db), admin: Admin = Depends(current_admin),
                csrf: str = Form(""), confirm_name: str = Form("")):
    check_csrf(request, csrf)
    site = own_site(db, admin, site_id)
    if confirm_name.strip() != site.name:
        return _site_page(request, db, admin, site, error="Type the site's name exactly to delete it.", status=400)
    name = site.name
    for device in db.query(Device).filter_by(site_id=site.id).all():
        request.app.state.accounts.forget_device(db, device)
    db.delete(site)
    audit(db, admin.org_id, admin.user.id, "site.deleted", name)
    db.commit()
    request.app.state.registry.forget(site_id)
    return see_other("/admin")
