# Mass Mailer

A small, simple web app for sending one legitimate business email to up to 10,000 recipients through your own SMTP server. Recipients are hidden from each other with BCC, replies go to the Reply-To address you choose, and you can add attachments. The app sends in the background and shows its progress.

It doesn't try to be a CRM or a marketing platform, and it has no features for getting around spam filters or provider limits.

---

## 1. Installation

You need **Python 3.10 or newer**.

**Windows (PowerShell)**
```powershell
cd mass-mailer
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**macOS / Linux**
```bash
cd mass-mailer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**Docker (optional)**
```bash
cp .env.example .env      # set ADMIN_PASSWORD and SECRET_KEY first
docker compose up -d --build
```

## 2. Configuration

Open `.env` and set at least these values:

| Setting | Meaning |
|---|---|
| `EMAIL_MODE` | `development` (the default) never sends real email. Every message goes to the built-in **Test Mailbox**. Set it to `production` to send for real. |
| `ADMIN_PASSWORD` | The password for signing in to the web interface. In production it must be at least 10 characters. |
| `SECRET_KEY` | A long random string that signs sessions and encrypts the stored SMTP password. You can generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SMTP_MAX_RECIPIENTS_PER_MESSAGE` | The most recipients your SMTP provider accepts in one message. The batch size can never be set higher than this. |
| `MAX_ATTACHMENT_MB` / `MAX_MESSAGE_MB` | Limits on attachment size and total email size. |
| `MAX_PARALLEL_SENDS` | How many sends one user can run at the same time, each through a different SMTP account (default 50). |

In development mode, if `ADMIN_PASSWORD` or `SECRET_KEY` is missing, the app generates one and saves it in `data/` (`data/admin_password.txt`). Production mode refuses to start without both.

**SMTP credentials are never stored in files or source code.** You enter them in the web interface. The app saves the password encrypted in `data/mailer.db` and never shows it again.

## 3. SMTP setup

Get these details from your email provider or IT department:

| Port | Security setting |
|---|---|
| **587** | STARTTLS (the most common choice) |
| **465** | SSL/TLS |
| **25** | None or STARTTLS, depending on the server. Only use "None" for a server on the same machine. |

The **From Email** should be the account you sign in with, or an address you are authorized to send as. Many providers reject anything else. If the From domain differs from the SMTP account's domain, the app shows a warning.

Many providers, such as Microsoft 365 and Google Workspace, require an "app password" or SMTP AUTH to be switched on for the account.

**Gmail:** use `smtp.gmail.com`, port 587 with STARTTLS, and your full Gmail address as the username. The password must be a 16-letter **App Password** (turn on 2-Step Verification, then create one at https://myaccount.google.com/apppasswords). Your normal Google password is refused. An App Password can't be shown again after you create it; if you lose it, delete it and create a new one.

### Saved SMTP accounts

You can save several SMTP accounts and switch between them. Fill in the settings, then click **Save account…** at the top of the SMTP card and give it a name. The settings are saved together with the password, which is encrypted. To switch, choose the account under **Saved accounts** and click **Use**. If an account passed **Test SMTP Connection** with exactly those settings, it stays verified when you switch back to it. Saving under an existing name replaces that account. Sends that are already running keep the account they started with.

## 4. Starting the application

```bash
python run.py
```

Open **http://127.0.0.1:8000** and sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`, or create an account.

The server listens only on 127.0.0.1 by default. If you need to reach it from other machines, put it behind an HTTPS reverse proxy (nginx, Caddy, IIS) and keep `COOKIE_SECURE=true`. Run a single process only (the default), because the sending queue and login sessions live in memory.

## 5. Importing recipients

In **2 · Recipients**, you can either:

* **paste addresses**, separated by commas, semicolons, spaces or new lines. `Name <email>` lines work too, or
* click **Import CSV**. These layouts are supported:

```csv
email
john@example.com
mary@example.com
```
```csv
name,email
John Doe,john@example.com
Mary Smith,mary@example.com
```
Header-less `John Doe,john@example.com` rows and `;` or tab separators also work.

Addresses are trimmed, lower-cased and checked for valid syntax. The app shows counts for **Total / Valid / Invalid / Duplicates / Ready to send**, and **Show invalid addresses** lists the problem entries. Click **Remove invalid & duplicates** to clean the list. Duplicates are never sent twice, even if you skip the cleanup. The app does no mailbox probing: it doesn't contact recipients' servers to check addresses. The limit is 10,000 recipients (`MAX_RECIPIENTS`).

## 6. Creating an email

