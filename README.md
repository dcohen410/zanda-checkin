# Zanda Checkin

Waiting-room check-in as a service for practices that use [Zanda](https://zandahealth.com).

- **Admin portal:** a practice creates an account, adds a *site* for each waiting room and pastes in that site's Zanda API key.
- **Check-in portal:** an iPad signs in once with the practice account and becomes a full-screen kiosk. Clients enter their date of birth, confirm "Hi Alex, is this you?", and the appointment is marked **Arrived** in Zanda. Anyone who can't be matched is asked to see reception.
- **Desktop app (Mac and Windows):** reception and clinicians sign in, choose a site, and get a pop-up such as "Alex N. has arrived for Sam Rivera" the moment someone checks in.

It runs as a single container, either locally or in the cloud.

> Not affiliated with or endorsed by Zanda Health. It uses the Zanda public API, which is **in beta**. Zanda's support site still describes the API as read-only. If marking an appointment Arrived isn't available on your account, switch the site to **notify-only**: staff get the desktop notification and mark Arrived in Zanda themselves.

## How it fits together

```
 iPad (Home Screen web app) ──HTTPS──┐
                                     ├──▶  Zanda Checkin container  ──HTTPS──▶  Zanda API
 Admin portal (any browser) ─HTTPS───┤      FastAPI · SQLite/Postgres          (per-site API key,
                                     │      encrypted keys · live events        encrypted at rest)
 Desktop app (Mac/Windows) ◀─HTTPS───┘      (Server-Sent Events)
```

| Part | Path | Signs in with | Can do |
|---|---|---|---|
| Admin portal | `/admin` | Practice account (session cookie) | Manage sites, API keys, devices; view activity |
| Check-in portal | `/checkin` | Practice account, once. The iPad then holds a kiosk-only token. | Look up and confirm check-ins for **its one site**. Can't open the admin portal. |
| Desktop app | `/api/v1` | Practice account. The app then holds a notifier-only token in the OS keychain. | List sites and receive live check-ins |

Every iPad and desktop app shows in the admin portal under **Signed-in devices**, and can be signed out from there.

## Identifying the client

1. The client enters their **date of birth** on a big on-screen keypad (DD/MM/YYYY). If the site requires it, they then tap the **first letter of their surname**.
2. Matching only uses **today's one-to-one appointments at the site's Zanda location** that start up to *90 minutes from now* or *30 minutes ago*. These limits are adjustable, and the appointment must be Pending, Confirmed or Arrived.
3. **Exactly one match** shows "Hi [preferred name], is this you?" with the time, plus the clinician's name unless the site hides it.
4. **No match, or more than one**, shows "Please see reception." The kiosk never guesses and never says why.

**Safeguards**
- Screens time out back to the start.
- Confirming needs a single-use, two-minute token.
- Repeated misses lock the kiosk to "see reception" for 5 minutes.
- No client details are stored, logged or sent anywhere except the live notification.

## Site settings

- **Zanda location** and **time zone**.
- **Check-in window**: minutes early and minutes late.
- **Ask for surname initial**: one extra tap for a much stronger match.
- **Show clinician name**: turn off if your waiting room is busy.
- **Mark Arrived in Zanda**: turn off for notify-only mode.
- **Send to reception flag**: choose a Zanda appointment flag. Clients whose appointment has it still check in, but are asked to see reception afterwards.

## Run it

### Locally with Docker

```bash
docker compose up --build
```

Open http://localhost:8000, create an account and add a site.

To try it without a Zanda key:

```bash
ZANDA_CHECKIN_DEMO=1 docker compose up --build
```

Any API key works in demo mode. Try these dates of birth:
- `01021990`: a normal match
- `15061985`: an appointment flagged "See reception"
- `03032001`: two people share it, so it goes to reception
- `09091999`: no match

### In the cloud

The image runs anywhere that runs containers, such as Fly.io, Render, Azure Container Apps, AWS App Runner/ECS or a VPS. Pick an **Australian region** for Australian practices.

- Put it behind **HTTPS**. Most platforms do this for you; otherwise use Caddy or Traefik.
- Run **one instance**. Live notifications are broadcast in memory; see the scaling notes below.
- Mount a persistent volume at `/data` (SQLite), or set `DATABASE_URL` to Postgres.

| Variable | Purpose |
|---|---|
| `PUBLIC_URL` | e.g. `https://checkin.example.com`. Turns on secure cookies and HSTS. |
| `ENCRYPTION_KEY` | Fernet key that encrypts stored Zanda API keys. **Set it and back it up**: if it's lost, every stored key is unreadable. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SECRET_KEY` | Signs session cookies. Any long random string. |
| `DATABASE_URL` | Default: SQLite in `/data`. Postgres: `postgresql+psycopg://user:pass@host/db` |
| `SIGNUP_CODE` | If set, new practices need this code to sign up (invite-only). |
| `FORWARDED_ALLOW_IPS` | Your reverse proxy's IP, so client IPs (used for sign-in rate limits) are read correctly. |
| `ZANDA_CHECKIN_DEMO` | `1` uses pretend Zanda data. |

If `SECRET_KEY` or `ENCRYPTION_KEY` aren't set, they're generated into `/data` on first start. That's fine locally, but set them explicitly in production.

### Without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m zanda_checkin --port 8000
```

## Setting up a practice

1. Go to `/signup` and create the practice account.
2. Add a site: name it, then paste the **Zanda API key**. In Zanda, the master user goes to Tools › Zanda API Key › Generate and copies the key (it's shown only once). If the practice has only one in-person location, it's picked automatically.
3. **iPad:**
   - Open `https://your-server/checkin` in Safari, sign in and choose the site.
   - Choose **Share › Add to Home Screen** and open it from the icon.
   - Lock it with Settings › Accessibility › **Guided Access**, set Auto-Lock to Never, and keep it on charge.
   - To sign the iPad out later, tap the top-right corner 5 times and sign in.
4. **Desktop app:**
   - Install it on reception and clinician computers.
   - Enter the server address, sign in, and choose the site.
   - It lives in the menu bar (Mac) or system tray (Windows), can open at login, and has a "Send a test notification" item.

## Desktop app

The source is in [`desktop/`](desktop), built with Electron.
- The sign-in token is encrypted with the OS keychain (`safeStorage`).
- The app listens with Server-Sent Events and reconnects with backoff.
- It refuses non-HTTPS servers, except `localhost`.

```bash
cd desktop
npm install
npm start        # run it
npm test         # SSE parser tests
```

Installers are built by GitHub Actions. Run the **Desktop app** workflow, or push a tag such as `desktop-v0.2.0`. It produces a `.dmg` (universal) and a Windows `.exe`. These builds are **unsigned**: sign them (an Apple Developer ID plus notarisation, and a Windows code-signing certificate) before giving them to practices.

## Security and privacy

A Zanda API key can read the whole practice, including clinical notes. Treat this service as holding health information.

- **API keys** are encrypted at rest (Fernet). They are never shown again after saving (only the last 4 characters) and never sent to iPads or desktop apps.
- **Zanda access is allow-listed in code.** [`zanda.py`](zanda_checkin/zanda.py) wraps only:
  - `GET /locations`
  - `GET /appointment-flags`
  - `GET /appointments`
  - `GET /client-profiles/{id}`
  - `PATCH /appointments/{id}` (to set Arrived)
- **Passwords** are hashed with scrypt.
- **Device tokens** are stored only as SHA-256 hashes and can be revoked.
- **Sign-in** is rate-limited, forms have CSRF protection, and a strict Content-Security-Policy is set.
- **Data kept:** practice accounts, site settings, devices, an audit log of admin actions, and a count of check-in outcomes. No client names, dates of birth or appointment details are stored. Client first names and last initials pass through only in the live notification.
- **Before offering this to other practices**, work through your obligations under the Privacy Act 1988, including APP 8 (cross-border disclosure), APP 11 (security) and the Notifiable Data Breaches scheme. You'll also need a privacy policy and data-processing terms. Get proper legal advice; this README isn't that.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

### Scaling notes

- A single container handles many practices: each check-in is a few Zanda calls, and the notifier connections are cheap.
- To run several instances, replace `Broadcaster` in [`registry.py`](zanda_checkin/registry.py) with Redis pub/sub or Postgres `LISTEN/NOTIFY`, and move the in-memory rate limits to the same store.
- The database tables are created on start-up. Add Alembic migrations before the schema changes in production.

## Roadmap

From a review of similar products (HotDoc, AutoMed, Jane, Cliniko add-ons, Envoy, Waitwhile and others):

- **Before launch:**
  - Confirm with Zanda that `PATCH /appointments` (Arrived) works on production accounts.
  - Add MFA for admin accounts.
  - Add a privacy policy and terms.
  - Sign the desktop builds.
- **Soon:**
  - Check in from the client's own phone (QR sign or SMS-reminder link).
  - Staff roles (owner, admin, reception, clinician), with clinicians notified only for their own clients.
  - Teams, Slack and email notifications.
  - Password reset by email.
  - Billing per site.
- **Later:**
  - Arrival analytics (on time, late, no-show).
  - Group appointment check-in.
  - Prompts to update contact details.
  - A walk-in route.
  - Languages other than English.

## Licence

MIT
