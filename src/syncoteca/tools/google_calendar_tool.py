from __future__ import annotations

import json
import os
from typing import Any

from crewai.tools import BaseTool
from pydantic import Field


class GoogleCalendarTool(BaseTool):
    name: str = "GoogleCalendarTool"
    description: str = (
        "Creates events in Google Calendar. "
        "Args: title, date (YYYY-MM-DD), time (HH:MM), duration_minutes (int), "
        "description (str), attendees (list of email strings)."
    )

    def _get_service(self) -> Any:
        token_json = os.environ.get("GOOGLE_TOKEN_JSON")
        if not token_json:
            return None
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds_data = json.loads(token_json)
        creds = Credentials(
            token=creds_data.get("token") or creds_data.get("access_token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
        )
        return build("calendar", "v3", credentials=creds)

    def _run(
        self,
        title: str,
        date: str,
        time: str = "10:00",
        duration_minutes: int = 60,
        description: str = "",
        attendees: list[str] | None = None,
    ) -> str:
        service = self._get_service()
        if service is None:
            return (
                f"[MOCK] Встреча «{title}» создана на {date} в {time} "
                f"({duration_minutes} мин). GOOGLE_TOKEN_JSON не задан."
            )

        from datetime import datetime, timedelta

        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event: dict[str, Any] = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Moscow"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Moscow"},
        }
        if attendees:
            event["attendees"] = [{"email": e} for e in attendees]

        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        link = created.get("htmlLink", "")
        return f"Встреча «{title}» создана на {date} в {time}. Ссылка: {link}"
