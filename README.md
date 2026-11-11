# 🚗 Car Insure — Car Insurance Claim Management System

**PROTECT YOUR JOURNEY** · Simple. Secure. Built around you.

A working first version of a car insurance claim portal built with Python, Flask, SQLite, HTML, CSS and JavaScript.

- **Customer side (mobile):** open a link on the phone → enter only the policy number → customer and vehicle details are fetched from the database → capture 5 photos and a damage video → submit → get a claim reference like `CLM-2026-000001`.
- **Insurance team side (desktop):** log in → dashboard with live counts → search and filter claims → open a claim → view the photo gallery (click to enlarge), play the damage video → update the status.

---

## 1. Requirements

- Python 3.9 or newer
- A laptop and a phone on the **same Wi-Fi network**

## 2. Create a virtual environment

```bash
cd car_insurance

# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Initialize the database and seed demo data

This happens **automatically** the first time you run `python app.py`.
To do it manually (safe to run any number of times):

```bash
python -m database.seed
```

To wipe all claims and uploaded files and start fresh:

```bash
python -m database.seed --reset
```

The database is created at `instance/insurance.db`.

## 5. Start the application

```bash
python app.py
```

The server listens on `0.0.0.0:5000` and prints the URLs to use, with your laptop's Wi-Fi IP detected automatically:

```
================================================
🚗 CAR INSURANCE CLAIM SYSTEM
================================================

Server started successfully.

💻 Computer:
http://127.0.0.1:5000/claim

📱 CUSTOMER PHONE:
http://192.168.1.10:5000/claim

💻 ADMIN:
http://192.168.1.10:5000/admin/login
...
```

## 6–8. URLs

| Who | Where | URL |
|---|---|---|
| Customer, on the laptop itself | laptop browser | `http://127.0.0.1:5000/claim` |
| **Customer, on a phone** | phone browser | `http://<LAPTOP-IP>:5000/claim` (printed at startup) |
| Insurance team | laptop browser | `http://127.0.0.1:5000/admin/login` or `http://<LAPTOP-IP>:5000/admin/login` |

`127.0.0.1` always means "this device". On the phone, `127.0.0.1` would point at the phone itself, so the phone **must** use the laptop's IP address.

### If the phone can't open the page

1. Check both devices are on the same Wi-Fi (not guest Wi-Fi, which often isolates devices).
2. **Windows:** the first time you run `python app.py`, Windows Firewall asks whether to allow Python — tick **Private networks** and allow. If you missed it: *Windows Security → Firewall & network protection → Allow an app through firewall → Python → Private*. Also make sure your Wi-Fi is set as a *Private* network.
3. **macOS:** allow incoming connections for Python if prompted (*System Settings → Network → Firewall*).
4. If you have several network adapters (VPN, Docker, VirtualBox), the printed IP might be the wrong one. Run `ipconfig` (Windows) or `ifconfig` / `ip addr` (macOS/Linux), find the Wi-Fi adapter's IPv4 address, and use that.

The customer's link is also shown in the admin portal under **Settings**.

## 9. Demo policy numbers

| Policy | Customer | Vehicle | Registration | Can claim? |
|---|---|---|---|---|
| **POL10001** | Teenu Karthick | Wagnor | TN38CT5038 | ✅ Active — use this one |
| POL10002 | Arjun Ramesh | Maruti Suzuki Swift | TN37CK5678 | ✅ Active |
| POL10003 | Priya Venkatesh | Tata Nexon | TN39BZ9012 | ✅ Active |
| POL10004 | Mohammed Farhan | Honda City | KA05MN3456 | ❌ Expired (shows the expired message) |
| POL10005 | Lakshmi Narayanan | Mahindra XUV700 | TN66DE7890 | ❌ Inactive (shows the not-active message) |
| POL10006 | Divya Shankar | Kia Seltos | TN38CQ4321 | ✅ Active |

Policy dates are set relative to the day the database is created, so active policies stay active.

## 10. Demo admin login

| Username | Password |
|---|---|
| `admin` | `Admin@123` |

The password is stored as a salted hash (Werkzeug `scrypt`/`pbkdf2`). Change it in the portal under **Settings → Change password**.

## 11. Where uploaded files are stored

```
uploads/
└── claims/
    └── CLM-2026-000001/
        ├── rear_photo.jpg
        ├── back_photo.jpg
        ├── left_photo.jpg
        ├── right_photo.jpg
        ├── damage_video.mp4
        └── chassis_photo.jpg
```

