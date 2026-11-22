# AuxiliaireDeViePlanning

Synchronise les prestations de Famille Services (Ximi/Xelya) avec un agenda
Google Calendar. La commande récupère le planning mensuel, vérifie son format,
puis crée ou met à jour les événements correspondants.

## Prérequis

- Python 3.11 ou plus récent
- `pass` et une clé GPG initialisée
- Un compte Ximi Famille Services
- Un compte de service Google avec l'API Google Calendar activée
- L'agenda cible partagé avec le compte de service en lui accordant le droit
  de modifier les événements

## Installation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

La commande `planning-aux` est installée dans `.venv/bin/`.

## Configuration des secrets

Initialiser `pass` si ce n'est pas déjà fait :

```bash
gpg --full-generate-key
pass init <identifiant-de-la-cle-gpg>
```

Enregistrer le mot de passe Ximi et le login dans des entrées séparées :

```bash
pass insert Service/FamilleService
pass insert Service/FamilleService/login
```

L'entrée du mot de passe doit commencer par le mot de passe lui-même.

Enregistrer également l'identifiant de l'agenda Google cible :

```bash
pass insert AuxiliaireDeViePlanning/calendar_id
```

Le compte de service doit avoir le droit de modifier les événements de cet
agenda. L'identifiant peut aussi être passé avec `--calendar-id`.

Pour Google Calendar, importer depuis le JSON du compte de service les champs
suivants sous `AuxiliaireDeViePlanning/<nom-du-champ>` :

```text
type
project_id
private_key_id
private_key
client_email
client_id
auth_uri
token_uri
auth_provider_x509_cert_url
client_x509_cert_url
universe_domain
```

Chaque entrée contient la valeur correspondante ; `private_key` peut être
multiligne. Le programme lit ces entrées directement et ne recrée pas de JSON.
Ne versionnez jamais le JSON d'origine, une clé privée ou les exports de planning.

## Utilisation

Synchroniser le mois courant :

```bash
.venv/bin/planning-aux
```

Choisir un mois :

```bash
.venv/bin/planning-aux --month 2026-11
```

Le mois doit être au format `AAAA-MM` et être proposé par le sélecteur mensuel
du site. Le mois courant en `Europe/Paris` est utilisé par défaut. Pour préciser
un autre agenda ou un autre fichier de log :

```bash
.venv/bin/planning-aux \
  --month 2026-11 \
  --calendar-id <identifiant-agenda> \
  --log-file /chemin/vers/planning_aux.log
```

Sans `--calendar-id`, l'identifiant est lu depuis
`AuxiliaireDeViePlanning/calendar_id` dans `pass`.

Les rendez-vous sont créés aux heures du planning dans le fuseau
`Europe/Paris`. Le titre contient l'intervenant ; les prestations de doublure
sont marquées « Doublure ». Une prestation sans intervenant est affichée comme
« Prestation à pourvoir ». Les événements sont identifiés par l'ID source Ximi :
les synchronisations suivantes mettent à jour les événements existants au lieu
de les dupliquer. Les événements absents du planning ne sont pas supprimés.

## Formats inattendus et journal

Si une information du site n'est pas reconnue (structure du tableau, date,
durée, identifiant de prestation ou nouveau produit), la synchronisation
s'arrête avant toute écriture dans Google Calendar. Le diagnostic est écrit
dans :

```text
~/.local/state/planning-aux/planning_aux.log
```

Une fenêtre indique le chemin du journal. Sans interface graphique, le message
est affiché dans le terminal. Le fichier de journal est créé avec des
permissions privées.

## Tests

```bash
.venv/bin/python -m pytest
```

Les tests utilisent des réponses HTTP simulées et des données synthétiques ;
ils n'effectuent pas de connexion à Famille Services ni d'écriture dans Google
Calendar.

## Données privées

Les pages HTML capturées depuis Famille Services peuvent contenir des noms,
horaires, jetons de formulaire ou autres données de session. Conservez-les
localement et ne les publiez pas. Les secrets Google et Ximi doivent rester dans
`pass`, jamais dans le dépôt.
