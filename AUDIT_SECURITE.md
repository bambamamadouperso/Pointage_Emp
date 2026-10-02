# Audit de sécurité — application « Pointage »

| | |
|---|---|
| **Périmètre** | Dépôt `Pointage_Emp`, branche `claude/vigilant-franklin-8zc16p`, commit audité `91192d5` (version 2026.10.02.3) |
| **Date** | 02/10/2026 |
| **Méthode** | Revue de code complète (~11 700 lignes Python + templates), analyse de l'historique Git (71 commits, gitleaks 8.21.2), `pip-audit` 2.10.1, tests dynamiques **uniquement en local** (TestClient + instance uvicorn locale + bases PostgreSQL/MariaDB de test). Aucun test sur la production. |
| **Hors périmètre** | Le serveur de production lui-même (OS, `.env` réel, pare-feu effectif, version PostgreSQL, comptes réels) : des commandes de vérification sont fournies pour chacun de ces points. |

---

## 1. Résumé exécutif

L'application est **bien construite sur le plan applicatif « classique »** : aucune injection SQL trouvée (requêtes paramétrées, identifiants échappés), XSS neutralisée par l'échappement automatique de Jinja, contrôle d'accès refusé par défaut, périmètre « mon équipe » imposé côté serveur, mots de passe hachés en PBKDF2 (240 000 itérations), justificatifs médicaux bien cloisonnés (nom aléatoire, signature vérifiée, `CSP: sandbox`). Les dépendances ne présentent aucune vulnérabilité connue.

En revanche, **l'authentification et le déploiement sont les points faibles majeurs** :

1. **Le mot de passe standard commun** à tout le personnel permet à n'importe quel salarié de se connecter **sous l'identité d'un collègue** (un directeur, par exemple), et — si ce compte a été promu — **d'obtenir les droits administrateur** (prouvé en local).
2. **Aucune limitation des tentatives de connexion**, **aucun HTTPS**, port ouvert à tout le réseau : les mots de passe et cookies circulent en clair et peuvent être devinés sans frein.
3. **Un manager peut lire toutes les tables de toutes les bases** via l'explorateur `/data`, y compris le catalogue PostgreSQL ; l'application se connecte vraisemblablement à PostgreSQL avec le **superutilisateur `postgres`** et tourne sous Windows en **SYSTEM**.
4. **Les données de santé** (arrêts maladie) sont stockées en clair, sans sauvegarde ni traçabilité des consultations.

### Score global : **4 / 10** (risque élevé)

| Critique | Haute | Moyenne | Faible / Info |
|:-:|:-:|:-:|:-:|
| **3** | **14** | **16** | **14** |

> Le score remonte à **~7,5/10** une fois les étapes 1 à 4 du plan de correction appliquées (≈ 2 à 3 jours de travail), et à **~8,5/10** après l'étape 5.

---

## 2. Cartographie (phase 1)

