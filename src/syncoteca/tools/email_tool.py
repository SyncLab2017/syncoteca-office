import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
from crewai.tools import BaseTool


class EmailDraftTool(BaseTool):
    """Draft and optionally send professional emails to rights holders or partners."""

    name: str = "email_draft"
    description: str = (
        "Draft a professional email. Input JSON with keys: "
        "'to' (recipient email), 'subject', 'body', 'send' (bool, default false). "
        "If send=true, sends via Resend API or SMTP."
    )

    def _run(self, to: str, subject: str, body: str, send: bool = False) -> str:
        sender = os.getenv("EMAIL_SMTP_USER", "denis@synclab.pro")
        draft = f"=== EMAIL DRAFT ===\nTo: {to}\nSubject: {subject}\n\n{body}\n==================="

        if not send:
            return f"Черновик готов (не отправлен):\n\n{draft}"

        resend_key = os.getenv("RESEND_API_KEY", "")
        if resend_key:
            return self._send_resend(resend_key, sender, to, subject, body, draft)
        return self._send_smtp(sender, to, subject, body, draft)

    def _send_resend(self, api_key: str, sender: str, to: str, subject: str, body: str, draft: str) -> str:
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject, "text": body},
                timeout=15,
            )
            resp.raise_for_status()
            return f"✅ Письмо отправлено на {to}.\n\n{draft}"
        except Exception as e:
            return f"Ошибка Resend: {e}\n\nЧерновик:\n{draft}"

    def _send_smtp(self, sender: str, to: str, subject: str, body: str, draft: str) -> str:
        host = os.getenv("EMAIL_SMTP_HOST")
        user = os.getenv("EMAIL_SMTP_USER")
        password = os.getenv("EMAIL_SMTP_PASS")
        port = int(os.getenv("EMAIL_SMTP_PORT", "587"))

        if not all([host, user, password]):
            return f"Ни RESEND_API_KEY, ни SMTP не настроены. Черновик:\n\n{draft}"

        try:
            msg = MIMEMultipart()
            msg["From"] = user
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(host, port) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)

            return f"✅ Письмо отправлено на {to}.\n\n{draft}"
        except Exception as e:
            return f"Ошибка SMTP: {e}\n\nЧерновик:\n{draft}"
