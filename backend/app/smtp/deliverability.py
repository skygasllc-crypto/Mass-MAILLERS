"""Deliverability check for the configured sender.

Gmail, Yahoo and Microsoft decide on inbox placement mainly from sender authentication
(SPF, DKIM, DMARC), an easy way to unsubscribe, and low complaint/bounce rates. This module
only *checks* those things and explains how to fix them; it does not try to bypass any filter.
"""
from __future__ import annotations

import dns.exception
import dns.resolver

from .config_store import SmtpConfig

# Free mailbox domains -> the SMTP hosts that may send for them. Their DMARC policies make
# receivers reject mail using these From addresses when it is sent through any other server.
FREE_MAIL = {
    "gmail.com": ("smtp.gmail.com",),
    "googlemail.com": ("smtp.gmail.com",),
    "yahoo.com": ("smtp.mail.yahoo.com",),
    "ymail.com": ("smtp.mail.yahoo.com",),
    "rocketmail.com": ("smtp.mail.yahoo.com",),
    "aol.com": ("smtp.aol.com",),
    "outlook.com": ("smtp-mail.outlook.com", "smtp.office365.com"),
    "hotmail.com": ("smtp-mail.outlook.com", "smtp.office365.com"),
    "live.com": ("smtp-mail.outlook.com", "smtp.office365.com"),
    "msn.com": ("smtp-mail.outlook.com", "smtp.office365.com"),
    "icloud.com": ("smtp.mail.me.com",),
    "me.com": ("smtp.mail.me.com",),
    "gmx.com": ("mail.gmx.com",),
    "zoho.com": ("smtp.zoho.com",),
    "yandex.com": ("smtp.yandex.com",),
}

# Approximate published sending limits. Providers change these; always check the current value.
PROVIDER_LIMITS = {
    "smtp.gmail.com": "Gmail allows about 500 recipients per day from a personal account "
                      "(about 2,000 with Google Workspace). Larger lists will be blocked until the next day.",
    "smtp-mail.outlook.com": "Outlook.com allows roughly 300 recipients per day for personal accounts.",
    "smtp.office365.com": "Microsoft 365 allows about 10,000 recipients per day, 30 messages per minute "
                          "and 500 recipients per message.",
    "smtp.mail.yahoo.com": "Yahoo personal accounts allow only a few hundred recipients per day.",
    "smtp.aol.com": "AOL personal accounts allow only a few hundred recipients per day.",
    "smtp.zoho.com": "Zoho limits depend on your plan; free plans are very limited.",
}

DKIM_SELECTORS = ("default", "selector1", "selector2", "google", "k1", "k2", "s1", "s2", "dkim",
                  "mail", "smtp", "mxvault", "zoho", "protonmail", "mandrill", "everlytickey1", "sig1")


def _txt(name: str) -> list[str] | None:
    """TXT records for ``name``; [] when none exist; None when DNS could not be queried."""
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError):
        return None
    return [b"".join(r.strings).decode("utf-8", errors="replace") for r in answer]


def _has_mx(domain: str) -> bool | None:
    try:
        dns.resolver.resolve(domain, "MX", lifetime=5)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except (dns.exception.DNSException, OSError):
        return None