| Élément | Détail |
|---|---|
| **Stack** | Python 3.12 (Docker) / 3.11 · FastAPI 0.141 / Starlette 1.7 · Uvicorn 0.54 (1 worker, HTTP) · Jinja2 3.1.6 (rendu serveur) · SQLAlchemy 2.1 · psycopg 3.3 · PyMySQL 1.2 · pyodbc 5.3 (HFSQL) · APScheduler 3.11 · cryptography 50 (Fernet) · openpyxl 3.1 |
| **Bases** | SQLite interne `data/app.db` (comptes, connexions chiffrées, arrêts, audit) · PostgreSQL cible (données synchronisées + tables/vues/fonctions `pointage_*`) · sources MariaDB / HFSQL / Google Sheets / Smartsheet |
| **Déploiement** | Windows : tâche planifiée `SYSTEM` + `app-service.cmd` (uvicorn `0.0.0.0`) **ou** Docker (`compose.serveur.yml`) · pas de reverse proxy, pas de TLS |
| **Entrées publiques** | `GET/POST /login`, `GET /health`, `/static/*` |
| **Entrées authentifiées** | ~100 routes : `/` `/runs` `/logs` `/data` `/jobs` (manager+), `/connections` `/admin/pointage|mails|users|audit` (admin), `/admin/parametres|feries|planning|terrain|arrets|resumes|verifier` (RH+admin), `/suivi` `/rapports` `/arrets` `/compte` (tous) |
| **Envois de fichiers** | justificatif d'arrêt (`POST /arrets`, tout utilisateur), classeurs de planning (50 Mo × N, RH), import agents terrain (RH) |
| **Sorties réseau** | bases de données (hôte libre), API Smartsheet (liste blanche d'hôtes), Google, SMTP |
| **Rôles** | compte de secours `.env` (admin, non désactivable) · `admin` · `rh` · `manager` · `lecteur` · comptes « employés » créés à la 1re connexion (e-mail + **mot de passe standard commun**) |
| **Données sensibles** | identifiants des bases/SMTP/jetons (chiffrés Fernet), hachages de mots de passe, **données de santé** (justificatifs + dates d'arrêt), pointages, hiérarchie, e-mails |

---

## 3. Tableau des failles

Les identifiants reprennent la phase où la faille a été trouvée : **V** = code (phase 2), **S** = secrets (phase 3), **D** = base de données (phase 4), **P/H** = dépendances et serveur (phase 5). ✅ = confirmé par un test dynamique local.

### 3.1 Critiques

| ID | Gravité | Catégorie | Fichier:ligne | Description | Impact | Correction proposée |
|---|---|---|---|---|---|---|
| **V-01** ✅ | Critique | Élévation de privilèges | `app/auth.py:79-84`, `app/staff_access.py:95` | Un compte « employé » (`auto_account`) sans mot de passe personnel accepte **toujours le mot de passe standard commun, quel que soit son rôle**. | Si un administrateur promeut ce compte (manager, RH, **admin**), tout salarié connaissant l'e-mail obtient ces droits : identifiants des bases, comptes, données de santé. | N'accepter le mot de passe standard que pour `role == "lecteur"` et `scope == "equipe"` ; à toute promotion, imposer `must_change_password=True` et `personal_password` requis. |
| **V-02** ✅ | Critique | Authentification | `app/staff_access.py:81-113`, `app/models.py:511` | Mot de passe standard **identique pour tout le personnel**, `force_change` désactivé par défaut ; e-mails devinables (prénom.nom@…). | Usurpation de n'importe quel collègue (y compris un directeur → visibilité sur toute sa hiérarchie, arrêts maladie de ses N-1). | Rendre `force_change` obligatoire et non désactivable ; à terme, remplacer par un lien d'activation à usage unique envoyé par e-mail. |
| **V-03** ✅ | Critique (si défaut) | Secret par défaut / sessions | `app/config.py:18,22`, `app/main.py:30-31` | `SECRET_KEY` par défaut `change-me-in-production`, `ADMIN_PASSWORD` par défaut `admin` ; l'application démarre quand même (simple avertissement). | Avec la clé connue : **cookie de session admin fabriqué sans mot de passe** (prouvé). La même clé chiffre les identifiants des bases. | Refuser de démarrer si `SECRET_KEY` < 32 caractères ou valeur par défaut, ou si `ADMIN_PASSWORD` est faible/par défaut. |

### 3.2 Hautes

| ID | Gravité | Catégorie | Fichier:ligne | Description | Impact | Correction proposée |
|---|---|---|---|---|---|---|
| **V-04** ✅ | Haute | Force brute / énumération | `app/main.py:135-152`, `app/auth.py:70-97` | Aucune limite d'essais (30 échecs puis succès) ; temps de réponse 182 ms (compte existant) vs 9 ms (inexistant). | Attaque par dictionnaire du compte de secours `admin` et du mot de passe standard ; liste des comptes existants. | Limitation par IP + identifiant (5 essais / 15 min, délai croissant), hachage factice si compte inconnu, alerte dans l'audit. |
| **V-05** ✅ | Haute | Contrôle d'accès | `app/auth.py:140`, `app/routers/data.py:133-215` | `/data/{conn}/{schéma}/{table}` (+ export CSV) ouvert au **manager** pour toute connexion, tout schéma, y compris `pg_catalog`. | Contournement des périmètres « équipe » et « RH » ; lecture des arrêts maladie, de l'annuaire, du catalogue (hachages si superutilisateur). | Réserver `/data` à l'admin, ou liste blanche des schémas/tables cibles des jobs ; interdire `pg_catalog`, `information_schema`. |
| **V-06** ✅ | Haute | Sessions | `app/main.py:77`, `app/main.py:155-158` | Session sans état côté serveur (cookie signé 12 h), **non révoquée** par la déconnexion ni par le changement de mot de passe ; cookie sans `Secure`. | Un cookie volé (réseau HTTP, poste partagé) reste valable 12 h, même après changement de mot de passe. | Version de session par utilisateur (incrémentée à la déconnexion / changement de mot de passe, vérifiée dans `account_state`) ; `https_only=True` ; inactivité 30-60 min. |
| **V-07** | Haute | Empoisonnement de lien (Host) | `app/routers/arrets.py:88` → `app/arrets.py:445`, `app/routers/admin.py:996` | Le lien des mails d'arrêt maladie est construit à partir de l'en-tête `Host` de la requête (accepté sans contrôle, cf. H-06). | Un employé déclare un arrêt avec `Host: site-pirate` : son N+1 et les RH reçoivent un mail officiel pointant vers une fausse page de connexion. | URL de base fixe (`APP_URL` dans `.env`) ; `TrustedHostMiddleware`. |
| **S-01** | Haute | Secrets | `.gitignore:4`, `.dockerignore:6` | Seul `.env` est ignoré ; les copies `.env.abime-<date>` créées par l'installeur (`installer-windows-sans-docker.ps1:251`) ne le sont pas. | Un `git add .` sur le serveur publie `SECRET_KEY` et `ADMIN_PASSWORD`. | Ignorer `.env*` avec `!.env.example` dans les deux fichiers. |
| **S-02** | Haute | Permissions fichiers | `installer-windows-sans-docker.ps1:351-365`, `installer-windows.ps1:338-339` | `.env`, ses copies et `data\` sont créés avec les droits par défaut (lisibles par tout utilisateur local). | Vol de `SECRET_KEY` → V-03 (sessions forgées) + déchiffrement des identifiants. | `icacls .env /inheritance:r /grant:r "SYSTEM:F" "Administrators:F"` (idem `data\`) ; supprimer les copies une fois la clé récupérée. |
| **D-01** | Haute | Moindre privilège BD | `app/sync.py:242-260`, `app/routers/suivi.py:32-41`, `app/pointage.py:841-905` | Un **seul compte PostgreSQL** pour la lecture web, l'écriture de synchronisation et le DDL (un lecteur sur `/suivi` peut déclencher `DROP/CREATE`). Les erreurs réelles conservées dans les tests montrent un usage probable de **`postgres`** (superutilisateur). | Toute faille web = contrôle total de PostgreSQL (lecture `pg_authid`, `COPY … TO PROGRAM` → exécution de commandes sur le serveur de base). | Deux rôles : `pointage_sync` (écriture, DDL de son schéma) et `pointage_web` (`SELECT` sur vues, `EXECUTE` fonctions, écriture limitée aux tables `pointage_*`) ; jamais `postgres`. |
| **D-10** | Haute | Données de santé | `app/arrets.py:153-175`, `app/models.py:384-430` | Justificatifs (PDF/images) et arrêts (dates, commentaires) **stockés en clair**. | Fuite de données de santé (RGPD art. 9) en cas d'accès au disque ou aux sauvegardes. | Chiffrement Fernet des justificatifs (clé dédiée `ARRETS_KEY`) ; BitLocker ; ACL sur `data\`. |
| **D-11** | Haute | Sauvegardes | (absent) | Aucune sauvegarde de `data/app.db`, `data/arrets/` et des tables `pointage_*` (planning, paramètres, fériés, terrain — **qui n'existent nulle part ailleurs**). | Perte définitive en cas de panne disque, rançongiciel ou erreur. | Sauvegarde quotidienne chiffrée hors serveur (`sqlite3 .backup`, `pg_dump -n <schéma>`, copie `data\arrets`) + test de restauration ; `.env` stocké séparément. |
| **H-01** | Haute | Transport | `app-service.cmd:13`, `Dockerfile:17`, `compose.serveur.yml:9` | HTTP en clair sur `0.0.0.0`, pas de TLS ni de HSTS. | Interception des mots de passe (dont le mot de passe standard), des cookies et des données de santé sur le réseau. | Reverse proxy HTTPS (IIS+ARR, Caddy ou nginx) ; uvicorn sur `127.0.0.1`. |
| **H-02** | Haute | Durcissement OS | `installer-windows-sans-docker.ps1:286-288` | L'application tourne sous **SYSTEM**, `RunLevel Highest`. | Toute exécution de code dans l'application ou une dépendance = contrôle total du serveur Windows ; élévation locale si le dossier de l'application est modifiable par des non-administrateurs. | Compte de service dédié non administrateur (option `TASK_ACCOUNT`) ; dossier en lecture seule pour ce compte sauf `data\` et `logs\`. |
| **H-03** | Haute | Exposition réseau | `installer-windows-sans-docker.ps1:417-423`, `installer-windows.ps1:373-388` | Règle de pare-feu sans restriction d'adresse ni de profil ; option `-OpenDatabasePorts` ouvrant 3306/5432 à tous. | Page de connexion (sans limite d'essais) et bases accessibles depuis tout le réseau, y compris invités/VPN. | `-RemoteAddress <sous-réseaux>` + `-Profile Domain,Private` ; jamais 3306/5432 au-delà des serveurs applicatifs. |
| **H-05** ✅ | Haute* | Déni de service | `app/main.py:135` (absence de limite) | Corps de requête non limité, **même sans être connecté** : 60 Mo multipart acceptés sur `/login` (303 en 0,56 s, écrits en fichier temporaire). | Saturation bande passante / disque temporaire / CPU par des envois répétés (aggravé par l'absence de limite de débit). | Limite de taille au reverse proxy (`1m` sur `/login`, `60m` sur les imports) + middleware refusant un `Content-Length` excessif ; limitation de débit. |
| **S-04** | Haute* | Configuration par défaut | `docker-compose.yml:20-37` | Compose de démonstration : mots de passe `rootpw`/`syncpw`, ports 3306/5432 publiés, `.env` facultatif → application en `admin`/`admin` + `SECRET_KEY` par défaut. | Si utilisé tel quel en production : prise de contrôle immédiate (V-03). | Marquer « démo uniquement », `required: true` sur `.env`, ports des bases retirés ou liés à `127.0.0.1`. |

\* Classées Haute ici en raison du cumul avec d'autres constats (pas de limite de débit, exposition réseau large).

### 3.3 Moyennes

| ID | Gravité | Catégorie | Fichier:ligne | Description | Impact | Correction proposée |
|---|---|---|---|---|---|---|
| **V-08 / H-04** ✅ | Moyenne | En-têtes de sécurité | `app/main.py` | Aucun en-tête CSP, X-Frame-Options, nosniff, Referrer-Policy, HSTS ; en-tête `server: uvicorn`. | Clickjacking ; aucune défense en profondeur contre une XSS future. | Middleware d'en-têtes (CSP `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, HSTS) ; `--no-server-header`. |
| **V-09** | Moyenne | CSRF | `app/main.py:77`, tous les formulaires | Pas de jeton CSRF, seule protection : `SameSite=Lax` ; CSRF de connexion possible. | Actions forcées depuis un sous-domaine du même site ou un navigateur ancien. | Jeton CSRF par session (champ caché + vérification dans le middleware pour tout POST) ; vérification de l'en-tête `Origin`. |
| **V-10** | Moyenne | Envois de fichiers | `app/routers/arrets.py:58`, `app/routers/admin.py:614-623` | La limite (10 Mo / 50 Mo) est vérifiée **après** la réception complète ; classeurs Excel non protégés contre les « bombes zip ». | Saturation disque par tout employé connecté (`/arrets`) ; blocage du processus par un classeur piégé (RH). | Limite de taille en amont (proxy/middleware) ; contrôle de la taille décompressée des `.xlsx` avant `openpyxl`. |
| **V-11** | Moyenne | Exfiltration / phishing | `app/routers/resumes.py:51-68`, `:123` | Le rôle RH peut envoyer un résumé d'équipe à **n'importe quelle adresse** et choisir l'URL du bouton (`app_url`). | Fuite de données nominatives par mail ; lien piégé dans les mails aux managers. | Limiter les essais aux domaines de l'entreprise ; `app_url` réservé à l'admin ou fixé dans `.env`. |
| **H-06** ✅ | Moyenne | Host header | `app/main.py` (pas de `TrustedHostMiddleware`) | Tout `Host` accepté (`Host: site-pirate.example` → 200). Cause de V-07. | Liens piégés, empoisonnement de caches intermédiaires. | `TrustedHostMiddleware(allowed_hosts=[...])`. |
| **H-07** | Moyenne | Journalisation | `app-service.cmd:13` | `--no-access-log` ; `logs\application.log` sans rotation, droits par défaut. | Impossible de reconstituer une intrusion ; croissance illimitée du fichier. | Journal d'accès au reverse proxy ; rotation ; ACL. |
| **H-08** | Moyenne | Conteneur | `Dockerfile:1-17`, `compose.serveur.yml` | Conteneur **root**, sans `read_only`, `cap_drop`, `no-new-privileges` ; port publié sur toutes les interfaces. | Compromission de l'application = root dans le conteneur, code modifiable. | `USER app`, `read_only: true` + `tmpfs`, `cap_drop: [ALL]`, `no-new-privileges`, `127.0.0.1:8000:8000`. |
| **S-03** | Moyenne | Gestion des clés | `app/crypto.py:10-15` | Sans `ENCRYPTION_KEY`, la clé de chiffrement des identifiants est dérivée de `SECRET_KEY`. | Une fuite de `SECRET_KEY` + `app.db` = tous les mots de passe des bases et du SMTP. | `ENCRYPTION_KEY` Fernet distincte + rechiffrement des identifiants. |
| **D-02** | Moyenne | Fonctions SQL | `app/pointage.py:621`, `:645` | Fonctions sans `SET search_path`. | Détournement des calculs si un rôle peut créer des objets dans `public`. | `SET search_path = pg_catalog, <schéma>` ; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`. |
| **D-03** | Moyenne | Transport BD | `app/sync.py:242-260`, `app/dbadmin.py:23-35` | Pas de `sslmode` (PostgreSQL), pas de TLS (MariaDB). | Identifiants et données en clair entre serveurs. | Option `sslmode=verify-full` / `ssl` dans le formulaire de connexion. |
| **D-04** | Moyenne | Intégrité | `app/models.py:275`, `app/auth.py:74-75` | `users.email` et `users.emp_matricule` non uniques ; la connexion par e-mail prend le 1er compte trouvé. | Ambiguïté de compte, rattachements en double. | Index unique sur `lower(email)` (hors vides) et refus des doublons dans `user_save`. |
| **D-05** | Moyenne | Intégrité | `app/models.py:258,265` | `role` / `scope` sans contrainte `CHECK`. | Rôle incohérent en cas d'écriture directe en base. | `CheckConstraint` sur les valeurs autorisées. |
| **D-06** | Moyenne | Intégrité | `app/sync.py:600-680` | Mode incrémental sans colonne clé = insertion seule. | **Doublons de pointages** après relance ou recouvrement du curseur. | Exiger une clé en mode incrémental (ou index unique cible). |
| **D-12** | Moyenne | Traçabilité | `app/routers/arrets.py:99-111`, `:169-179` | Consultation d'un arrêt et téléchargement d'un justificatif non journalisés. | Pas de traçabilité des accès aux données de santé (RGPD). | `auth.audit(...)` dans `detail` et `attachment`. |
| **P-01** | Moyenne | Dépendances | `requirements.txt:1-15` | Versions non figées (`>=`), pas de verrouillage ni de hachages. | Versions de production inconnues et jamais mises à jour ; installation non reproductible. | `pip-compile --generate-hashes`, `--require-hashes`, `pip-audit` mensuel. |
| **P-02** | Moyenne | Chaîne d'approvisionnement | `installer-windows-sans-docker.ps1:145`, `installer-windows.ps1:222` | Installeurs Python / Docker téléchargés et exécutés en admin sans vérification d'empreinte. | Exécution d'un binaire altéré (proxy compromis, etc.). | `Get-FileHash` (SHA-256 publié) ou `Get-AuthenticodeSignature`. |

### 3.4 Faibles / Info

| ID | Gravité | Catégorie | Fichier:ligne | Description | Correction proposée |
|---|---|---|---|---|---|
| **V-12** | Faible | Fuite d'information | `app/main.py:102-108` | La page 500 affiche le message d'exception (SQL, hôte) à tout utilisateur connecté. | Détail réservé à l'admin ; identifiant d'erreur pour les autres. |
| **V-13 / H-09** | Faible | Fuite d'information | `app/main.py:203-210` | `/health` public avec version exacte. | `{"status":"ok"}` seulement. |
| **V-14** | Faible | Injection de formules | `app/routers/suivi.py:238`, `app/explorer.py:201-210` | Exports Excel/CSV : une valeur commençant par `=`, `+`, `-`, `@` devient une formule. | Préfixer ces valeurs d'une apostrophe. |
| **V-15** | Faible | Autorisation | `app/routers/admin.py:1021` | Compte créé à la main : périmètre « tous » par défaut. | Défaut « équipe » pour le rôle lecteur. |
| **V-16** | Faible | Logique | `app/routers/admin.py:1081-1090`, `app/auth.py:83` | Réinitialisation admin sans effet sur un compte employé utilisant le mot de passe standard. | Marquer `personal_password=True` à la réinitialisation. |
| **V-17** | Faible | Politique de mot de passe | `app/auth.py:40-43` | 8 caractères minimum, pas de liste noire, pas de MFA (même admin). | 12 caractères + liste noire ; TOTP pour admin/RH. |
| **S-05** | Faible | Secrets | `app/config.py:18,22` | Valeurs par défaut des secrets dans le code. | Aucune valeur par défaut (voir V-03). |
| **S-06** | Info | Fuite d'information | `app/templates/admin/resumes.html:49`, `tests/test_errors.py` | Adresse IP interne réelle du serveur dans un placeholder et des tests. | Remplacer par `http://serveur:8000`. |
| **S-07** | Info | Cache | `app/routers/connections.py:127,141` | Mot de passe saisi renvoyé dans le formulaire en cas d'erreur, sans `no-store`. | `Cache-Control: no-store`. |
| **D-07** | Faible | Intégrité | `app/pointage.py:1891-1898`, `:873-879`, `:842-849` | Pas de `CHECK (du <= au)`, pas de FK planning → postes, pas d'unicité `(cle, date_effet)`. | Ajouter ces contraintes. |
| **D-08** | Faible | Migrations | `app/database.py:44-55` | Migration « légère » sans contraintes ni défauts. | Alembic. |
| **D-09** | Faible | Concurrence | `app/staff_access.py:22-28` | Tables de réglages « ligne unique » sans contrainte. | Ligne `id=1` imposée. |
| **D-13** | Faible | Journalisation | `app/scheduler.py:97-107`, `app/models.py:289` | Audit jamais purgé, modifiable par quiconque accède à `app.db`. | Rétention (1 an) ; export vers un serveur de logs. |
| **P-03** | Faible | Conteneur | `Dockerfile:1`, `docker-compose.yml:23,34` | Images non figées par empreinte. | `@sha256:`. |

### 3.5 Points forts vérifiés (à conserver)

- Requêtes SQL paramétrées ; identifiants échappés par `qi()` / `lit()` (`app/pointage.py:263-268`) ; explorateur en SQLAlchemy Core.
- Échappement automatique Jinja ; aucun `|safe` sur une donnée utilisateur ; mails échappés (`app/mails.py:184-191`).
- Contrôle d'accès refusé par défaut (`app/auth.py:149`), rôle relu en base à chaque requête.
- Périmètre « équipe » appliqué côté serveur (suivi, détail, rapports, exports) ; contrôles par objet sur les arrêts (`can_see`, `can_act`).
- PBKDF2-SHA256 240 000 itérations, comparaisons en temps constant, session régénérée à la connexion, `back_url` sans redirection externe.
- Justificatifs : extension + signature binaire, nom UUID, anti-traversée, `CSP: sandbox`, `nosniff`, `no-store`.
- Identifiants des sources chiffrés (Fernet) et jamais réaffichés.
- Historique Git propre : aucun secret réel (gitleaks + revue manuelle) ; pip-audit : 0 vulnérabilité.
- `/static` sans listage ni traversée ; OpenAPI désactivé.

---

## 4. Scénarios d'attaque et tests de vérification (phase 6)

> **Tous les tests ci-dessous sont non destructifs et à lancer sur l'environnement de TEST** (copie de l'application, base de test). Remplacer `<test>` par l'URL de test (ex. `http://serveur-test:8000`). Ils sont écrits pour PowerShell (`curl.exe`) ou bash ; « Attendu après correction » donne le résultat qui prouve que la faille est corrigée.

