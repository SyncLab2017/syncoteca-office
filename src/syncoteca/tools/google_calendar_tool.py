"""Google Calendar tool — creates events in Denis's calendar."""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

from crewai.tools import BaseTool
from pydantic import Field


class GoogleCalendarTool(BaseTool):
    """Create events in Google Calendar."""

    name: str = "create_calendar_event"
    description: str = (
        "Создать встречу в Google Календарь Дениса. "
        "Укажи: title (название), date (дата YYYY-MM-DD), time (время HH:MM, московское), "
        "duration_minutes (длительность, по умолчанию 60), "
        "description (описание, необязательно), attendees (список email, необязательно)."
    )

    def _run(
        self,
        title: str,
        date: str,
        time: str = "10:00",
        duration_minutes: int = 60,
        description: str = "",
        attendees: Optional[list] = None,
    ) -> str:
        try:
            service = self._get_service()
            if service is None:
                return self._mock_event(title, date, time, duration_minutes, description)

            start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            end_dt = start_dt + timedelta(minutes=duration_minutes)

            tz = "Europe/Moscow"
            event = {
                "summary": title,
                "description": description,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
            }

            if attendees:
                emails = attendees if isinstance(attendees, list) else [attendees]
                event["attendees"] = [{"email": e.strip()} for e in emails if e.strip()]

            result = service.events().insert(
                calendarId=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
                body=event,
                sendNotifications=True,
            ).execute()

            link = result.get("htmlLink", "—")
            event_id = result.get("id", "—")
            return (
                f"Встреча создана.\n"
                f"Название: {title}\n"
                f"Дата: {date} {time} МСК ({duration_minutes} мин)\n"
                f"Ссылка: {link}\n"
                f"ID: {event_id}"
            )

        except Exception as e:
            return f"Ошибка создания встречи: {e}"

    def _get_service(self):
        """Build Google Calendar service from env vars."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            token_json = os.getenv("GOOGLE_TOKEN_JSON")
            if not token_json:
                return None

            token_data = json.loads(token_json)
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
                scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/calendar"]),
            )

            if creds.expired and creds.refresh_token:
                creds.refresh(Request())

            return build("calendar", "v3", credentials=creds)

        except ImportError:
            return None
        except Exception:
            return None

    def _mock_event(
        self, title: str, date: str, time: str, duration_minutes: int, description: str
    ) -> str:
        """Fallback when GOOGLE_TOKEN_JSON is not set."""
        return (
            f"[ТЕСТ — Google Calendar не подключён]\n"
            f"Была бы создана встреча:\n"
            f"Название: {title}\n"
            f"Дата: {date} {time} МСК ({duration_minutes} мин)\n"
            f"Описание: {description or '—'}\n"
            f"Для подключения установи GOOGLE_TOKEN_JSON в .env"
        )
