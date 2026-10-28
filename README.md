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
- **Durée standard** = (MIN(P2, fin de journée) − début validé) − pause déduite, où début validé = début de
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
**export Excel** et **export PDF** (A4 paysage, filtres en en-tête, statuts colorés, objectif en vert/rouge, ligne « Moyenne ») de la vue filtrée.

**Horaires postés** (*Administration → Horaires postés*) : pour les agents en équipes (3×8, 2×12…), le planning
Excel (ex. « PLANNING ATF » : légende des codes, ligne `MATRICULE`, une ligne par jour) est importé tel quel.
Les jours planifiés remplacent l'horaire de bureau : retard et durée standard calculés sur le poste du jour
(P1 06h–14h, 1806 18h–06h…), poste de nuit compté le jour où il commence (départ du lendemain rattaché, marge
réglable « marge avant/après un poste »), repos `P4` → « Repos (planning) », congé `P11` → « Congé Annuel »,
formation → « Formation », absence possible un week-end planifié. Tables PostgreSQL `pointage_postes`
(codes modifiables) et `pointage_planning` ; colonne `poste` dans `v_pointage_journalier`. La grille affiche les heures
planifiées de chaque agent ; au-delà de 48 h par semaine en moyenne, l'agent est signalé (à l'import et en rouge).

**Relances des validations** (*Administration → Relances des validations*, rôles RH et admin) : une demande de
congé (ou de télétravail, de mission) non validée n'est pas prise en compte dans le pointage, l'employé apparaît
« Absent ». Chaque jour choisi, à l'heure choisie, le **prochain validateur** de chaque demande en attente reçoit un
seul e-mail qui regroupe ses demandes (employé, période, état, étape, jours déjà comptés absents) avec un bouton
« Valider dans HR Smart » (adresse paramétrable).
- Par table de demandes : statuts « en attente » (ex. « En Validation, Validé Par N1 »), colonne du matricule du
  prochain validateur (ex. `ProchainvalidateurMatricule`), colonnes facultatives de l'étape et de la date de demande.
- Rythme : première relance après N jours d'attente, puis tous les N jours, nombre maximal par demande ; seules les
  demandes déjà commencées ou qui commencent bientôt (fenêtre paramétrable) sont relancées.
- Adresses RH en copie (à partir de N jours comptés absents) ; elles reçoivent aussi les demandes dont le validateur
  est introuvable ou sans adresse. Même SMTP et même mode test/production que les mails de badge ; journal des envois.
- Dans le suivi journalier, les absences couvertes par une demande non validée portent la mention
  « ⏳ Congé … en attente de validation (état) ».