### Scénario A — Usurpation d'un collègue puis prise de contrôle (V-02 → V-01 → V-05 → D-01)

**Attaquant :** un salarié quelconque, sur le réseau interne.

1. Il connaît le mot de passe standard (communiqué à tous) et devine l'e-mail de son directeur (`prenom.nom@entreprise`).
2. Il se connecte sous son identité : il voit les pointages de toute la direction et les arrêts maladie dont le directeur est N+1.
3. Si l'administrateur a promu ce compte en `manager` / `admin` sans que le directeur ait choisi son mot de passe, l'attaquant hérite de ces droits (**prouvé en local : `/connections` → 200**).
4. En manager, il ouvre `/data/<conn>/pg_catalog/pg_roles`, puis toutes les tables synchronisées et `pointage_arrets_maladie` ; si la connexion utilise `postgres`, il lit aussi `pg_authid`.

**Ce qu'il obtient :** données RH et de santé de toute l'entreprise, identifiants des bases ; potentiellement le serveur PostgreSQL.

**Test de vérification (test) :**
```powershell
# 1) Mot de passe standard sur le compte d'un collègue (accès du personnel ouvert, compte "employé" sans mot de passe personnel)
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}\n" -c c.txt -d "username=<email_collegue>&password=<mot_de_passe_standard>" <test>/login
#    Attendu après correction : connexion acceptée UNE seule fois puis redirection forcée vers /compte (mot de passe personnel exigé)
# 2) Promouvoir ce compte en manager dans l'écran Utilisateurs, se déconnecter, puis rejouer la commande 1
#    Attendu après correction : refus (303 vers /login)
# 3) Explorateur avec un compte manager
curl.exe -s -o NUL -w "%{http_code}\n" -b c.txt <test>/data/1/pg_catalog/pg_roles
#    Attendu après correction : 303 vers /suivi (refusé) ou message « schéma non autorisé »
```
```sql
-- 4) Sur PostgreSQL (lecture seule) : le compte de l'application est-il superutilisateur ?
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = '<utilisateur_appli>';
-- Attendu après correction : rolsuper = f pour les deux rôles (pointage_web, pointage_sync)
```

