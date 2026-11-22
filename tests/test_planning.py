from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from planning_aux import planning


TABLE = """<table>
<thead><tr>
<th>Intervenant</th><th>Date</th><th class="hidden-xs">Dur&#233;e</th>
<th class="hidden-xs">Produit</th><th>Statut</th><th class="visible-xs">Détails</th>
</tr></thead>
<tbody><tr>
<td class="hidden-xs"> EXEMPLE,   Camille </td>
<td class="visible-xs">EXEMPLE</td>
<td class="visible-xs">03/11/26 08:00</td>
<td class="hidden-xs">03/11/2026 08:00</td>
<td class="hidden-xs">01:30</td>
<td class="hidden-xs">DOUBLURE - Doublure</td>
<td class="hidden-xs">Réalisée</td>
<td class="visible-xs"><button data-target="#modal-Intervention-12345"></button>
<div>Date 03/11/2026 08:00 Durée 01:30 Doublure</div></td>
</tr></tbody></table>"""

MONTH_FORM = """<form action="/Ximi2/fasv/Interventions/List" method="post">
<input name="__RequestVerificationToken" type="hidden" value="fresh+token&amp;value">
<select id="dateFilter" name="dateFilter"><optgroup label="Par mois">
<option value="M-1">Mois d&#39;Octobre 2026</option>
<option selected="selected" value="M0">Mois de Novembre 2026</option>
<option value="M1">Mois de D&#233;cembre 2026</option>
<option value="M2">Mois de Janvier 2027</option>
</optgroup><optgroup label="Par semaine"><option value="W0">Semaine</option></optgroup>
</select></form>"""


def synthetic_month():
    rows = []
    for index in range(57):
        day = 2 + index * 28 // 56
        start = datetime(2026, 11, day, 16, 30)
        date_text = start.strftime("%d/%m/%Y %H:%M")
        name = "" if index == 30 else f"Intervenant de test {index + 1}"
        duration = "03:00" if index in (0, 56) else "01:30"
        product = "DOUBLURE - Doublure" if index in (3, 12, 24, 44) else "2019 PCH - 2019 PCH"
        status = "A pourvoir" if not name else "Réalisée"
        rows.append(f"""<tr>
<td class="hidden-xs">{name}</td>
<td class="visible-xs">TEST</td>
<td class="visible-xs">{start.strftime("%d/%m/%y %H:%M")}</td>
<td class="hidden-xs">{date_text}</td>
<td class="hidden-xs">{duration}</td>
<td class="hidden-xs">{product}</td>
<td class="hidden-xs">{status}</td>
<td class="visible-xs"><button data-target="#modal-Intervention-{90000 + index}"></button>
<div>Date {date_text} Durée {duration}</div></td>
</tr>""")
    return TABLE[:TABLE.index("<tbody>")] + "<tbody>" + "".join(rows) + "</tbody></table>"


def test_extracts_only_desktop_fields():
    assert planning.parse_interventions(TABLE) == [
        planning.Intervention(
            source_id="12345",
            intervenant="EXEMPLE, Camille",
            debut=datetime(2026, 11, 3, 8),
            duree=timedelta(hours=1, minutes=30),
            doublure=True,
        ),
    ]


def test_synthetic_month_has_no_mobile_duplicates():
    interventions = planning.parse_interventions(synthetic_month())
    assert len(interventions) == 57
    assert sum(item.doublure for item in interventions) == 4
    assert interventions[0].debut == datetime(2026, 11, 2, 16, 30)
    assert interventions[-1].debut == datetime(2026, 11, 30, 16, 30)
    assert interventions[0].duree == interventions[-1].duree == timedelta(hours=3)
    assert any(item.intervenant is None for item in interventions)


def test_other_product_is_not_a_doublure():
    html = TABLE.replace("DOUBLURE - Doublure", "2019 PCH - 2019 PCH")
    assert not planning.parse_interventions(html)[0].doublure


def test_unknown_product_is_reported_for_logging_and_user_notification():
    html = TABLE.replace("DOUBLURE - Doublure", "Produit ajouté par le site")
    with pytest.raises(planning.UnknownPlanningInfo, match="libellé de produit non reconnu"):
        planning.parse_interventions(html)


def test_unassigned_intervention_is_preserved():
    html = TABLE.replace(" EXEMPLE,   Camille ", "").replace("Réalisée", "A pourvoir")
    intervention = planning.parse_interventions(html)[0]
    assert intervention.intervenant is None
    assert intervention.debut == datetime(2026, 11, 3, 8)
    assert intervention.duree == timedelta(minutes=90)


@pytest.mark.parametrize(("old", "new", "message"), [
    ("03/11/2026 08:00", "31/02/2026 08:00", "date"),
    ("01:30</td>", "01:90</td>", "durée"),
    ('<td class="hidden-xs">DOUBLURE - Doublure</td>', "", "incomplètes"),
])
def test_invalid_row_is_not_silently_discarded(old, new, message):
    html = TABLE.replace(old, new)
    if message == "incomplètes":
        html = html.replace('<td class="hidden-xs">Réalisée</td>', "")
    with pytest.raises(planning.PlanningError, match=message):
        planning.parse_interventions(html)


def test_empty_table_is_distinct_from_missing_table():
    empty = TABLE[:TABLE.index("<tbody>")] + "<tbody></tbody></table>"
    assert planning.parse_interventions(empty) == []
    with pytest.raises(planning.PlanningError, match="tableau"):
        planning.parse_interventions("<html>Sign in</html>")


