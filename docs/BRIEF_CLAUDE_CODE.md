# Brief de développement — HR Smart - Attendance

> **Mode d'emploi.** Ouvre Claude Code dans un dossier vide (ou un dépôt neuf), puis donne-lui ce fichier :
> « Lis `BRIEF_CLAUDE_CODE.md` et développe l'application en suivant les phases de la section 12.
> Après chaque phase : tests verts, commit, et un court compte rendu. »
> Le brief décrit **quoi** construire et **les règles exactes**. Pour les détails d'implémentation, Claude Code
> décide, en respectant la stack et les contraintes.

---

## 1. Objectif

Application web en français qui suit les **pointages** et la **présence** du personnel :

- elle **copie** les données des badgeuses et de l'outil RH (HR Smart) dans **PostgreSQL** ;
- elle **calcule** chaque jour, pour chaque employé, le premier et le dernier pointage, le **statut** (à l'heure,
  en retard, absent, congé…) et les **durées** ;
- elle gère les **exceptions** avec des circuits de validation : justification des retards, arrêts maladie,
  maternité et allaitement, activités extérieures ;
- elle **prévient** par e-mail : confirmations de badge, rappels de pointage, résumés aux responsables,
  relances des valideurs, alerte de site sans pointage ;
- elle produit des **rapports** exportables en Excel, PDF et PowerPoint.

Nom affiché : **HR Smart - Attendance** (constante unique `APP_NAME`). Interface 100 % en français.

## 2. Stack imposée

| Élément | Choix |
|---|---|
| Langage | Python 3.11 |
| Web | FastAPI + Jinja2 (rendu serveur, pas de SPA), sessions signées (itsdangerous) |
| Base interne | SQLAlchemy 2 ; SQLite par défaut (`APP_DB_URL`), PostgreSQL possible |
| Sources | PyMySQL (MariaDB/MySQL), pyodbc (HFSQL via ODBC), API Google Sheets et Smartsheet |
| Cible et calculs | PostgreSQL (psycopg 3) : fonctions SQL installées et **versionnées** par l'application |
| Planification | APScheduler 3 (BackgroundScheduler) |
| Exports | openpyxl (Excel), fpdf2 (PDF), python-pptx (PowerPoint, graphiques natifs) |
| Chiffrement | cryptography (Fernet) pour les mots de passe des connexions |
| Tests | pytest + TestClient ; bases PostgreSQL et MariaDB de test via variables d'environnement |
| Déploiement | Dockerfile + docker-compose (app, postgres, mariadb) ; script d'installation Windows sans Docker (venv + service) |

Pas de framework front : HTML + CSS maison (un seul `style.css`), un peu de JavaScript vanilla.

## 3. Configuration (.env)

`APP_DB_URL`, `SECRET_KEY` (sessions), `ENCRYPTION_KEY` (Fernet, sinon dérivée de SECRET_KEY),
`ADMIN_USERNAME` / `ADMIN_PASSWORD` (compte créé au premier démarrage), `APP_TIMEZONE`, `LOG_LEVEL`,
`LOG_RETENTION_DAYS` (30), `BATCH_SIZE` (5000), `SCHEDULER_MAX_WORKERS` (4), `SCHEDULER_ENABLED`,
`MAX_RUN_MINUTES` (360), `LOCK_WAIT_MINUTES` (10), `RETRY_MAX` (3), `RETRY_DELAY_MINUTES` (2).

## 4. Rôles et droits

Accès **refusé par défaut** : une table de règles (méthode, regex de chemin, rôle minimum) ; une page non prévue
est réservée à l'administrateur. Un refus redirige vers `/suivi` (303), et renvoie 403 pour une requête JSON.

| Rôle | Périmètre | Accès |
|---|---|---|
| `lecteur` (employé) | Tout le personnel, ou **« son équipe »** (lui + toute sa hiérarchie descendante) selon le compte | Suivi journalier, Rapports, Justification des retards, Arrêts maladie, Activités extérieures, Maternité et allaitement, Mon compte, Aide |
| `manager` (technique) | Comme le lecteur | + Synchronisation : tableau de bord, jobs, exécutions, logs, données |
| `rh` | Toujours tout le personnel | Menus du lecteur + **Temps hebdomadaire** ; voit la « Durée standard » ; saisies validées d'office |
| `admin` | Tout | Tous les menus, dont Ressources humaines, Mails et relances, Administration |

