from datetime import date, datetime, timedelta, timezone
import re
from types import TracebackType

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from planning_aux.secrets import get_secret


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _load_credentials() -> Credentials:
    fields = ("type", "private_key_id", "private_key", "client_email", "client_id", "token_uri")
    info = {}
    for field in fields:
        value = get_secret(f"AuxiliaireDeViePlanning/{field}")
        info[field] = value if field == "private_key" else value.removesuffix("\n")
    if info["type"] != "service_account":
        raise ValueError("Les identifiants Google doivent être ceux d'un compte de service.")
    if info["token_uri"] != "https://oauth2.googleapis.com/token":
        raise ValueError("L'adresse du service de jetons Google est inattendue.")
    return Credentials.from_service_account_info(info, scopes=SCOPES)


class CalendarClient:
    """Client Google Calendar utilisant le compte de service stocké dans pass."""

    def __init__(self, calendar_id: str) -> None:
        if not calendar_id.strip():
            raise ValueError("L'identifiant de l'agenda est obligatoire.")
        self._calendar_id = calendar_id
        self._service = build(
            "calendar", "v3", credentials=_load_credentials(), cache_discovery=False,
        )

    def __enter__(self) -> "CalendarClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._service.close()

    def create_all_day_event(self, summary: str, day: date) -> dict[str, object]:
        """Crée un événement sur une journée (fin exclusive selon Google Calendar)."""
        if not summary.strip():
            raise ValueError("Le titre de l'événement est obligatoire.")
        payload = self._service.events().insert(
            calendarId=self._calendar_id,
            body={
                "summary": summary,
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
            },
        ).execute()
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise ValueError("La réponse Google ne contient pas l'identifiant de l'événement créé.")
        return payload

    def get_event(self, event_id: str) -> dict[str, object]:
        """Relit un événement depuis l'agenda cible."""
        if not event_id:
            raise ValueError("L'identifiant de l'événement est obligatoire.")
        payload = self._service.events().get(
            calendarId=self._calendar_id, eventId=event_id,
        ).execute()
        if not isinstance(payload, dict) or payload.get("id") != event_id:
            raise ValueError("La réponse Google ne correspond pas à l'événement demandé.")
        return payload

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, object]]:
        """Liste toutes les occurrences chevauchant l'intervalle, avec pagination."""
        events = []
        token = None
        seen_tokens = set()
        while True:
            response = self._service.events().list(
                calendarId=self._calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                showDeleted=False,
                maxResults=2500,
                pageToken=token,
            ).execute()
            if not isinstance(response, dict):
                raise ValueError("La liste des événements Google est invalide.")
            items = response.get("items")
            if items is None and "items" not in response and response.get("kind") == "calendar#events":
                items = []
            if not isinstance(items, list):
                raise ValueError("La liste des événements Google est invalide.")
            if not all(isinstance(item, dict) for item in items):
                raise ValueError("La liste des événements Google contient un événement invalide.")
            events.extend(items)
            token = response.get("nextPageToken")
            if token is None:
                return events
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise ValueError("Le jeton de pagination Google est invalide ou dupliqué.")
            seen_tokens.add(token)

    def create_intervention_event(
        self,
        source_id: str,
        summary: str,
        start: datetime,
        end: datetime,
        *,
        description: str = "",
    ) -> dict[str, object]:
        """Crée un événement avec un identifiant Google indépendant de l'ID Ximi."""
        if re.fullmatch(r"[0-9]+", source_id) is None:
            raise ValueError("L'identifiant de prestation doit être numérique.")
        if (
            not summary.strip()
            or start.utcoffset() is None
            or end.utcoffset() is None
            or end.astimezone(timezone.utc) <= start.astimezone(timezone.utc)
        ):
            raise ValueError("Le titre et les horaires de l'événement sont invalides.")
        payload = self._service.events().insert(
            calendarId=self._calendar_id,
            body={
                "summary": summary,
                "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Paris"},
                "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Paris"},
                "extendedProperties": {"private": {"planningAuxSourceId": source_id}},
            },
        ).execute()
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
            raise ValueError("La réponse Google ne contient pas l'identifiant de l'événement créé.")
        return payload

    def delete_event(self, event_id: str, etag: str) -> None:
        """Supprime l'événement seulement s'il n'a pas changé depuis sa lecture."""
        if not event_id or not etag:
            raise ValueError("L'identifiant et la version de l'événement sont obligatoires.")
        request = self._service.events().delete(calendarId=self._calendar_id, eventId=event_id)
        request.headers["If-Match"] = etag
        request.execute()

    def upsert_intervention_event(
        self,
        source_id: str,
        summary: str,
        start: datetime,
        end: datetime,
        *,
        description: str = "",
    ) -> tuple[str, dict[str, object]]:
        """Crée ou met à jour l'événement identifié par l'ID Ximi."""
        if re.fullmatch(r"[0-9]+", source_id) is None:
            raise ValueError("L'identifiant de prestation doit être numérique.")
        if not summary.strip():
            raise ValueError("Le titre de l'événement est obligatoire.")
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Le début de l'événement doit avoir un fuseau horaire.")
        if end.tzinfo is None or end.utcoffset() is None or end <= start:
            raise ValueError("La fin doit avoir un fuseau horaire et suivre le début.")

        event_id = "a" + source_id
        body = {
            "id": event_id,
            "summary": summary,
            "description": description,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": "Europe/Paris",
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": "Europe/Paris",
            },
            "extendedProperties": {
                "private": {"planningAuxSourceId": source_id},
            },
        }
        try:
            payload = self._service.events().insert(
                calendarId=self._calendar_id, body=body,
            ).execute()
            action = "created"
        except HttpError as exc:
            if exc.resp.status != 409:
                raise
            existing = self._service.events().get(
                calendarId=self._calendar_id, eventId=event_id,
            ).execute()
            properties = existing.get("extendedProperties", {})
            private_properties = properties.get("private", {}) if isinstance(properties, dict) else {}
            if not isinstance(private_properties, dict) or private_properties.get("planningAuxSourceId") != source_id:
                raise ValueError("Collision avec un événement Google qui n'appartient pas à cette prestation.") from exc
            update_body = {key: value for key, value in body.items() if key != "id"}
            payload = self._service.events().patch(
                calendarId=self._calendar_id, eventId=event_id, body=update_body,
            ).execute()
            action = "updated"
        if not isinstance(payload, dict) or payload.get("id") != event_id:
            raise ValueError("La réponse Google ne correspond pas à l'intervention envoyée.")
        return action, payload
