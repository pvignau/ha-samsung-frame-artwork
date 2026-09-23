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
| **Album partagé iCloud** | Photos d'un album iCloud public (lien `https://www.icloud.com/photos/…`) |

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
