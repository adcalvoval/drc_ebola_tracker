#!/usr/bin/env python3
"""
Polls INRB-UMIE/BDBV2026-Data processed CSV files for the latest MVE sitrep data.

Data lives at:
  https://github.com/INRB-UMIE/BDBV2026-Data/tree/main/data/insp_sitrep/processed

All CSVs are plain HTTPS — no Git LFS, no PDF parsing required.

Imported by fetch_feed.py: call poll_and_update() -> data dict or None.
Standalone:                python fetch_inrb_sitrep.py
"""

import csv
import io
import json
import os
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

_DIR = os.path.dirname(os.path.abspath(__file__))

_CSV_BASE = (
    'https://raw.githubusercontent.com/INRB-UMIE/BDBV2026-Data'
    '/main/data/insp_sitrep/processed'
)
REPO_URL  = 'https://github.com/INRB-UMIE/BDBV2026-Data/tree/main/data/insp_sitrep/processed'
OUT_FILE  = os.path.join(_DIR, 'data', 'inrb_sitrep.json')

# National (DRC total) time-series CSVs
_NATIONAL_CSVS = {
    'confirmed': 'insp_sitrep__national_cumulative_confirmed_cases__daily.csv',
    'deaths':    'insp_sitrep__national_cumulative_confirmed_deaths__daily.csv',
    'recovered': 'insp_sitrep__national_cumulative_recovered_cases__daily.csv',
    'suspected': 'insp_sitrep__national_cumulative_suspected_cases__daily.csv',
}

# Zone-level cumulative confirmed cases (for topHealthZones)
_ZONE_CSV = 'insp_sitrep__cumulative_confirmed_cases__daily.csv'


# HTTP helper

def _fetch_text(path):
    url = f'{_CSV_BASE}/{path}'
    req = Request(url, headers={'User-Agent': 'ebola-map-fetcher/1.0'})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode('utf-8')


# CSV parsing helpers

def _latest_value(csv_text):
    """Return (date_str, int_value) from the last non-empty row of a 3-column CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    last_date = last_val = None
    for row in reader:
        raw = list(row.values())
        date_col = raw[1] if len(raw) > 1 else None
        val_col  = raw[2] if len(raw) > 2 else None
        if not date_col or not val_col:
            continue
        try:
            v = int(val_col)
            last_date, last_val = date_col.strip(), v
        except ValueError:
            pass
    return last_date, last_val


def _zone_snapshot(csv_text):
    """
    Return {zone_name: count} using the most recent value per zone.
    Zones are updated at different times in the INRB CSV, so filtering to a
    single global latest date would drop all zones not updated on that date.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    # {zone: (date_str, int_value)}
    best = {}
    for row in reader:
        raw = list(row.values())
        if len(raw) < 3:
            continue
        nom, date, val = raw[0].strip(), raw[1].strip(), raw[2].strip()
        if not nom or nom in ('NA', 'ND') or not date:
            continue
        try:
            v = int(val)
        except ValueError:
            continue
        prev_date, _ = best.get(nom, ('', 0))
        if date >= prev_date:
            best[nom] = (date, v)
    return {nom: v for nom, (_, v) in best.items()}


# Persistence

def _load_persisted():
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


# Main API

def poll_and_update():
    """
    Fetch the latest INRB sitrep figures from processed CSV files.
    Returns parsed epi data dict, or None if unavailable.
    Persists result to data/inrb_sitrep.json.
    """
    os.makedirs(os.path.join(_DIR, 'data'), exist_ok=True)

    # Quick check: fetch confirmed cases CSV to get the latest date
    try:
        conf_text = _fetch_text(_NATIONAL_CSVS['confirmed'])
    except (URLError, OSError) as e:
        print(f'INRB: Could not fetch CSV -- {e}')
        return _load_persisted()

    latest_date, latest_confirmed = _latest_value(conf_text)
    if not latest_date:
        print('INRB: No data found in confirmed cases CSV.')
        return _load_persisted()

    # Skip re-fetching if we already have this date
    persisted = _load_persisted()
    if persisted and persisted.get('asOf') == latest_date:
        print(f'INRB: Already up to date (as of {latest_date}).')
        return persisted

    print(f'INRB: New data found for {latest_date} (confirmed={latest_confirmed}). Fetching all CSVs...')

    # Fetch remaining national CSVs
    drc = {'confirmed': latest_confirmed}
    for field, filename in _NATIONAL_CSVS.items():
        if field == 'confirmed':
            continue
        try:
            text = _fetch_text(filename)
            _, val = _latest_value(text)
            if val is not None:
                drc[field] = val
        except (URLError, OSError) as e:
            print(f'INRB: Could not fetch {filename} -- {e}')

    # Fetch zone-level breakdown for topHealthZones
    try:
        zone_text = _fetch_text(_ZONE_CSV)
        zones = _zone_snapshot(zone_text)
        if zones:
            # Sort descending by cases; keep all zones with >0
            drc['topHealthZones'] = dict(
                sorted(zones.items(), key=lambda x: x[1], reverse=True)
            )
            drc['zonesAffected'] = len(zones)
    except (URLError, OSError) as e:
        print(f'INRB: Could not fetch zone CSV -- {e}')

    result = {
        'fetchedAt': datetime.now(timezone.utc).isoformat(),
        'asOf':      latest_date,
        'url':       REPO_URL,
        'drc':       drc,
    }
    with open(OUT_FILE, 'w', encoding='utf-8') as fh:
        json.dump(result, fh, indent=2)

    print(f'INRB: Saved -- confirmed={drc.get("confirmed")} deaths={drc.get("deaths")} '
          f'recovered={drc.get("recovered")} zones={drc.get("zonesAffected")}')
    return result


if __name__ == '__main__':
    data = poll_and_update()
    print(json.dumps(data, indent=2))
