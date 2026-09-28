# Synchro MariaDB / HFSQL / Google Sheets → PostgreSQL

Application qui copie des tables de bases **MariaDB/MySQL**, **HFSQL Client/Serveur** (PC SOFT) et des onglets de classeurs **Google Sheets**
vers des bases **PostgreSQL** à intervalle régulier, administrée depuis un **tableau de bord web**
avec historique et logs.

## Fonctionnalités

- **Connexions** : déclarez vos sources (MariaDB, HFSQL, Google Sheets) et cibles (PostgreSQL), testez-les en un clic.
  Pour une base, **« Lister les bases du serveur »** propose les bases existantes (un clic pour choisir) et,
  pour PostgreSQL, **« Créer une nouvelle base »** la crée directement (l'utilisateur doit avoir le droit
  `CREATEDB` : `ALTER ROLE mon_user CREATEDB;`).
  Les mots de passe sont chiffrés dans la base interne.
- **Jobs** : un job relie une source à une cible (et un schéma PostgreSQL) et s'exécute toutes les
  *N* secondes / minutes / heures / jours. Activation, désactivation et lancement manuel depuis l'interface.
- **Tables** : choisissez les tables à copier (ou « Ajouter toutes les tables ») et leur mode :
  - **Complet** : la table cible est vidée puis rechargée dans une seule transaction (jamais vue vide).
  - **Incrémental** : seules les lignes dont la *colonne de suivi* (`id` auto-incrémenté, `updated_at`…)
    a progressé sont lues. Avec une clé primaire (ou des colonnes clés saisies), les lignes sont
    insérées ou mises à jour (*upsert*) ; sans clé, elles sont ajoutées. Le curseur est sauvegardé après
    chaque lot : un job interrompu reprend où il s'était arrêté. Le curseur peut être réinitialisé.
- **Création automatique** du schéma et des tables cibles (types convertis : `UNSIGNED`, `ENUM`, `SET`,
  `TIME`, `YEAR`, `BIT`, dates `0000-00-00`, caractères NUL…). Une colonne ajoutée dans la source est
  ajoutée dans la cible au passage suivant.
- **Données** : liste des tables PostgreSQL avec leur nombre de lignes, le job qui les alimente et la
  comparaison avec la source (bouton « Comparer avec les sources »). Chaque table se consulte avec
  recherche, filtres par colonne (=, ≠, >, ≥, <, ≤, contient, commence par, vide / non vide), tri,
  pagination et export CSV des lignes filtrées.
- **Tableau de bord** : état des jobs, prochaines exécutions, succès/erreurs sur 24 h, lignes transférées.
- **Logs** : journal de chaque exécution et de chaque table, filtres (job, niveau, table, texte),
  pagination, export CSV. Purge automatique après `LOG_RETENTION_DAYS` jours.
- **Vider et réimporter** : depuis la page d'un job (« ⟲ Réimporter » sur une table, ou « ⟲ Tout réimporter »)
  ou depuis la page d'une table dans *Données*. La table PostgreSQL est vidée, le curseur remis à zéro et
  toutes les lignes sont rechargées depuis la source (option : recréer aussi la structure si des colonnes
  ou des types ont changé). L'opération est journalisée (« réimport complet » dans l'historique).
- Une table en erreur n'arrête pas les autres (état « Partiel ») ; un même job ne tourne jamais deux fois
  en parallèle.
