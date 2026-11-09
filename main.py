"""
Lead forwarding API (stateless, no database) for real-estate agency listing pages.

Flow
----
1. The listing page POSTs a lead to /api/v1/leads together with a signed routing token.
2. The token (JWT, HS256, signed with SECRET_KEY) carries the routing info:
       agency_email  -> where the alert e-mail is sent
       agency_slug   -> agency identifier (used in logs / sheet rows)
       property_ref  -> property reference
       sheet_id      -> (optional) Google Sheet ID; falls back to GOOGLE_SHEET_ID
       exp           -> (optional) expiry, verified automatically when present
3. The agent is alerted by SMTP immediately; the Google Sheets row is appended in the background.

RGPD
----
Nothing is written to disk or to any database. Personal data only transits through memory,
SMTP and the agency's own Google Sheet. Logs never contain name / phone / e-mail.

Environment variables
---------------------
SECRET_KEY                    required, >= 32 chars
SMTP_HOST, SMTP_PORT          port 465 -> implicit SSL, anything else -> STARTTLS (default 587)
SMTP_USER, SMTP_PASSWORD      optional if the relay needs no auth
SMTP_FROM                     sender address (defaults to SMTP_USER)
GOOGLE_SERVICE_ACCOUNT_FILE   defaults to ./service_account.json (Sheets is skipped if absent)
GOOGLE_SHEET_ID               default sheet when the token has no sheet_id claim

Install:  pip install fastapi "uvicorn[standard]" "pydantic[email]" PyJWT gspread
Run:      uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    field_validator,
)

try:  # Google Sheets is optional: the API still works without gspread installed.
    import gspread
except ImportError:  # pragma: no cover
    gspread = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("leads")

PARIS = ZoneInfo("Europe/Paris")
JWT_ALGORITHM = "HS256"
SMTP_TIMEOUT_SECONDS = 10


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    secret_key: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    service_account_path: Path
    default_sheet_id: str


def load_settings() -> Settings:
    secret = os.getenv("SECRET_KEY", "")
    if len(secret) < 32:
        # Fail fast: a weak or missing key would make every routing token forgeable.
        raise RuntimeError("SECRET_KEY must be set and be at least 32 characters long.")
    smtp_user = os.getenv("SMTP_USER", "")
    return Settings(
        secret_key=secret,
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=smtp_user,
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", smtp_user),
        service_account_path=Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")),
        default_sheet_id=os.getenv("GOOGLE_SHEET_ID", ""),
    )


settings = load_settings()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class LeadPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=10, max_length=4096)
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    email: EmailStr
    financing_status: str = Field(min_length=2, max_length=120)

    @field_validator("full_name", "financing_status")
    @classmethod
    def collapse_whitespace(cls, value: str) -> str:
        # Removes line breaks / tabs (blocks header injection in the e-mail subject).
        return " ".join(value.split())

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not re.fullmatch(r"\+?[0-9\s.\-()]+", value):
            raise ValueError("Invalid phone number")
        if not 9 <= len(re.sub(r"\D", "", value)) <= 15:
            raise ValueError("Invalid phone number")
        return value


class RoutingClaims(BaseModel):
    agency_email: EmailStr
    agency_slug: str = Field(min_length=1, max_length=80)
    property_ref: str = Field(min_length=1, max_length=80)
    sheet_id: str | None = Field(default=None, max_length=128)


# --------------------------------------------------------------------------- #
# JWT utilities
# --------------------------------------------------------------------------- #
class TokenError(Exception):
    """Raised when a routing token is invalid, expired or incomplete."""


def decode_routing_token(token: str) -> RoutingClaims:
    """Verify the signature (and `exp` if present) and extract the routing claims."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid token") from exc

    try:
        return RoutingClaims.model_validate(payload)
    except ValidationError as exc:
        raise TokenError("Token claims incomplete") from exc


