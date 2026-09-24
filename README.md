# Samsung Frame Artwork

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
| `image_mode` | `fill` | `fill` = recadrage, `fit` = bandes noires |
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

Copier `custom_components/samsung_frame/` dans le dossier `config/` de Home
Assistant, redémarrer, puis **Paramètres → Appareils et services → Ajouter une
intégration → Samsung Frame Artwork**.

La TV doit être allumée (ou en mode Art) lors du premier appairage : elle affiche
une demande d'autorisation à accepter avec la télécommande. Le jeton est ensuite
conservé dans `.storage/samsung_frame_<entry_id>_tv_token`.

## Notes de version

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
