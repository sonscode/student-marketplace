"""Reusable email delivery service using the Resend API.

Environment variables required:
    RESEND_API_KEY  — Resend API key (required for sending)
    RESEND_FROM     — Sender address, e.g. "Azison <noreply@azison.com>"
                      (defaults to "Azison <noreply@azison.com>" if not set)

Usage:
    from services.email import send_email
    send_email(to="user@example.com", subject="Hello", html="<p>Hi</p>")
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Azison <noreply@azison.com>"


def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> bool:
    """Send an email via the Resend API.

    Args:
        to:      Recipient email address.
        subject: Email subject line.
        html:    HTML body content.
        text:    Plain-text fallback body (auto-generated from html if omitted).

    Returns:
        True if the API call succeeded (2xx), False otherwise.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return False

    from_addr = os.environ.get("RESEND_FROM", DEFAULT_FROM)

    payload = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.ok:
            logger.info("Email sent to %s (subject=%s)", to, subject)
            return True
        else:
            logger.error(
                "Resend API error for %s: %d %s",
                to,
                resp.status_code,
                resp.text,
            )
            return False
    except requests.RequestException as exc:
        logger.error("Resend API request failed for %s: %s", to, exc)
        return False


def send_password_reset_email(to: str, reset_url: str) -> bool:
    """Send a password reset email with both HTML and plain-text versions."""
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Manrope,Helvetica,Arial,sans-serif;background:#f7f4ec;">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#f7f4ec;padding:24px 0;">
<tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.06);">
<tr><td style="padding:32px 28px 20px;">
<h1 style="margin:0 0 6px;font-size:1.3rem;color:#1b1d20;">Password Reset</h1>
<p style="margin:0 0 18px;color:#60656f;font-size:0.92rem;line-height:1.5;">
We received a request to reset your Azison account password.
Click the button below to set a new one. This link expires in 30 minutes.
</p>
<table cellpadding="0" cellspacing="0">
<tr><td style="border-radius:10px;background:#8b5cf6;padding:12px 24px;">
<a href="{reset_url}" style="color:#ffffff;text-decoration:none;font-weight:700;font-size:0.95rem;display:inline-block;">
Reset Password
</a>
</td></tr>
</table>
<p style="margin:18px 0 0;color:#60656f;font-size:0.82rem;line-height:1.5;">
If you didn't request this, you can safely ignore this email.
</p>
</td></tr>
<tr><td style="padding:16px 28px;background:#f7f4ec;font-size:0.78rem;color:#90959e;">
Azison &bull; Student Marketplace
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    text = (
        f"Password Reset\n\n"
        f"We received a request to reset your Azison account password.\n"
        f"Click the link below to set a new one. This link expires in 30 minutes.\n\n"
        f"{reset_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"Azison — Student Marketplace"
    )

    return send_email(to=to, subject="Reset your Azison password", html=html, text=text)