**Maternité et allaitement** : périodes par employée (matricule, du, au).
- *Demande par l'employée* (*Suivi → Maternité et allaitement*, compte rattaché à son matricule) : congé de maternité
  ou période d'allaitement (heures par jour au choix), certificat facultatif. La demande suit le **circuit de
  validation paramétré** dans *Ressources humaines → Maternité : périodes et circuit* (mêmes étapes que les arrêts
  maladie : responsable N+1, agent RH habilité, utilisateur désigné ; aucune étape = validée d'office), avec
  notifications par e-mail facultatives. Elle ne s'applique au suivi qu'une fois validée ; annulée par les RH, la
  période est retirée. Une saisie par un agent RH (ou dans la page RH) est validée d'office.
- *Congé de maternité* : statut « Congé maternité » chaque jour attendu de la période, même si l'employée badge ;
  durée attribuée par jour paramétrable (*Paramètres horaires*, 8h par défaut). Compté comme un congé dans les
  rapports (ni absence, ni journée disponible).
- *Période d'allaitement* : les heures d'allaitement (1 h, 2 h ou 3 h ; paramètre « Heures d'allaitement par jour »,
  1 h par défaut, ou valeur choisie pour la période) peuvent être prises en début ou en fin de journée. La durée
  validée part du **temps réellement travaillé** (du premier au dernier pointage, pause déduite) complété par les
  heures d'allaitement jusqu'à l'objectif : 7h travaillées + 1 h → 8h standard ; 6h + 1 h → 7h ; jamais au-delà de
  l'objectif ni moins que la durée travaillée. Le seuil de retard est décalé des heures d'allaitement (7h45 + 1 h →
  retard à partir de 8h45). Une journée sans pointage reste une absence. La durée effective reste la durée réelle ;
  le suivi affiche la mention « Allaitement » (colonne dans l'export).

**Filtre « Horaires »** (*Suivi journalier* et *Rapports*, exports compris) : *tous*, *horaires postés* (agents qui
ont au moins un jour dans le planning sur la période affichée) ou *horaire de bureau* (les autres), pour inclure ou
exclure les agents postés des indicateurs et des tableaux.

**Missions** (*Source des pointages → Missions*) : table des autorisations de mission (ex. feuille Smartsheet
synchronisée), reliée aux employés par matricule ou par **adresse e-mail** (colonne « contact » Smartsheet) ;
plusieurs colonnes « date aller » / « date retour » possibles (du premier aller au dernier retour) ; un jour ouvré ou
un poste planifié couvert par une mission approuvée devient « En mission », avec la
« durée attribuée par jour de mission » (8h par défaut) en durée standard. Congé et arrêt maladie l'emportent.

**Résumés par mail** (*Administration → Résumés par mail*) : chaque responsable abonné reçoit le point de la veille
(quotidien) et/ou de la semaine passée (hebdomadaire) sur son équipe N-1 (ou toute sa hiérarchie) : taux de présence
et de ponctualité avec l'évolution, absences, retards, oublis de badge, autres situations, détail par collaborateur.
Serveur SMTP et mode test/production des mails de badge ; un seul envoi par responsable et par période.

**Rappels de pointage** (*Administration → Rappels de pointage*) : avertissements et alertes par e-mail aux
employés pour limiter les oublis de badge. Cinq messages, activables séparément, à heure paramétrable et au texte
personnalisable (objet et texte, variables `{prenom}`, `{nom}`, `{matricule}`, `{date}`, `{poste}`, `{arrivee}`,
`{dernier}`) :
- *matin — avertissement* (arrivée non pointée) : employé attendu (jour ouvré, sans congé, arrêt, télétravail,
  mission, formation ni repos, hors agents terrain) sans aucun pointage à l'heure choisie (ex. 08:15) ; horaires
  postés : N minutes après le début du poste ;
- *matin — alerte* : toujours aucun pointage plus tard (ex. 09:30, ou N minutes après le début du poste), avec le
  responsable N+1 en copie si souhaité ;
- *soir — avertissement « n'oubliez pas de badger en partant »* : à tous les employés encore présents (nombre impair
  de pointages) à l'heure choisie (ex. 16:45), ou N minutes avant la fin de leur poste ;
- *soir — départ non pointé* : employé qui n'a badgé que son arrivée à l'heure choisie (ex. 17:45) ; poste
  planifié : N minutes après la fin du poste (nuits comprises) ;
- *oubli de la veille* : chaque matin, rappel des journées restées incomplètes (un seul pointage) des 3 derniers
  jours, avec le responsable N+1 en copie si souhaité.

Les rappels ont la même présentation que le mail de confirmation de badge (en-tête de l'entreprise, case
« Arrivée / Date », pointages de la journée), en orange pour un avertissement et en rouge pour une alerte.
**Destinataires** : par défaut, les employés cochés dans *Mails de badge → Abonnés*, à la même adresse que leur
confirmation de pointage (ou, au choix, tous les employés actifs ayant une adresse e-mail) ; filtres supplémentaires
par service, site et direction, matricules exclus, message libre ajouté à chaque rappel.
Un seul rappel par employé, type et journée ; vérification toutes les 5 minutes ; aucun envoi si les pointages ne
sont plus synchronisés depuis le délai choisi (garde-fou). Même serveur SMTP et même mode test/production que les
mails de badge. La page montre la situation du jour (qui recevrait quel message, à quelle heure), un aperçu, un essai
et le journal des envois.

**Direction** : quand la colonne est configurée (*Administration → Source des pointages*, champ « Direction » de la
table Personnel, avec une table de libellés si c'est un code), le suivi journalier et le tableau « Par employé » des
rapports ont une **colonne Direction** dédiée (triable dans le suivi, reprise dans les exports Excel et PDF), en plus
du **filtre Direction** et du regroupement « Par direction ».

**Site du pointage** (*Administration → Source des pointages*, colonne du lecteur ou du site dans la table des
pointages, avec une table de libellés facultative, ex. code du terminal → nom du site) : le suivi journalier affiche
sous l'heure du premier et du dernier pointage le site correspondant (en orange si le départ a lieu sur un autre
site) ; repris dans le détail des pointages et l'export Excel.

**Surveillance des sites** (*Mails et relances → Surveillance des sites*) : chaque jour de contrôle, à l'heure
paramétrée, mail à une liste de destinataires si un site n'a remonté aucun pointage depuis le début de la journée
(lecteur en panne, réseau, synchronisation arrêtée). Sites surveillés au choix (par défaut tous ceux qui ont pointé
récemment), jours fériés exclus si souhaité, un seul contrôle par jour, bouton « Contrôler maintenant », journal.

**Durées attribuées automatiquement** (*Paramètres horaires*, historisé avec la date d'effet) : une case par
situation — agents terrain (minimum par jour), congés, télétravail, arrêt maladie, mission, congé de maternité, repos
du planning. Décochée, la situation n'a plus de durée standard d'office (8h par défaut) : seule compte la durée des
pointages (agent terrain : temps pointé, sans minimum ; journée sans pointage : aucune durée). Le statut ne change pas.

**Préférences e-mail des employés** : sur sa page *Mon compte*, chaque employé (compte rattaché à son matricule)
coche s'il veut recevoir les **mails de badge** (confirmation de chaque pointage) et les **rappels de pointage**.
L'administrateur fait ce choix pour chaque utilisateur (*Administration → Utilisateurs → Modifier*, rubrique
« E-mails de l'employé ») et peut le **verrouiller** (l'employé ne peut plus le changer) ; *Mails de badge → Abonnés*
permet de le faire en masse (colonnes Badge et Rappels). Les envois respectent ces choix : mails de badge pour les
cases « badge » cochées ; rappels pour les cases « rappels » cochées (public « abonnés ») ou pour tous sauf ceux
qui les ont refusés (public « tous »).

**Serveur d'envoi des e-mails** (*Mails et relances → Serveur d'envoi des e-mails*, administrateurs) : serveur SMTP,
sécurité, compte, adresse et nom d'expéditeur, « Répondre à », nom de l'entreprise, mode test / production et
adresses de test, communs à tous les mails de l'application (badge, rappels, relances, résumés, surveillance des
sites, notifications) ; mail d'essai et état de chaque envoi.