### Scénario B — Devinette du compte de secours (V-04, H-03, V-03)

**Attaquant :** toute machine du réseau (y compris Wi-Fi invités si non isolé).

1. `/health` lui donne la version ; le temps de réponse lui confirme que `admin` existe (182 ms vs 9 ms).
2. Il lance un dictionnaire sur `admin` sans aucun blocage.
3. Si `ADMIN_PASSWORD` est faible, il obtient l'admin de secours (non désactivable). Si `SECRET_KEY` vaut la valeur par défaut, il n'a même pas besoin du mot de passe : il **fabrique** le cookie de session.

**Ce qu'il obtient :** administration complète.

**Tests de vérification (test) :**
```bash
# Limitation des essais : 10 mauvais mots de passe puis le bon
for i in $(seq 1 10); do curl -s -o /dev/null -w "%{http_code} " -d "username=admin&password=faux$i" <test>/login; done; echo
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" -d "username=admin&password=<bon_mdp>" <test>/login
#   Attendu après correction : les derniers essais renvoient 429, et le bon mot de passe est refusé pendant la période de blocage

# Énumération par le temps de réponse
for u in admin nexistepas; do curl -s -o /dev/null -w "$u %{time_total}\n" -d "username=$u&password=x" <test>/login; done
#   Attendu après correction : temps comparables (écart < 30 ms)
```
```python
# Falsification de session avec la clé par défaut (pip install itsdangerous)
import base64, json, itsdangerous, urllib.request
cookie = itsdangerous.TimestampSigner("change-me-in-production").sign(
    base64.b64encode(json.dumps({"user": "admin", "role": "admin"}).encode())).decode()
req = urllib.request.Request("<test>/admin/users", headers={"Cookie": f"session={cookie}"})
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
try:
    print(urllib.request.build_opener(NoRedirect).open(req).status)   # 200 = VULNÉRABLE
except urllib.error.HTTPError as e:
    print(e.code)                                                       # 303 = clé non par défaut (OK)
# Attendu après correction : l'application REFUSE DE DÉMARRER avec la clé par défaut
```

