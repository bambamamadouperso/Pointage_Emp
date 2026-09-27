-- Données de démonstration (base source MariaDB) : employés et pointages.
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS pointage CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE pointage;

CREATE TABLE IF NOT EXISTS employes (
  id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  matricule VARCHAR(20) NOT NULL UNIQUE,
  nom VARCHAR(100) NOT NULL,
  prenom VARCHAR(100) NOT NULL,
  service ENUM('RH','Production','Logistique','Direction') NOT NULL DEFAULT 'Production',
  actif TINYINT(1) NOT NULL DEFAULT 1,
  date_embauche DATE,
  salaire DECIMAL(10,2),
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pointages (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  employe_id INT UNSIGNED NOT NULL,
  jour DATE NOT NULL,
  heure_arrivee TIME,
  heure_depart TIME,
  type SET('normal','nuit','weekend') DEFAULT 'normal',
  commentaire TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_jour (jour)
);

INSERT INTO employes (matricule, nom, prenom, service, actif, date_embauche, salaire) VALUES
  ('E001', 'Diallo', 'Awa', 'RH', 1, '2019-03-01', 450000.00),
  ('E002', 'Ndiaye', 'Moussa', 'Production', 1, '2020-06-15', 320000.00),
  ('E003', 'Sow', 'Fatou', 'Logistique', 0, '2018-01-10', 350000.00),
  ('E004', 'Fall', 'Ibrahima', 'Direction', 1, '2015-09-01', 900000.00);

INSERT INTO pointages (employe_id, jour, heure_arrivee, heure_depart, type, commentaire) VALUES
  (1, CURDATE() - INTERVAL 1 DAY, '08:02:00', '17:05:00', 'normal', NULL),
  (2, CURDATE() - INTERVAL 1 DAY, '07:55:00', '16:30:00', 'normal', 'RAS'),
  (3, CURDATE() - INTERVAL 1 DAY, '22:00:00', '06:00:00', 'nuit,weekend', 'Équipe de nuit'),
  (4, CURDATE(), '09:10:00', NULL, 'normal', NULL);
