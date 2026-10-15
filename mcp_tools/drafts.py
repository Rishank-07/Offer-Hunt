"""Local email drafts tool for OFFERHUNT (Milestone M1).

Creates local .eml and .txt draft files with state DRAFTED.
Safety invariant: NEVER sends emails. Disallowed recipient checks prevent
accidental drafting to fabricated, placeholder, or dummy email addresses.
"""

from __future__ import annotations

import email.policy
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Union


DEFAULT_DRAFTS_DIR = Path("artifacts/drafts").resolve()

# Strict RFC-compliant email regex
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$"
)

# Prohibited placeholder / dummy / fabricated domains and prefixes
DISALLOWED_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "placeholder.com",
    "fake.com",
    "dummy.com",
    "invalid.com",
    "sample.com",
    "domain.com",
    "company.com",
    "email.com",
}

DISALLOWED_PREFIXES = {
    "none",
    "null",
    "undefined",
    "fake",
    "test",
    "dummy",
    "placeholder",
    "unknown",
}


class DraftSecurityError(ValueError):
    """Raised when an invalid, unsafe, or fabricated recipient email is provided."""
    pass


@dataclass
class DraftRecord:
    """Record representing a saved local email draft."""
    draft_id: str
    eml_path: str
    txt_path: str
    recipient: str
    subject: str
    state: str  # Always "DRAFTED"
    created_at_utc: str


def validate_recipient_email(recipient: str) -> str:
    """Validate that recipient email is well-formed and not fabricated or placeholder.

    Raises:
        DraftSecurityError if recipient is malformed, dummy, or fabricated.
    """
    clean_recipient = recipient.strip().lower()

    # 1. Syntax check
    if not EMAIL_REGEX.match(clean_recipient):
        raise DraftSecurityError(
            f"Invalid recipient email syntax: '{recipient}'."
        )

    # 2. Extract local part and domain
    try:
        local_part, domain = clean_recipient.split("@", 1)
    except ValueError:
        raise DraftSecurityError(f"Malformed email address: '{recipient}'.")

    # 3. Reject known placeholder or fabricated domains
    if domain in DISALLOWED_DOMAINS or domain.endswith(".example"):
        raise DraftSecurityError(
            f"Fabricated or placeholder domain rejected: '{domain}' in '{recipient}'."
        )

    # 4. Reject placeholder prefixes
    if local_part in DISALLOWED_PREFIXES:
        raise DraftSecurityError(
            f"Placeholder recipient username rejected: '{local_part}' in '{recipient}'."
        )

    # 5. Check domain has valid dot structure and at least 2 char TLD
    domain_parts = domain.split(".")
    if len(domain_parts) < 2 or len(domain_parts[-1]) < 2:
        raise DraftSecurityError(
            f"Incomplete or invalid top-level domain in recipient: '{recipient}'."
        )

    return clean_recipient


def create_local_draft(
    recipient: str,
    subject: str,
    body: str,
    drafts_dir: Union[str, Path] = DEFAULT_DRAFTS_DIR,
    sender: str = "noreply@offerhunt.local",
) -> DraftRecord:
    """Create a local .eml and plain text draft.

    State is guaranteed to be 'DRAFTED'. Never sends or opens any network sockets.
    """
    valid_recipient = validate_recipient_email(recipient)

    target_dir = Path(drafts_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat()

    # 1. Assemble EmailMessage
    msg = EmailMessage(policy=email.policy.default)
    msg["To"] = valid_recipient
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc)
    msg["X-Draft-State"] = "DRAFTED"
    msg["X-OfferHunt-Draft-ID"] = draft_id
    msg["X-OfferHunt-Never-Send"] = "true"
    msg.set_content(body)

    # 2. Save .eml file
    eml_file = target_dir / f"{draft_id}.eml"
    with open(eml_file, "wb") as f:
        f.write(msg.as_bytes())

    # 3. Save .txt file for quick inspection
    txt_file = target_dir / f"{draft_id}.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(f"STATE: DRAFTED\n")
        f.write(f"DRAFT_ID: {draft_id}\n")
        f.write(f"CREATED_AT_UTC: {created_at}\n")
        f.write(f"TO: {valid_recipient}\n")
        f.write(f"FROM: {sender}\n")
        f.write(f"SUBJECT: {subject}\n")
        f.write("-" * 50 + "\n")
        f.write(body + "\n")

    return DraftRecord(
        draft_id=draft_id,
        eml_path=str(eml_file),
        txt_path=str(txt_file),
        recipient=valid_recipient,
        subject=subject,
        state="DRAFTED",
        created_at_utc=created_at,
    )
