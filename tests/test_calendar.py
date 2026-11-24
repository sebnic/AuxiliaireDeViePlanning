from datetime import date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from planning_aux import calendar


def test_credentials_preserve_multiline_key(monkeypatch):
    values = {
        "type": "service_account\n",
        "private_key_id": "test-key-id\n",
        "private_key": "-----BEGIN PRIVATE KEY-----\nmultiline\n-----END PRIVATE KEY-----\n",
        "client_email": "test@example.invalid\n",
        "client_id": "test-id\n",
        "token_uri": "https://oauth2.googleapis.com/token\n",
    }
    monkeypatch.setattr(calendar, "get_secret", lambda entry: values[entry.split("/")[-1]])
    factory = Mock()
    monkeypatch.setattr(calendar.Credentials, "from_service_account_info", factory)
    assert calendar._load_credentials() is factory.return_value
    info = factory.call_args.args[0]
    assert info["private_key"] == values["private_key"]
    assert info["client_email"] == "test@example.invalid"
    assert factory.call_args.kwargs["scopes"] == calendar.SCOPES


def test_create_and_read_event_with_exclusive_end_and_target_calendar(monkeypatch):
    credentials = Mock()
    monkeypatch.setattr(calendar, "_load_credentials", lambda: credentials)
    service = Mock()
    builder = Mock(return_value=service)
    event = {"id": "test-event", "summary": "Test"}
    events = service.events.return_value
    events.insert.return_value.execute.return_value = event
    events.get.return_value.execute.return_value = event
    monkeypatch.setattr(calendar, "build", builder)
    with calendar.CalendarClient("test@example.invalid") as client:
        assert client.create_all_day_event("Test", date(2026, 12, 31)) == event
        assert client.get_event("test-event") == event
    builder.assert_called_once_with(
        "calendar", "v3", credentials=credentials, cache_discovery=False,
    )
    events.insert.assert_called_once_with(calendarId="test@example.invalid", body={
        "summary": "Test",
        "start": {"date": "2026-12-31"},
        "end": {"date": "2027-01-01"},
    })
    events.get.assert_called_once_with(calendarId="test@example.invalid", eventId="test-event")
    events.insert.return_value.execute.assert_called_once_with()
    events.get.return_value.execute.assert_called_once_with()
    service.close.assert_called_once()


def test_google_error_is_propagated_and_session_closed(monkeypatch):
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    service = Mock()
    service.events.return_value.insert.return_value.execute.side_effect = HttpError(
        Response({"status": "403"}), b'{"error":{"message":"Access denied"}}',
    )
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    with pytest.raises(HttpError, match="Access denied"):
        with calendar.CalendarClient("test@example.invalid") as client:
            client.create_all_day_event("Test", date(2026, 11, 18))
    service.close.assert_called_once()


def test_upsert_creates_timezone_aware_intervention(monkeypatch):
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    service = Mock()
    result = {"id": "a12345", "summary": "Prestation"}
    service.events.return_value.insert.return_value.execute.return_value = result
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    zone = ZoneInfo("Europe/Paris")
    start = datetime(2026, 11, 23, 8, tzinfo=zone)
    end = datetime(2026, 11, 23, 9, 30, tzinfo=zone)
    with calendar.CalendarClient("target") as client:
        action, created = client.upsert_intervention_event(
            "12345", "Prestation", start, end, description="Doublure",
        )
    assert action == "created"
    assert created == result
    service.events.return_value.insert.assert_called_once_with(
        calendarId="target",
        body={
            "id": "a12345",
            "summary": "Prestation",
            "description": "Doublure",
            "start": {
                "dateTime": "2026-11-23T08:00:00+01:00",
                "timeZone": "Europe/Paris",
            },
            "end": {
                "dateTime": "2026-11-23T09:30:00+01:00",
                "timeZone": "Europe/Paris",
            },
            "extendedProperties": {"private": {"planningAuxSourceId": "12345"}},
        },
    )


def test_upsert_updates_existing_owned_intervention(monkeypatch):
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    service = Mock()
    service.events.return_value.insert.return_value.execute.side_effect = HttpError(
        Response({"status": "409"}), b"duplicate",
    )
    service.events.return_value.get.return_value.execute.return_value = {
        "id": "a12345",
        "extendedProperties": {"private": {"planningAuxSourceId": "12345"}},
    }
    updated = {"id": "a12345", "summary": "Updated"}
    service.events.return_value.patch.return_value.execute.return_value = updated
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    zone = ZoneInfo("Europe/Paris")
    with calendar.CalendarClient("target") as client:
        action, result = client.upsert_intervention_event(
            "12345", "Updated", datetime(2026, 11, 23, 8, tzinfo=zone),
            datetime(2026, 11, 23, 9, tzinfo=zone),
        )
    assert action == "updated"
    assert result == updated
    service.events.return_value.get.assert_called_once_with(
        calendarId="target", eventId="a12345",
    )
    patched_body = service.events.return_value.patch.call_args.kwargs["body"]
    assert "id" not in patched_body
    assert patched_body["summary"] == "Updated"


