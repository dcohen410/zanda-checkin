"""Zanda Checkin: a waiting-room check-in kiosk for Zanda practices."""
from __future__ import annotations

import hmac
import logging
import secrets
import time as clock
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .checkin import CheckinService, parse_dob
from .config import Config
from .zanda import REGIONS, ZandaClient, ZandaError

__version__ = "0.1.0"
log = logging.getLogger(__name__)

PIN_MAX_TRIES = 5
PIN_LOCKOUT = 5 * 60


def create_app(config: Config | None = None, client_factory=ZandaClient) -> Flask:
    app = Flask(__name__)
    cfg = config or Config()
    app.secret_key = cfg["secret_key"]
    app.config.update(
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
    )
    state = {"service": None, "pin_fails": [], "pin_locked_until": 0.0}

    # -- helpers -------------------------------------------------------------
    def make_client(api_key=None, region=None):
        return client_factory(api_key or cfg["api_key"], region or cfg["region"], cfg["time_zone"])

    def service() -> CheckinService:
        if state["service"] is None:
            state["service"] = CheckinService(
                make_client(),
                location_id=int(cfg["location_id"]),
                time_zone=cfg["time_zone"],
                minutes_early=int(cfg["minutes_early"]),
                minutes_late=int(cfg["minutes_late"]),
                reception_flag=cfg["reception_flag"],
            )
        return state["service"]

    def reset_service():
        state["service"] = None

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def check_csrf():
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Form expired. Go back and try again.")

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                return redirect(url_for("admin_login"))
            return view(*args, **kwargs)
        return wrapper

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["app_version"] = __version__

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # -- kiosk -----------------------------------------------------------------
    @app.get("/")
    def kiosk():
        if not cfg.has_api_key:
            return redirect(url_for("setup"))
        if not cfg.is_ready:
            return render_template("not_ready.html", cfg=cfg)
        return render_template("kiosk.html", cfg=cfg)

    @app.post("/api/lookup")
    def api_lookup():
        if not request.is_json or not cfg.is_ready:
            abort(400)
        dob = parse_dob(str((request.get_json(silent=True) or {}).get("dob", "")))
        if dob is None:
            return jsonify(status="invalid")
        try:
            result = service().lookup(dob)
        except ZandaError as exc:
            log.error("Zanda lookup failed: %s", exc)
            return jsonify(status="reception", reason="error")
        except Exception:
            log.exception("Lookup failed")
            return jsonify(status="reception", reason="error")
        if result.status != "match":
            return jsonify(status="reception", reason=result.reason)
        return jsonify(status="match", token=result.token, person=result.candidate.public())

    @app.post("/api/confirm")
    def api_confirm():
        if not request.is_json or not cfg.is_ready:
            abort(400)
        token = str((request.get_json(silent=True) or {}).get("token", ""))
        try:
            cand = service().confirm(token)
        except ZandaError as exc:
            log.error("Zanda check-in failed: %s", exc)
            return jsonify(status="reception", reason="error")
        except Exception:
            log.exception("Check-in failed")
            return jsonify(status="reception", reason="error")
        if cand is None:
            return jsonify(status="reception", reason="expired")
        return jsonify(status="done", firstName=cand.first_name, seeReception=cand.see_reception)

    @app.post("/api/cancel")
    def api_cancel():
        if request.is_json and cfg.is_ready:
            service().cancel(str((request.get_json(silent=True) or {}).get("token", "")))
        return jsonify(status="ok")

    @app.get("/manifest.webmanifest")
    def manifest():
        name = cfg["practice_name"] or "Check in"
        resp = jsonify({
            "name": f"{name} check-in", "short_name": "Check in",
            "start_url": "/", "scope": "/", "display": "standalone",
            "orientation": "any", "background_color": "#f6f3ee", "theme_color": "#f6f3ee",
            "icons": [{"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"}],
        })
        resp.mimetype = "application/manifest+json"
        return resp

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, ready=cfg.is_ready, version=__version__)

    # -- first-run setup -------------------------------------------------------
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if cfg.has_api_key:
            return redirect(url_for("admin"))
        error = None
        form = {"region": "au", "practice_name": ""}
        if request.method == "POST":
            check_csrf()
            form = request.form
            key = form.get("api_key", "").strip()
            region = form.get("region", "au")
            pin, pin2 = form.get("pin", ""), form.get("pin2", "")
            if not key:
                error = "Paste the API key from Zanda."
            elif region not in REGIONS:
                error = "Choose your Zanda region."
            elif not (pin.isdigit() and 4 <= len(pin) <= 8):
                error = "The staff PIN needs to be 4 to 8 digits."
            elif pin != pin2:
                error = "The two PINs don't match."
            else:
                try:
                    locations = make_client(key, region).list_locations()
                except ZandaError as exc:
                    error = ("Zanda didn't accept that key. Check it was copied in full and hasn't been revoked."
                             if exc.is_auth_problem else f"Zanda returned an error ({exc.status}). Try again shortly.")
                except Exception:
                    log.exception("Setup connection failed")
                    error = "Couldn't reach Zanda. Check this computer's internet connection."
                else:
                    in_person = [l for l in locations if l.get("serviceModality") == "In Person"] or locations
                    only = in_person[0] if len(in_person) == 1 else None
                    cfg.update(
                        api_key=key, region=region,
                        practice_name=form.get("practice_name", "").strip()[:80],
                        admin_pin_hash=generate_password_hash(pin),
                        location_id=only["id"] if only else None,
                        location_name=only["name"] if only else "",
                    )
                    reset_service()
                    session.clear()
                    session.permanent = True
                    session["admin"] = True
                    return redirect(url_for("admin"))
        return render_template("setup.html", error=error, form=form, regions=REGIONS)

    # -- staff settings (PIN protected) ----------------------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not cfg.has_api_key:
            return redirect(url_for("setup"))
        error = None
        now = clock.monotonic()
        if request.method == "POST":
            check_csrf()
            if now < state["pin_locked_until"]:
                error = "Too many tries. Wait five minutes."
            elif check_password_hash(cfg["admin_pin_hash"], request.form.get("pin", "")):
                state["pin_fails"].clear()
                csrf = session.get("csrf")
                session.clear()
                session.permanent = True
                session["admin"] = True
                session["csrf"] = csrf
                return redirect(url_for("admin"))
            else:
                state["pin_fails"] = [t for t in state["pin_fails"] if now - t < PIN_LOCKOUT] + [now]
                if len(state["pin_fails"]) >= PIN_MAX_TRIES:
                    state["pin_locked_until"] = now + PIN_LOCKOUT
                    state["pin_fails"].clear()
                error = "That PIN didn't match."
        return render_template("admin_login.html", error=error, cfg=cfg)

    @app.route("/admin", methods=["GET", "POST"])
    @admin_required
    def admin():
        message = error = None
        if request.method == "POST":
            check_csrf()
            f = request.form
            try:
                early = max(0, min(240, int(f.get("minutes_early", cfg["minutes_early"]))))
                late = max(0, min(120, int(f.get("minutes_late", cfg["minutes_late"]))))
            except ValueError:
                early, late = cfg["minutes_early"], cfg["minutes_late"]
            loc_id = f.get("location_id") or None
            loc_name = f.get("location_name_" + str(loc_id), "") if loc_id else ""
            cfg.update(
                practice_name=f.get("practice_name", "").strip()[:80],
                location_id=int(loc_id) if loc_id else None,
                location_name=loc_name,
                minutes_early=early, minutes_late=late,
                reception_flag=f.get("reception_flag", "").strip(),
            )
            reset_service()
            message = "Saved."
        locations, flags = [], []
        try:
            client = make_client()
            locations = client.list_locations()
            flags = client.list_appointment_flags()
        except ZandaError as exc:
            error = ("Zanda isn't accepting the saved API key. It may have been revoked. Remove it and set up again."
                     if exc.is_auth_problem else f"Couldn't load settings from Zanda ({exc.status}).")
        except Exception:
            log.exception("Admin load failed")
            error = "Couldn't reach Zanda. Check this computer's internet connection."
        return render_template("admin.html", cfg=cfg, locations=locations, flags=flags,
                               message=message, error=error)

    @app.post("/admin/forget-key")
    @admin_required
    def forget_key():
        check_csrf()
        cfg.update(api_key="", location_id=None, location_name="", admin_pin_hash="")
        reset_service()
        session.clear()
        return redirect(url_for("setup"))

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("kiosk"))

    return app