- Un compte est rattaché à un employé par son **matricule** : c'est ce qui en fait le N+1 de ses collaborateurs.
- Drapeau de compte **« agent RH habilité »** (`sick_leave_hr`) : valide les étapes RH des circuits.
- Le périmètre « son équipe » est appliqué **côté serveur**, y compris dans les exports.
- Connexion des employés sans compte : e-mail de leur fiche + **mot de passe standard** défini par l'admin ;
  le compte est créé à la première connexion et l'employé peut ensuite choisir son mot de passe.
- Mot de passe provisoire (créé ou réinitialisé par l'admin) : changement obligatoire à la connexion.

## 5. Synchronisation (sources → PostgreSQL)

- **Connexions** : MariaDB/MySQL, HFSQL (ODBC), Google Sheets, Smartsheet (sources) ; PostgreSQL (cible).
  Test en un clic, liste des bases du serveur, création d'une base PostgreSQL. Mots de passe chiffrés (Fernet).
- **Jobs** : source → cible + schéma, intervalle (secondes, minutes, heures, jours), activation, lancement manuel.
- **Tables** : mode **complet** (vidage + rechargement dans une transaction) ou **incrémental** (colonne de suivi,
  upsert sur clé, curseur sauvegardé après chaque lot, réinitialisable). Création automatique des tables cibles
  et des colonnes ajoutées (conversion des types MySQL : UNSIGNED, ENUM, SET, TIME, YEAR, BIT, dates `0000-00-00`, NUL).
- Une table en erreur n'arrête pas les autres (état « Partiel ») ; un job ne tourne jamais deux fois en parallèle.
- **Arrêt** manuel d'une exécution ; arrêt automatique après `MAX_RUN_MINUTES` ; attente de verrou limitée ;
  **relances** automatiques (`RETRY_MAX`, `RETRY_DELAY_MINUTES`) puis **suspension** du job.
- **Réimport** complet d'une table ou d'un job. **Données** : liste des tables, comptage, comparaison avec la
  source, consultation avec recherche, filtres par colonne, tri, pagination, export CSV.
- **Logs** par exécution et par table, filtres, export CSV, purge après `LOG_RETENTION_DAYS`.

## 6. Source des pointages (configuration, page admin)

L'admin désigne dans PostgreSQL (objet `Mapping` sérialisé en JSON) :

- **Pointages** : table, colonne employé, horodatage (ou date + heure séparées), **site du pointage** (colonne,
  ou clé vers une table de libellés).
- **Employés** : table, clé (= employé des pointages), matricule, nom, prénom (ou table « personnes » liée),
  service (ou table de libellés), colonne « actif » + valeurs actives, e-mail.
- **Dimensions** facultatives, chacune dans la table des employés ou des personnes, avec table de libellés :
  statut du personnel (cadre, non cadre…), **direction**, **site**. Correspondance des codes insensible à
  la casse, aux espaces et au suffixe `.0`.
- **Hiérarchie** : table (employé → responsable), par clé ou par matricule.
- **Demandes HR Smart** validées : congés (type annuel / exceptionnel), télétravail, missions — table, colonnes
  employé/début/fin/état, valeurs d'état « approuvée ».
- Bouton **« Installer les calculs »** : crée les objets PostgreSQL (section 7), avec un numéro `SQL_VERSION`
  réinstallé automatiquement quand il change. Diagnostics (directions non trouvées, population inactive…).

## 7. Objets PostgreSQL et règles de calcul

Objets créés dans le schéma choisi : `pointage_parametres` (clé, valeur, date d'effet, auteur),
`pointage_jours_feries`, `pointage_terrain` (agents terrain), `pointage_postes` (codes de poste),
`pointage_planning` (planning des agents postés), `pointage_arrets_maladie`, `pointage_maternite`,
`pointage_activites` ; vues `v_pointage_brut`, `v_pointage_employes`, `v_pointage_journalier` ; fonctions
`f_pointage_equipe(racine)` (hiérarchie descendante avec niveau) et **`f_pointage_journalier(du, au)`** :
une ligne par employé et par jour avec `emp_key, matricule, nom, prenom, service, responsable, jour, jour_ouvre,
premier_pointage, dernier_pointage, nb_pointages, statut, statut_libelle, debut_valide, fin_validee,
pause_deduite, duree_validee, duree_effective (+ en minutes), hors_liste, terrain, retard_min, categorie, poste,
direction, site, allaitement, site_premier, site_dernier, activite, retard_arrivee`.

### 7.1 Paramètres historisés (valeur par défaut)

Chaque paramètre a une **date d'effet** : le calcul d'un jour prend la valeur en vigueur ce jour-là ; un
changement peut être programmé.

| Clé | Défaut | Clé | Défaut |
|---|---|---|---|
| debut_journee | 07:30 | duree_conge | 08:00 |
| seuil_retard | 07:45 | duree_teletravail | 08:00 |
| debut_pause / fin_pause | 13:00 / 14:00 | duree_terrain | 08:00 |
| duree_pause_deduite | 01:30 | duree_arret_maladie | 08:00 |
| fin_journee | 16:30 | duree_maternite | 08:00 |
| jours_ouvres | 1,2,3,4,5 | duree_mission | 08:00 |
| objectif_duree (jour) | 08:00 | duree_repos | 08:00 |
| objectif_hebdo | 40:00 (jusqu'à 168 h) | heures_allaitement | 01:00 (01/02/03) |
| marge_poste | 04:00 | attribution_auto | TERRAIN,CONGE,TELETRAVAIL,ARRET_MALADIE,MISSION,CONGE_MATERNITE,REPOS |

Contrôles : début ≤ seuil ; début < début pause < fin pause < fin de journée.

### 7.2 Statut du jour (ordre de priorité)

Codes et libellés : `A_L_HEURE` À l'heure, `RETARD` En retard, `ABSENT` Absent, `INCOMPLET` Pointage incomplet,
`NON_OUVRE` Jour non ouvré, `CONGE_ANNUEL`, `CONGE_EXCEP`, `CONGE_MATERNITE`, `TELETRAVAIL`, `TERRAIN` Sur le
terrain, `ARRET_MALADIE`, `REPOS` Repos (planning), `FORMATION`, `MISSION` En mission, `ACTIVITE` Activité extérieure.

1. Planning posté : code de repos → `REPOS` ; formation / férié planifiés → leur statut.
2. Mission approuvée un jour travaillé (sans arrêt ni congé) → `MISSION`.
3. Poste de travail planifié : sans pointage → maladie, congé, télétravail, activité, sinon `ABSENT` ; un seul
   pointage (sans activité) → `INCOMPLET` ; activité couvrant le début du poste → `A_L_HEURE` ;
   1er pointage ≥ début du poste + tolérance + heures d'allaitement → `RETARD` ; sinon `A_L_HEURE`.
   Tolérance = seuil_retard − debut_journee. Les pointages se rattachent au poste dans ± marge_poste (nuits).
4. Sans pointage : jour ouvré → maladie, congé, télétravail, activité, terrain, sinon `ABSENT` ;
   jour non ouvré (ou férié) → télétravail sinon `NON_OUVRE`.
5. Agent terrain un jour ouvré → `TERRAIN` (ni retard, ni incomplet).
6. Un seul pointage (sans activité) → `INCOMPLET` (« pointage partiel » dans les indicateurs).
7. Activité extérieure le matin (début ≤ seuil et arrivée ≤ fin + tolérance) ou journée entière → `A_L_HEURE`.
8. 1er pointage ≥ seuil_retard + heures d'allaitement → `RETARD`, sinon `A_L_HEURE`.

Congés, télétravail et missions ne comptent que **validés dans HR Smart**. Un employé inactif n'est jamais absent.

### 7.3 Retards

- `retard_min` (statut RETARD) = 1er pointage − début de journée (ou début du poste) − heures d'allaitement.
- `retard_arrivee` = même calcul, **aussi pour un `INCOMPLET`** dont l'unique pointage dépasse la limite ;
  il déclenche la justification et **ne change pas** les indicateurs de retard.

### 7.4 Durées

- **Standard** (`duree_validee`) : de `debut_valide` (début de journée si arrivée avant le seuil, sinon l'arrivée)
  à `fin_validee` (min(départ, fin de journée)), moins la pause déduite si présent avant le début et après la fin
  de la pause. Poste planifié : du début du poste (ou de l'arrivée tardive) à min(départ, fin du poste) − pause du poste.
- **Effective** : dernier − premier pointage − pause.
- **Attribuées** (pour les situations cochées dans `attribution_auto`) : congé, télétravail, arrêt, mission,
  maternité, repos reçoivent leur durée paramétrée ; terrain : au moins `duree_terrain`, plus si les pointages le justifient.
- **Allaitement** : max(standard, min(objectif, temps travaillé + heures d'allaitement)).
- **Activité extérieure** : selon le paramètre du module, plage déclarée (sans pointage : la plage ; journée : le
  forfait) ou forfait par jour ; temps au bureau + partie de l'activité hors pointages, plafonné à l'objectif.
- **Après 16h30** (colonne d'écran, pas dans la fonction) : si ≥ 2 pointages, pas de poste et départ > 16h30 :
  dernier pointage − max(premier pointage, 16h30).

## 8. Écrans et fonctionnalités

### 8.1 Suivi journalier (`/suivi`)
- Jour ou période ; navigation jour par jour ; pagination (lignes par page au choix).
- Indicateurs : présents (dont incomplets), retards, absents, autres situations, hors liste, ponctualité,
  heures moyennes d'arrivée/départ, durées moyennes.
- Colonnes, dans l'ordre : [Date], Matricule, Nom (« Hors liste » si badge sans fiche), Statut du personnel,
  Direction, Service (site dessous), Responsable, 1er pointage (site dessous), **Statut**, Dernier pointage (site,
  orange s'il diffère ; « +1 j » de nuit), Durée standard (RH/admin), Durée effective (vert ≥ objectif, rouge
  sinon), **Après 16h30** (dernière). Toutes triables. Ligne « Moyenne » (présents au bureau).
- Sous le statut : poste planifié, activité validée (🎓) ou en attente (⏳), demande HR Smart en attente sur un
  absent, heures d'allaitement, **mention de justification du retard** et bouton **« Justifier »** (voir 8.5).
- Clic sur une ligne : tous les pointages bruts du jour (avec site).
- Filtres : employé, service, direction, site, statut du personnel (défaut paramétrable), « Équipe de » (+ N-1
  seulement), personnes de la liste / hors liste, horaires bureau / postés, statuts (cases), lignes par page.
- Exports Excel, PDF, PowerPoint respectant filtres et périmètre.
- Alertes : synchronisation arrêtée (dernier pointage ancien), population suspecte, badges sans fiche.

### 8.2 Rapports (`/rapports`)
- Périodes prédéfinies (7 j, 30 j, mois…) ou personnalisée (≤ 1 an), mêmes filtres que le suivi.
- KPI : taux de présence, d'absentéisme, de ponctualité, retards, minutes de retard, pointages partiels, heures
  standard (RH/admin) ; comparaison avec la période précédente.
- Graphiques (SVG serveur) : tendance par jour/semaine ; par jour de la semaine **avec taux de présence**.
- Tableaux par direction, statut du personnel, service, site ; tableau **par employé** (100 à l'écran) triable
  par absences, retards, minutes, pointages partiels, taux de présence, heures standard, **heures de télétravail**, nom.
- Exports Excel (toutes les feuilles), PDF, PowerPoint (graphiques natifs modifiables). Pas de « heures d'arrivée »
  ni « à suivre » ici.

### 8.3 Temps hebdomadaire (`/admin/hebdo`, RH + admin, menu Suivi)
Par employé et par semaine (lundi → dimanche) : somme des durées effectives vs `objectif_hebdo` (40h) ;
cellule verte/rouge, moyenne, écart moyen par semaine, taux d'atteinte, nombre de semaines sous l'objectif.
1, 2, 4, 8 ou 13 semaines ; filtres service, direction, employé, « seulement sous l'objectif » ; tri ; export Excel.

### 8.4 Analyses (`/admin/analyses`, admin)
Répartition des heures d'arrivée et « employés à suivre » (absences répétées, retards fréquents…).

### 8.5 Demandes et circuits de validation
Mécanisme commun : **circuit paramétrable** (liste ordonnée d'étapes : `responsable` = N+1 de l'employé,
`rh` = agents RH habilités, `utilisateur` = compte désigné), statuts `en_attente` / `valide` / `refuse` / `annule`,
historique des actions, pièce jointe (PDF/JPG/PNG ≤ 10 Mo, nom aléatoire, type vérifié, servie avec
`Content-Security-Policy: sandbox`), encadré « À valider » + **compteur dans le menu**, e-mails optionnels
(valideurs de l'étape, puis l'employé). Règles :
- N+1 sans compte rattaché → l'étape revient aux RH ; l'admin peut tout valider.
- Saisie par un RH → validée d'office ; par le responsable pour son équipe → d'office (option selon module).
- Refus = motif obligatoire. Annulation : l'auteur tant que c'est en attente ; les RH ensuite.
- Un éditeur de circuit partagé (ajout, ordre, suppression d'étapes).

| Module | Saisie | Circuit par défaut | Effet |
|---|---|---|---|
| **Justification des retards** (`/retards`) | jour, motif (liste paramétrable), explication, pièce jointe facultative ou obligatoire ; délai paramétrable (7 j) ; un seul justificatif actif par jour ; le jour doit avoir `retard_arrivee > 0` | N+1 | Sous le statut : « ✓ Justifié » (vert), « Justification non approuvée » (orange), « Justification refusée » (rouge, le bouton revient). Le retard **reste compté**. « Mes retards » liste les retards justifiables. Exports : « En retard — Justifié »… |
| **Arrêts maladie** (`/arrets`) | période, justificatif médical | N+1 → RH | copié dans `pointage_arrets_maladie` → statut ARRET_MALADIE |
| **Maternité et allaitement** (`/maternite`) | congé de maternité ou période d'allaitement (1, 2 ou 3 h/jour) | paramétrable | copié dans `pointage_maternite` |
| **Activités extérieures** (`/activites`) | type (liste : Formation, Réunion extérieure, Visite client, Autre), journée ou plage, plusieurs jours, lieu, objet, justificatif ; délai après coup (5 j) ; pas de chevauchement | au choix : **information du N+1 par e-mail** (prise en compte immédiate) ou validation par circuit | copié dans `pointage_activites` → ACTIVITE / pas de retard ni d'incomplet pendant l'activité / durée créditée |

Le bouton « Justifier » s'affiche dans le suivi quand `retard_arrivee > 0`, pour l'employé concerné et pour les RH.

### 8.6 E-mails (serveur d'envoi unique)
Page **Serveur d'envoi** (SMTP, TLS, expéditeur « HR Smart - Attendance », adresses de test, mode **test** /
**production** : en test, tout part vers les adresses de test avec un bandeau). Chaque envoi est journalisé.

| E-mail | Règles |
|---|---|
| Mails de badge | après chaque synchro, un mail par nouveau pointage aux abonnés ; jamais l'historique avant activation ; dédoublonnage (employé + horodatage) ; plafond par passage ; modèle HTML éditable ; onglets Paramètres / Abonnés / Modèle / Journal |
| Rappels de pointage | 5 messages activables à heure fixe : arrivée non pointée (avertissement, puis alerte avec N+1 en copie), départ à ne pas oublier, départ non pointé, pointages partiels des 3 derniers jours ; postés : relatif au poste ; exclut congés, arrêts, télétravail, terrain, missions |
| Résumés par mail | quotidien (veille) ou hebdo (semaine passée) aux responsables, équipe N-1 ou toute la hiérarchie ; un envoi par responsable/période ; aperçu et essai |
| Relances des validations | prochain valideur HR Smart des demandes en attente (congé, télétravail, mission) ; un mail groupé par jour ; espacement, maximum ; copie RH si valideur introuvable |
| Surveillance des sites | chaque jour à l'heure dite, alerte si un site n'a remonté aucun pointage |
| Information du N+1 | activité extérieure déclarée en mode « information » |

Préférences par employé (« Mon compte » : mails de badge, rappels) ; l'admin peut les fixer et les verrouiller.
Les pages Résumés et Rappels ont des onglets (Paramètres, Destinataires/Situation du jour, Aperçu et essai, Journal)
pilotés par l'ancre d'URL.

### 8.7 Administration
- **Ressources humaines** (admin) : Vue d'ensemble, Paramètres horaires (+ jours fériés, attribution auto avec
  date d'effet), Agents terrain, Horaires postés (codes de poste, import du planning Excel), circuits Maternité /
  Arrêts / Activités / Retards, **Vérifier un employé** (pourquoi absent ou non un jour donné).
- **Mails et relances** (admin) : section 8.6.
- **Administration** : Analyses, **Apparence** (fond de la page de connexion parmi 5 SVG : dégradé, vagues,
  rubans, réseau, nuit), Source des pointages, Utilisateurs, **Journal d'audit** (toute action sensible).
- **Aide** : page « Indicateurs et calculs » (définitions et formules avec les valeurs de paramètres en vigueur).
- Commande `python -m app.comptes_test` : crée `test.employe` (lecteur rattaché au responsable ayant le plus de
  N-1, périmètre équipe) et `test.rh`, mots de passe passés en argument.

## 9. Interface et charte

- Menu latéral à groupes repliables (mémorisés) : Suivi, Synchronisation, Ressources humaines, Mails et relances,
  Administration, Aide ; badges de compteur ; bas de menu : utilisateur + déconnexion ; version en pied de page.
- Responsive (menu burger sur téléphone), thème clair/sombre selon le système, tableau du suivi ajusté à la largeur.
- **Logo** HR Smart Attendance (monogramme « HR ») : menu, page de connexion (logo complet, pas de titre en double),
  favicon, bandeau des PDF et PowerPoint.
- Couleurs : bleu **#0D4D92** (primaire), turquoise **#13A8C6** (accent), menu **#071D3D** ; statuts : vert à
  l'heure, orange retard, rouge absent, gris incomplet, etc. ; tokens CSS en variables.

## 10. Sécurité (exigences, y compris les correctifs de l'audit du 02/10/2026)

- Mots de passe PBKDF2-SHA256 (≥ 240 000 itérations) ; secrets des connexions chiffrés ; `SECRET_KEY` obligatoire en production.
- Requêtes SQL paramétrées, identifiants échappés ; échappement Jinja ; jeton CSRF sur les formulaires POST.
- **À intégrer dès la reconstruction** : limitation des tentatives de connexion, HTTPS (reverse proxy) et cookies
  `Secure`/`HttpOnly`/`SameSite`, compte PostgreSQL dédié non superutilisateur, explorateur de données limité à
  l'admin, mot de passe standard désactivable et à usage unique, pièces jointes de santé chiffrées et sauvegardées,
  traçabilité des consultations de justificatifs médicaux.

## 11. Tests et critères d'acceptation

- Jeu de données de test (PostgreSQL + MariaDB) : ~7 employés, une hiérarchie, une semaine de pointages
  couvrant retard, absence, incomplet, terrain, poste de nuit, congé, télétravail, mission, férié.
- Tests automatisés obligatoires pour : chaque règle de statut et de durée (7.2 à 7.4), paramètres datés,
  droits de chaque rôle (pages autorisées et refusées), périmètre équipe (écran et exports), chaque circuit
  (création, validation, refus, annulation, N+1 sans compte), e-mails en mode test, exports (ouvrables, bonnes colonnes).
- Recette manuelle : suivre `docs/Cahier_de_recette_HR_Smart_Attendance.xlsx` (67 tests par rôle). Critère :
  aucun KO de priorité haute.

## 12. Phases de développement (un commit par phase, tests verts)

1. **Socle** : FastAPI, base interne, modèles, authentification, rôles, menu, charte, logo, page de connexion.
2. **Synchronisation** : connexions, jobs, planificateur, modes complet/incrémental, logs, données.
3. **Source des pointages + calculs PostgreSQL** : mapping, installation versionnée, paramètres datés, fériés,
   terrain, postes/planning ; tests de toutes les règles de la section 7.
4. **Suivi journalier** complet + exports Excel/PDF/PowerPoint.
5. **Rapports**, Temps hebdomadaire, Analyses, Aide.
6. **Demandes et circuits** : éditeur de circuit commun, puis retards, arrêts, maternité, activités.
7. **E-mails** : serveur d'envoi, mails de badge, rappels, résumés, relances, surveillance des sites, préférences.
8. **Administration** : utilisateurs, apparence, audit, vérifier un employé, comptes de test.
9. **Sécurité** (section 10), Docker, installateur Windows, README d'installation, recette.

## 13. Conventions

- Code et commentaires en français ; noms de variables en anglais ou français, mais cohérents.
- Une fonction PostgreSQL = source de vérité des calculs ; l'application ne recalcule pas les statuts en Python.
- Toute nouvelle colonne de modèle : migration légère au démarrage (ajout des colonnes manquantes).
- Numéro de version `AAAA.MM.JJ.N` dans `app/__init__.py`, affiché en pied de page.
- Ne jamais écrire de mot de passe, clé ou adresse réelle dans le code, les tests ou la documentation.
