import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from crewai.tools import BaseTool
from pydantic import Field


class EmailDraftTool(BaseTool):
    """Draft and optionally send professional emails to rights holders or partners."""

    name: str = "email_draft"
    description: str = (
        "Draft a professional email. Input JSON with keys: "
        "'to' (recipient email), 'subject', 'body', 'send' (bool, default false). "
        "If send=true and SMTP is configured, sends the email."
    )

    def _run(self, to: str, subject: str, body: str, send: bool = False) -> str:
        draft = f"=== EMAIL DRAFT ===\nTo: {to}\nSubject: {subject}\n\n{body}\n==================="

        if not send:
            return f"Черновик готов (не отправлен):\n\n{draft}"

        host = os.getenv("EMAIL_SMTP_HOST")
        user = os.getenv("EMAIL_SMTP_USER")
        password = os.getenv("EMAIL_SMTP_PASS")
        port = int(os.getenv("EMAIL_SMTP_PORT", "587"))

        if not all([host, user, password]):
            return f"SMTP не настроен. Черновик:\n\n{draft}"

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

            return f"Письмо отправлено на {to}.\n\n{draft}"
        except Exception as e:
            return f"Ошибка отправки: {e}\n\nЧерновик:\n{draft}"
