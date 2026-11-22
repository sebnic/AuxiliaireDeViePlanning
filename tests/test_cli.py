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

        def upsert_intervention_event(self, *args, **kwargs):
            seen["event"] = (args, kwargs)
            return "created", {"id": "a12345"}

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
    assert "1 créées, 0 mises à jour" in capsys.readouterr().out
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_cli_defaults_to_current_local_month(monkeypatch, tmp_path):
    class FixedDateTime:
        @staticmethod
        def now(tz):
            return datetime(2026, 11, 22, 19, tzinfo=tz)

    class EmptyCalendar:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

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
