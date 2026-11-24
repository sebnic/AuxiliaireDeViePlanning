# Planning auxiliaires — Document de conception

Outil en ligne de commande qui extrait les horaires d'auxiliaire de vie depuis
l'extranet Ximi (Xelya, « Familles Services ») et les enregistre dans Google Calendar.

## Vue d'ensemble

| Étape | Sujet | Statut |
|-------|-------|--------|
| 1 | Récupération du mot de passe (`pass`) | ✅ Fait |
| 2 | Authentification sur le SSO Xelya | Implémentée, validation réelle à faire |
| 3 | Scraping du planning | Implémenté, tests sur données synthétiques |
| 4 | Écriture dans Google Calendar | Réactualisation mensuelle implémentée |
| 5 | Orchestration / exécution planifiée | CLI implémentée |

Contraintes :

- Pas d'API pour Ximi → **scraping**.
- Authentification via le SSO `sso.xelya.io` (tenant `Ximi2/fasv`, comptes locaux, flux OpenID Connect avec `request_uri`).
- Le mot de passe et les paramètres privés sont conservés dans **pass**.

## Choix techniques généraux

- **Langage : Python ≥ 3.11** — bon écosystème pour le scraping (Playwright / httpx + BeautifulSoup)
  et l'API Google Calendar (`google-api-python-client`).
- **Structure** : package `src/planning_aux/`, tests `pytest` dans `tests/`.

```
pyproject.toml
src/planning_aux/
    secrets.py    # étape 1
    auth.py       # étape 2
    planning.py   # étape 3
    calendar.py   # client Google Calendar
    cli.py        # orchestration en ligne de commande
tests/
docs/conception.md
samples/          # pages HTML capturées pour le développement du scraper
```

Installation :

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

---

## Étape 1 — Récupération du mot de passe

### Choix