### Scénario C — Écoute réseau et rejeu de session (H-01, V-06)

**Attaquant :** une personne sur le même réseau (poste compromis, port miroir, Wi-Fi).

1. Le trafic HTTP en clair lui livre mots de passe et cookies `session=…`.
2. Il rejoue le cookie depuis son poste : valable 12 h, **même après déconnexion et changement de mot de passe de la victime** (prouvé en local : `/jobs` → 200).

**Test de vérification (test) :**
```powershell
curl.exe -sI http://<serveur>:8000/login          # Attendu après correction : connexion refusée ou redirection 301 vers https://
curl.exe -sI https://<serveur>/login | findstr /i "strict-transport set-cookie"
#   Attendu : Strict-Transport-Security présent ; cookie avec "secure"
# Rejeu : 1) se connecter dans un navigateur, copier le cookie "session" ; 2) cliquer Déconnexion ;
# 3) rejouer le cookie copié :
curl.exe -s -o NUL -w "%{http_code}\n" -H "Cookie: session=<cookie_copié>" https://<serveur>/suivi
#   Attendu après correction : 303 vers /login
```

### Scénario D — Mail piégé via l'en-tête Host (V-07, H-06)

**Attaquant :** un employé connecté.

1. Il déclare un arrêt maladie en forçant `Host: portail-rh-entreprise.com` (domaine qu'il contrôle).
2. Son N+1 et les RH reçoivent le mail **officiel** « Arrêt à valider » dont le bouton mène à sa fausse page de connexion.

**Ce qu'il obtient :** les identifiants d'un manager ou d'un RH.

**Test de vérification (test, mails en mode TEST) :**
```powershell
curl.exe -s -o NUL -w "%{http_code}\n" -H "Host: site-pirate.example" <test>/login
#   Attendu après correction : 400 (Invalid host header)
# Puis déclarer un arrêt de test depuis le navigateur et vérifier, dans le mail reçu sur l'adresse de test,
# que le lien commence par l'URL configurée (APP_URL) et non par l'hôte de la requête.
```

### Scénario E — Déni de service sans authentification (H-05, V-10)

**Attaquant :** toute machine du réseau.

1. Envois répétés de corps multipart de 60 Mo sur `/login` : acceptés et écrits sur disque (prouvé : 303 en 0,56 s).
2. En parallèle, saturation du processus unique (1 worker) et du disque temporaire.

**Test de vérification (test, une seule requête — ne pas boucler) :**
```bash
head -c 5000000 /dev/zero > gros.bin
curl -s -o /dev/null -w "%{http_code}\n" -F username=admin -F password=x -F f=@gros.bin <test>/login
#   Attendu après correction : 413 (Request Entity Too Large)
```

### Scénario F — Clickjacking et CSRF (V-08, V-09)

1. Une page piégée intègre l'application dans une iframe invisible et fait cliquer l'utilisateur connecté sur « Valider l'arrêt » ou « Lancer le job ».

**Test de vérification (test) :**
```powershell
curl.exe -sI <test>/login | findstr /i "x-frame-options content-security-policy x-content-type-options referrer-policy"
#   Attendu après correction : les 4 en-têtes présents (X-Frame-Options: DENY, CSP avec frame-ancestors 'none')
# CSRF : POST sans jeton depuis un compte connecté
curl.exe -s -o NUL -w "%{http_code}\n" -b c.txt -d "notify=1" <test>/admin/arrets/notifications
#   Attendu après correction : 403 (jeton CSRF manquant)
```

### Scénario G — Fuite de secrets sur le serveur (S-01, S-02, S-03)

1. Un utilisateur local du serveur (ou un script de sauvegarde mal configuré) lit `.env` ou une copie `.env.abime-*`.
2. Avec `SECRET_KEY` : sessions forgées (scénario B) ; avec `app.db` en plus : déchiffrement de tous les identifiants des bases et du SMTP.

**Test de vérification (serveur, lecture seule) :**
```powershell
Get-ChildItem -Force "<dossier_appli>\.env*"            # Attendu : uniquement .env (et .env.example)
icacls "<dossier_appli>\.env"; icacls "<dossier_appli>\data"
#   Attendu : uniquement SYSTEM / Administrators / compte de service — pas de "Users" ni "Everyone"
git -C "<dossier_appli>" status --ignored --short | findstr ".env"
#   Attendu : toutes les variantes .env* listées comme ignorées (!!)
```

### Scénario H — Compromission du serveur Windows (H-02, H-03)

1. Une faille future dans l'application, une dépendance ou le pilote ODBC donne l'exécution de code : comme le processus tourne sous **SYSTEM**, l'attaquant contrôle tout le serveur (et peut se propager).
2. Si des utilisateurs standard peuvent écrire dans `.venv\` ou `app\`, ils remplacent un fichier Python et obtiennent SYSTEM au redémarrage de la tâche.

**Test de vérification (serveur, lecture seule) :**
```powershell
Get-ScheduledTask | Where-Object TaskName -like "*ynchro*" |
  Select-Object TaskName, @{n="Compte";e={$_.Principal.UserId}}, @{n="Niveau";e={$_.Principal.RunLevel}}
#   Attendu après correction : compte de service dédié, niveau "Limited"
icacls "<dossier_appli>\.venv" | findstr /i "Users Everyone Utilisateurs"
#   Attendu : aucune ligne avec droit (M) ou (W) pour ces groupes
Get-NetFirewallRule -DisplayName "Synchro*" | Get-NetFirewallAddressFilter | Select-Object RemoteAddress
#   Attendu : sous-réseaux précis, pas "Any"
```

---

## 5. Plan de correction (une étape à la fois)

Chaque étape est indépendante, testable avec la checklist (section 6) et déployable seule. Ordre = rapport gain de sécurité / effort.

### Étape 0 — Vérifications immédiates sur le serveur (aucun code, < 1 h)
1. Vérifier le `.env` réel : `SECRET_KEY` aléatoire ≥ 32 caractères, `ADMIN_PASSWORD` robuste, `ENCRYPTION_KEY` renseignée (V-03, S-03).
2. Supprimer ou protéger les copies `.env.abime-*` ; appliquer les ACL `icacls` sur `.env` et `data\` (S-01, S-02).
3. Exécuter la requête SQL `rolsuper` (D-01) et noter le résultat.
4. **Changer le mot de passe administrateur HFSQL** apparu dans une capture d'écran.
5. Si l'accès du personnel est ouvert : cocher « Exiger un mot de passe personnel à la 1re connexion » (atténue V-02 en attendant l'étape 1).

### Étape 1 — Authentification (code, ~1 jour) — *Critiques V-01, V-02, V-03 + V-04*
1. Mot de passe standard limité à `lecteur` + périmètre « équipe », jamais pour un compte promu ; `force_change` imposé (V-01, V-02).
2. Refus de démarrage si secrets par défaut/faibles (V-03, S-05).
3. Limitation des essais de connexion par IP et identifiant + hachage factice (V-04).
4. Tests automatisés associés (le projet a déjà une suite de 122 tests).

### Étape 2 — Contrôle d'accès et sessions (code, ~1 jour) — *V-05, V-06, V-07, H-06*
1. `/data` réservé à l'admin + liste blanche des schémas (V-05).
2. Version de session par utilisateur, révocation à la déconnexion / changement de mot de passe, inactivité courte (V-06).
3. `APP_URL` configurée pour tous les liens de mails + `TrustedHostMiddleware` (V-07, H-06).

### Étape 3 — Transport et exposition réseau (infra, ~½ jour) — *H-01, H-03, H-05, H-07*
1. Reverse proxy HTTPS (certificat interne), uvicorn lié à `127.0.0.1`, cookie `https_only` (H-01).
2. Pare-feu restreint aux sous-réseaux des postes ; fermer 3306/5432 (H-03).
3. Au proxy : limite de taille (1 Mo sur `/login`, 60 Mo sur les imports), limite de débit, journal d'accès avec rotation (H-05, H-07).

### Étape 4 — Base de données et données de santé (BD + code, ~1 jour) — *D-01, D-10, D-11, D-12*
1. Créer `pointage_sync` et `pointage_web`, retirer `postgres` des connexions (D-01) ; `SET search_path` sur les fonctions, `REVOKE CREATE ON SCHEMA public` (D-02).
2. Chiffrer les justificatifs (clé dédiée) ; BitLocker (D-10).
3. Mettre en place la sauvegarde quotidienne chiffrée + test de restauration (D-11).
4. Journaliser la consultation des arrêts et justificatifs (D-12).

### Étape 5 — Durcissement applicatif (code, ~1 jour) — *V-08, V-09, V-10, V-11, V-12*
1. Middleware d'en-têtes de sécurité (V-08/H-04).
2. Jetons CSRF + vérification `Origin` (V-09).
3. Contrôle de la taille décompressée des `.xlsx` (V-10).
4. Restreindre les envois d'essai RH et `app_url` (V-11) ; page 500 sans détail pour les non-admins (V-12) ; `/health` minimal (V-13).

### Étape 6 — Système et chaîne d'approvisionnement (infra, ~½ jour) — *H-02, H-08, P-01, P-02, S-04*
1. Compte de service dédié non administrateur + ACL du dossier (H-02).
2. Dockerfile non-root, compose durci ; compose de démo marqué et sécurisé (H-08, S-04).
3. `requirements.lock` avec hachages + `pip-audit` mensuel ; vérification des installeurs (P-01, P-02).

### Étape 7 — Intégrité et finitions (code, ~1 jour) — *D-04 à D-09, D-13, V-14 à V-17, S-06, S-07, P-03*
Contraintes d'unicité/CHECK, clé obligatoire en incrémental, Alembic, échappement des formules dans les exports, politique de mot de passe + MFA admin/RH, rétention de l'audit, nettoyage des IP internes.

---

## 6. Checklist finale de re-test

À dérouler sur l'environnement de **test** après chaque étape, puis une fois en production (lecture seule).

### Authentification et sessions
- [ ] Mot de passe standard : accepté au plus une fois, puis changement imposé (Scénario A-1)
- [ ] Compte employé promu manager/admin : mot de passe standard refusé (Scénario A-2)
- [ ] 10 échecs de connexion → blocage temporaire (429), y compris pour `admin` (Scénario B)
- [ ] Temps de réponse identiques compte existant / inexistant (Scénario B)
- [ ] L'application refuse de démarrer avec `SECRET_KEY` ou `ADMIN_PASSWORD` par défaut (Scénario B)
- [ ] Cookie rejoué après déconnexion → `/login` ; après changement de mot de passe → `/login` (Scénario C)
- [ ] Cookie de session avec attributs `HttpOnly; Secure; SameSite=Lax`

### Contrôle d'accès
- [ ] Manager : `/data/...` refusé ; `pg_catalog` refusé pour tous (Scénario A-3)
- [ ] Lecteur « équipe » : `/suivi`, `/suivi/detail`, `/rapports`, exports limités à sa hiérarchie (tests existants `test_account_limited_to_team`)
- [ ] RH : accès `/admin/parametres|planning|terrain|arrets|resumes`, refus `/connections`, `/jobs`, `/admin/pointage|users|mails|audit` (test existant `test_rh_role_sees_everyone_without_sync`)
- [ ] Jeton CSRF manquant → 403 (Scénario F)

### Transport, en-têtes, réseau
- [ ] HTTP redirigé vers HTTPS ; HSTS présent (Scénario C)
- [ ] `X-Frame-Options`, `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy` présents ; pas d'en-tête `server: uvicorn` (Scénario F)
- [ ] `Host` inconnu → 400 ; liens des mails = `APP_URL` (Scénario D)
- [ ] Corps de 5 Mo sur `/login` → 413 (Scénario E)
- [ ] `/health` ne renvoie que `{"status":"ok"}`
- [ ] Pare-feu : `RemoteAddress` restreint ; 3306/5432 fermés depuis un poste client (`Test-NetConnection <serveur> -Port 5432`)

### Base de données et données de santé
- [ ] `rolsuper = f` pour les comptes utilisés par l'application (Scénario A-4)
- [ ] `pointage_web` ne peut ni `CREATE` ni `DROP` (`SELECT has_schema_privilege('pointage_web', '<schéma>', 'CREATE')` = f)
- [ ] Justificatif sur disque illisible directement (fichier chiffré) ; toujours consultable via l'application
- [ ] Consultation d'un arrêt / d'un justificatif visible dans le journal d'audit
- [ ] Sauvegarde de la veille présente hors serveur ; restauration testée sur une machine de test

### Secrets et système
- [ ] `.gitignore` / `.dockerignore` couvrent `.env*` ; aucune copie `.env.abime-*` sur le serveur (Scénario G)
- [ ] ACL : `.env` et `data\` accessibles uniquement à SYSTEM / Administrators / compte de service (Scénario G)
- [ ] Tâche planifiée sous compte de service dédié, niveau « Limited » ; `.venv\` non modifiable par les utilisateurs (Scénario H)
- [ ] `ENCRYPTION_KEY` distincte de `SECRET_KEY`
- [ ] Mot de passe administrateur HFSQL changé

### Dépendances
- [ ] `pip-audit` sur le serveur : 0 vulnérabilité ; installation depuis `requirements.lock` avec hachages
- [ ] gitleaks sur l'historique : 0 secret réel (`gitleaks git . --log-opts="--all"`)
- [ ] Suite de tests du projet verte (`pytest`, avec `TEST_POSTGRES_URL` et `TEST_MARIADB_URL`)
