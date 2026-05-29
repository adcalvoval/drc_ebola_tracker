# Ebola Outbreak Tracker — DRC & Uganda

A live dashboard tracking information on the 2026 Bundibugyo ebolavirus outbreak in eastern DRC and Uganda published on WHO's EIOS platform. 

**Live:** deployed on Vercel · Data refreshed every 3 hours via GitHub Actions

---

## What the dashboard shows

| Element | What it displays |
|---|---|
| **Stats bar** | Deaths, suspected cases, WHO alert status (PHEIC), and countries affected — drawn from the latest WHO EIOS articles |
| **Interactive map** | DRC provinces coloured by outbreak status (epicenter / spread / none), Uganda regions with imported-case overlay |
| **Province popups** | Click any province for case counts, source authority tier, and last-updated timestamp |
| **Sidebar feed** | Up to 150 recent WHO EIOS articles, tagged and linked |
| **Auto-refresh** | The browser reloads `feed.json` every 30 minutes without a full page reload |

### Map layers

Three toggleable layers are available via the panel in the top-left of the map:

- **DRC Provinces (Admin 1)** — 26 provinces, loaded on startup (~1 MB). Provinces are classified as *epicenter* (Ituri), *confirmed spread* (Nord-Kivu, Sud-Kivu), or *unaffected*.
- **DRC Territories (Admin 2)** — 189 territories, loaded on demand (~6 MB). Territories are classified spatially by which Admin 1 province contains them. Specific popups exist for Djugu (active transmission zone) and Goma (high-risk urban centre with international airport).
- **Uganda Regions (Admin 1)** — loaded on startup. Central Region (Kampala) is highlighted when imported cases are confirmed.

### Source authority tiers

Every figure shown on the dashboard carries a coloured tier badge indicating its source:

| Badge | Source | Weight |
|---|---|---|
| `WHO` | WHO / OMS direct statements | 3 (highest) |
| `MoH` | DRC or Uganda Ministry of Health | 2 |
| `CDC` | Africa CDC, US CDC, national CDCs | 1 |
| `Media` | News media | 0 (lowest) |

When multiple articles report conflicting figures, the highest-authority tier wins. Deaths are only shown if a weight-3 (WHO) article reported them.

### High-water marks

Outbreak case counts do not decrease. As the RSS feed window rotates older articles out, a later scrape can surface a lower figure from a different article even when the real count is higher. To prevent the stats bar from appearing to go backwards, `fetch_feed.py` maintains a `data/high_water.json` file that records the highest value ever extracted for each field. The frontend always shows the greater of the live value and the high-water mark; stale high-water values are rendered at reduced opacity with a "last seen" date.

---

## Architecture

```
┌──────────────────────────────────────────┐
│  WHO EIOS RSS feed (XML, every 3 hours)  │
└───────────────────┬──────────────────────┘
                    │ fetch_feed.py
                    ▼
         ┌──────────────────┐
         │  Regex extraction │  classify · extract_numbers · official_weight
         │  (EN/FR/ES/PT/AR) │  extract_provincial_breakdown · extract_uga_cases
         └────────┬─────────┘
                  │
         ┌────────▼─────────┐
         │  data/feed.json   │  ← committed to git by GitHub Actions
         │  data/feed.js     │
         │  data/high_water  │
         └────────┬─────────┘
                  │  served statically by Vercel
                  ▼
         ┌──────────────────┐
         │   index.html      │  Leaflet map + stats bar + sidebar
         └──────────────────┘
                  │  fetch() on load and every 30 min
                  ▼
         geoBoundaries GeoJSON (live from GitHub)
         COD-AB DRC ADM1 · ADM2 · UGA ADM1
```


---

## Data pipeline — `fetch_feed.py`

The analysis is done through Python regex and keyword matching. No external AI service is called, no LLM.

### 1. Feed fetch

Connects to the WHO EIOS RSS endpoint (requires a static token embedded in the URL) and retrieves up to 150 articles as XML. Each article provides a `<title>`, `<description>` (HTML-stripped), `<link>`, and `<pubDate>`.

The articles are filtered directly on IFRC's EIOS Board, searching for articles on Ebola from specific sources: European News, Medical, Medical Official, NGO, Official, Science. This is to minimize inaccurate data, misinformation, and to increase the chances of catching official figures, primarily WHO.

### 2. Article classification

Each article is assigned one of four tags by scanning for keywords in the title and description:

| Tag | Keywords matched |
|---|---|
| `pheic` | "PHEIC", "public health emergency", "urgence de santé publique", etc. |
| `cases` | "cases", "deaths", "confirmed", "suspected", "cas", "حالات", etc. |
| `response` | "response", "deployed", "preparedness", "border", "travel", etc. |
| `analysis` | (fallback — none of the above matched) |

### 3. Number extraction

`extract_numbers()` runs regex patterns against each article's text (title + description combined) across five languages: English, French, Spanish, Portuguese, and Arabic. It extracts:

- **Deaths** — "X deaths", "X décès", "X fallecidos", "X وفيات", and qualifier variants ("more than X", "plus de X", etc.)
- **Suspected cases** — "X suspected cases", "X cas suspects", "X casos sospechosos", etc.
- **Confirmed cases** — "X confirmed cases", "X cas confirmés", "X casos confirmados", etc.
- **Active patients** — "X patients under active care", "X malades activement pris en charge", etc.