def issue_routing_token(
    agency_email: str,
    agency_slug: str,
    property_ref: str,
    sheet_id: str | None = None,
    ttl_days: int | None = None,
) -> str:
    """Helper to generate the `encrypted_token` injected into each listing page."""
    claims: dict = {
        "agency_email": agency_email,
        "agency_slug": agency_slug,
        "property_ref": property_ref,
    }
    if sheet_id:
        claims["sheet_id"] = sheet_id
    if ttl_days:
        claims["exp"] = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    return jwt.encode(claims, settings.secret_key, algorithm=JWT_ALGORITHM)


# --------------------------------------------------------------------------- #
# E-mail (SMTP)
# --------------------------------------------------------------------------- #
# status -> (text colour, background colour, priority label)
FINANCING_BADGES: dict[str, tuple[str, str, str]] = {
    "Accord de principe bancaire obtenu": ("#065F46", "#D1FAE5", "Prioritaire"),
    "Achat au comptant": ("#065F46", "#D1FAE5", "Prioritaire"),
    "Demande de prêt en cours": ("#92400E", "#FEF3C7", "À qualifier"),
    "Doit vendre un bien d'abord": ("#92400E", "#FEF3C7", "À qualifier"),
}
DEFAULT_BADGE = ("#334155", "#E2E8F0", "À qualifier")


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _detail_row(label: str, value_html: str) -> str:
    return (
        '<tr>'
        f'<td style="padding:10px 0;width:130px;color:#64748B;font-size:13px;'
        f'border-bottom:1px solid #E2E8F0;">{label}</td>'
        f'<td style="padding:10px 0;color:#0F172A;font-size:15px;font-weight:600;'
        f'border-bottom:1px solid #E2E8F0;">{value_html}</td>'
        '</tr>'
    )


def _render_html(lead: LeadPayload, claims: RoutingClaims, received_at: str) -> str:
    e = html.escape
    fg, bg, priority = FINANCING_BADGES.get(lead.financing_status, DEFAULT_BADGE)
    tel = re.sub(r"[^\d+]", "", lead.phone)

    rows = "".join(
        [
            _detail_row("Nom", e(lead.full_name)),
            _detail_row(
                "Téléphone",
                f'<a href="tel:{e(tel)}" style="color:#0F172A;text-decoration:none;">{e(lead.phone)}</a>',
            ),
            _detail_row(
                "Email",
                f'<a href="mailto:{e(lead.email)}" style="color:#0F172A;">{e(lead.email)}</a>',
            ),
            _detail_row("Bien", e(claims.property_ref)),
            _detail_row("Reçu le", e(received_at)),
        ]
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<body style="margin:0;padding:0;background:#F1F5F9;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:12px;overflow:hidden;">
        <tr>
          <td style="background:#0F172A;padding:24px 28px;border-bottom:3px solid #D4AF37;">
            <p style="margin:0;color:#D4AF37;font-size:12px;letter-spacing:2px;text-transform:uppercase;">Nouvelle demande de visite</p>
            <h1 style="margin:6px 0 0;color:#FFFFFF;font-size:22px;font-weight:600;">Réf. {e(claims.property_ref)}</h1>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px 8px;">
            <p style="margin:0 0 8px;color:#64748B;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Situation financière</p>
            <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;background:{bg};border-radius:10px;">
              <tr><td style="padding:14px 16px;">
                <span style="display:inline-block;background:{fg};color:#FFFFFF;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:3px 8px;border-radius:999px;">{e(priority)}</span>
                <p style="margin:8px 0 0;color:{fg};font-size:17px;font-weight:700;">{e(lead.financing_status)}</p>
              </td></tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 28px 24px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px 28px;">
            <a href="tel:{e(tel)}" style="display:inline-block;background:#D4AF37;color:#0F172A;font-weight:700;font-size:15px;text-decoration:none;padding:13px 22px;border-radius:10px;">Appeler maintenant</a>
          </td>
        </tr>
        <tr>
          <td style="background:#F8FAFC;padding:16px 28px;color:#94A3B8;font-size:11px;line-height:1.5;">
            Agence : {e(claims.agency_slug)} · Ce message contient des données personnelles : ne le transférez pas et supprimez-le une fois la demande traitée (RGPD).
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _render_text(lead: LeadPayload, claims: RoutingClaims, received_at: str) -> str:
    return (
        f"Nouvelle demande de visite - Réf. {claims.property_ref}\n\n"
        f"Situation financière : {lead.financing_status}\n"
        f"Nom       : {lead.full_name}\n"
        f"Téléphone : {lead.phone}\n"
        f"Email     : {lead.email}\n"
        f"Reçu le   : {received_at}\n"
    )


def build_alert_email(lead: LeadPayload, claims: RoutingClaims) -> EmailMessage:
    received_at = datetime.now(PARIS).strftime("%d/%m/%Y à %H:%M")
    msg = EmailMessage()
    msg["Subject"] = f"Nouveau lead · Réf. {claims.property_ref} · {lead.full_name}"
    
    sender_name = "ImmoLead Notifications"
    sender_email = settings.smtp_from
    msg["From"] = Header(f"{sender_name} <{sender_email}>", "utf-8")
    
    msg["To"] = str(claims.agency_email)
    msg["Reply-To"] = str(lead.email)
    msg.set_content(_render_text(lead, claims, received_at))
    msg.add_alternative(_render_html(lead, claims, received_at), subtype="html")
    return msg


def _send_email_sync(msg: EmailMessage) -> None:
    """Blocking SMTP send; always called through a worker thread."""
    context = ssl.create_default_context()

    def deliver(client: smtplib.SMTP) -> None:
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(msg)

    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        ) as client:
            deliver(client)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            deliver(client)


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #
def sheets_available() -> bool:
    return gspread is not None and settings.service_account_path.is_file()