NordPass n'a ni CLI ni API pour les comptes personnels. Le mot de passe est donc recopié
une fois depuis NordPass dans [`pass`](https://www.passwordstore.org/) (chiffrement GPG),
et le programme se contente de le lire. Principe retenu : **le minimum**.

### Mise en place (manuelle, une fois)

```bash
sudo dnf install pass
gpg --full-generate-key          # si aucune clé GPG
pass init <email-gpg>
pass insert Service/FamilleService  # coller le mot de passe depuis NordPass
```

### Implémentation (`src/planning_aux/secrets.py`)

`get_password(entry="Service/FamilleService") -> str` exécute `pass show <entry>` (sans shell)
et renvoie la première ligne. Si l'entrée est absente ou si GPG ne peut pas déchiffrer,
`subprocess.CalledProcessError` est levée.

À noter pour l'étape 5 : en exécution non interactive (cron), `gpg-agent` doit avoir la
phrase de passe en cache.

### Tests

`tests/test_secrets.py` simule `subprocess.run` : vérifie la commande appelée, la première
ligne renvoyée et la propagation de l'erreur.

---

## Étape 2 — Authentification SSO Xelya

### Parcours observé

L'entrée stable est
`https://extranet.ximi.xelya.io/Ximi2/fasv/Account/SignIn?returnUrl=~%2Ffasv`.
Elle redirige vers le SSO avec un nouveau `request_uri`. Ne pas conserver une URL
SSO temporaire en configuration.

La page de connexion n'a pas de champ caché : son conteneur expose
`data-tenant-code`, `data-authorize-query` et `data-redirect-url`.
Son JavaScript envoie `{"Login": ..., "Password": ...}` en JSON à
`https://sso.xelya.io/api/identity/sign-in?tenant-code=Ximi2%2Ffasv`.
En cas de succès, il navigue vers `data-redirect-url` (résolu depuis la racine du SSO).

### Implémentation

`authenticate(client: httpx.Client, login: str) -> httpx.Response` dans
`src/planning_aux/auth.py` :

1. Ouvre l'entrée stable et récupère le contexte de connexion frais.
2. Vérifie l'origine SSO, le tenant et la destination d'autorisation avant de lire
   le mot de passe via `get_password()` (`Service/FamilleService`).
3. Envoie une seule tentative de connexion, sans réessai automatique.
4. Suit le parcours d'autorisation avec les mêmes cookies. Prend également en
   charge le retour OpenID Connect standard par formulaire POST caché, uniquement
   vers l'origine connue de l'extranet. Ce mode reste à confirmer sur le site réel.
5. Vérifie la présence du formulaire de déconnexion sur l'extranet avant de
   considérer la connexion comme réussie.

Pour l'utilisation normale, `authenticated_session(login)` conserve les cookies
pendant le traitement, puis appelle `logout(client)` dans un `finally` et ferme
le client HTTP. La déconnexion a donc lieu même si le traitement lève une exception :

```python
from planning_aux.auth import authenticated_session

with authenticated_session(login="VOTRE_IDENTIFIANT") as client:
    # Récupérer le planning avec client ici (étape 3).
    pass
# La déconnexion serveur a été effectuée avant la fermeture du client.
```

Fermer seulement le client HTTP ne libère pas la session côté serveur et peut
empêcher une connexion simultanée dans le navigateur. La déconnexion est effectuée
à la fin du traitement, pas immédiatement après l'authentification, afin de laisser
le temps de récupérer le planning.

`logout(client)` récupère une page authentifiée fraîche, extrait le champ caché
`__RequestVerificationToken` du formulaire de déconnexion, puis le soumet à
`/Ximi2/fasv/Account/SignOut` en POST avec les cookies. Ce formulaire et son jeton
ont été observés sur une page authentifiée. Le site redirige ensuite vers
`sso.xelya.io/logout`, qui peut répondre HTTP 400 si la redirection est suivie.
Le client ne suit donc pas cette redirection : il vérifie qu'une page protégée
de l'extranet redirige vers le formulaire de connexion avant d'effacer ses cookies.

Un jeton absent ou une déconnexion non confirmée lève `LogoutError` ; les erreurs
réseau/HTTP sont propagées. Si le traitement et la déconnexion échouent tous deux,
l'erreur de déconnexion est levée avec l'erreur initiale dans sa chaîne de contexte.
Le client est fermé même dans ce cas. L'API bas niveau `authenticate(client, login)`
reste disponible, mais son appelant doit appeler explicitement `logout(client)`
après utilisation.

Le mot de passe ne figure ni dans le code ni dans la documentation. L'identifiant
est stocké séparément dans `pass` sous `Service/FamilleService/login` et chargé
à l'exécution ; il ne figure pas dans le code. Aucune interface CLI n'est encore
définie.

`AuthenticationError` signale un refus, un compte verrouillé, un accès interdit,
un contexte inattendu ou un retour non authentifié. La double authentification
est détectée mais non implémentée. Les erreurs réseau/HTTP du parcours et les
erreurs de `pass` sont propagées.

### Validation

`tests/test_auth.py` utilise `httpx.MockTransport`, sans accès réseau ni vrais
identifiants : cookies SSO/extranet, redirections, retour OIDC par POST, refus et
contrôle du retour authentifié, déconnexion avec un jeton frais, nettoyage après
une exception de traitement et propagation des échecs de déconnexion.
Le parcours réel de connexion et de déconnexion a été validé avec le compte Ximi.
Le site redirige après déconnexion vers `sso.xelya.io/logout`, dont le suivi
échouait en HTTP 400 ; le code ne suit plus cette redirection et confirme la
déconnexion en vérifiant la redirection d'une page protégée vers le formulaire
de connexion.

## Étape 3 — Scraping du planning

La page est récupérée par GET sur
`https://extranet.ximi.xelya.io/Ximi2/fasv/Interventions/List` avec le client
authentifié. `fetch_planning_html(client)` retourne son HTML ; une erreur HTTP
ou une redirection vers une autre page est signalée explicitement.

`parse_interventions(html)` extrait une liste de `Intervention` :

| Champ | Type | Source |
|-------|------|--------|
| `intervenant` | `str \| None` | Nom complet, ou `None` si la cellule est vide (prestation à pourvoir) |
| `debut` | `datetime` | Date et heure au format `JJ/MM/AAAA HH:MM` |
| `duree` | `timedelta` | Durée `HH:MM` |
| `doublure` | `bool` | Présence du mot « doublure », sans distinction de casse, dans Produit |

Les autres informations (statut, demande, commentaire et libellé complet du
produit) ne sont pas conservées. Les prestations sans intervenant sont conservées.
Les heures restent les heures locales affichées,
sans fuseau assigné ; leur conversion pour Google Calendar reste à définir.

Le HTML contient des cellules `.visible-xs` pour le mobile et des fenêtres de
détails qui répètent les informations. Seules les cellules directes non mobiles
du tableau identifié par ses en-têtes sont lues. Une ligne invalide lève
`PlanningError` avec son numéro, sans être ignorée silencieusement. Un tableau
présent avec un corps vide retourne une liste vide ; une page sans tableau est
une erreur.

`get_interventions(login, month=None)` enchaîne connexion, récupération, extraction et
déconnexion via `authenticated_session`, y compris si l'extraction échoue :

```python
from planning_aux.planning import get_interventions

prestations = get_interventions(login="VOTRE_IDENTIFIANT")
novembre = get_interventions(login="VOTRE_IDENTIFIANT", month="2026-11")
# La session serveur est déconnectée avant le retour de la fonction.
```

Sans `month`, le filtre du site reste sur le mois courant. Pour choisir un mois,
`get_interventions` et `fetch_planning_html` acceptent le paramètre nommé `month`
au format `AAAA-MM` (par exemple `2026-11`) ou une valeur d'option mensuelle du
sélecteur `dateFilter` :

- `M-1` : mois précédent ;
- `M0` : mois courant ;
- `M1` : mois suivant ;
- `M2` : deuxième mois à venir.

Les valeurs `M-1`, `M0`, etc. sont relatives au mois courant du serveur, pas au
dernier mois choisi. Une date `AAAA-MM` est associée à partir du libellé de mois
affiché par le sélecteur. Seules les options effectivement proposées par la page
sont acceptées : leur disponibilité est vérifiée à chaque appel, sans supposer
que la plage reste fixe.
Le code ouvre d'abord la page par GET, récupère le jeton CSRF du formulaire de
filtrage (pas celui de déconnexion), puis envoie un POST à la même adresse avec
`dateFilter` et `__RequestVerificationToken`. Il conserve les cookies et vérifie
que la réponse marque le mois demandé comme sélectionné avant d'en extraire les
prestations. Un mois invalide ou non disponible lève `ValueError` ; un formulaire,
jeton ou résultat inattendu lève `PlanningError`. La déconnexion reste effectuée
à la sortie, même si la sélection échoue. La sélection par semaine reste hors périmètre.

`tests/test_planning.py` utilise un planning synthétique pour vérifier les
prestations, doublures, champs mobiles/desktop et erreurs de format, ainsi que
l'envoi du filtre mensuel avec son jeton CSRF, la confirmation du mois et le
nettoyage en cas d'échec. Les données réelles de planning ne sont pas incluses
dans le dépôt.

## Étape 4 — Google Calendar

### Demande comprise — stockage des informations Google dans `pass`

Le fichier JSON d'identifiants du compte de service contient des informations
destinées à l'accès à l'API Google Calendar. L'objectif est de
stocker les valeurs de ses différentes clés dans `pass`, sous un préfixe commun :

```text
AuxiliaireDeViePlanning/<nom de la clé>
```

Chaque clé doit donc avoir sa propre entrée, plutôt qu'une entrée unique contenant
le fichier JSON entier. Les noms exacts et la structure des entrées seront
déterminés après examen du fichier. Si le JSON contient des objets imbriqués,
des listes ou des valeurs multilignes, leur représentation devra préserver
l'intégralité des informations nécessaires à l'authentification.

Les valeurs sensibles ne doivent être ni affichées, ni copiées dans la
documentation ou le code source. Le stockage utilisera le chiffrement GPG de
`pass`. Une entrée existante ne devra pas être écrasée sans accord.

Après validation, l'import a été effectué : les 11 clés de premier niveau du JSON
ont chacune été enregistrées sous `AuxiliaireDeViePlanning/<nom de la clé>`.
Toutes les valeurs sont textuelles ; la clé privée conserve ses lignes.
Chaque entrée a été relue et comparée à la valeur importée sans afficher son
contenu. Aucune entrée existante n'a été écrasée. Une seconde vérification des
11 valeurs a confirmé leur conformité au fichier source, en tenant compte du
seul retour à la ligne final ajouté pour les valeurs qui n'en avaient pas.
Après cette vérification et sur demande, le fichier JSON source a été supprimé ;
son absence a été confirmée.

Le 22 novembre 2026, le compte de service a été remplacé par les identifiants
du nouveau JSON fourni. Les 11 entrées `pass` ont été mises à jour puis comparées
octet pour octet aux valeurs du fichier (avec un retour à la ligne final lorsque
nécessaire). La clé privée multiligne a également été vérifiée. Le JSON source a
été supprimé après validation, et son absence confirmée.

### Client Python Google Calendar

`CalendarClient(calendar_id)` dans `src/planning_aux/calendar.py` utilise
le client officiel `google-api-python-client` (`build("calendar", "v3", ...)`)
avec `google-auth` pour l'authentification. Les identifiants sont
ceux d'un compte de service. Ils sont reconstruits en mémoire depuis `pass`,
sans recréer de fichier JSON. `get_secret(entry)` lit l'entrée complète ;
contrairement à `get_password`, il préserve les lignes de la clé privée.
La portée OAuth est `https://www.googleapis.com/auth/calendar.events`.

Le compte de service doit avoir accès à l'agenda cible, partagé avec lui avec
le droit de modifier les événements. L'alias `primary` désigne l'agenda du
compte authentifié, donc ici celui du compte de service, et non automatiquement
l'agenda personnel de l'utilisateur.

```python
from datetime import date

from planning_aux.calendar import CalendarClient

with CalendarClient(calendar_id="IDENTIFIANT_AGENDA_PARTAGE") as client:
    event = client.create_all_day_event("Événement de tests", date(2026, 11, 18))
    confirmed = client.get_event(event["id"])
```

Le client ferme la ressource Google à la sortie du bloc. Les méthodes utilisent
`events().insert(...).execute()`, `events().get(...).execute()` et `events().patch(...).execute()`.
Les erreurs de `pass`, d'authentification et `googleapiclient.errors.HttpError`
sont propagées ; une réponse inattendue lève
`ValueError`. Les événements sur une journée utilisent une date de fin exclusive.
La CLI utilise `list_events`, `create_intervention_event` et `delete_event` pour
comparer et réactualiser les événements. L'ID Ximi est conservé dans les
propriétés privées des nouveaux événements, mais n'est pas un critère d'égalité.
Les dates sont envoyées avec le fuseau `Europe/Paris`. La suppression utilise
`If-Match` avec l'ETag lu afin de refuser un événement modifié entre-temps.

### Ligne de commande et synchronisation

Après installation éditable, lancer :

```bash
.venv/bin/planning-aux
.venv/bin/planning-aux --month 2026-11
.venv/bin/planning-aux --month 2026-12 --calendar-id IDENTIFIANT_AGENDA
```

Sans `--month`, la commande choisit le mois courant en `Europe/Paris`. Le login
est lu depuis `pass` (`Service/FamilleService/login`) et le mot de passe depuis
`Service/FamilleService`. L'agenda par défaut est lu dans
`AuxiliaireDeViePlanning/calendar_id` ; `--calendar-id` permet de le remplacer. Dans cet environnement,
la commande est invoquée via `.venv/bin/planning-aux` car le dossier virtuel
n'est pas ajouté automatiquement au `PATH`.

Les rendez-vous sont créés aux heures locales du planning, avec fin calculée par
la durée. Le titre est `Prestation — <intervenant>` ; une doublure ajoute
`— Doublure`. Une prestation non attribuée devient `Prestation à pourvoir`.
La comparaison porte sur la date, l'heure de début, la durée et l'intervenant,
sans tenir compte de l'ID source ni du marqueur de doublure. Un événement
identique reste inchangé, une prestation manquante est créée et un événement
absent est supprimé, y compris s'il a été créé manuellement. L'agenda doit donc
être réservé à Famille Services. Le mois est déterminé par le début en
`Europe/Paris` et les événements récurrents sont développés en occurrences.
Les titres Google doivent respecter le format décrit ci-dessus ; les événements
sur une journée et les titres non reconnus interrompent la synchronisation.
Les doublons identiques et les données inattendues interrompent aussi le
traitement avant toute écriture. Les créations précèdent les suppressions ; une
erreur pendant les écritures peut nécessiter une nouvelle synchronisation.

### Informations inconnues et journal

Un tableau ou une ligne non reconnue (en-têtes modifiés, date/durée ou identifiant
source invalide, nouveau libellé de produit) lève `UnknownPlanningInfo`.
L'intégralité du planning est analysée avant tout envoi à Google : en cas de
format inconnu, la synchronisation s'arrête sans créer d'événements partiels.
Une entrée est conservée si elle contient le mot « doublure » ; le libellé normal
connu actuellement est `2019 PCH - 2019 PCH`. Tout autre libellé déclenche
l'alerte afin d'éviter de le classer silencieusement comme non-doublure.

Le détail du diagnostic est ajouté au fichier
`~/.local/state/planning-aux/planning_aux.log` (permissions répertoire `0700`,
fichier `0600`). Une fenêtre d'avertissement affiche son chemin. Dans un
environnement sans interface graphique, le même message est envoyé sur stderr.
`--log-file` permet de choisir un autre emplacement.

### Validation Google Calendar

Les tests automatisés couvrent la lecture des identifiants, la pagination,
la comparaison des événements, la création et la suppression conditionnelle,
les doublons, les erreurs, le journal et l'interface CLI. Les tests ne
nécessitent aucun accès réel à Google Calendar.

## Étape 5 — Orchestration

L'orchestration synchrone est réalisée par `planning-aux` (`src/planning_aux/cli.py`).
Elle récupère le planning et les événements du mois demandé, les vérifie, puis
crée ou supprime les événements nécessaires dans Google Calendar. Les codes de
sortie sont `0` en succès, `1` en erreur technique et `2` lorsqu'une information
de planning inconnue nécessite une analyse. La planification périodique
(cron/systemd) reste à configurer. L'identifiant de l'agenda est lu depuis
`AuxiliaireDeViePlanning/calendar_id` dans `pass`, sauf si `--calendar-id` est
fourni explicitement.