@pytest.mark.parametrize("status", [200, 503])
def test_fetch_uses_authenticated_client_and_propagates_http_errors(status):
    def handler(request):
        assert request.url == planning.PLANNING_URL
        assert request.headers["cookie"] == "session=active"
        return httpx.Response(status, text=TABLE)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("session", "active", domain="extranet.ximi.xelya.io")
        if status == 200:
            assert planning.fetch_planning_html(client) == TABLE
        else:
            with pytest.raises(httpx.HTTPStatusError):
                planning.fetch_planning_html(client)


def test_redirect_to_login_is_not_treated_as_a_planning():
    def handler(request):
        if request.url == planning.PLANNING_URL:
            return httpx.Response(302, headers={"Location": "https://sso.xelya.io/login"})
        return httpx.Response(200, text="<html>Sign in</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(planning.PlanningError, match="redirigée"):
            planning.fetch_planning_html(client)


@pytest.mark.parametrize("html", [TABLE, "<html>Invalid planning</html>"])
def test_get_interventions_exits_session_on_success_and_parse_error(monkeypatch, html):
    closed = []

    @contextmanager
    def session(login):
        assert login == "user"
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html),
        )) as client:
            try:
                yield client
            finally:
                closed.append(True)

    monkeypatch.setattr(planning, "authenticated_session", session)
    if html == TABLE:
        assert len(planning.get_interventions("user")) == 1
    else:
        with pytest.raises(planning.PlanningError):
            planning.get_interventions("user")
    assert closed == [True]


@pytest.mark.parametrize("month", ["M-1", "M0", "M1", "M2"])
def test_selects_available_month_with_form_csrf_and_session_cookie(month):
    methods = []
    expected_date = {
        "M-1": datetime(2026, 10, 3, 8),
        "M0": datetime(2026, 11, 3, 8),
        "M1": datetime(2026, 12, 3, 8),
        "M2": datetime(2027, 1, 3, 8),
    }[month]
    selected_form = MONTH_FORM.replace('selected="selected" ', "").replace(
        f'value="{month}"', f'selected="selected" value="{month}"',
    )
    selected_page = selected_form + TABLE.replace(
        "03/11/2026 08:00", expected_date.strftime("%d/%m/%Y %H:%M"),
    )

    def handler(request):
        methods.append(request.method)
        assert request.url == planning.PLANNING_URL
        assert request.headers["cookie"] == "session=active"
        if request.method == "GET":
            return httpx.Response(200, text=MONTH_FORM + TABLE)
        assert parse_qs(request.content.decode()) == {
            "dateFilter": [month],
            "__RequestVerificationToken": ["fresh+token&value"],
        }
        return httpx.Response(200, text=selected_page)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("session", "active", domain="extranet.ximi.xelya.io")
        html = planning.fetch_planning_html(client, month=month)
        assert html == selected_page
        assert planning.parse_interventions(html)[0].debut == expected_date
    assert methods == ["GET", "POST"]


@pytest.mark.parametrize("month", ["W0", "M99"])
def test_invalid_or_unavailable_month_is_not_posted(month):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text=MONTH_FORM + TABLE)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            planning.fetch_planning_html(client, month=month)


@pytest.mark.parametrize("page", [
    TABLE,
    MONTH_FORM.replace('value="fresh+token&amp;value"', 'value=""') + TABLE,
])
def test_month_selection_requires_its_form_and_csrf(page):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text=page)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(planning.PlanningError):
            planning.fetch_planning_html(client, month="M1")


@pytest.mark.parametrize("failure", ["ignored-month", "login-redirect", "http-error"])
def test_month_selection_failures_are_explicit(failure):
    def handler(request):
        if request.url.host == "sso.xelya.io":
            return httpx.Response(200, text="<html>Sign in</html>")
        if request.method == "GET":
            return httpx.Response(200, text=MONTH_FORM + TABLE)
        if failure == "login-redirect":
            return httpx.Response(302, headers={"Location": "https://sso.xelya.io/login"})
        return httpx.Response(503 if failure == "http-error" else 200, text=MONTH_FORM + TABLE)

    error = httpx.HTTPStatusError if failure == "http-error" else planning.PlanningError
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error):
            planning.fetch_planning_html(client, month="M1")


def test_get_interventions_forwards_month_and_leaves_session_on_selection_error(monkeypatch):
    closed = []

    @contextmanager
    def session(login):
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=MONTH_FORM + TABLE),
        )) as client:
            try:
                yield client
            finally:
                closed.append(True)

    monkeypatch.setattr(planning, "authenticated_session", session)
    with pytest.raises(planning.PlanningError, match="confirmé"):
        planning.get_interventions("user", month="M1")
    assert closed == [True]


@pytest.mark.parametrize(("month", "expected_value"), [
    ("2026-10", "M-1"),
    ("2026-11", "M0"),
    ("2026-12", "M1"),
    ("2027-01", "M2"),
])
def test_selects_month_by_calendar_year_and_month(month, expected_value):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=MONTH_FORM + TABLE)
        assert parse_qs(request.content.decode())["dateFilter"] == [expected_value]
        selected_form = MONTH_FORM.replace('selected="selected" ', "").replace(
            f'value="{expected_value}"', f'selected="selected" value="{expected_value}"',
        )
        return httpx.Response(200, text=selected_form + TABLE)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert planning.fetch_planning_html(client, month=month).startswith(
            MONTH_FORM.replace('selected="selected" ', "").replace(
                f'value="{expected_value}"', f'selected="selected" value="{expected_value}"',
            )
        )


def test_month_not_available_for_calendar_month():
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=MONTH_FORM + TABLE),
    )) as client:
        with pytest.raises(ValueError, match="n'est pas proposé"):
            planning.fetch_planning_html(client, month="2026-09")