def test_upsert_does_not_overwrite_unrelated_id_collision(monkeypatch):
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    service = Mock()
    service.events.return_value.insert.return_value.execute.side_effect = HttpError(
        Response({"status": "409"}), b"duplicate",
    )
    service.events.return_value.get.return_value.execute.return_value = {
        "id": "a12345",
        "extendedProperties": {"private": {"planningAuxSourceId": "other"}},
    }
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    zone = ZoneInfo("Europe/Paris")
    with calendar.CalendarClient("target") as client, pytest.raises(ValueError, match="Collision"):
        client.upsert_intervention_event(
            "12345", "Prestation", datetime(2026, 11, 23, 8, tzinfo=zone),
            datetime(2026, 11, 23, 9, tzinfo=zone),
        )
    service.events.return_value.patch.assert_not_called()


def test_upsert_requires_aware_valid_interval(monkeypatch):
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=Mock()))
    with calendar.CalendarClient("target") as client:
        with pytest.raises(ValueError, match="fuseau horaire"):
            client.upsert_intervention_event(
                "12345", "Prestation", datetime(2026, 11, 23, 8),
                datetime(2026, 11, 23, 9),
            )
        with pytest.raises(ValueError, match="suivre le début"):
            client.upsert_intervention_event(
                "12345", "Prestation",
                datetime(2026, 11, 23, 9, tzinfo=ZoneInfo("Europe/Paris")),
                datetime(2026, 11, 23, 8, tzinfo=ZoneInfo("Europe/Paris")),
            )


def test_list_events_reads_all_pages_and_expands_recurring_events(monkeypatch):
    service = Mock()
    service.events.return_value.list.return_value.execute.side_effect = [
        {"items": [{"id": "first"}], "nextPageToken": "next"},
        {"items": [{"id": "second"}]},
    ]
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    start = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Paris"))
    end = datetime(2026, 12, 1, tzinfo=ZoneInfo("Europe/Paris"))
    with calendar.CalendarClient("target") as client:
        assert client.list_events(start, end) == [{"id": "first"}, {"id": "second"}]
    calls = service.events.return_value.list.call_args_list
    assert calls[0].kwargs == {
        "calendarId": "target", "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "singleEvents": True, "showDeleted": False, "maxResults": 2500, "pageToken": None,
    }
    assert calls[1].kwargs["pageToken"] == "next"


def test_list_events_rejects_partial_or_looping_pagination(monkeypatch):
    service = Mock()
    service.events.return_value.list.return_value.execute.side_effect = [
        {"items": [], "nextPageToken": "same"},
        {"items": [], "nextPageToken": "same"},
    ]
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    start = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Paris"))
    with calendar.CalendarClient("target") as client, pytest.raises(ValueError, match="pagination"):
        client.list_events(start, start)


def test_list_events_accepts_empty_google_collection_without_items(monkeypatch):
    service = Mock()
    service.events.return_value.list.return_value.execute.return_value = {"kind": "calendar#events"}
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    start = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Paris"))
    end = datetime(2026, 12, 1, tzinfo=ZoneInfo("Europe/Paris"))
    with calendar.CalendarClient("target") as client:
        assert client.list_events(start, end) == []


def test_create_and_conditional_delete_intervention(monkeypatch):
    service = Mock()
    events = service.events.return_value
    events.insert.return_value.execute.return_value = {"id": "google-generated"}
    events.delete.return_value.headers = {}
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    zone = ZoneInfo("Europe/Paris")
    start = datetime(2026, 11, 23, 8, tzinfo=zone)
    end = datetime(2026, 11, 23, 9, tzinfo=zone)
    with calendar.CalendarClient("target") as client:
        assert client.create_intervention_event("123", "Prestation — Exemple", start, end)["id"] == "google-generated"
        client.delete_event("old", '"version"')
    body = events.insert.call_args.kwargs["body"]
    assert "id" not in body
    assert body["extendedProperties"]["private"]["planningAuxSourceId"] == "123"
    events.delete.assert_called_once_with(calendarId="target", eventId="old")
    assert events.delete.return_value.headers["If-Match"] == '"version"'
    events.delete.return_value.execute.assert_called_once()


def test_delete_propagates_concurrent_modification(monkeypatch):
    service = Mock()
    request = service.events.return_value.delete.return_value
    request.headers = {}
    request.execute.side_effect = HttpError(Response({"status": "412"}), b"changed")
    monkeypatch.setattr(calendar, "_load_credentials", Mock())
    monkeypatch.setattr(calendar, "build", Mock(return_value=service))
    with calendar.CalendarClient("target") as client, pytest.raises(HttpError):
        client.delete_event("event", '"old-version"')
