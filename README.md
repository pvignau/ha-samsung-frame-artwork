# Samsung Frame Artwork

[![Validation](https://github.com/pyvignau/ha-samsung-frame-artwork/actions/workflows/validate.yml/badge.svg)](https://github.com/pyvignau/ha-samsung-frame-artwork/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)
[![licence](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)

Intégration Home Assistant qui fait tourner automatiquement les œuvres affichées
en mode Art sur une TV **Samsung The Frame**.

## Fonctionnement

À intervalle configurable (6 h par défaut), l'intégration tire une source au sort
(pondération réglable), télécharge une image, la recadre en 3840×2160 JPEG
(≤ 1,9 Mo, limite de la TV) et l'envoie à la TV via
[`samsungtvws`](https://github.com/NickWaterton/samsung-tv-ws-api).

### Sources

| Source | Description |
|---|---|
| **theframetv.com** | Catalogue d'œuvres gratuites 4K, scrapé et mis en cache localement |
| **Album partagé iCloud** | Photos d'un album iCloud public (lien `https://photos.icloud.com/shared/album/…`) |

#### Albums iCloud : deux générations d'API

Apple a migré les albums partagés vers **CloudKit**. L'intégration gère les deux :

1. **CloudKit** (liens récents) — `records/resolve` fournit un jeton d'accès
   anonyme, la partition et la zone de l'album ; `changes/zone` énumère ensuite
   les photos avec leurs URL signées.
2. **`sharedstreams`** (anciens albums) — conservé en repli automatique.

Quand l'original est en HEIC, que Pillow ne lit pas sans greffon, la dérivée
JPEG générée par Apple est utilisée à la place.

Ces API ne sont pas documentées : elles ont été reconstituées par observation du
client web d'Apple et peuvent changer sans préavis. En cas de panne, le journal
indique l'étape qui échoue.

Un historique persistant évite de réafficher les mêmes œuvres tant que le
catalogue n'a pas été parcouru.

## Entités et services

- `sensor.<tv>_current_artwork` — œuvre actuellement affichée (attributs : source, date, chemin local)
- `button.<tv>_update_artwork_now` — force un changement immédiat
- Service `samsung_frame.update_artwork` — idem, depuis une automatisation
- Service `samsung_frame.refresh_theframetv_index` — force un re-scraping du catalogue

## Options

| Option | Défaut | Rôle |
|---|---|---|
| `interval_hours` | 6 | Intervalle de rotation |
| `history_size` | 20 | Anti-répétition |
| `image_mode` | `fill` | `fill` = recadrage centré, `fit` = bandes noires, `smart` = recadrage sur les visages |

### Recadrage intelligent (`smart`)

Une photo de portrait recadrée en 16:9 perd le haut du crâne quand le cadre est
centré. En mode `smart`, les visages sont détectés (cascades de Haar d'OpenCV)
et le cadre est calé dessus, avec une marge et un léger décalage vers le haut
conforme à la composition d'un portrait. S'il n'y a personne sur la photo — le
cas courant des œuvres de theframetv.com — le comportement est identique à
`fill`.

La détection tourne sur une version réduite de l'image (800 px au plus grand
côté), dans l'executor de Home Assistant. Elle nécessite
`opencv-python-headless`, épinglé sur la branche 4.x : OpenCV 5 a retiré
`CascadeClassifier` et les cascades. Si la bibliothèque est absente ou
inutilisable, le recadrage retombe sur le centrage avec un avertissement dans le
journal.
| `image_width` / `image_height` | 3840 × 2160 | Résolution cible |
| `max_tv_images` | 10 | **Images conservées dans la mémoire de la TV** |
| `cache_max_mb` | 300 | Taille max du cache disque local |
| `index_refresh_days` | 7 | Fréquence de re-scraping du catalogue |
| `skip_when_watching` | activé | **Ne pas interrompre le visionnage** (voir ci-dessous) |

### Ne pas interrompre le visionnage

Pousser une œuvre bascule la TV en mode Art, ce qui coupe ce qui est en cours de
lecture. Quand l'option est active, la rotation programmée vérifie d'abord l'état
du mode Art : si la TV affiche du contenu, le cycle est ignoré — rien n'est
téléchargé ni envoyé — et réessayé toutes les 15 minutes, de sorte que l'œuvre
change peu après l'extinction.

C'est bien le mode Art qui sert de critère, et non l'alimentation : sur une
Frame, `PowerState` vaut `on` aussi bien en mode Art qu'en cours de visionnage.

Le bouton **Update Artwork Now** pousse une image dans tous les cas (action
manuelle explicite) ; le service `samsung_frame.update_artwork`, lui, respecte la
protection, car il est surtout appelé depuis des automatisations.

## Installation

### Via HACS (recommandé)

HACS → menu ⋮ → **Dépôts personnalisés** → ajouter
`https://github.com/pyvignau/ha-samsung-frame-artwork` en catégorie **Integration**,
puis installer **Samsung Frame Artwork** et redémarrer Home Assistant.

### Manuellement

Copier `custom_components/samsung_frame/` dans le dossier `config/` de Home
Assistant, puis redémarrer.

### Configuration

**Paramètres → Appareils et services → Ajouter une intégration → Samsung Frame
Artwork**.

La TV doit être allumée (ou en mode Art) lors du premier appairage : elle affiche
une demande d'autorisation à accepter avec la télécommande. Le jeton est ensuite
conservé dans `.storage/samsung_frame_<entry_id>_tv_token`.

Au premier démarrage, Home Assistant installe les dépendances dans
`config/deps`, dont `opencv-python-headless` (~45 Mo) : ce démarrage-là est
nettement plus long que les suivants.

> **Pensez à réserver l'adresse IP de la TV dans votre DHCP.** Un changement
> d'adresse coupe l'intégration silencieusement : les images continuent d'être
> téléchargées, mais plus rien n'est envoyé.

## Avertissement

theframetv.com est parcouru par scraping, et les API d'albums partagés iCloud
(CloudKit comme `sharedstreams`) ne sont pas documentées : elles ont été
reconstituées par observation du client web d'Apple. Ces deux sources peuvent
cesser de fonctionner sans préavis. En cas de panne, le journal Home Assistant
indique l'étape exacte qui échoue.

## Notes de version

### 1.3.0

Mode de recadrage **`smart`** : les visages présents sur la photo sont détectés
et le cadre est calé dessus, au lieu du recadrage centré qui décapite les
portraits. Sans visage détecté, le résultat est identique à `fill`.

Prise en charge des **albums partagés iCloud modernes**, qu'Apple a migrés de
l'API `sharedstreams` vers CloudKit ; l'ancienne API reste en repli automatique.
Les originaux en HEIC, illisibles par Pillow, passent par la dérivée JPEG.

**Protection du visionnage** : la rotation programmée ne bascule plus la TV en
mode Art pendant que vous regardez un contenu, et réessaie toutes les 15 minutes.

### 1.2.0

Correctif de la panne de fond : l'intégration **empilait les images dans la
mémoire interne de la TV sans jamais les supprimer**, jusqu'à saturation — tous
les envois échouaient alors silencieusement. Les anciennes images sont désormais
purgées automatiquement (`max_tv_images`).

Également : rafraîchissement périodique du catalogue (il restait figé), purge LRU
du cache disque, correction du scraper theframetv.com, correction du suivi de
redirection de partition iCloud et d'une traversée de chemin via les noms de
fichiers iCloud, bornage des dimensions d'image, `unique_id` sur la config entry,
dépendance `samsungtvws` épinglée sur un commit, traductions FR.

Le catalogue était également plafonné à 5 pages de scraping, soit les 75 œuvres
les plus récentes — toutes issues du même lot saisonnier (77 % de Noël). La
borne est portée à 40 pages (la boucle s'arrête d'elle-même sur la première page
vide) : **333 œuvres** indexées, 21 % de Noël.

### 1.1.0

Version initiale : config flow, deux sources, rotation pondérée, historique persistant.