def check(cfg: SmtpConfig) -> dict:
    items: list[dict] = []

    def add(name: str, status: str, message: str) -> None:
        items.append({"name": name, "status": status, "message": message})  # ok | warn | error | info

    if not cfg.from_email or "@" not in cfg.from_email:
        add("From address", "error", "Enter a From email address first.")
        return {"items": items, "domain": None}

    domain = cfg.from_email.rpartition("@")[2].lower()
    host = cfg.host.lower()

    # --- free mailbox used as From -------------------------------------------------------
    if domain in FREE_MAIL:
        allowed = FREE_MAIL[domain]
        if host and host not in allowed:
            add("From address", "error",
                f"@{domain} addresses may only be sent through {' or '.join(allowed)}. Sent through "
                f"{cfg.host}, Gmail, Yahoo and Outlook will reject or spam-folder the email ({domain} "
                "publishes a strict DMARC policy). Use an address on your own domain instead.")
        else:
            add("From address", "ok", f"@{domain} is sent through its own provider, so it is authenticated by {domain}.")
        add("Personal mailbox", "warn",
            "Personal mailboxes are not meant for bulk sending and have low daily limits. For regular "
            "sending to hundreds of people, use an address on your own domain (e.g. news@yourcompany.com).")
    else:
        # --- own domain: check DNS authentication ----------------------------------------
        mx = _has_mx(domain)
        if mx is None:
            add("DNS", "warn", "Could not query DNS (offline?). Domain checks were skipped.")
        else:
            add("MX record", "ok" if mx else "warn",
                f"{domain} can receive mail (bounces and replies)." if mx else
                f"{domain} has no MX record. Some providers reject mail from domains that cannot receive mail.")

            spf = _txt(domain) or []
            spf = [r for r in spf if r.lower().startswith("v=spf1")]
            if len(spf) == 1:
                add("SPF", "ok", f"SPF record found: {spf[0][:160]}")
                add("SPF includes your SMTP server?", "info",
                    f"Make sure this record authorizes {cfg.host or 'your SMTP server'} (e.g. include:... "
                    "from your provider's setup guide).")
            elif len(spf) > 1:
                add("SPF", "error", "More than one SPF record found. A domain must have exactly one; merge them.")
            else:
                add("SPF", "error", f"No SPF record on {domain}. Gmail and Yahoo require SPF or DKIM; add the "
                                    "TXT record your email provider gives you.")

            dmarc = [r for r in (_txt(f"_dmarc.{domain}") or []) if r.lower().startswith("v=dmarc1")]
            if dmarc:
                add("DMARC", "ok", f"DMARC record found: {dmarc[0][:160]}")
            else:
                add("DMARC", "error", f"No DMARC record at _dmarc.{domain}. Gmail and Yahoo require DMARC for "
                                      "bulk senders. Start with a TXT record: v=DMARC1; p=none; rua=mailto:you@"
                                      f"{domain}")

            found = [s for s in DKIM_SELECTORS if any("p=" in r for r in (_txt(f"{s}._domainkey.{domain}") or []))]
            if found:
                add("DKIM", "ok", f"DKIM key found (selector: {', '.join(found)}).")
            else:
                add("DKIM", "warn", "No DKIM key found under common selector names. Your provider may use a "
                                    "custom selector: turn on DKIM signing in your email provider's admin panel, "
                                    "then confirm with a test email (see below).")

    # --- account / From alignment ----------------------------------------------------
    if "@" in cfg.username and cfg.username.rpartition("@")[2].lower() != domain:
        add("Sender identity", "warn",
            f"You sign in as {cfg.username} but send as {cfg.from_email}. Only do this if the server "
            "allows that address (an alias or 'send as' permission), otherwise mail is rejected.")

    # --- unsubscribe -----------------------------------------------------------------
    if cfg.unsubscribe_url:
        add("Unsubscribe", "ok", "One-click unsubscribe (List-Unsubscribe-Post) is set up.")
    else:
        add("Unsubscribe", "ok" if cfg.add_footer else "warn",
            f"List-Unsubscribe header points to {cfg.unsubscribe_mailto}."
            + ("" if cfg.add_footer else " Turn on the unsubscribe footer too, so people can opt out instead "
                                         "of pressing 'Report spam'."))

    # --- provider limits -------------------------------------------------------------
    if host in PROVIDER_LIMITS:
        add("Sending limit", "info", PROVIDER_LIMITS[host])

    add("Verify with a test", "info",
        "Send a test to a Gmail address, open it, choose ⋮ → 'Show original' and check that SPF, DKIM "
        "and DMARC all say PASS.")
    return {"items": items, "domain": domain}
