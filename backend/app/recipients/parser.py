"""Recipient parsing, normalization, validation and de-duplication.

Only syntax is checked. No mailbox probing (SMTP VRFY/RCPT checks) is performed.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_NAMED_RE = re.compile(r'^\s*"?(?P<name>[^"<]*?)"?\s*<(?P<email>[^<>]*)>\s*$')
_HEADER_WORDS = {"email", "e-mail", "email address", "e-mail address", "mail", "name", "full name"}
_EMAIL_COLS = {"email", "e-mail", "email address", "e-mail address", "mail", "email_address"}
_NAME_COLS = {"name", "full name", "full_name", "fullname", "first name", "first_name", "firstname", "contact"}
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>",;]')

MAX_CSV_BYTES = 2 * 1024 * 1024


@dataclass
class Recipient:
    email: str
    name: str = ""


@dataclass
class ParseResult:
    valid: list[Recipient] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    total: int = 0

    def stats(self) -> dict:
        return {
            "total": self.total,
            "valid": len(self.valid),
            "invalid": len(self.invalid),
            "duplicates": len(self.duplicates),
            "ready": len(self.valid),
            "named": sum(1 for r in self.valid if r.name),
        }


def normalize_email(raw: str) -> str:
    value = raw.strip().strip("<>").strip().strip("'\"").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value.strip().lower()


def clean_name(raw: str) -> str:
    name = _UNSAFE_NAME_CHARS.sub(" ", raw or "")
    return re.sub(r"\s+", " ", name).strip()[:100]


def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254 or email.count("@") != 1:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or len(local) > 64:
        return False
    return bool(_LOCAL_RE.match(local) and _DOMAIN_RE.match(domain))


class _Collector:
    def __init__(self) -> None:
        self.result = ParseResult()
        self._seen: set[str] = set()

    def add(self, raw_email: str, name: str = "") -> None:
        raw_email = raw_email.strip()
        if not raw_email:
            return
        self.result.total += 1
        email = normalize_email(raw_email)
        if not is_valid_email(email):
            self.result.invalid.append(raw_email[:200])
            return
        if email in self._seen:
            self.result.duplicates.append(email)
            return
        self._seen.add(email)
        self.result.valid.append(Recipient(email=email, name=clean_name(name)))


def parse_text(text: str) -> ParseResult:
    """Parse pasted addresses separated by commas, semicolons, spaces or newlines.

    Also understands ``Name <email>`` and ``Name, email`` lines (as produced by CSV import).
    """
    collector = _Collector()
    for index, line in enumerate((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        pieces = [p.strip() for p in re.split(r"[,;\t]", line)]
        if index == 0 and all(p.lower() in _HEADER_WORDS for p in pieces if p):
            continue  # pasted CSV header row
        with_at = [p for p in pieces if "@" in p]
        without_at = [p for p in pieces if p and "@" not in p]
        if len(pieces) == 2 and len(with_at) == 1 and len(without_at) == 1 and not _NAMED_RE.match(with_at[0]):
            collector.add(with_at[0], without_at[0])  # "Name, email" / "email, Name"
            continue
        for piece in pieces:
            if not piece:
                continue
            named = _NAMED_RE.match(piece)
            if named:
                collector.add(named.group("email"), named.group("name"))
            else:
                for token in piece.split():
                    collector.add(token)
    return collector.result


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_csv(data: bytes) -> ParseResult:
    """Parse a CSV with an ``email`` column (optionally ``name``), or header-less ``name,email`` rows."""
    if len(data) > MAX_CSV_BYTES:
        raise ValueError("CSV file is too large (maximum 2 MB).")
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    collector = _Collector()
    if not rows:
        return collector.result

    header = [c.strip().lower() for c in rows[0]]
    email_col = next((i for i, c in enumerate(header) if c in _EMAIL_COLS), None)
    if email_col is not None:
        name_col = next((i for i, c in enumerate(header) if c in _NAME_COLS), None)
        last_col = next((i for i, c in enumerate(header) if c in {"last name", "last_name", "lastname"}), None)
        first_last = (
            name_col is not None and last_col is not None
            and header[name_col] in {"first name", "first_name", "firstname"}
        )
        for row in rows[1:]:
            email = row[email_col] if email_col < len(row) else ""
            name = row[name_col] if name_col is not None and name_col < len(row) else ""
            if first_last and last_col < len(row):
                name = f"{name} {row[last_col]}"
            if not email.strip():
                if any(c.strip() for c in row):
                    collector.result.total += 1
                    collector.result.invalid.append(",".join(row)[:200])
                continue
            collector.add(email, name)
        return collector.result

    # No recognised header: find the cell containing '@' in each row; another cell is the name.
    for row in rows:
        cells = [c.strip() for c in row]
        email = next((c for c in cells if "@" in c), None)
        if email is None:
            if any(c.lower() in _HEADER_WORDS for c in cells):
                continue
            collector.result.total += 1
            collector.result.invalid.append(",".join(cells)[:200])
            continue
        name = next((c for c in cells if c and c != email and "@" not in c), "")
        collector.add(email, name)
    return collector.result


def to_text(recipients: list[Recipient]) -> str:
    """Render recipients one per line (``Name <email>`` when a name is known)."""
    return "\n".join(f"{r.name} <{r.email}>" if r.name else r.email for r in recipients)


def make_batches(recipients: list, batch_size: int) -> list[list]:
    if batch_size < 1:
        raise ValueError("Batch size must be at least 1")
    return [recipients[i:i + batch_size] for i in range(0, len(recipients), batch_size)]