Files are named by evidence type, never by the customer's original filename. They are served only to logged-in staff through `/admin/file/<id>`; disk paths are never shown in the browser.

---

## AI photo check (vehicle view validation)

Every photo is checked as soon as the customer adds it, and again on the server before the claim is created.

| Slot | Must show |
|---|---|
| Rear view, Back view | the rear of the vehicle (tail lights, boot/tailgate, rear bumper…). Angled three-quarter rear photos are fine if the rear dominates |
| Left side | the vehicle's own **left** side (driver's-seat perspective; the car's front points to the left of the photo) |
| Right side | the vehicle's own **right** side |
| Chassis number | a close-up of the chassis/VIN number, plate or label |

**How it decides**
1. Local quality checks with Pillow: unreadable file, resolution under 480 px, almost black, blank, too dark, extremely blurry.
2. A Gemini vision model describes the photo. It is **not told which slot** the photo is for, so it can't just agree with the customer.
3. Python rules turn that into `FRONT / REAR / LEFT_SIDE / RIGHT_SIDE / CHASSIS / UNKNOWN`, compare it with the slot, and apply the confidence threshold (`0.80` by default). Left and right are worked out from which way the car's front points in the image.
4. If confidence is below the threshold the photo is not accepted ("Unable to confidently verify this image").
5. Filenames are never used.

**Setup (one time)**
1. Get an API key at https://aistudio.google.com.
2. Copy `.env.example` to `.env` (next to `app.py`) and set `GEMINI_API_KEY=...`.
3. `pip install -r requirements.txt`, then `python app.py`. The startup banner shows `🤖 AI PHOTO CHECK: ON …`.