**Aide** (menu *Aide → Indicateurs et calculs*, accessible à tous) : définition de chaque indicateur (statuts,
retard, durées, suivi, rapports, alertes, couleurs, filtres) avec sa méthode et sa formule de calcul, illustrées par
les paramètres horaires en vigueur.

**Rapports — analyses réservées à l'administrateur** : le graphique « Heures d'arrivée au bureau » et le tableau
« À suivre » (employés à suivre) ne sont affichés qu'aux administrateurs, dans une partie dédiée en bas de la page ;
la feuille « Alertes » de l'export Excel et la section « Employés à suivre » du PDF leur sont aussi réservées.

**Durée standard** (anciennement « durée validée ») : réservée aux **RH et aux administrateurs** — suivi, rapports
(heures standard, moyenne par jour au bureau) et exports Excel/PDF ; les managers et lecteurs ne la voient pas.
*Administration → Paramètres horaires → Affichage du suivi journalier* permet aussi de la masquer pour tous ; quand
elle est visible, chacun peut la masquer pour lui-même (« Masquer la durée standard » dans le suivi). Dans le suivi et
ses exports, la colonne **Statut** est placée entre le premier et le dernier pointage.

**Arrêts maladie** (*Arrêts maladie*, *Administration → Circuit arrêts maladie*) : déclaration par l'employé avec
justificatif, circuit de validation paramétrable, saisie RH validée d'office. **Notifications par e-mail**
(facultatives) : le valideur de l'étape en cours est prévenu, puis l'employé quand l'arrêt est validé ou refusé ;
envoi via le serveur SMTP des mails de badge et soumis à son mode test / production. Adresse : celle du compte
(*Utilisateurs*), sinon celle de la fiche employé.

**Périmètre par compte** : un lecteur ou un manager peut être rattaché à un employé (matricule) et limité à
**son équipe** : il ne voit (écran, détail, export) que lui-même et toutes les personnes placées sous lui.

**Rôles** (*Administration → Utilisateurs*) :

| Rôle | Accès |
|---|---|
| Lecteur | Suivi journalier, exports Excel et PDF |
| RH | Suivi, rapports et exports de **tout le personnel** (même rattaché à un employé), paramètres horaires et jours fériés, horaires postés, agents terrain, circuit et traitement des arrêts maladie, résumés par mail, rappels de pointage — **sans** synchronisation (tableau de bord, jobs, exécutions, logs, données) ni sources de données (connexions, source des pointages), ni utilisateurs, mails de badge ou audit |
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

## Source Smartsheet

*Connexions → Nouvelle connexion → Smartsheet* : jeton d'accès API (Smartsheet → Compte → Applications et
intégrations → Accès à l'API), serveur (app.smartsheet.com ou .eu) et feuille(s) (identifiant ou nom exact).
« Tester la connexion » sans feuille liste les feuilles accessibles. Chaque feuille devient une table (en-têtes
simplifiés, dates et cases à cocher typées, colonne `row_id` pour la clé) ; synchronisation par un job, mode Complet
conseillé.

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
