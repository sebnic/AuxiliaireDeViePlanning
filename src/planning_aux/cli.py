import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

import httpx
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError

from planning_aux.calendar import CalendarClient
from planning_aux.planning import Intervention, PlanningError, UnknownPlanningInfo, get_interventions
from planning_aux.secrets import get_secret


DEFAULT_LOG_FILE = Path.home() / ".local" / "state" / "planning-aux" / "planning_aux.log"
LOGIN_ENTRY = "Service/FamilleService/login"
CALENDAR_ID_ENTRY = "AuxiliaireDeViePlanning/calendar_id"
TIME_ZONE = ZoneInfo("Europe/Paris")


def _month_argument(value: str) -> str:
    if re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value) is None:
        raise argparse.ArgumentTypeError("Le mois doit être au format AAAA-MM, par exemple 2026-11.")
    return value


def _configure_logging(path: Path) -> logging.Logger:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)

    logger = logging.getLogger("planning_aux")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _notify_unknown_info(log_path: Path) -> None:
    message = (
        "Une information inconnue du planning ou de l'agenda a été détectée.\n"
        f"Consultez le fichier de log :\n{log_path}"
    )
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        print(message, file=sys.stderr)
        return

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Planning auxiliaires", message)
    except tk.TclError:
        print(message, file=sys.stderr)
    finally:
        if root is not None:
            root.destroy()


def _event_summary(intervention: Intervention) -> str:
    title = f"Prestation — {intervention.intervenant}" if intervention.intervenant else "Prestation à pourvoir"
    if intervention.doublure:
        title += " — Doublure"
    return title


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=TIME_ZONE)
    if number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=TIME_ZONE)
    else:
        end = datetime(year, number + 1, 1, tzinfo=TIME_ZONE)
    return start, end


def _google_datetime(value: object) -> datetime:
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        raise UnknownPlanningInfo("Date ou horaire Google non reconnu (événement sur une journée ?).")
    try:
        result = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnknownPlanningInfo("Date ou horaire Google invalide.") from exc
    if result.utcoffset() is None:
        raise UnknownPlanningInfo("Fuseau horaire absent d'un événement Google.")
    return result


def _google_intervenant(summary: object) -> str | None:
    if summary == "Prestation à pourvoir" or summary == "Prestation à pourvoir — Doublure":
        return None
    if not isinstance(summary, str) or not summary.startswith("Prestation — "):
        raise UnknownPlanningInfo(f"Titre d'événement Google non reconnu : {summary!r}.")
    name = summary.removeprefix("Prestation — ").removesuffix(" — Doublure")
    if not name or name != name.strip():
        raise UnknownPlanningInfo(f"Intervenant Google non reconnu : {summary!r}.")
    return name