In **3 · Compose Email**:

* **Subject**
* **HTML** or **Plain text** mode. In HTML mode the toolbar inserts basic tags (bold, italic, paragraph, heading, link, list). If you type plain text in HTML mode, it is turned into paragraphs automatically. The app always adds a plain-text copy (`multipart/alternative`).
* **Preview** shows the message exactly as it will be built.
* **Optional personalization:** tick *Personalize with `{{name}}`* and write `Hello {{name}},`. Names come from the CSV or from `Name <email>` lines. Recipients without a name get the fallback word (default: "there"). With personalization on, each recipient gets their own copy addressed only to them.

## 7. Setting Reply-To

Enter it in **SMTP Settings → Reply-To**. The app adds it as the standard `Reply-To:` header, so when a recipient clicks *Reply*, the response goes to that address instead of the From address. The app checks that it is a valid address before sending. If you leave it empty, replies go to the From address.

## 8. Adding attachments

Click **Add Attachment**. Each file is listed with its name, its size and a **Remove** button.

* Allowed types: PDF, DOC, DOCX, XLS, XLSX, CSV, PNG, JPG, ZIP, TXT (set with `ALLOWED_ATTACHMENT_EXTENSIONS`).
* The app checks the file contents too, so a renamed `.exe` is still rejected.
* Executable files (`.exe .bat .cmd .scr .ps1 …`) are blocked unless an administrator sets `ALLOW_EXECUTABLE_ATTACHMENTS=true`.
* Before sending, the app works out the size of the finished email and refuses anything larger than `MAX_MESSAGE_MB`.

## 9. Sending a test

In **5 · Send**, enter your own address under *Send test* and click **Send Test**. The test is the exact campaign message: same From, Reply-To, Subject, HTML and attachments. Check how it looks in your inbox and that Reply goes to the right address.

In development mode, the test appears in the **Test Mailbox** at the bottom of the page. There you can open each message, see its headers, MIME structure and envelope recipients, and download it as `.eml`.

## 10. Sending a batch

1. Choose the **BCC batch size** (default 100) and the **Sending mode**:

   | Mode | Pause between batches |
   |---|---|
   | Conservative | You choose: 10 s, 30 s (default), 1 min, 2 min, 5 min, 10 min or 20 min |
   | Normal | 10 s |
   | Fast | 3 s |

   With Conservative selected, a **Pause between batches** list appears. The hint below the sending mode shows the total pause time for the whole list (for example, 100 batches with a 5-minute pause adds about 8 hours). The page remembers the mode and pause you last chose. The pause you choose is used exactly, also in development mode. Development mode shortens only the fixed Normal/Fast pauses, and the Conservative default when no pause is chosen, tenfold.
2. Click **SEND EMAIL**. A **READY TO SEND** summary appears with the recipient count, number of batches, From, Reply-To, Subject, attachments and size. Nothing is sent until you click **SEND NOW**.
3. Sending runs in the background, so you can keep using the page. The progress panel shows the percentage, the completed count, successes, failures, the current batch and the current status. **Stop sending** cancels the remaining batches.
4. When it finishes, **SENDING COMPLETE** shows the totals. From there:
   * **View Failed**: each failed address with its status, SMTP response and timestamp.
   * **Export Failed** / **Export Results**: download a CSV.
   * **New Email**: clears the recipients, message and attachments.

**Sending keeps going after you log out.** Sends run on the server, not in your browser, so you can close the page or log out and log back in later. The **Sends in progress** panel lists every send that is still running. Click **View** to see one in the progress panel.

**Sending through several SMTP accounts at once.** Each send keeps the SMTP settings it was started with, so changing the settings afterwards doesn't affect it. To send another bulk email through a different account at the same time, enter that account in **SMTP Configuration**, save and test it, and send again. Each SMTP account can run only one send at a time, so its pause between batches and the provider's limits are respected. One user can run up to `MAX_PARALLEL_SENDS` sends at once (default 50). The app drops its copy of the SMTP credentials as soon as a send ends.

Results are saved locally in `data/mailer.db`.

**Production mode** only sends after **Test SMTP Connection** has succeeded with the current settings. That test checks DNS, the TCP connection, TLS and authentication. If you change the host, port, security setting, username or password, you have to run the test again.

## 11. Understanding BCC batching

A list of 1,000 recipients is **not** sent as one giant message. The app works like this:

```
Recipient list → validation → batches of 100 → one SMTP message per batch → next batch
```

For each batch:

* The recipients go **only in the SMTP envelope** (`RCPT TO`). They are **never** written into the `To`, `Cc` or `Bcc` headers or the body, so no recipient can see anyone else.
* The visible `To:` header shows your From address, or the *Visible "To" address* you set.
* Every message gets correct `From`, `To`, `Reply-To`, `Subject`, `Date`, `Message-ID`, `MIME-Version` and `Content-Type` headers.

Failures are handled like this:

* **Permanent (5xx)**, e.g. `550 Mailbox unavailable`: the address is marked FAILED and **not retried**.
* **Temporary (4xx)**, e.g. `451 try again later`: the app retries only the affected addresses, at most `MAX_RETRIES` times (default 2), waiting `RETRY_DELAY_SECONDS` between tries.
* **Authentication rejected mid-send**: the job stops. The remaining addresses are marked "not sent" so you can export them.

The MIME structure of an HTML email with an attachment:

```
multipart/mixed
├── multipart/alternative
│   ├── text/plain
│   └── text/html
└── application/pdf  (attachment)
```

## User accounts and the admin

* **Anyone can create an account** on the sign-in page (**Create an account**). Each user sets up and
  tests **their own SMTP account**, and has their own templates, attachments, send history and
  do-not-send list. Users can never see each other's data.
* The **main admin** is created automatically from `ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env`. Sign in
  with that email and password. Changing `ADMIN_PASSWORD` and restarting is also how you reset it.
* Admins see a **Users** panel at the top of the page listing every account, when it registered and last
  signed in, and how many emails it has sent. From there an admin can:
  * **Disable** an account: the user is signed out immediately and cannot sign in. **Enable** reverses it.
  * **Delete** an account, together with all of its data (SMTP settings, templates, attachments, history).
  * **Approve** new accounts, if `REGISTRATION_REQUIRES_APPROVAL=true`. By default new accounts can be
    used immediately.
* Users can change their own password with **Change password** in the top bar.
* Each user can run one send at a time; different users can send at the same time.
* To protect your network, regular users can only use public SMTP servers on the standard ports
  25, 465, 587 and 2525 (`ALLOW_PRIVATE_SMTP_HOSTS`). Admins are exempt.
* Set `ALLOW_REGISTRATION=false` to close sign-ups.

**Before you let other people reach the site:** put it behind HTTPS (for example nginx or Caddy with a
certificate), keep `COOKIE_SECURE=true`, and use a strong `ADMIN_PASSWORD` and `SECRET_KEY`. Without
HTTPS, passwords would travel over the network unencrypted.

Upgrading from the single-user version: on the first start, your existing SMTP settings, templates,
attachments, history and do-not-send list are moved to the main admin account.

## Saved templates

The **Template** bar at the top of *Compose Email* stores messages you send often:

* **Save as template…** saves the current subject, message, format, personalization settings **and attachments** under a name.
* **Load** fills in the email from a template, including new copies of its attachments.
* **Update** overwrites the selected template with what's in the composer now. **Delete** removes it.

Templates are plain folders on disk, so they are easy to back up or copy to another computer:

```
templates/<id>/template.json    name, subject, body, format, personalization
templates/<id>/att-*.bin        copies of the template's attachments
```

## Getting accepted by Gmail, Yahoo, Outlook and other providers

No software can guarantee the inbox. Gmail, Yahoo and Microsoft decide from **who you are** (sender authentication), **whether people want your mail** (complaints, unsubscribes) and **list quality** (bounces). The app handles the parts it can and checks the rest:

| What | Where |
|---|---|
| `List-Unsubscribe` header on every email, so the provider's own **Unsubscribe** button appears. It adds RFC 8058 one-click unsubscribe when you enter an https link. | SMTP Settings → Deliverability |
| Optional footer with your company address and how to unsubscribe | SMTP Settings → Deliverability → *Add an unsubscribe footer* |
| **Check deliverability**: looks up SPF, DKIM, DMARC and MX for your From domain and flags setups that providers reject | SMTP Settings |
| **Individual messages** delivery: each recipient gets their own copy with their address in `To:`. Providers trust this more than large BCC messages. Messages are paced (2 s / 1 s / 0.5 s apart) to stay within per-minute limits. | Send → Delivery |
| **Do-not-send list**: people who unsubscribe, plus addresses that bounce with "mailbox does not exist" (added automatically), are skipped in every future send | Recipients → *Do-not-send list* |
| Content warnings (ALL-CAPS subjects, `!!`, URL shorteners, image-only emails, missing unsubscribe line) | the confirmation dialog |

What **you** need to set up once, at your domain's DNS or email provider:

1. **SPF**: a TXT record on your domain that authorizes your SMTP server (your provider's guide gives the exact `include:`).
2. **DKIM**: switch on DKIM signing in your email provider's admin panel and publish the key it gives you.
3. **DMARC**: a TXT record at `_dmarc.yourdomain.com`. Start with `v=DMARC1; p=none; rua=mailto:you@yourdomain.com`.
4. Use a **From address on your own domain**. `@gmail.com`, `@yahoo.com` and `@outlook.com` addresses may only be sent through those providers' own SMTP servers, and personal accounts allow only a few hundred recipients per day.
5. Send a test to a Gmail address, open it, choose **⋮ → Show original** and confirm that **SPF, DKIM and DMARC all say PASS**.
6. Send only to people who expect your email, and honor unsubscribes: add them to the do-not-send list.

## 12. Troubleshooting

| Problem | What to do |
|---|---|
| ✗ Could not resolve the SMTP host name | Check the spelling of the host name and your internet or DNS connection. |
| ✗ Could not connect to port … | The port is wrong or blocked. Many home ISPs and cloud hosts block port 25; try 587. |
| ✗ Secure connection failed | Security setting doesn't match the port: **465 → SSL/TLS**, **587 → STARTTLS**. |
| ✗ TLS certificate could not be verified | The server's certificate isn't trusted. Use the provider's official host name. |
| ✗ Authentication failed | Check the username and password. Many providers need an **app password**, or SMTP AUTH switched on for the account. |
| "Run Test SMTP Connection before sending" | You're in production mode and the settings haven't been verified yet, or they changed since the last test. |
| Many `550`/`554` failures | The server rejected the sender or the recipients. Check that your From address is authorized and that your domain has SPF/DKIM set up. |
| `452 Too many recipients` | Lower the **BCC batch size**, and set `SMTP_MAX_RECIPIENTS_PER_MESSAGE` to your provider's limit. |
| Login keeps asking for the password | You're on plain `http://` with `COOKIE_SECURE=true`. Use HTTPS, or for localhost only set `COOKIE_SECURE=false`. |
| "This SMTP account is already sending" | A send through the same SMTP account is still running. Wait for it to finish, or use a different SMTP account. |
| "Stopped: application was restarted" | The app was closed during a send. Export the results; addresses marked NOT_SENT did not receive the email. |

### Development mode test addresses

In `EMAIL_MODE=development` you can try the failure handling safely with these reserved domains:

* `anything@bounce.test` → permanent `550` failure
* `anything@tempfail.test` → temporary `451` on every try, so it fails after the retries run out
* `anything@retry.test` → `451` on the first try, accepted on the retry

---

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a **local mock SMTP server** (aiosmtpd) and never contact a real provider. They cover the SMTP connection test (DNS, TCP, STARTTLS, authentication), email validation, CSV import, duplicate removal, BCC batching and privacy, Reply-To, HTML/MIME structure, attachments, personalization, temporary and permanent failures, retries, progress, the login/CSRF/rate-limit protections, and the full 1,000-recipient workflow.

## Security notes

* Sign-in is required, with HttpOnly and SameSite=Strict cookies. Secure cookies are the default in production.
* Every change request needs a CSRF token. Login, uploads, tests and sends are rate-limited.
* The SMTP password is encrypted at rest (Fernet), never returned to the browser, and never logged.
* The app refuses to send the password over an unencrypted connection unless the server is on localhost.
* Header fields are checked for line breaks to block header injection. Uploaded file names are sanitized, and files are stored under random names.
* The app is **not** an SMTP relay. It only sends the message you compose, through the account you configure.
* Logs contain job IDs and counts, not recipient addresses.

## Project structure

```
mass-mailer/
  backend/app/
    main.py            FastAPI app + security headers
    config.py          settings from .env
    db.py              SQLite
    api/routes.py      JSON API
    smtp/              stored SMTP settings, connection test, transports
    recipients/        parsing, validation, de-duplication, batching
    sending/           MIME message builder, background sending engine
    attachments/       upload validation and storage
    models/            request schemas
    utils/             sessions/CSRF/rate limiting, encryption, HTML↔text
  frontend/            index.html, styles.css, app.js (no build step)
  uploads/  data/      runtime files (git-ignored)
  tests/
  run.py  .env.example  requirements.txt  Dockerfile  docker-compose.yml
```

Use this tool only for recipients who expect to hear from you, and follow the email laws that apply to you (CAN-SPAM, GDPR/PECR, CASL, …). Include your contact details and a way for people to opt out in the message.