Arabic-Indic and Persian digit forms (٠–٩, ۰–۹) are normalised to ASCII before matching. Comma- and space-separated thousands separators (`1,234` / `1 234`) are collapsed to plain integers. Small number words in English and French ("one", "deux", etc.) are converted to digits before regex runs.

### 4. Source authority scoring

`official_weight()` assigns a weight 0–3 to each article's text by matching known organisation names and attribution phrases in all five languages. This weight determines which article's figures take priority when multiple articles report different numbers.

### 5. Provincial breakdown

`extract_provincial_breakdown()` extracts province-level confirmed case counts for Ituri, Nord-Kivu, Sud-Kivu, and Goma using location-anchored patterns in EN and FR (e.g. `"78 en Ituri, 4 au Nord-Kivu"`, `"X confirmed cases in Goma"`). When a sitrep-style combined line is detected it is parsed in a single pass; individual province mentions fall back to per-pattern matching.

### 6. Uganda extraction

`extract_uga_cases()` runs Uganda-specific patterns that require explicit proximity of "Uganda" / "Ouganda" / "أوغندا" to the case number to prevent DRC totals being misattributed. A ceiling of 200 is enforced — any figure approaching DRC totals is discarded as a likely mis-attribution.

### 7. Stats aggregation

`compute_stats()` runs over articles published in the last 72 hours (falling back to all items if fewer than 5 qualify). For each authority tier (0–3) it keeps the highest value seen per field. The winning tier for each field is the highest tier that reported a non-zero value. The output includes:

- `drc` — best-available figures for DRC (deaths, suspected, confirmed, active)
- `drcMeta` — which authority tier and which article headline produced each figure
- `drcTiers` — per-tier breakdown for transparency
- `provinces` — best per-province confirmed counts with source weight
- `uga` — Uganda case count and "mentioned" flag
- `whoAlert` — `"PHEIC"` if any article in the feed mentions a PHEIC declaration
- `sourceLabel` — human-readable label for the winning source tier

### 8. High-water persistence

After each run, `data/high_water.json` is updated. Values only ever increase. Uganda counts are sanity-checked against DRC totals (rejected if ≥ 30% of DRC suspected total or > 200) to prevent obvious mis-attributions from poisoning the high-water store.

---

## Automation — GitHub Actions

`.github/workflows/fetch-feed.yml` runs `fetch_feed.py` on a schedule:

- **Every 3 hours** (cron `0 */3 * * *`)
- **On-demand** via `workflow_dispatch`

After a successful fetch the script commits `data/feed.json`, `data/feed.js`, and `data/high_water.json` back to the repository with message `Update feed data`. Vercel's GitHub integration then deploys the updated static files automatically.

---

## Boundary data

Administrative boundaries are fetched live by the browser from [geoBoundaries](https://www.geoboundaries.org/) (wmgeolab), which uses OCHA Common Operational Datasets (COD-AB):

| Layer | Source path |
|---|---|
| DRC Admin 1 (26 provinces) | `geoBoundaries-COD-ADM1.geojson` |
| DRC Admin 2 (189 territories) | `geoBoundaries-COD-ADM2.geojson` — lazy-loaded |
| Uganda Admin 1 (regions) | `geoBoundaries-UGA-ADM1.geojson` |

Admin 2 territories lack ISO codes in this dataset; outbreak status is assigned by spatial containment — if a territory's centroid falls within the bounding box of an affected Admin 1 province, it inherits that province's status.

---

## Local development

No build step required. Run the data pipeline with:

```bash
python fetch_feed.py
```

Then open `index.html` directly in a browser (or serve it with any static file server — the map requires `data/feed.json` to be reachable at `./data/feed.json`).

For local serving:

```bash
python -m http.server 8000
# open http://localhost:8000
```

The Windows Task Scheduler can run `fetch_feed.py` on a local schedule if GitHub Actions is not being used (see the comment block at the top of the script).

---

## File structure

```
ebola-outbreak-map/
├── index.html              # Single-page dashboard (Leaflet map, stats, sidebar)
├── fetch_feed.py           # Data pipeline — fetch, parse, analyse, commit
├── data/
│   ├── feed.json           # Latest parsed feed (committed by CI)
│   ├── feed.js             # Same data as window.FEED_DATA = {...} (legacy fallback)
│   └── high_water.json     # Persistent high-water marks for epi figures
├── .github/workflows/
│   └── fetch-feed.yml      # GitHub Actions cron job
└── vercel.json             # Vercel deployment config
```

---

## Data caveats

- Figures are extracted automatically from news aggregation text and **cannot be verified for accuracy**. They should be treated as indicative, not authoritative.
- The WHO EIOS feed aggregates media and official sources; not all articles reflect verified WHO data.
- Province-level case counts depend on explicit geographic attribution in article text and may be incomplete.
- The high-water mechanism means figures shown may be from a previous fetch if the current feed window contains no newer report.

---

## Dependencies

| Dependency | Usage |
|---|---|
| [Leaflet 1.9.4](https://leafletjs.com/) | Interactive map, loaded from CDN |
| [CARTO Dark Matter](https://carto.com/basemaps/) | Map basemap tiles |
| [geoBoundaries](https://www.geoboundaries.org/) | Administrative boundary GeoJSON |
| Python 3 stdlib only | `xml.etree`, `urllib`, `re`, `json` — no `pip install` needed |
| [Vercel](https://vercel.com/) | Static hosting and CDN |
| [Vercel Web Analytics](https://vercel.com/analytics) | Page-view analytics (`/_vercel/insights/script.js`) |
