import argparse
from datetime import datetime, timedelta
import stat
from zoneinfo import ZoneInfo

import pytest

from planning_aux import cli
from planning_aux.planning import Intervention, UnknownPlanningInfo


def test_cli_syncs_month_with_timezone_and_event_fields(monkeypatch, tmp_path, capsys):
    seen = {}
    scheduled = Intervention(
        source_id="12345",
        intervenant="Exemple",
        debut=datetime(2026, 11, 23, 8),
        duree=timedelta(minutes=90),
        doublure=True,
    )

    class FakeCalendar:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def list_events(self, start, end):
            return []

        def create_intervention_event(self, *args, **kwargs):
            seen["event"] = (args, kwargs)
            return {"id": "new-event"}

    def get_interventions(login, *, month):
        seen["login"] = login
        seen["month"] = month
        return [scheduled]

    monkeypatch.setattr(cli, "get_secret", lambda entry: "stored-login\n")
    monkeypatch.setattr(cli, "get_interventions", get_interventions)
    monkeypatch.setattr(cli, "CalendarClient", lambda calendar_id: FakeCalendar())
    log_path = tmp_path / "logs" / "planning.log"
    result = cli.main([
        "--month", "2026-11",
        "--calendar-id", "test-calendar",
        "--log-file", str(log_path),
    ])
    args, kwargs = seen["event"]
    assert result == 0
    assert seen == {"login": "stored-login", "month": "2026-11", "event": (args, kwargs)}
    assert args[:2] == ("12345", "Prestation — Exemple — Doublure")
    assert args[2] == datetime(2026, 11, 23, 8, tzinfo=ZoneInfo("Europe/Paris"))
    assert args[3] == datetime(2026, 11, 23, 9, 30, tzinfo=ZoneInfo("Europe/Paris"))
    assert kwargs == {"description": "Doublure"}
    assert "1 créées, 0 supprimées, 0 inchangées" in capsys.readouterr().out
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_cli_defaults_to_current_local_month(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @staticmethod
        def now(tz):
            return datetime(2026, 11, 22, 19, tzinfo=tz)

    seen = {}
    secrets = {
        cli.LOGIN_ENTRY: "login\n",
        cli.CALENDAR_ID_ENTRY: "stored-calendar\n",
    }

    class EmptyCalendar:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def list_events(self, start, end):
            return []

    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    monkeypatch.setattr(cli, "get_secret", lambda entry: secrets[entry])
    monkeypatch.setattr(
        cli, "get_interventions",
        lambda login, *, month: seen.update(month=month) or [],
    )
    monkeypatch.setattr(
        cli, "CalendarClient",
        lambda calendar_id: seen.update(calendar_id=calendar_id) or EmptyCalendar(),
    )
    assert cli.main(["--log-file", str(tmp_path / "planning.log")]) == 0
    assert seen == {"month": "2026-11", "calendar_id": "stored-calendar"}


def test_cli_logs_unknown_info_and_notifies_with_log_path(monkeypatch, tmp_path):
    log_path = tmp_path / "planning.log"
    notifications = []
    monkeypatch.setattr(cli, "get_secret", lambda entry: "login\n")

    def unknown(*args, **kwargs):
        raise UnknownPlanningInfo("Produit inconnu")

    monkeypatch.setattr(cli, "get_interventions", unknown)
    monkeypatch.setattr(cli, "_notify_unknown_info", notifications.append)
    assert cli.main(["--log-file", str(log_path)]) == 2
    assert "Produit inconnu" in log_path.read_text(encoding="utf-8")
    assert notifications == [log_path]
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_log_setup_does_not_change_permissions_of_existing_parent(tmp_path):
    log_path = tmp_path / "planning.log"
    tmp_path.chmod(0o755)
    logger = cli._configure_logging(log_path)
    try:
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_month_argument_requires_year_and_month():
    assert cli._month_argument("2026-11") == "2026-11"
    with pytest.raises(argparse.ArgumentTypeError):
        cli._month_argument("M0")


def intervention(source_id="123", name="Exemple", hour=8, minute=0, duration=90, doublure=False):
    return Intervention(
        source_id=source_id,
        intervenant=name,
        debut=datetime(2026, 11, 23, hour, minute),
        duree=timedelta(minutes=duration),
        doublure=doublure,
    )


def google_event(event_id="event", summary="Prestation — Exemple",
                 start="2026-11-23T08:00:00+01:00", end="2026-11-23T09:30:00+01:00"):
    return {
        "id": event_id,
        "etag": f'"{event_id}-version"',
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


class RefreshCalendar:
    def __init__(self, events):
        self.events = events
        self.created = []
        self.deleted = []

    def list_events(self, start, end):
        assert start == datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Paris"))
        assert end == datetime(2026, 12, 1, tzinfo=ZoneInfo("Europe/Paris"))
        return self.events

    def create_intervention_event(self, *args, **kwargs):
        self.created.append((args, kwargs))

    def delete_event(self, *args):
        self.deleted.append(args)


def test_refresh_compares_intervenant_and_times_not_source_id_or_doublure():
    existing = google_event("manual", "Prestation — Exemple — Doublure",
                            "2026-11-23T07:00:00Z", "2026-11-23T08:30:00Z")
    calendar = RefreshCalendar([existing])
    assert cli._refresh(calendar, [intervention(source_id="987")], "2026-11") == (0, 0, 1)
    assert calendar.created == []
    assert calendar.deleted == []


def test_refresh_compares_unassigned_interventions():
    calendar = RefreshCalendar([google_event(summary="Prestation à pourvoir — Doublure")])
    assert cli._refresh(calendar, [intervention(name=None)], "2026-11") == (0, 0, 1)
    assert not calendar.created and not calendar.deleted


def test_refresh_is_idempotent_after_creation():
    class StatefulCalendar(RefreshCalendar):
        def create_intervention_event(self, *args, **kwargs):
            super().create_intervention_event(*args, **kwargs)
            self.events.append(google_event("created", args[1], args[2].isoformat(), args[3].isoformat()))

    calendar = StatefulCalendar([])
    assert cli._refresh(calendar, [intervention()], "2026-11") == (1, 0, 0)
    assert cli._refresh(calendar, [intervention()], "2026-11") == (0, 0, 1)
    assert len(calendar.created) == 1
    assert calendar.deleted == []


@pytest.mark.parametrize("change", ["date", "time", "duration", "intervenant"])
def test_refresh_replaces_changed_event(change):
    event = google_event()
    if change == "date":
        event["start"]["dateTime"] = "2026-11-22T08:00:00+01:00"
        event["end"]["dateTime"] = "2026-11-22T09:30:00+01:00"
    elif change == "time":
        event["start"]["dateTime"] = "2026-11-23T10:00:00+01:00"
        event["end"]["dateTime"] = "2026-11-23T11:30:00+01:00"
    elif change == "duration":
        event["end"]["dateTime"] = "2026-11-23T09:00:00+01:00"
    else:
        event["summary"] = "Prestation — Autre"
    calendar = RefreshCalendar([event])
    assert cli._refresh(calendar, [intervention()], "2026-11") == (1, 1, 0)
    assert calendar.created[0][0][:2] == ("123", "Prestation — Exemple")
    assert calendar.deleted == [("event", '"event-version"')]


def test_refresh_deletes_unmatched_even_when_not_owned_and_keeps_adjacent_month():
    calendar = RefreshCalendar([
        google_event("obsolete"),
        google_event("previous", start="2026-10-31T23:30:00+01:00", end="2026-11-01T01:00:00+01:00"),
    ])
    assert cli._refresh(calendar, [], "2026-11") == (0, 1, 0)
    assert calendar.deleted == [("obsolete", '"obsolete-version"')]


@pytest.mark.parametrize("side", ["planning", "google"])
def test_refresh_rejects_duplicates_before_any_write(side):
    calendar = RefreshCalendar([google_event(), google_event("duplicate")] if side == "google" else [])
    entries = [intervention(), intervention(source_id="456")] if side == "planning" else [intervention()]
    with pytest.raises(UnknownPlanningInfo, match="double"):
        cli._refresh(calendar, entries, "2026-11")
    assert not calendar.created and not calendar.deleted


@pytest.mark.parametrize("invalid", [
    {"summary": "Autre titre"},
    {"start": {"date": "2026-11-23"}},
    {"end": {"dateTime": "2026-11-23T08:00:00+01:00"}},
    {"etag": None},
])
def test_refresh_rejects_uninterpretable_google_event_before_writes(invalid):
    event = google_event()
    event.update(invalid)
    calendar = RefreshCalendar([event])
    with pytest.raises(UnknownPlanningInfo):
        cli._refresh(calendar, [intervention(hour=10)], "2026-11")
    assert not calendar.created and not calendar.deleted


def test_refresh_does_not_delete_if_creation_fails():
    class FailingCalendar(RefreshCalendar):
        def create_intervention_event(self, *args, **kwargs):
            raise ValueError("Échec de création")

    calendar = FailingCalendar([google_event()])
    with pytest.raises(ValueError, match="Échec"):
        cli._refresh(calendar, [intervention(hour=10)], "2026-11")
    assert calendar.deleted == []


def test_refresh_rejects_planning_outside_requested_month():
    entry = intervention()
    entry = Intervention(entry.source_id, entry.intervenant, datetime(2026, 12, 1), entry.duree, False)
    calendar = RefreshCalendar([google_event()])
    with pytest.raises(UnknownPlanningInfo, match="hors du mois"):
        cli._refresh(calendar, [entry], "2026-11")
    assert not calendar.created and not calendar.deleted


@pytest.mark.parametrize(("day", "message"), [
    (datetime(2026, 3, 29, 2, 30), "inexistant"),
    (datetime(2026, 10, 25, 2, 30), "ambigu"),
])
def test_refresh_rejects_unresolvable_dst_times(day, message):
    entry = Intervention("123", "Exemple", day, timedelta(hours=1), False)
    calendar = RefreshCalendar([])
    with pytest.raises(UnknownPlanningInfo, match=message):
        cli._refresh(calendar, [entry], day.strftime("%Y-%m"))
    assert not calendar.created and not calendar.deleted


def test_refresh_does_not_write_if_google_listing_fails():
    class FailingCalendar(RefreshCalendar):
        def list_events(self, start, end):
            raise ValueError("Échec de lecture")

    calendar = FailingCalendar([])
    with pytest.raises(ValueError, match="lecture"):
        cli._refresh(calendar, [intervention()], "2026-11")
    assert not calendar.created and not calendar.deleted