Settings, all in **`config.py`** and overridable in `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `PHOTO_VALIDATION_MODE` | `auto` | `auto` = AI check when a key is set; with no key, photos are accepted as NOT VERIFIED (quality checks still run). `required` = photos must pass the AI check, nothing can be submitted without it. `off` = never use AI |
| `PHOTO_CONFIDENCE_THRESHOLD` | `0.80` | Minimum model confidence to accept a photo |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Configurable Google Gemini model (e.g. `gemini-3.5-flash-lite`, `gemini-2.5-pro`) |
| `PHOTO_VALIDATION_TIMEOUT` | `45` | Seconds before a check gives up |
| `MAX_PHOTO_CHECKS_PER_SESSION` | `40` | Abuse guard per customer session |

Without a key (default `auto` mode) the app works normally: photos get the local quality checks, the customer sees "✓ Photo added", and the claims team sees "Not AI-checked". Once a key is set, every photo must pass the AI check. If the key is set but the AI can't be reached, the photo is **not** accepted (the customer sees "We couldn't check this photo" with a Try again button) and the exact reason is printed in the server window and shown on the admin Settings page. Use `PHOTO_VALIDATION_MODE=required` for production so photos are never accepted without the AI.

**Cost:** each photo is resized to at most 1568 px before sending, so one check is roughly 2,000–2,500 input tokens. Five photos per claim is about a few US cents with Sonnet. The same photo is never analyzed twice (results are cached by content), so the server-side re-check at submit is normally free.

**Try it on your own photos**

```bash
python check_photo.py my_car.jpg rear_photo
python check_photo.py vin.jpg chassis_photo
```

**What the claims team sees:** each photo on the claim page shows "AI check: expected Rear · detected Rear · 94%", and the chassis section shows the number the AI read. This is stored per file in `claim_files` (`expected_view`, `detected_view`, `confidence`, `validated_at`, `validation_status`, `validation_details`). Existing databases are upgraded automatically.

**Switching AI provider:** the app talks to the model only through `services/vision_providers.py`. Add a class with `status()` and `analyze(jpeg_bytes)` that returns the fields in `OBSERVATION_SCHEMA`, register it in `PROVIDERS`, and set `PHOTO_VALIDATION_PROVIDER`.

**Tests:** `python -m unittest discover -s tests -t .` (96 tests, no API key needed; the model is replaced by a scripted stand-in).

## Vehicle number and chassis number

**Vehicle (registration) number:** after the policy is verified, the customer sees the registration from the policy, pre-filled and editable, and confirms it. It is stored normalized (uppercase, no spaces, dashes or dots), so `TN 38 CT 5038` is saved and shown as `TN38CT5038`. The format is checked against Indian registration patterns, including the BH series. If it differs from the policy record, the customer must confirm it twice, and the claims team sees "Policy record: … · verify" next to it.

**Chassis number (OCR):** read on the server from the chassis-plate photo; nothing the browser sends is trusted.
1. **Text reading:** reuses the existing Gemini vision call from the photo check, which transcribes every plate line and returns the VIN and engine number separately (no extra AI call). Without an AI key, a local **Tesseract** install is used if present (optional, see below).
2. **Picking the chassis number** (`services/chassis_ocr_service.py`):
   - lines labelled ENGINE / ENG / E.NO / MOTOR are never used;
   - lines labelled CHASSIS / VIN / FRAME are preferred;
   - the 17-character VIN format (no I, O or Q) is preferred;
   - spaces and punctuation added by OCR are removed;
   - a character the reader couldn't make out is never silently dropped.
3. **Status:**
   - **Verified:** a valid 17-character VIN where the AI's readings agree, with nothing corrected.
   - **Pending manual review:** anything uncertain, such as a format problem, readings that disagree, an I/O→1/0 correction, a partly legible plate, or a reading by basic OCR only.
   - **Not detected:** nothing could be read.
4. **Claims team:** the claim page shows the chassis number in Claim details and under the chassis photo, with the OCR status, a **Confirm / Save edit** box (stored as "Verified (manual check)" with the officer's name), and the original OCR text plus the engine number for audit.

The original chassis photo is never modified. Stored on `claims`: `chassis_number`, `chassis_ocr_status`, `chassis_ocr_confidence`, `chassis_original_ocr_text` (JSON audit) and `chassis_verified_by`. Existing databases get these columns automatically.

**Optional offline OCR (Tesseract):** install it from https://github.com/UB-Mannheim/tesseract/wiki (Windows). The app finds `C:\Program Files\Tesseract-OCR\tesseract.exe` automatically, or you can set `TESSERACT_CMD` in `.env`. Set `CHASSIS_OCR_FALLBACK=off` to disable it. Numbers read only by Tesseract always go to manual review.

## AI damage analysis and repair estimation (claims team)

After a claim is submitted, the four exterior photos (rear, back, left, right) are analyzed in the background with the **same Gemini vision model** used for the photo check. The customer never waits for it. On the claim page the team gets:

- **AI damage analysis:** pick a photo (Rear / Back / Left / Right), then switch between **Original** (the exact uploaded image) and **Damage analysis** (boxes drawn over it). Click a box to see damage type, confidence, location, severity and estimated repair. A summary lists each damage area with its confidence.
- **Repair estimation:** detected damage items, Parts / Labour / Paint / Other, and the **Estimated total**, labelled "Estimated cost · Subject to final inspection" and "Pending final inspection".

Rules that keep it honest:
- The original photos are only read, never modified. Boxes are an HTML layer drawn in the browser.
- No damage is drawn unless the model reports it. A photo with no damage says so.
- Detections below `DAMAGE_CONFIDENCE_THRESHOLD` (0.70) appear as dashed "Possible damage — requires manual review" and are not priced.
- If severity can't be judged ("Requires manual review"), the item is listed but not priced.
- The same damage seen in two photos (e.g. rear and back) is counted once.
- Claims with no analysis (older claims, no API key, or an API error) show "AI analysis unavailable. Review manually." With a key configured, a **Run AI damage analysis** / **Re-run analysis** button appears.

**Rates:** all amounts come from the sample rate card in `services/estimation_service.py` (`RATE_CARD`, `SEVERITY_MULTIPLIER`, `ROUND_TO`). Replace them with your workshop rates. They are not real quotes.

| Setting (config.py / .env) | Default | Meaning |
|---|---|---|
| `DAMAGE_ANALYSIS_MODE` | `auto` | `auto` = run when an AI key is configured, `off` = never |
| `DAMAGE_CONFIDENCE_THRESHOLD` | `0.70` | Below this, damage is "possible" and not priced |
| `CURRENCY_SYMBOL` | `₹` | Shown on the estimate |

Stored in two new tables: `damage_assessments` (one row per detected area: type, part, location, severity, confidence, box, estimated cost) and `claim_estimations` (status plus parts, labour, paint, other and total). Existing tables are unchanged; old databases get the new tables automatically. Cost: four extra vision calls per claim.

## Design and branding

- Gold and black corporate theme: `#0B0F14` black, `#F5B800` gold, `#FFC928` bright gold, `#F5F6F8` light background.
- Fonts: **Montserrat** (headings) and **Inter** (body), bundled in `static/fonts/` so everything works offline (SIL Open Font License, included).
- Hero backgrounds use a built-in night-highway illustration (`static/images/hero-art.svg`).

