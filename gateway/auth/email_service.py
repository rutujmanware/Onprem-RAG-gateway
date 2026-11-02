import asyncio
import json
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _load_env_defaults() -> None:
    """Zero-dependency loader to read Onprem-RAG-gateway/.env into os.environ."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ and v:
                        os.environ[k] = v
        except Exception:
            pass


_load_env_defaults()


class EmailService:
    """
    Manages institutional email notifications and OTP dispatch for IISc users.
    Supports standard SMTP delivery (TLS/STARTTLS/plain) and audit outbox tracking.
    Defaulted to Option B (smtp.gmail.com:587 TLS).
    """

    def __init__(self, redis_client: Any = None):
        self.redis = redis_client
        self.outbox: List[Dict[str, Any]] = []
        self.reload_config()

    def reload_config(self) -> None:
        """Reload SMTP settings from environment variables."""
        _load_env_defaults()
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_from = os.getenv(
            "SMTP_FROM", self.smtp_user or "noreply-rag@iisc.ac.in"
        )
        self.smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in (
            "true",
            "1",
            "yes",
        )

    def set_redis(self, redis_client: Any) -> None:
        self.redis = redis_client

    def format_otp_email(
        self, to_email: str, otp: str, purpose: str = "register"
    ) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"IISc Thesis RAG Gateway <{self.smtp_from}>"
        msg["To"] = to_email
        msg["Reply-To"] = self.smtp_from
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-Auto-Response-Suppress"] = "All"

        if purpose == "reset":
            msg["Subject"] = f"[IISc RAG Gateway] Password Reset Verification Code: {otp}"
            action_desc = "reset your campus account password"
        else:
            msg["Subject"] = f"[IISc RAG Gateway] Account Registration Verification Code: {otp}"
            action_desc = "complete your institutional account registration"

        plain_text = f"""Indian Institute of Science (IISc), Bangalore
Institutional Thesis RAG Gateway Verification

Dear IISc Member,

Your one-time verification code (OTP) to {action_desc} is:

    ======================
           {otp}
    ======================

This code is valid for 5 minutes (300 seconds).
For security reasons, never share this verification code with anyone.

If you did not request this verification code, please ignore this email.

---
IISc Thesis RAG Gateway • Supercomputer Education and Research Centre (SERC)
Zero-Egress On-Premises Institutional Retrieval Service
"""

        html_text = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
    .card {{ max-width: 520px; margin: 0 auto; background: #1e293b; border-radius: 12px; border: 1px solid #334155; padding: 32px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    .header {{ text-align: center; border-bottom: 1px solid #334155; padding-bottom: 20px; margin-bottom: 24px; }}
    .brand {{ font-size: 18px; font-weight: bold; color: #38bdf8; }}
    .subtitle {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; margin-top: 4px; }}
    .otp-box {{ background: #0f172a; border: 2px dashed #38bdf8; border-radius: 8px; text-align: center; padding: 18px; margin: 24px 0; }}
    .otp-code {{ font-size: 32px; font-weight: 800; letter-spacing: 6px; color: #38bdf8; font-family: monospace; }}
    .info {{ font-size: 14px; line-height: 1.6; color: #cbd5e1; }}
    .warning {{ font-size: 12px; color: #f59e0b; margin-top: 16px; }}
    .footer {{ font-size: 11px; color: #64748b; text-align: center; border-top: 1px solid #334155; margin-top: 24px; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="brand">🏛️ Indian Institute of Science</div>
      <div class="subtitle">Institutional Thesis RAG Gateway</div>
    </div>
    <div class="info">
      <p>Dear IISc Member,</p>
      <p>Your one-time verification code (OTP) to <strong>{action_desc}</strong> is:</p>
    </div>
    <div class="otp-box">
      <div class="otp-code">{otp}</div>
    </div>
    <div class="info">
      <p>This verification code is strictly confidential and expires in <strong>5 minutes</strong>.</p>
    </div>
    <div class="warning">
      ⚠️ If you did not request this verification code, please disregard this notice.
    </div>
    <div class="footer">
      On-Premises Zero-Egress Gateway • IISc Intranet
    </div>
  </div>
</body>
</html>
"""

        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html_text, "html"))
        return msg

    def send_otp_sync(
        self, to_email: str, otp: str, purpose: str = "register"
    ) -> Dict[str, Any]:
        """Synchronously attempts SMTP delivery and logs to outbox."""
        self.reload_config()
        clean_email = to_email.strip().lower()
        msg = self.format_otp_email(clean_email, otp, purpose)
        status = "queued"
        error_msg = None

        logger.info(
            "[OTP AUDIT] Verification code for %s (%s): %s",
            clean_email,
            purpose,
            otp,
        )

        try:
            # Check for credentials if targeting remote SMTP server
            if self.smtp_host not in ("localhost", "127.0.0.1") and (
                not self.smtp_user or not self.smtp_password
            ):
                raise smtplib.SMTPException(
                    f"SMTP credentials missing in .env for {self.smtp_host}. Please set SMTP_USER and SMTP_PASSWORD."
                )

            if self.smtp_port == 465:
                # Implicit SSL (Port 465)
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10)
            else:
                # Explicit STARTTLS (Port 587 or Port 25)
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10)
                if self.smtp_use_tls or self.smtp_port == 587:
                    server.starttls()

            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)

            server.send_message(msg)
            server.quit()
            status = "sent"
            logger.info(
                "Successfully dispatched OTP email to %s via SMTP %s:%s",
                clean_email,
                self.smtp_host,
                self.smtp_port,
            )
        except Exception as e:
            status = "failed_smtp"
            error_msg = str(e)
            logger.warning(
                "SMTP dispatch for %s (host %s:%s) encountered: %s. Notification preserved in outbox.",
                clean_email,
                self.smtp_host,
                self.smtp_port,
                e,
            )

        record = {
            "to": clean_email,
            "purpose": purpose,
            "status": status,
            "error": error_msg,
            "timestamp": int(time.time()),
            "subject": msg["Subject"],
        }
        self.outbox.append(record)
        return record

    async def send_otp(
        self, to_email: str, otp: str, purpose: str = "register"
    ) -> Dict[str, Any]:
        """Asynchronously dispatches the OTP email in a background thread."""
        record = await asyncio.to_thread(self.send_otp_sync, to_email, otp, purpose)
        if self.redis is not None and hasattr(self.redis, "rpush"):
            try:
                await self.redis.rpush(
                    f"mail:outbox:{to_email.strip().lower()}", json.dumps(record)
                )
            except Exception:
                pass
        return record


email_service = EmailService()