def _refresh(calendar: CalendarClient, interventions: list[Intervention], month: str) -> tuple[int, int, int]:
    start, end = _month_bounds(month)
    expected = {}
    for intervention in interventions:
        local_start = intervention.debut.replace(tzinfo=TIME_ZONE)
        utc_start = local_start.astimezone(timezone.utc)
        if utc_start.astimezone(TIME_ZONE).replace(tzinfo=None) != intervention.debut:
            raise UnknownPlanningInfo("Horaire Famille Services inexistant dans le fuseau Europe/Paris.")
        alternate = local_start.replace(fold=1).astimezone(timezone.utc)
        if alternate != utc_start and alternate.astimezone(TIME_ZONE).replace(tzinfo=None) == intervention.debut:
            raise UnknownPlanningInfo("Horaire Famille Services ambigu dans le fuseau Europe/Paris.")
        if not start <= local_start < end or intervention.duree.total_seconds() <= 0:
            raise UnknownPlanningInfo("Date hors du mois demandé ou durée invalide dans le planning.")
        key = (utc_start, utc_start + intervention.duree, intervention.intervenant)
        if key in expected:
            raise UnknownPlanningInfo("Prestations Famille Services identiques en double.")
        expected[key] = intervention

    actual = {}
    seen_ids = set()
    for event in calendar.list_events(start, end):
        google_start = _google_datetime(event.get("start"))
        if not start <= google_start.astimezone(TIME_ZONE) < end:
            continue
        google_end = _google_datetime(event.get("end"))
        event_id, etag = event.get("id"), event.get("etag")
        if not isinstance(event_id, str) or not event_id or not isinstance(etag, str) or not etag:
            raise UnknownPlanningInfo("Identifiant ou version d'un événement Google absent.")
        if event_id in seen_ids:
            raise UnknownPlanningInfo("Identifiant d'événement Google dupliqué.")
        seen_ids.add(event_id)
        key = (
            google_start.astimezone(timezone.utc),
            google_end.astimezone(timezone.utc),
            _google_intervenant(event.get("summary")),
        )
        if key[1] <= key[0]:
            raise UnknownPlanningInfo("Durée d'un événement Google invalide.")
        if key in actual:
            raise UnknownPlanningInfo("Événements Google identiques en double.")
        actual[key] = (event_id, etag)

    missing = expected.keys() - actual.keys()
    obsolete = actual.keys() - expected.keys()
    for key in expected:
        if key not in missing:
            continue
        intervention = expected[key]
        calendar.create_intervention_event(
            intervention.source_id,
            _event_summary(intervention),
            key[0].astimezone(TIME_ZONE),
            key[1].astimezone(TIME_ZONE),
            description="Doublure" if intervention.doublure else "",
        )
    for key in actual:
        if key in obsolete:
            calendar.delete_event(*actual[key])
    return len(missing), len(obsolete), len(expected) - len(missing)


def main(argv: list[str] | None = None) -> int:
    current_month = datetime.now(TIME_ZONE).strftime("%Y-%m")
    parser = argparse.ArgumentParser(
        description="Synchronise les prestations du planning Famille Services dans Google Calendar.",
    )
    parser.add_argument(
        "--month",
        type=_month_argument,
        default=current_month,
        metavar="AAAA-MM",
        help=f"Mois à synchroniser (défaut : {current_month}).",
    )
    parser.add_argument(
        "--calendar-id",
        help=f"Identifiant de l'agenda Google cible (défaut : entrée pass {CALENDAR_ID_ENTRY}).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help=f"Fichier de log (défaut : {DEFAULT_LOG_FILE}).",
    )
    args = parser.parse_args(argv)
    log_path = args.log_file.expanduser()
    try:
        logger = _configure_logging(log_path)
    except OSError as exc:
        print(f"Impossible d'ouvrir le fichier de log {log_path}: {exc}", file=sys.stderr)
        return 1

    try:
        login = get_secret(LOGIN_ENTRY).removesuffix("\n")
        if not login:
            raise ValueError(f"L'entrée pass {LOGIN_ENTRY} est vide.")
        calendar_id = args.calendar_id
        if calendar_id is None:
            calendar_id = get_secret(CALENDAR_ID_ENTRY).removesuffix("\n")
        if not calendar_id.strip():
            raise ValueError(f"L'identifiant d'agenda {CALENDAR_ID_ENTRY} est vide.")
        interventions = get_interventions(login, month=args.month)
        with CalendarClient(calendar_id) as calendar:
            created, deleted, unchanged = _refresh(calendar, interventions, args.month)
    except UnknownPlanningInfo as exc:
        logger.error("Information inconnue détectée : %s", exc)
        _notify_unknown_info(log_path)
        print(f"Synchronisation interrompue ; détails : {log_path}", file=sys.stderr)
        return 2
    except (
        PlanningError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        GoogleAuthError,
        HttpError,
    ) as exc:
        logger.exception("Échec de la synchronisation")
        print(f"Échec de la synchronisation : {exc}", file=sys.stderr)
        return 1

    logger.info(
        "Synchronisation %s terminée : %d prestations, %d créées, %d supprimées, %d inchangées.",
        args.month, len(interventions), created, deleted, unchanged,
    )
    print(
        f"Mois {args.month} synchronisé : {len(interventions)} prestations, "
        f"{created} créées, {deleted} supprimées, {unchanged} inchangées."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
