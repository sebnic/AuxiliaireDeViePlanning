from dataclasses import dataclass
from datetime import datetime, timedelta
import re

import httpx
from bs4 import BeautifulSoup

from planning_aux.auth import EXTRANET, authenticated_session


PLANNING_URL = EXTRANET + "/Ximi2/fasv/Interventions/List"


class PlanningError(RuntimeError):
    """La page du planning ne contient pas les données attendues."""


class UnknownPlanningInfo(PlanningError):
    """La page contient une structure ou une valeur qui n'est pas reconnue."""


@dataclass(frozen=True)
class Intervention:
    source_id: str
    intervenant: str | None
    debut: datetime
    duree: timedelta
    doublure: bool


FRENCH_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


def _check_planning_response(response: httpx.Response) -> None:
    response.raise_for_status()
    if response.url.copy_with(query=None, fragment=None) != httpx.URL(PLANNING_URL):
        raise PlanningError("La requête du planning a été redirigée vers une page inattendue.")


def fetch_planning_html(client: httpx.Client, *, month: str | None = None) -> str:
    """Récupère le planning, éventuellement pour une option mensuelle du site."""
    if month is not None and re.fullmatch(r"(?:M-?[0-9]+|[0-9]{4}-(?:0[1-9]|1[0-2]))", month) is None:
        raise ValueError("Le mois doit être au format AAAA-MM ou une option du site telle que M0.")
    response = client.get(PLANNING_URL, follow_redirects=True)
    _check_planning_response(response)
    if month is None:
        return response.text

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one('form[action="/Ximi2/fasv/Interventions/List"][method="post" i]')
    selector = form.select_one('select[name="dateFilter"]') if form is not None else None
    if form is None or selector is None:
        raise PlanningError("Le formulaire de sélection du mois est absent.")
    month_options = []
    for group in selector.select("optgroup"):
        group_label = " ".join(group.get_attribute_list("label")).casefold()
        if "mois" not in group_label:
            continue
        for option in group.select("option"):
            if option.has_attr("disabled"):
                continue
            option_value = option.get("value")
            label = " ".join(option.get_text(" ", strip=True).split()).casefold()
            match = re.search(
                r"\b(?:de\s+|d['’])([a-zà-ÿ]+)\s+([0-9]{4})\b",
                label,
            )
            if match is None or match[1] not in FRENCH_MONTHS or not isinstance(option_value, str):
                raise UnknownPlanningInfo(
                    f"Option mensuelle non reconnue dans le sélecteur du site : {label!r}."
                )
            month_options.append((option_value, f"{int(match[2]):04d}-{FRENCH_MONTHS[match[1]]:02d}"))
    if not month_options:
        raise UnknownPlanningInfo("Le sélecteur ne contient aucune option mensuelle reconnue.")
    if month.startswith("M"):
        selected_month = month
        if selected_month not in {value for value, _ in month_options}:
            raise ValueError("Le mois demandé n'est pas proposé par le sélecteur du site.")
    else:
        selected_month = next(
            (value for value, month_name in month_options if month_name == month),
            None,
        )
        if selected_month is None:
            raise ValueError(f"Le mois {month} n'est pas proposé par le sélecteur du site.")
    token = form.select_one('input[name="__RequestVerificationToken"]')
    value = token.get("value") if token is not None else None
    if not isinstance(value, str) or not value:
        raise PlanningError("Le jeton CSRF de sélection du mois est absent.")
    response = client.post(
        PLANNING_URL,
        data={"dateFilter": selected_month, "__RequestVerificationToken": value},
        follow_redirects=True,
    )
    _check_planning_response(response)
    selected = BeautifulSoup(response.text, "html.parser").select_one(
        'select[name="dateFilter"] option[selected]'
    )
    if selected is None or selected.get("value") != selected_month:
        raise PlanningError("Le serveur n'a pas confirmé la sélection du mois demandé.")
    return response.text


def parse_interventions(html: str) -> list[Intervention]:
    """Extrait les colonnes utiles du tableau, sans les doublons mobiles."""
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.select("table"):
        headers = [
            cell.get_text(" ", strip=True).casefold()
            for cell in candidate.select(":scope > thead > tr > th:not(.visible-xs)")
        ]
        if headers[:4] == ["intervenant", "date", "durée", "produit"]:
            table = candidate
            break
    if table is None:
        observed_headers = [
            [
                cell.get_text(" ", strip=True)
                for cell in candidate.select(":scope > thead > tr > th")
            ]
            for candidate in soup.select("table")
        ]
        raise UnknownPlanningInfo(
            f"Le tableau attendu est absent ou ses colonnes ont changé; en-têtes observés : {observed_headers!r}."
        )
    body = table.find("tbody", recursive=False)
    if body is None:
        raise UnknownPlanningInfo("Le corps du tableau des interventions est absent.")

    interventions = []
    seen_ids = set()
    for index, row in enumerate(body.find_all("tr", recursive=False), start=1):
        cells = row.select(":scope > td:not(.visible-xs)")
        if len(cells) < 4:
            raise UnknownPlanningInfo(
                f"Ligne {index} : colonnes de prestation incomplètes ({len(cells)} cellules de bureau)."
            )
        name, start, duration, product = [
            " ".join(cell.get_text(" ", strip=True).split()) for cell in cells[:4]
        ]
        button = row.select_one('button[data-target^="#modal-Intervention-"]')
        target = button.get("data-target") if button is not None else None
        source_id = (
            target.removeprefix("#modal-Intervention-")
            if isinstance(target, str)
            and re.fullmatch(r"#modal-Intervention-[0-9]+", target)
            else None
        )
        if source_id is None or source_id in seen_ids:
            raise UnknownPlanningInfo(
                f"Ligne {index} : identifiant source absent, invalide ou dupliqué."
            )
        seen_ids.add(source_id)
        try:
            debut = datetime.strptime(start, "%d/%m/%Y %H:%M")
        except ValueError as exc:
            raise UnknownPlanningInfo(
                f"Ligne {index} : format de date non reconnu ({start!r})."
            ) from exc
        match = re.fullmatch(r"([0-9]+):([0-5][0-9])", duration)
        if match is None:
            raise UnknownPlanningInfo(
                f"Ligne {index} : format de durée non reconnu ({duration!r})."
            )
        normalized_product = " ".join(product.split()).casefold()
        is_doublure = re.search(r"\bdoublure\b", normalized_product) is not None
        if not is_doublure and normalized_product != "2019 pch - 2019 pch":
            raise UnknownPlanningInfo(
                f"Ligne {index} : libellé de produit non reconnu ({product!r})."
            )
        interventions.append(Intervention(
            source_id=source_id,
            intervenant=name or None,
            debut=debut,
            duree=timedelta(hours=int(match[1]), minutes=int(match[2])),
            doublure=is_doublure,
        ))
    return interventions


def get_interventions(login: str, *, month: str | None = None) -> list[Intervention]:
    """Récupère les prestations puis déconnecte la session côté serveur."""
    with authenticated_session(login) as client:
        return parse_interventions(fetch_planning_html(client, month=month))
