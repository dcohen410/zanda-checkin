# Zanda Checkin

A waiting-room check-in kiosk for practices that use [Zanda](https://zandahealth.com). Clients tap **Check in** on an iPad and enter their date of birth. They confirm their first name, clinician and appointment time, and the appointment is marked **Arrived** in Zanda. Anyone who can't be matched is asked to see reception.

> Not affiliated with or endorsed by Zanda Health. It uses the Zanda public API, which is **in beta** and currently only open to practices in Zanda's beta group. Endpoints may change before general release.

## How it works

```
 iPad (Home Screen web app)  ──LAN──▶  Check-in server (a Mac in the office)  ──HTTPS──▶  Zanda API
   welcome · DOB keypad ·               holds the API key, finds the                       GET appointments
   confirm · done / reception           appointment, marks it Arrived                      GET client profile
                                                                                            PATCH appointment
```

- **The iPad never holds the API key.** It's a thin full-screen web app. The key lives only on the server computer, in `~/.zanda-checkin/config.json` (owner-only permissions).
- **Why a server at all?** A Zanda key can read the whole practice, including clinical notes. Keeping it off a device in a public waiting room matters. The server is also the hub for the planned reception notifications (see the roadmap below).
- **Allow-listed API use.** [`zanda.py`](zanda_checkin/zanda.py) wraps only the five endpoints the kiosk needs. It never calls notes, forms, invoices or payments.

## Identifying the client

The aim is as little typing as possible, but no guessing:

1. The client enters their **date of birth** on a big on-screen keypad (DD/MM/YYYY).
2. The server looks only at **today's appointments at this kiosk's location** that start between *90 minutes from now* and *30 minutes ago* (adjustable). It only considers one-to-one client appointments that are Pending, Confirmed or already Arrived.
3. **Exactly one match** shows "Hi Alex, is this you?" with their clinician and time. Their preferred name is used if Zanda has one.
4. **No match, or more than one**, shows "Please see reception." The kiosk never guesses, and never says why.
5. **Yes, that's me** sets `attendanceState` to `Arrived` in Zanda. **No, that's not me** goes to reception.

In a two-hour window at one location, a date of birth is close to unique. The confirmation screen is the second check. Other safeguards:
- Every screen times out back to the start.
- The confirm step uses a single-use, two-minute token, so the iPad can't mark any other appointment.
- After 6 unmatched attempts in 10 minutes, everyone goes to reception for 5 minutes.
- Nothing about clients is written to disk or logs.

**Sent to reception on purpose:**
- group appointments
- telehealth locations (choose your in-person location)
- cancelled or completed appointments
- shared dates of birth

### Reception flag

In settings, choose a Zanda **appointment flag** (for example "See reception"). When reception puts that flag on an appointment in Zanda, the client still checks in, but the done screen asks them to see reception before taking a seat.

## Setup

**You need:** a Mac (or any computer with Python 3.11+) that stays on in the office, an iPad on the same Wi-Fi, and a Zanda API key. To make a key in Zanda, the master user goes to Tools › Zanda API Key, selects Generate, and copies the key (it's shown once).

```bash
git clone https://github.com/dcohen410/zanda-checkin.git
cd zanda-checkin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m zanda_checkin
```

The server prints the address to open on the iPad, for example `http://192.168.1.20:8080/`.

1. **On the iPad**, open that address in Safari. Paste the API key, choose your region, and set a staff PIN.
   - Tip: do this step on the server Mac at `http://localhost:8080/setup` instead, so the key never crosses the Wi-Fi.
2. Choose the kiosk's **location** if you have more than one, plus the optional reception flag.
3. In Safari, choose **Share › Add to Home Screen** and open it from the icon. It runs full screen with no address bar.
4. Lock the iPad to the app:
   - Turn on **Settings › Accessibility › Guided Access** and triple-click to start it.
   - Set **Settings › Display & Brightness › Auto-Lock** to Never.
   - Keep the iPad on charge.
5. **Staff settings:** tap the top-right corner of the kiosk 5 times, then enter the PIN.

**Keep the server running:** use a Login Item or `launchd` on the Mac, and stop the Mac from sleeping (System Settings › Energy). Give the Mac a fixed IP address (a DHCP reservation on your router) so the iPad's address doesn't change.

### Try it without a key

```bash
ZANDA_CHECKIN_DEMO=1 .venv/bin/python -m zanda_checkin
```

Demo mode uses made-up appointments, and any API key works. Try these dates of birth:
- `01021990`: a normal match
- `15061985`: flagged for reception
- `03032001`: a shared date of birth, so it goes to reception
- `09091999`: no match

## Security notes

- The traffic between the iPad and the server is plain HTTP on your local network. Keep the kiosk on a trusted staff Wi-Fi, not guest Wi-Fi.
- The settings pages need the staff PIN. They lock after 5 wrong tries and log out after 15 minutes.
- **Remove API key** in settings deletes it from the server. Also revoke it in Zanda (Tools › Zanda API Key › Revoke).
- Zanda keys have no per-resource permissions yet, so treat the server Mac as holding access to your whole Zanda account. Keep it updated and behind its own login.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Roadmap

- **Reception notifications on staff Macs.** A small menu-bar app on each reception or clinician Mac connects to the check-in server on the local network. After each check-in, the server broadcasts a short notification, for example "Alex N. has arrived for Sam Rivera", which pops up and fades away. The hook is already in place: `CheckinService.listeners` receives a `CheckinEvent` (`client_label`, `practitioner`, `starts_at`, `see_reception`) after every check-in.
- Group appointment check-in (`PATCH /group-appointments/{id}/participants/{clientId}`).
- An optional second identifier (such as the first letter of the surname) for larger practices.

## API reference

[Zanda API docs](https://zandaapi.zandahealth.com/docs/). This app uses:
- `GET /locations`
- `GET /appointment-flags`
- `GET /appointments`
- `GET /client-profiles/{id}`
- `PATCH /appointments/{id}` with `{"attendanceState": "Arrived"}`. This endpoint is marked [BETA].

## Licence

MIT