### Using your own photos

Drop photographs into `static/images/photos/` with these names and they replace the illustration automatically (no code change, just refresh the browser):

| File | Where it appears |
|---|---|
| `hero.jpg` | Customer welcome page and policy pages |
| `inspection.jpg` | Vehicle inspection (evidence) page header |
| `success.jpg` | Claim received page |
| `login.jpg` | Insurance team login page and claim header |

`.jpg`, `.jpeg`, `.webp` and `.png` all work. A dark overlay is applied automatically. Keep each under about 500 KB so phones load them fast, and only use photos you have the rights to.

## Project structure

```
car_insurance/
├── config.py               # all settings (AI check, thresholds); reads .env
├── check_photo.py          # try the AI check from the command line
├── tests/                  # unit + integration tests
├── app.py                  # app factory, error pages, LAN IP banner, runs on 0.0.0.0:5000
├── security.py             # CSRF tokens + admin login guard
├── requirements.txt
├── database/
│   ├── models.py           # SQLite schema + connection helpers
│   └── seed.py             # demo policies + demo admin (python -m database.seed)
├── routes/
│   ├── customer.py         # /claim, /claim/verify, /claim/evidence, /claim/submit, /claim/success/<ref>
│   └── admin.py            # /admin/login, /admin/dashboard, /admin/claims[/pending|/approved|/rejected], /admin/claim/<id>, /admin/settings
├── services/
│   ├── policy_service.py   # policy lookup + eligibility (swap this for a real policy system)
│   ├── claim_service.py    # claim reference, atomic claim + file creation, stats, status
│   ├── file_service.py     # extension + magic-byte validation, size limits, safe storage
│   ├── vehicle_image_validator.py  # AI photo check: quality checks + view decision rules
│   ├── vision_providers.py # Gemini vision provider (swappable): photo check + damage detection
│   ├── damage_detection_service.py # background damage analysis per claim, read model for the page
│   └── estimation_service.py       # configurable rate card → Parts/Labour/Paint/Other/Total
├── templates/
│   ├── customer/  base, _macros, welcome, verify, evidence, success, error
│   └── admin/     base, login, dashboard, claim_detail, settings, error
├── static/
│   ├── css/fonts.css, css/customer.css, css/admin.css
│   ├── js/evidence.js, js/admin.js
│   ├── fonts/              # Montserrat + Inter (bundled)
│   └── images/logo.svg, hero-art.svg, photos/  (optional real photos)
├── uploads/claims/         # evidence files, one folder per claim
└── instance/insurance.db   # created on first run (+ instance/secret_key)
```

## Database tables

- **policies** — `id, policy_number (unique), customer_name, customer_phone, vehicle_number, vehicle_make, vehicle_model, policy_status, policy_start_date, policy_end_date, created_at`
- **claims** — `id, claim_reference (unique), policy_id → policies.id, policy_number, customer_name, vehicle_number, submitted_at, status, reviewed_by, created_at, updated_at`
- **claim_files** — `id, claim_id → claims.id (cascade), file_type, file_path, original_filename, mime_type, file_size, uploaded_at` (one file per type per claim)
- **admin_users** — `id, username, full_name, password_hash, created_at`

To connect a real policy database, replace `find_policy()` and `get_policy_by_id()` in `services/policy_service.py`; nothing else reads the policies table directly except the admin Settings page.

## Upload rules

| Evidence | Formats | Max size |
|---|---|---|
| Rear, back, left, right, chassis photos | JPG, JPEG, PNG, WEBP | 15 MB each |
| Damage video | MP4, MOV | 100 MB |

Files are checked by extension **and** by their actual content (magic bytes), in the browser and again on the server.

## Security summary

- Policy number is validated and looked up with parameterized SQL; customers never see database errors or tracebacks.
- The evidence page and submit endpoint only work for a policy verified in the same browser session and still eligible.
- After a successful submit the session is cleared, so a claim can't be accidentally submitted twice; the button is also disabled while uploading.
- Admin: hashed passwords, session login, every `/admin` page and evidence file requires login, CSRF tokens on all forms, session regenerated on login, `no-store` caching on admin pages, logout.