@lru_cache(maxsize=1)
def _gspread_client():
    # In-memory only: credentials are read from the service account file, nothing is persisted.
    return gspread.service_account(filename=str(settings.service_account_path))


def append_lead_to_sheet(lead: LeadPayload, claims: RoutingClaims) -> None:
    """Runs as a background task (threadpool), so it never delays the HTTP response."""
    sheet_id = claims.sheet_id or settings.default_sheet_id
    if not sheet_id:
        logger.info("sheet skipped (no sheet id) slug=%s", claims.agency_slug)
        return

    row = [
        datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S"),
        claims.agency_slug,
        claims.property_ref,
        lead.full_name,
        lead.phone,
        str(lead.email),
        lead.financing_status,
    ]
    try:
        worksheet = _gspread_client().open_by_key(sheet_id).sheet1
        # RAW: values are stored as plain text, which neutralises spreadsheet formula injection.
        worksheet.append_row(row, value_input_option="RAW")
        logger.info("sheet row appended slug=%s ref=%s", claims.agency_slug, claims.property_ref)
    except Exception as exc:  # noqa: BLE001 - background task must never raise
        logger.error(
            "sheet append failed slug=%s ref=%s err=%s",
            claims.agency_slug,
            claims.property_ref,
            type(exc).__name__,
        )


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="Lead Forwarding API", version="1.0.0")

# Agencies use custom domains, so every origin is allowed. Credentials stay disabled
# (required with a wildcard origin, and the API relies on the signed token, not cookies).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    max_age=86400,
)


@app.api_route("/health", methods=["GET", "HEAD"], tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/leads", tags=["leads"])
async def create_lead(lead: LeadPayload, background_tasks: BackgroundTasks) -> dict[str, str]:
    # 1. Verify the routing token and extract destination info (no database lookup).
    try:
        claims = decode_routing_token(lead.token)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if not smtp_configured():
        logger.error("SMTP is not configured")
        raise HTTPException(status_code=503, detail="Email service unavailable")

    # 2. Alert the agent right away. If this fails the client gets an error and can retry;
    #    the sheet row is only queued after a successful send, so retries create no duplicates.
    message = build_alert_email(lead, claims)
    try:
        await asyncio.to_thread(_send_email_sync, message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "email delivery failed slug=%s ref=%s err=%s",
            claims.agency_slug,
            claims.property_ref,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Unable to deliver the request") from exc

    # 3. Append to Google Sheets in the background, only if credentials are present.
    if sheets_available():
        background_tasks.add_task(append_lead_to_sheet, lead, claims)

    logger.info("lead forwarded slug=%s ref=%s", claims.agency_slug, claims.property_ref)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))