"""JSON API for the desktop notifier app (bearer-token auth) and live events (SSE)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..accounts import device_by_token
from ..app import audit, touch
from ..db import Device, Organisation, Site
from ..security import new_device_token
from .deps import get_db

router = APIRouter(prefix="/api/v1")
HEARTBEAT = 20         # seconds between keep-alive comments
RECHECK = 30           # seconds between "is this device still signed in?" checks


def bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def notifier_device(request: Request, db) -> Device:
    device = device_by_token(db, bearer(request), "notifier")
    if device is None:
        raise HTTPException(401, "Signed out")
    touch(db, device)
    return device


@router.post("/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400)
    if not isinstance(body, dict):
        raise HTTPException(400)
    return await run_in_threadpool(_login, request, body)


def _login(request: Request, body: dict):
    db = request.app.state.Session()
    try:
        result = request.app.state.accounts.authenticate(
            request, db, str(body.get("email", ""))[:254], str(body.get("password", ""))[:1024],
            str(body.get("code", ""))[:32], require_mfa=True)
        if result.error:
            return JSONResponse({"error": result.error, "needsCode": result.needs_code}, status_code=401)
        user = result.user
        token, token_hash = new_device_token()
        name = str(body.get("deviceName", "") or "Desktop app")[:120]
        db.add(Device(organisation_id=user.organisation_id, user_id=user.id, kind="notifier",
                      name=name, token_hash=token_hash))
        audit(db, user.organisation_id, user.id, "device.notifier_added", name)
        db.commit()
        org = db.get(Organisation, user.organisation_id)
        return {"token": token, "organisation": {"name": org.name}, "user": {"name": user.name, "email": user.email}}
    finally:
        db.close()


@router.post("/auth/logout")
def logout(request: Request, db=Depends(get_db)):
    device = notifier_device(request, db)
    request.app.state.accounts.forget_device(db, device)
    audit(db, device.organisation_id, device.user_id, "device.signed_out", f"notifier: {device.name}")
    db.commit()
    return {"ok": True}


@router.get("/sites")
def sites(request: Request, db=Depends(get_db)):
    device = notifier_device(request, db)
    rows = db.query(Site).filter_by(organisation_id=device.organisation_id).order_by(Site.name).all()
    return {"sites": [{"id": s.id, "name": s.name, "locationName": s.location_name, "ready": s.is_ready}
                      for s in rows]}


@router.get("/sites/{site_id}/events")
async def events(site_id: int, request: Request):
    """Server-Sent Events: one `checkin` event per check-in at this site."""
    def authorise():
        db = request.app.state.Session()
        try:
            device = notifier_device(request, db)
            site = db.get(Site, site_id)
            if site is None or site.organisation_id != device.organisation_id:
                raise HTTPException(404)
            return device.id, site.name
        finally:
            db.close()

    device_id, site_name = await run_in_threadpool(authorise)

    def still_signed_in() -> bool:
        db = request.app.state.Session()
        try:
            d = db.get(Device, device_id)
            return d is not None and not d.revoked
        finally:
            db.close()

    broadcaster = request.app.state.broadcaster
    if not broadcaster.claim(device_id):
        raise HTTPException(429, "Too many open connections for this device")
    queue = broadcaster.subscribe(site_id)

    async def stream():
        loop = asyncio.get_running_loop()
        last_check = loop.time()
        try:
            yield "retry: 5000\n"
            yield f"event: hello\ndata: {json.dumps({'siteId': site_id, 'siteName': site_name})}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT)
                    yield f"event: checkin\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if await request.is_disconnected():
                    break
                if loop.time() - last_check > RECHECK:
                    last_check = loop.time()
                    if not await run_in_threadpool(still_signed_in):
                        yield "event: signed_out\ndata: {}\n\n"
                        break
        finally:
            broadcaster.unsubscribe(site_id, queue)
            broadcaster.release(device_id)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
