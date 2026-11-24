# Implémentation de la synchronisation

Ce document décrit le fonctionnement actuel de la commande `planning-aux`.
Pour l'installation et les exemples d'utilisation, voir le
[README](../README.md).

## Composants

| Fichier | Responsabilité |
|---|---|
| [`cli.py`](../src/planning_aux/cli.py) | Arguments, journal, comparaison et orchestration |
| [`secrets.py`](../src/planning_aux/secrets.py) | Lecture des entrées `pass` |
| [`auth.py`](../src/planning_aux/auth.py) | Connexion et déconnexion Ximi/Xelya |
| [`planning.py`](../src/planning_aux/planning.py) | Sélection du mois et extraction des interventions |
| [`calendar.py`](../src/planning_aux/calendar.py) | Client officiel Google Calendar |

Le point d'entrée déclaré dans [`pyproject.toml`](../pyproject.toml) est
`planning_aux.cli:main`. La commande s'exécute une fois et n'installe pas de
planification périodique.

## Configuration et secrets

La commande accepte `--month AAAA-MM`, `--calendar-id` et `--log-file`.
Le mois par défaut est le mois courant à Paris. Sans identifiant d'agenda
explicite, il est lu dans `pass` sous `AuxiliaireDeViePlanning/calendar_id`.
Le login Ximi provient de `Service/FamilleService/login` et le mot de passe de
la première ligne de `Service/FamilleService`.

Le client Google reconstruit en mémoire les informations du compte de service
à partir des entrées `AuxiliaireDeViePlanning/type`, `private_key_id`,
`private_key`, `client_email`, `client_id` et `token_uri`. La clé privée
multiligne est conservée telle quelle. Le type et l'URL de jeton sont vérifiés.
La portée OAuth utilisée est `calendar.events`.

## Récupération du planning

1. `authenticated_session` ouvre un client HTTPX avec cookies, puis suit le
   parcours d'authentification SSO. Il vérifie notamment l'origine de la page,
   le tenant et la destination d'autorisation. Une double authentification n'est
   pas prise en charge.
2. `fetch_planning_html` ouvre la page des interventions et recherche le mois
   demandé dans les options mensuelles du formulaire. Il soumet le filtre avec
   son jeton CSRF, puis vérifie que le site confirme le mois sélectionné. Un
   mois que le site ne propose pas est refusé.
3. `parse_interventions` recherche les colonnes attendues et ignore les
   cellules réservées à l'affichage mobile. Chaque ligne produit une
   `Intervention` : identifiant numérique Ximi, intervenant facultatif, début,
   durée et indicateur de doublure. Les identifiants absents ou répétés, les
   dates et durées invalides et les produits non reconnus sont signalés.
4. À la sortie de la session, le programme soumet le formulaire de déconnexion
   avec un jeton CSRF frais. Il vérifie ensuite qu'une page protégée redirige
   vers la connexion, puis ferme le client.

Le planning est entièrement récupéré et analysé, et la déconnexion vérifiée,
avant l'accès à Google Calendar. Une table reconnue mais vide représente un
planning vide ; elle n'est pas assimilée à une erreur.

## Lecture de Google Calendar

`CalendarClient.list_events` demande les événements qui chevauchent les bornes
du mois à Paris, avec `singleEvents=True` pour développer les récurrences.
Toutes les pages sont lues ; une réponse ou un jeton de pagination invalide
interrompt le traitement. Les événements supprimés ne sont pas demandés.

La comparaison ne retient que les événements dont le **début** appartient au
mois demandé en `Europe/Paris`. Une occurrence commencée le mois précédent,
même si elle déborde sur le mois demandé, n'est pas supprimée.

Chaque événement retenu doit avoir un identifiant, un ETag, un début et une fin
horaires avec décalage de fuseau, ainsi qu'un titre reconnu :

- `Prestation — <intervenant>` ;
- `Prestation à pourvoir` si aucun intervenant n'est affecté ;
- l'un de ces titres suivi de ` — Doublure`.

Les événements sur une journée et les titres inconnus interrompent la
réactualisation : le programme ne peut pas les comparer sans ambiguïté.

## Comparaison et écritures

La fonction `_refresh` construit deux ensembles de clés composées du début
et de la fin en UTC et du nom de l'intervenant. Cela revient à comparer la
date, l'heure de début, la durée et l'intervenant. Le nom est comparé
exactement ; l'ID Ximi et le marqueur de doublure ne font pas partie de la clé.
Ainsi, un changement limité à la doublure ne modifie pas un événement existant.

Les horaires Famille Services sont interprétés en `Europe/Paris`. Une heure
locale inexistante ou ambiguë lors d'un changement d'heure, une durée non
positive, une prestation hors du mois, ou deux clés identiques d'un même côté
interrompent le traitement avant toute écriture.

Une fois les deux ensembles validés :

| Présence de la clé | Action |
|---|---|
| Planning et agenda | Aucun changement |
| Planning uniquement | Création d'un événement horaire |
| Agenda uniquement | Suppression de l'événement |

Les créations précèdent les suppressions. Les nouveaux événements reçoivent un
identifiant Google et conservent l'ID Ximi dans la propriété privée
`planningAuxSourceId` ; cette propriété ne limite pas les suppressions.
L'agenda doit donc être **réservé à Famille Services**, y compris lorsque son
contenu a été ajouté manuellement. Un planning vide valide entraîne la
suppression de tous les événements comparables du mois.

La suppression envoie l'ETag récupéré avec l'en-tête `If-Match` : Google refuse
la suppression si l'événement a changé depuis sa lecture. Les opérations ne sont
pas une transaction : si une création échoue, aucune suppression n'est lancée,
mais des créations précédentes peuvent avoir réussi ; si une suppression échoue,
des écritures précédentes peuvent déjà avoir réussi. Après correction, une
nouvelle exécution recalcule les différences.

`upsert_intervention_event` reste disponible comme méthode bas niveau historique
fondée sur l'ID Ximi, mais la commande de réactualisation ne l'utilise pas.

## Erreurs et vérification

Une `UnknownPlanningInfo` déclenche une entrée dans le journal et une fenêtre
indiquant son chemin ; sans interface graphique, le message est écrit sur
`stderr`. Le journal par défaut est
`~/.local/state/planning-aux/planning_aux.log`, créé avec les permissions
`0600`. Les erreurs techniques prises en charge sont signalées sur `stderr`
et journalisées. La commande renvoie `0` en cas de succès, `2` pour une
information inconnue et `1` pour les erreurs techniques prises en charge.

Les [tests](../tests/) utilisent des données synthétiques et des services
simulés. Ils couvrent notamment les formats, la pagination, les doublons, les
quatre critères de comparaison, les créations et suppressions, les conflits
d'ETag et la répétition d'une synchronisation. Ils n'effectuent pas d'écriture
sur les services réels.
