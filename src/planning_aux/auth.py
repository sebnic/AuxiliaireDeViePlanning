from collections.abc import Generator
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from planning_aux.secrets import get_password


EXTRANET = "https://extranet.ximi.xelya.io"
SSO = "https://sso.xelya.io"
ENTRY_URL = EXTRANET + "/Ximi2/fasv/Account/SignIn?returnUrl=~%2Ffasv"
TENANT = "Ximi2/fasv"


class AuthenticationError(RuntimeError):
    """La connexion n'a pas abouti à une session authentifiée sur l'extranet."""


class LogoutError(RuntimeError):
    """La déconnexion côté serveur n'a pas pu être confirmée."""


def _same_origin(url: str, origin: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(origin)
    return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)


def _is_authenticated(response: httpx.Response) -> bool:
    if not _same_origin(str(response.url), EXTRANET):
        return False
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.select_one('form[action="/Ximi2/fasv/Account/SignOut"]') is not None


def authenticate(client: httpx.Client, login: str) -> httpx.Response:
    """Connecte le client à l'extranet, avec le mot de passe lu dans pass.

    Le client appartient à l'appelant, qui appelle logout avant de le fermer.
    Les cookies restent dans ce client pour les requêtes suivantes.
    Préférer authenticated_session pour garantir ce nettoyage.
    """
    if not login.strip():
        raise ValueError("L'identifiant de connexion est obligatoire.")

    response = client.get(ENTRY_URL, follow_redirects=True)
    response.raise_for_status()
    if _is_authenticated(response):
        return response
    if not _same_origin(str(response.url), SSO):
        raise AuthenticationError("La page de connexion SSO attendue est absente.")

    soup = BeautifulSoup(response.text, "html.parser")
    root = soup.select_one('[data-testid="local-signin-page"]')
    if root is None or root.get("data-tenant-code") != TENANT:
        raise AuthenticationError("La page de connexion ou le tenant est inattendu.")
    redirect = root.get("data-redirect-url")
    if not isinstance(redirect, str) or not redirect:
        raise AuthenticationError("Le contexte de redirection SSO est absent.")
    authorize_url = urljoin(SSO + "/", redirect)
    if not _same_origin(authorize_url, SSO) or urlsplit(authorize_url).path != "/authorize":
        raise AuthenticationError("La destination d'autorisation SSO est inattendue.")

    result = client.post(
        SSO + "/api/identity/sign-in",
        params={"tenant-code": TENANT},
        json={"Login": login, "Password": get_password()},
        follow_redirects=False,
    )
    if result.status_code != 200:
        raise AuthenticationError(
            f"Le service de connexion a répondu avec le statut HTTP {result.status_code}."
        )
    try:
        payload = result.json()
    except ValueError as exc:
        raise AuthenticationError("La réponse de connexion n'est pas un JSON valide.") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("La réponse de connexion a un format inattendu.")
    # Le script du site normalise les clés camelCase et PascalCase.
    flags = {key[:1].upper() + key[1:]: value for key, value in payload.items()}
    if flags.get("RequiresTwoFactor") is True:
        raise AuthenticationError("Une double authentification est requise et n'est pas prise en charge.")
    if flags.get("IsLockedOut") is True:
        raise AuthenticationError("Le compte est verrouillé. Aucune nouvelle tentative automatique.")
    if flags.get("IsNotAllowed") is True:
        raise AuthenticationError("La connexion n'est pas autorisée pour ce compte.")
    if flags.get("Succeeded") is not True:
        raise AuthenticationError("Connexion refusée ou réponse de connexion inattendue.")

    response = client.get(authorize_url, follow_redirects=True)
    response.raise_for_status()
    # OpenID Connect peut renvoyer un formulaire auto-soumis (response_mode=form_post).
    if _same_origin(str(response.url), SSO):
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.select_one('form[method="post" i]')
        if form is not None:
            action = form.get("action")
            fields = {}
            for field in form.select('input[type="hidden" i]'):
                name, value = field.get("name"), field.get("value", "")
                if isinstance(name, str) and isinstance(value, str):
                    fields[name] = value
            callback_url = urljoin(str(response.url), action) if isinstance(action, str) else ""
            if not _same_origin(callback_url, EXTRANET) or "state" not in fields:
                raise AuthenticationError("Le formulaire de retour OpenID Connect est inattendu.")
            if not any(key in fields for key in ("code", "id_token", "error")):
                raise AuthenticationError("Le formulaire ne contient pas de réponse OpenID Connect.")
            response = client.post(callback_url, data=fields, follow_redirects=True)
            response.raise_for_status()

    if not _is_authenticated(response):
        raise AuthenticationError(
            "Le retour sur l'extranet n'est pas authentifié : contexte expiré ou parcours inattendu."
        )
    return response


def logout(client: httpx.Client) -> None:
    """Soumet le formulaire de déconnexion avec un jeton CSRF frais."""
    page = client.get(ENTRY_URL, follow_redirects=True)
    page.raise_for_status()
    if not _is_authenticated(page):
        raise LogoutError("Impossible de récupérer le formulaire de déconnexion authentifié.")
    soup = BeautifulSoup(page.text, "html.parser")
    token = soup.select_one(
        'form[action="/Ximi2/fasv/Account/SignOut"] '
        'input[name="__RequestVerificationToken"]'
    )
    value = token.get("value") if token is not None else None
    if not isinstance(value, str) or not value:
        raise LogoutError("Le jeton CSRF de déconnexion est absent.")

    response = client.post(
        EXTRANET + "/Ximi2/fasv/Account/SignOut",
        data={"__RequestVerificationToken": value},
        follow_redirects=False,
    )
    if response.is_error:
        response.raise_for_status()
    if not response.is_success and not (
        300 <= response.status_code < 400 and response.headers.get("location")
    ):
        raise LogoutError("La requête de déconnexion a retourné une réponse inattendue.")
    verification = client.get(
        EXTRANET + "/Ximi2/fasv/Interventions/List",
        follow_redirects=False,
    )
    location = verification.headers.get("location")
    signin_url = urljoin(str(verification.url), location) if location else ""
    if (
        verification.status_code not in (301, 302, 303, 307, 308)
        or not _same_origin(signin_url, EXTRANET)
        or urlsplit(signin_url).path != "/Ximi2/fasv/Account/SignIn"
    ):
        raise LogoutError("Le retour à la page de connexion après déconnexion n'est pas confirmé.")
    client.cookies.clear()


@contextmanager
def authenticated_session(login: str) -> Generator[httpx.Client, None, None]:
    """Fournit un client connecté et déconnecte le serveur à la sortie du bloc."""
    with httpx.Client(timeout=30.0) as client:
        authenticate(client, login)
        try:
            yield client
        finally:
            logout(client)
