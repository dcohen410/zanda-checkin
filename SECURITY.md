# Security

Zanda Checkin holds Zanda API keys. A Zanda key can read a practice's whole account, including clinical notes, so this service is built to treat them as health information.

## Who can read a practice's Zanda API key?

| Who | Can they get the key? | Why |
|---|---|---|
| The practice's signed-in staff | Never shown, only used | After saving, the portal shows only the last 4 characters. |
| A check-in iPad | No | The iPad holds a device token that *unlocks* the key on the server; the key itself is never sent to it. |
| The desktop app | No | It never needs the key and never receives it. |
| **The service operator, with the database, backups and every server secret** | **No** (at rest), provided passwords are strong | See "Zero-access encryption" and "Offline password guessing" below. |
| Someone who steals the database | No | Same, plus every blob is also sealed with `ENCRYPTION_KEY`. |
| Someone who can change the code running on the server | **Yes, while devices are using it** | See "The limit" below. |

### Zero-access encryption

Each practice has a random 256-bit **master key (OMK)**. Every Zanda API key is encrypted with that practice's OMK (AES-256-GCM, bound to the practice and site IDs).

The OMK is never stored on its own. It is only stored *wrapped* (encrypted) separately for each thing the practice holds:
- **A staff account:** wrapped with a key derived from that person's password, using scrypt and a separate salt. The server stores only an scrypt hash of the password, which can't be turned back into it.
- **A check-in iPad:** wrapped with a key derived from that iPad's random 256-bit device token. The server stores only a SHA-256 hash of the token.
- **A signed-in browser:** wrapped with a key derived from a random secret that exists only in that browser's cookie. The session expires after 12 hours and is deleted on sign-out or password change.

On top of that, every wrapped blob and every encrypted key is sealed again with the server's `ENCRYPTION_KEY`.

So the database, the backups, `ENCRYPTION_KEY` and `SECRET_KEY` together still can't produce a Zanda key. Recovering one needs something only the practice has: a password, an iPad's token or a live browser session. `tests/test_web.py::test_database_and_server_key_cannot_reveal_api_key` checks this. It scans the raw database file for the key, the password and the device token, and tries to decrypt with the server's keys.

### Offline password guessing

Someone holding both the database and `ENCRYPTION_KEY` can try to guess a staff password offline. Each correct guess would unlock that practice's master key. Defences:
- **Expensive guesses.** Every guess costs a full scrypt run (about 128 MB of memory).
- **Strong passwords required.** At least 12 characters, and the zxcvbn strength checker must rate it 3 or 4 out of 4, so common or predictable passwords are refused.
- **Two-step sign-in doesn't help here,** because it guards sign-in rather than the encryption. The planned fix is passkeys: derive the key from a passkey's PRF output, which can't be guessed at all.

### Sessions

- Browser session key copies are deleted at sign-out, at password change, when the app finds them expired, at start-up and every 5 minutes.
- Each copy is cryptographically bound to its expiry time, so changing the expiry in the database doesn't make it usable again.
- The iPad set-up session lasts 5 minutes.

**The trade-off is deliberate.** If a practice loses its password, its authenticator and its recovery codes, nobody can recover its stored Zanda keys, including us. The practice makes a new key in Zanda and pastes it in again. A future password-reset feature will work the same way.

### The limit

Zanda's API doesn't accept calls from web pages: a browser-style CORS preflight is refused with 403. So the iPad can't talk to Zanda directly. When an iPad checks someone in, it sends its device token, and the server unlocks the key **in memory** for that request and calls Zanda. The key also stays in memory for up to 15 idle minutes while the site is in use.

An operator who changes the server code, or reads the process's memory, could capture keys at that moment. No server-side design can rule that out. What reduces and exposes the risk:
- **Open source and reproducible:** practices or their auditors can read exactly what runs. Publish signed container images built by CI from tagged commits, so the image can be matched to the source.
- **Least privilege at Zanda:** when Zanda adds per-resource key permissions (on their roadmap), give the key only appointments, appointment flags, locations and client profiles, with write access only to appointments.
- **Revocation:** a practice can revoke its key in Zanda at any time, and every iPad and desktop app can be signed out from the admin portal.
- **Stronger options, if you need a hard guarantee:**
  1. **A native iPad app** (Swift or React Native) calling Zanda directly. Native apps aren't subject to CORS, so the key would be stored only in the iPad's Keychain and never reach the server. The server would then only relay "Alex N. has arrived".
  2. **Confidential computing:** run the container in an attested enclave, such as AWS Nitro Enclaves, Azure Confidential Containers or GCP Confidential Space. The key-unlock step then happens in memory the host operator can't read, and practices can verify which code is running.

## Encryption

