import argparse
from datetime import datetime
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
        "Une information inconnue du planning a été détectée.\n"
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
        created = 0
        updated = 0
        with CalendarClient(calendar_id) as calendar:
            for intervention in interventions:
                start = intervention.debut.replace(tzinfo=TIME_ZONE)
                end = start + intervention.duree
                description = "Doublure" if intervention.doublure else ""
                action, _ = calendar.upsert_intervention_event(
                    intervention.source_id,
                    _event_summary(intervention),
                    start,
                    end,
                    description=description,
                )
                if action == "created":
                    created += 1
                elif action == "updated":
                    updated += 1
                else:
                    raise ValueError("Le client Google a retourné une action inconnue.")
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
        "Synchronisation %s terminée : %d prestations, %d créées, %d mises à jour.",
        args.month, len(interventions), created, updated,
    )
    print(
        f"Mois {args.month} synchronisé : {len(interventions)} prestations, "
        f"{created} créées, {updated} mises à jour."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
