import html as _html_lib
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from crewai.tools import BaseTool

# Update link URLs here if they change
EMAIL_SIGNATURE_TEXT = (
    "\n\n--\nBest regards,\n\n"
    "Денис Шарко | Denis Sharko\n"
    "Head of SYNC LAB | +7 919 760 7600\n"
    "Music Licensing, Supervision & Production\n\n"
    "Sync Lab | NeoSounds Ltd | Sound Scape | Twisted Jukebox | Сайт | Кинопоиск\n\n"
    "Оставить отзыв | feedback here\n"
    "Мы работаем в Контур.Диадок.\n\n"
    "---\n"
    "Содержимое этого электронного письма и все вложения являются КОНФИДЕНЦИАЛЬНЫМИ "
    "и могут быть защищены законом о защите персональных данных. Если вы не являетесь "
    "адресатом, вам запрещается сохранять, копировать или использовать это электронное "
    "письмо или вложения к нему в каких-либо целях, а также разглашать его содержание "
    "полностью или частично.\n\n"
    "The contents of this e-mail and any attachments are CONFIDENTIAL and may also be "
    "legally privileged. If you are not the intended recipient, you must not retain, copy "
    "or use this e-mail or any attachment for any purpose, nor disclose all or any part "
    "of the contents to any other person."
)

EMAIL_SIGNATURE_HTML = """
<br><br>
<table cellpadding="0" cellspacing="0" border="0" style="font-family:'Montserrat',Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:#333333;">
  <tr><td style="padding-bottom:4px;color:#2bb4d7;">Best regards,&nbsp;</td></tr>
  <tr><td style="padding-bottom:1px;color:#2bb4d7;">Денис Шарко | Denis Sharko</td></tr>
  <tr><td style="padding-bottom:1px;color:#2bb4d7;">Head of SYNC LAB | +7 919 760 7600</td></tr>
  <tr><td style="padding-bottom:14px;color:#2eb6d7;">Music Licensing, Supervision &amp; Production</td></tr>
  <tr><td style="padding-bottom:14px;font-size:12px;font-weight:bold;">
    <a href="https://synclab.sourceaudio.com/#!artists" style="color:#0068da;font-weight:bold;text-decoration:none;">Sync Lab</a><font color="#0068da"> | </font><a href="https://www.neosounds.com/" style="color:#008f00;font-weight:bold;text-decoration:none;">NeoSounds Ltd</a><font color="#0068da"> | </font><a href="https://soundscape.io/" style="color:#942192;font-weight:bold;text-decoration:none;">Sound Scape</a><font color="#0068da"> | </font><a href="https://www.twistedjukebox.com" style="color:#ff40ff;font-weight:bold;text-decoration:none;">Twisted Jukebox</a><font color="#0068da"> |</font><br><a href="https://synclab.pro/" style="color:#000000;font-weight:bold;text-decoration:none;">Сайт</a><font color="#0068da"> | </font><a href="https://www.kinopoisk.ru/name/6269377/" style="color:#e61e1c;font-weight:bold;text-decoration:none;">Кинопоиск</a>
  </td></tr>
  <tr><td style="padding-bottom:2px;">
    <font color="#2bb4d7">Оставить&nbsp;</font><a href="https://goo.gl/forms/iYHf5wKi5CBYHWtl1" style="color:#2bb4d7;text-decoration:underline;">отзыв</a><font color="#2bb4d7"> | feedback</font>&nbsp;<a href="x-webdoc://6A875865-DF45-48B6-8AC1-CC74F2764D5F" style="color:#0068da;text-decoration:underline;">here</a>
  </td></tr>
  <tr><td style="padding-bottom:16px;color:#2eb6d7;">
    Мы работаем в <a href="https://kontur.ru/diadoc" target="_blank" style="color:#0068da;text-decoration:underline;">Контур.Диадок</a>.
  </td></tr>
  <tr><td style="font-size:11px;color:#5e5e5e;border-top:1px solid #eeeeee;padding-top:10px;max-width:600px;">
    Содержимое этого электронного письма и все вложения являются КОНФИДЕНЦИАЛЬНЫМИ и могут быть защищены законом о защите персональных данных. Если вы не являетесь адресатом, вам запрещается сохранять, копировать или использовать это электронное письмо или вложения к нему в каких-либо целях, а также разглашать его содержание полностью или частично.<br><br>
    The contents of this e-mail and any attachments are CONFIDENTIAL and may also be legally privileged. If you are not the intended recipient, you must not retain, copy or use this e-mail or any attachment for any purpose, nor disclose all or any part of the contents to any other person.
  </td></tr>
</table>"""


def _text_to_html(text: str) -> str:
    escaped = _html_lib.escape(text)
    return "<p>" + escaped.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


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
        html_body = _text_to_html(body) + EMAIL_SIGNATURE_HTML
        text_body = body + EMAIL_SIGNATURE_TEXT
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject, "html": html_body, "text": text_body},
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
            msg = MIMEMultipart("alternative")
            msg["From"] = user
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body + EMAIL_SIGNATURE_TEXT, "plain", "utf-8"))
            msg.attach(MIMEText(_text_to_html(body) + EMAIL_SIGNATURE_HTML, "html", "utf-8"))

            with smtplib.SMTP(host, port) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)

            return f"✅ Письмо отправлено на {to}.\n\n{draft}"
        except Exception as e:
            return f"Ошибка SMTP: {e}\n\nЧерновик:\n{draft}"