| What | How |
|---|---|
| Browser ↔ service | TLS 1.2/1.3 via Caddy (automatic Let's Encrypt). Plain HTTP only redirects. HSTS: 2 years, includeSubDomains, preload. |
| Desktop app ↔ service | HTTPS only. The app refuses `http://` except for localhost, and the OS validates certificates. |
| Service ↔ Zanda | HTTPS to fixed regional hosts, with certificate validation (no user-supplied URLs). |
| Service ↔ Postgres | The app refuses to start unless a remote database uses `sslmode=verify-full` or `verify-ca` (encrypted, with the certificate checked). |
| Caddy ↔ app | Plain HTTP on the private Docker network only. The app port isn't published to the host. |
| Zanda API keys at rest | AES-256-GCM under the practice master key, then sealed with `ENCRYPTION_KEY` (Fernet). |
| Practice master keys | Wrapped per holder (above). Never stored unwrapped. |
| Passwords | scrypt (N=2¹⁷, r=8, p=1), with a random salt for each password. |
| Device tokens | 256-bit random tokens, stored only as SHA-256 hashes. |
| Authenticator secrets | Encrypted with `ENCRYPTION_KEY`. Recovery codes are stored hashed. |
| Volumes and backups | Use encrypted disks and encrypted backups from your host (on by default at most cloud providers). |

In production the app refuses to start unless all of these hold:
- `PUBLIC_URL` is `https://`.
- `SECRET_KEY` and `ENCRYPTION_KEY` come from the environment, so they're never written to the data volume next to the database.
- `SIGNUP_CODE` is set (or `ALLOW_OPEN_SIGNUP=1` is set on purpose).

`ALLOW_INSECURE_HTTP=1` relaxes these checks, for local testing only. `scripts/make-env.sh` generates strong values.

## Accounts and devices

- **Two-step sign-in is required.** Every account must set up an authenticator app before using the admin portal, signing in an iPad or signing in the desktop app.
  - Codes can't be reused, and a ±30 s clock drift is allowed.
  - There are 8 one-time recovery codes.
- **Sign-in rate limits:** in any 15 minutes, 10 failed attempts per email+IP pair, 50 per email and 40 per IP address.
  - Failures on a waiting-room iPad's "sign out" screen are counted separately, so nobody at the iPad can lock staff out.
  - Unknown emails take the same time to check as real ones.
  - Password hashing runs at most 4 at a time, so a flood of attempts queues instead of exhausting memory.
  - Sign-up is limited to 5 attempts per IP address per hour.
- **Request size:** request bodies are capped at 64 KB at both Caddy and the app (chunked uploads too).
- **Server-side sessions:** these can be revoked. Changing a password signs the account out everywhere else.
- **iPads hold a kiosk-only token** in a `__Secure-`, HttpOnly, SameSite=Strict cookie scoped to `/checkin`. With it the iPad can only check people in for its own site, and it can't open the admin portal. Revoking the iPad deletes its key wrap, so the token unlocks nothing afterwards.
- **The desktop app:**
  - Stores its token encrypted with the OS keychain (`safeStorage`).
  - Is locked down: no navigation, no pop-ups and no device permissions, with a sandbox, context isolation, and IPC that only answers its own window.
  - Has Electron fuses set at build time (no run-as-Node, no `--inspect`, asar integrity checks, load only from asar), so a local process can't start it in debug mode to read the token.
  - Hides client names in notifications **by default**, because notifications can appear on a locked screen.
  - Live connections are capped at 3 per signed-in app, and revocation takes effect within about 30 seconds.

## Web protections

- **CSRF:** tokens on every form; the kiosk's JSON endpoints require `Content-Type: application/json`.
- **Content-Security-Policy:** `default-src 'self'` with no inline scripts, plus `frame-ancestors 'none'` and `upgrade-insecure-requests`.
- **Other headers:** COOP/CORP same-origin, a Permissions-Policy that turns off camera, mic, location and payment, `nosniff`, and `no-referrer`.
- **Allowed hosts:** the app only answers to the `PUBLIC_URL` host.
- **Cookies:** the session cookie is `__Host-` prefixed and Secure.
- **What's logged:** errors are logged by type only. Client names, dates of birth and appointment details are never logged or stored.

## Kiosk privacy

- Matching only looks at today's appointments at one location, inside a time window.
- Ambiguous matches go to reception.
- The confirm step uses a single-use, two-minute token.
- Six misses in 10 minutes lock the kiosk to "see reception" for 5 minutes.
- Screens time out back to the start.
- **The defaults are privacy-first:** the surname-initial step is on, and the clinician's name is hidden. A practice can change both.
- The lockout survives settings changes and idle restarts of a site's service.
- Each site chooses whether staff notifications include the client's first name and initial.

## Supply chain

- Python packages are installed with `--require-hashes` from `requirements.lock`.
- Base images are pinned by digest, and GitHub Actions by commit SHA.
- Dependabot proposes updates, and CI runs `pip-audit` on the locked set.

## Known gaps (not yet fixed)

- **Passkeys.** Deriving the practice key from a passkey's PRF instead of a password would make offline guessing impossible.
- **Rotating the practice key** after a suspected password leak: re-encrypt site keys and sign every device out.
- **Email verification** at sign-up. Sign-up still reveals whether an email already has an account.
- **Expiry or rotation** for desktop-app tokens (they're revocable, but don't expire on their own).
- **Signed installers.** Signing and notarising the desktop installers needs an Apple Developer ID and a Windows certificate.
- **A per-device iPad pairing code**, instead of typing the practice password on the iPad.
- **Least-privilege Zanda keys.** These depend on Zanda adding scoped permissions.

## Operating it safely

- Set `ENCRYPTION_KEY` and `SECRET_KEY` explicitly. Keep them in your platform's secret store, not in the repo or the image, and back up `ENCRYPTION_KEY`.
- Set `SIGNUP_CODE` so only invited practices can sign up.
- Host in Australia for Australian practices, to meet APP 8 (cross-border disclosure) and client expectations.
- Keep dependencies current. Dependabot is configured, and CI runs `pip-audit`.
- Sign the desktop installers (an Apple Developer ID with notarisation, and a Windows code-signing certificate) before you distribute them.
- Have a written data-breach response plan (the Notifiable Data Breaches scheme), a privacy policy and data-processing terms. Get legal advice; this document isn't legal advice.

## Reporting a vulnerability

Please report security issues privately via GitHub's "Report a vulnerability" (Security tab), not in public issues.