- **Arrêter une exécution** : bouton « ■ Arrêter » (liste des jobs, page du job ou de l'exécution). La requête
  en cours est annulée (PostgreSQL, MariaDB, pilote HFSQL) ; les lots déjà écrits en mode incrémental sont
  conservés, la table en cours en mode complet revient à son état précédent. L'exécution passe à l'état
  « Arrêté » et le job peut être relancé aussitôt. Une exécution qui dépasse `MAX_RUN_MINUTES` est arrêtée
  automatiquement, et l'attente d'un verrou PostgreSQL est limitée à `LOCK_WAIT_MINUTES`. Ces deux délais
  se règlent aussi job par job (formulaire du job, rubrique « Protections »).
- **Relances automatiques** : après un arrêt automatique, le job est relancé après `RETRY_DELAY_MINUTES`
  minutes, jusqu'à `RETRY_MAX` fois (3 par défaut). Une exécution réussie remet le compteur à zéro. Si la
  dernière relance échoue encore, le job est **suspendu** (badge rouge, plus aucune exécution) jusqu'à ce
  qu'il soit réactivé. Un arrêt manuel ne déclenche pas de relance. Réglable job par job.

> Le mode incrémental ne répercute pas les suppressions faites dans la source. Pour une table où des
> lignes sont supprimées, utilisez le mode complet.

## Suivi des pointages (présences et retards)

Menu **« Suivi journalier »** : pour chaque employé et chaque jour, premier et dernier pointage, statut et
durées, à partir des tables copiées dans PostgreSQL (ex. `punchlog` et la table des employés de HRSmart).

**Mise en route (administrateur)** : *Administration → Source des pointages*. Choisissez la base PostgreSQL,
le schéma, la table des pointages (colonne employé, date/heure — ou date + heure dans deux colonnes, formats
HFSQL `AAAAMMJJ` / `HHMMSS` acceptés) et la table des employés (identifiant, matricule, service, éventuellement
une table des services et une colonne « actif »). Le **nom et le prénom** peuvent être dans la table des employés
ou dans une **autre table** (ex. `Personnel`, reliée par une colonne de la table des employés). La **hiérarchie**
(responsable N+1 de chaque employé) peut être dans la table des employés ou dans une table dédiée ; les colonnes
employé / responsable peuvent contenir la clé employé, le matricule ou l'identifiant de la table des noms.
« Enregistrer et installer » crée dans PostgreSQL :

| Objet | Rôle |
|---|---|
| `f_pointage_journalier(du, au)` | Fonction de calcul (utilisée par l'application et l'export) |
| `v_pointage_journalier` | Vue de tout l'historique, pour Power BI ou toute autre requête |
| `v_pointage_employes` | Employés : matricule, nom, prénom, service, actif, responsable |
| `f_pointage_equipe(responsable)` | Toute l'équipe d'un responsable (niveau 0 = lui, 1 = directs, 2…) |
| `v_pointage_brut` | Pointages bruts normalisés (employé, horodatage, jour) |
| `pointage_parametres` | Paramètres horaires historisés (valeur, date d'effet, auteur) |
| `pointage_jours_feries` | Jours fériés et fermetures (personne n'y est absent) |

**Règles de calcul** (par employé et par jour, avec les paramètres en vigueur ce jour-là) :

- P1 = premier pointage, P2 = dernier pointage (les pointages intermédiaires sont ignorés).
- **En retard** si P1 ≥ seuil de retard (7h45 compris), sinon **à l'heure**.
- **Durée validée** = (MIN(P2, fin de journée) − début validé) − pause déduite, où début validé = début de
  journée (7h30) si P1 < seuil de retard, sinon P1.
- **Durée effective** = (P2 − P1) − pause déduite.
- La pause (1h30 par défaut) n'est déduite que si la présence couvre la plage de pause (P1 < début de pause et
  P2 > fin de pause). Une durée n'est jamais négative.
- Un seul pointage : **pointage incomplet** (durées non calculées). Aucun pointage un jour ouvré : **absent** ;
  un jour non ouvré ou férié : **jour non ouvré** (pas d'absence).
- La liste part d'une **liste de référence** (*Source des pointages → Qui doit pointer ?*) : la table des
  employés, ou **tout le personnel de la table des noms** (ex. `Personnel`, y compris les personnes sans fiche
  dans la table des employés). Chaque personne active de cette liste apparaît chaque jour, qu'elle ait pointé ou
  non. La colonne « actif » (facultative) exclut les inactifs des absences. Un panneau de contrôle indique le
  nombre de personnes attendues, exclues, sans pointage, les badges hors liste et la date du **dernier pointage
  reçu** (alerte si la synchronisation semble arrêtée, aussi affichée dans le suivi). Les personnes qui ont pointé sans figurer dans la table des employés apparaissent aussi, marquées
  **« Hors liste »** (filtre « Personnes » et carte « Hors liste »).
- Après une mise à jour de l'application, la fonction et les vues PostgreSQL sont réinstallées automatiquement
  à la première ouverture du suivi (entrée dans le journal d'audit).

**Paramètres** (*Administration → Paramètres horaires*) : début/fin de journée, début/fin de pause, seuil de
retard, pause déduite, jours ouvrés. Chaque modification est enregistrée avec sa **date d'effet** et son auteur :
les jours antérieurs restent calculés avec les anciennes valeurs (une date d'effet passée recalcule depuis cette date).

**Écran** : cartes (présents, retards, absents, taux de ponctualité, moyennes), tableau coloré (vert à l'heure,
orange retard, rouge absent, gris incomplet), clic sur une ligne = tous les pointages bruts de la journée.
Filtres (jour ou période, employé, service, **équipe d'un responsable** — toute sa hiérarchie ou ses directs —,
statuts), colonne « Responsable », tri par colonne, filtres conservés dans l'URL (lien partageable),
**export Excel** de la vue filtrée.

**Périmètre par compte** : un lecteur ou un manager peut être rattaché à un employé (matricule) et limité à
**son équipe** : il ne voit (écran, détail, export) que lui-même et toutes les personnes placées sous lui.

**Rôles** (*Administration → Utilisateurs*) :

| Rôle | Accès |
|---|---|
| Lecteur | Suivi journalier et export Excel |
| Manager | + tableau de bord, jobs (consultation, lancement, arrêt), exécutions, logs, données |
| Administrateur | Tout : connexions, modification des jobs, paramètres, source des pointages, utilisateurs, audit |

Le compte défini dans `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`) reste un administrateur de secours.
Le **journal d'audit** (*Administration → Journal d'audit*) trace les connexions, les changements de
paramètres, d'utilisateurs, de configuration, de connexions et de jobs (qui, quand, avant → après).

## Source HFSQL Client/Serveur

La lecture passe par le **pilote ODBC HFSQL** de PC SOFT, à installer (gratuit) sur le serveur qui exécute
l'application, en **64 bits** (même architecture que Python). Dans **Connexions → + Source HFSQL** :
hôte du serveur HFSQL, port (**4900** par défaut), base, utilisateur (souvent `admin`) et mot de passe.

- Le pilote est détecté automatiquement (nom contenant « HFSQL » ou « HyperFile »). Le champ
  **Options ODBC** permet d'en imposer un (`DRIVER=HyperFileSQL`) et d'ajouter des paramètres, par
  exemple `Password=…` si les fichiers HFSQL sont protégés par mot de passe.
- Alternative : déclarez une **source ODBC système** (odbcad32 64 bits → DSN système) et indiquez
  `DSN=nom_de_la_source` dans Options ODBC ; le pilote y lit serveur, port et base.
- L'ouverture d'une connexion HFSQL peut être lente (plus d'une minute sur certains serveurs) : le délai
  est de 4 minutes (`HFSQL_CONNECT_TIMEOUT` dans `.env`, en secondes) et la connexion est **gardée ouverte**
  entre deux exécutions, pour ne payer ce délai qu'une fois.
- Sous Windows, HFSQL est lu via **.NET (System.Data.Odbc)** dans un processus PowerShell dédié
  (`app/odbc_bridge.ps1`) : le pilote HFSQL plante avec pyodbc (violation d'accès 0xC0000005) mais fonctionne
  avec .NET. `HFSQL_ENGINE=pyodbc` dans `.env` force l'ancien moteur.
- Le pilote ODBC HFSQL s'exécute dans un **processus séparé** : s'il plante ou bloque, seul ce processus
  est arrêté, le site reste disponible et l'erreur est affichée (délai des requêtes : `HFSQL_QUERY_TIMEOUT`,
  1800 s par défaut).
- Si le pilote ne fonctionne qu'avec un vrai compte Windows (il plante sous le compte SYSTEM), relancez
  l'installation avec `.\installer-windows-sans-docker.ps1 -ServiceAccount "DOMAINE\utilisateur"` : l'application
  tournera sous ce compte (mot de passe demandé, démarrage avec Windows sans session ouverte).
- En cas de blocage, lancez **`diagnostic-hfsql.bat`** dans une session Windows ouverte : il teste chaque
  étape (port, connexion ODBC, tables) avec sa durée, affiche une éventuelle fenêtre du pilote et écrit
  un rapport dans `logs\diagnostic-hfsql.txt`.
- Les tables et leurs types sont lus par ODBC ; la clé primaire est utilisée pour l'upsert si le pilote
  l'expose, sinon indiquez des colonnes clés. Les modes Complet / Incrémental et le réimport complet
  fonctionnent comme pour MariaDB. Les dates vides HFSQL deviennent `NULL`.
- Sous Docker, il faut aussi le pilote ODBC HFSQL pour Linux dans l'image ; l'installation Windows
  sans Docker est la plus simple pour HFSQL.

## Source Google Sheets

Chaque **onglet** du classeur devient une table PostgreSQL. La **première ligne** contient les en-têtes,
convertis en noms de colonnes simples (« Date d'arrivée » → `date_d_arrivee`). Les types (entier,
décimal, date, date-heure, booléen, texte) sont déduits des cellules ; une valeur incompatible avec le
type d'une colonne devient `NULL` et est signalée dans les logs. Le classeur est téléchargé une fois
par exécution du job.

Modes : **Complet** (la table reflète exactement l'onglet, suppressions comprises) ou **Incrémental**
avec une colonne de suivi (ex. une date) ; indiquez des **colonnes clés** (ex. `matricule`) pour mettre à
jour les lignes existantes (upsert). Les doublons de clé dans la feuille sont signalés, la dernière ligne
l'emporte.

Deux façons de donner l'accès, dans **Connexions → + Source Google Sheets** :

1. **Lien public** (le plus simple) : dans Google Sheets, *Partager → Accès général : Tous les
   utilisateurs disposant du lien (Lecteur)*, puis collez le lien du classeur.
2. **Compte de service** (classeur privé, recommandé pour des données sensibles) :
   - dans [Google Cloud Console](https://console.cloud.google.com/), créez un projet et activez
     l'**API Google Drive** ;
   - *IAM → Comptes de service* : créez un compte, puis *Clés → Ajouter une clé → JSON* ;
   - collez le contenu du fichier JSON dans la connexion (il est chiffré dans la base interne) ;
   - partagez le classeur (Lecteur) avec l'adresse e-mail du compte de service.

Le serveur doit pouvoir joindre `docs.google.com` / `www.googleapis.com` en HTTPS.

## Installation sur Windows (le plus simple)

1. Téléchargez le projet (bouton **Code → Download ZIP** sur GitHub) et extrayez-le, par ex. dans `C:\Pointage_Emp`.
2. Double-cliquez sur **`installer-windows.bat`** et acceptez la demande de droits administrateur.
3. Choisissez le mot de passe du compte `admin` quand il est demandé.

Le script s'occupe de tout :

- **Docker** : s'il est absent, active WSL 2 et installe Docker Desktop (un redémarrage peut être demandé ;
  l'installation reprend seule à l'ouverture de session suivante). S'il est présent mais arrêté, il le
  démarre ; s'il est en conteneurs Windows, il le bascule en conteneurs Linux.
- crée le fichier `.env` (mot de passe admin, clé secrète aléatoire, fuseau `Africa/Dakar`) ;
- construit et démarre l'application, vérifie qu'elle répond ;
- ouvre le port dans le pare-feu Windows et affiche l'adresse du tableau de bord.

Options (en PowerShell) : `.\installer-windows.ps1 -Port 8080 -OpenDatabasePorts` (`-OpenDatabasePorts`
ouvre aussi 3306 et 5432 si les bases sont sur ce serveur ; `-NoFirewall` ne touche pas au pare-feu).
Pour **mettre à jour**, remplacez les fichiers et relancez `installer-windows.bat` (le `.env` est conservé).
Le déroulé est enregistré dans `installation.log`.

> Pour une base installée sur le même serveur, saisissez l'hôte **`host.docker.internal`** dans
> l'écran *Connexions* (`localhost` désigne le conteneur). Docker Desktop démarre à l'ouverture de
> session Windows : sur un serveur, configurez une ouverture de session automatique.

### Serveur sans virtualisation : installation sans Docker

Si Docker Desktop affiche **« Virtualization support not detected »** (machine virtuelle sans
virtualisation imbriquée, VPS, virtualisation désactivée dans le BIOS), utilisez
**`installer-windows-sans-docker.bat`** :

- installe Python 3.12 si besoin, puis les dépendances dans un dossier `.venv` ;
- crée le `.env` (même principe que ci-dessus) ;
- enregistre l'application comme **tâche Windows lancée au démarrage du serveur** (compte SYSTEM,
  relancée automatiquement en cas d'arrêt) : aucune session ouverte n'est nécessaire ;
- ouvre le port dans le pare-feu. Journal technique : `logs\application.log`.

Sans Docker, une base installée sur le même serveur s'atteint avec l'hôte **`localhost`**.
Pour arrêter ou démarrer l'application : Planificateur de tâches → « Synchro MariaDB-PostgreSQL ».

## Démarrage rapide avec Docker

```bash
cp .env.example .env      # puis changez ADMIN_PASSWORD et SECRET_KEY
docker compose up -d --build
```

Ouvrez http://localhost:8000 et connectez-vous avec `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

Le `docker-compose.yml` démarre aussi une base MariaDB de démonstration (`pointage` : tables `employes`
et `pointages`) et une base PostgreSQL `dwh`. Pour les essayer, créez dans **Connexions** :

| Nom   | Type       | Hôte      | Port | Base       | Utilisateur / mot de passe |
|-------|------------|-----------|------|------------|----------------------------|
| src   | MariaDB    | `mariadb` | 3306 | `pointage` | `sync` / `syncpw`          |
| dwh   | PostgreSQL | `postgres`| 5432 | `dwh`      | `sync` / `syncpw`          |

puis un **job** `src → dwh`, ajoutez les tables et cliquez sur **▶ Lancer maintenant**.

En production, utilisez `compose.serveur.yml`, qui lance uniquement l'application :
`docker compose -f compose.serveur.yml up -d --build`.

## Installation sans Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Le planificateur tourne dans le processus web : lancez **un seul worker** (`--workers 1`), sinon
chaque worker exécuterait les jobs.

## Configuration

| Variable                | Défaut                     | Rôle |
|-------------------------|----------------------------|------|
| `ADMIN_USERNAME`        | `admin`                    | Identifiant du tableau de bord |
| `ADMIN_PASSWORD`        | `admin`                    | Mot de passe du tableau de bord (**à changer**) |
| `SECRET_KEY`            | `change-me-in-production`  | Signature des sessions (**à changer**) |
| `ENCRYPTION_KEY`        | dérivée de `SECRET_KEY`    | Clé Fernet de chiffrement des mots de passe des bases |
| `APP_DB_URL`            | `sqlite:///./data/app.db`  | Base interne (SQLite ou PostgreSQL) |
| `APP_TIMEZONE`          | `UTC`                      | Fuseau d'affichage des dates (ex. `Africa/Dakar`) |
| `LOG_LEVEL`             | `INFO`                     | `DEBUG` enregistre aussi chaque lot écrit |
| `LOG_RETENTION_DAYS`    | `30`                       | Conservation des logs et de l'historique |
| `BATCH_SIZE`            | `5000`                     | Lignes lues/écrites par lot |
| `SCHEDULER_MAX_WORKERS` | `4`                        | Jobs exécutés en parallèle au maximum |
| `MAX_RUN_MINUTES`       | `360`                      | Arrêt automatique d'une exécution plus longue (0 = sans limite) |
| `LOCK_WAIT_MINUTES`     | `10`                       | Attente maximale d'un verrou PostgreSQL (0 = sans limite) |
| `RETRY_MAX`             | `3`                        | Relances après un arrêt automatique, puis suspension du job (0 = aucune) |
| `RETRY_DELAY_MINUTES`   | `2`                        | Délai avant chaque relance automatique |

Si vous changez `SECRET_KEY` (ou `ENCRYPTION_KEY`), ressaisissez les mots de passe des connexions.

**Droits nécessaires** : `SELECT` sur la base source ; sur la cible, droit de créer le schéma et les
tables (ou tables déjà existantes avec une clé primaire / contrainte unique sur les colonnes clés).

## Structure

```
app/
  main.py          application FastAPI, authentification
  sync.py          moteur de synchronisation (types, modes complet/incrémental, upsert)
  scheduler.py     planification APScheduler, purge des logs
  models.py        connexions, jobs, tables, exécutions, logs
  routers/         pages : tableau de bord, connexions, jobs, exécutions, logs
  templates/       vues HTML (Jinja2)
docker/mariadb-init/   données de démonstration
tests/             tests unitaires, web et d'intégration
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Les tests d'intégration s'exécutent sur de vraies bases si ces variables sont définies :

```bash
export TEST_MARIADB_URL=mysql+pymysql://tester:testpw@127.0.0.1:3306/sync_test
export TEST_POSTGRES_URL=postgresql+psycopg://tester:testpw@127.0.0.1:5432/sync_test
.venv/bin/pytest
```
