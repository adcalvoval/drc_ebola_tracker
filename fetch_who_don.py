#!/usr/bin/env python3
"""
Polls the WHO emergency events page for new Disease Outbreak News (DON) bulletins,
extracts epidemiological data from each, and writes to data/who_don.json.

Imported by fetch_feed.py: call poll_and_update() -> latest data dict or None.
Standalone:                python fetch_who_don.py
"""

import html as _html
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import urlopen, Request
from urllib.error import URLError

_DIR = os.path.dirname(os.path.abspath(__file__))

# WHO emergency events page listing all DONs for this outbreak.
# Update this URL if a new outbreak event is declared.
EMERGENCY_EVENTS_URL = 'https://www.who.int/emergencies/emergency-events/item/2026-e000253'
OUT_FILE  = os.path.join(_DIR, 'data', 'who_don.json')
SEEN_FILE = os.path.join(_DIR, 'data', 'who_don_seen.json')


# ── Text helpers ──────────────────────────────────────────────────────────────

_WORD_MAP = {
    # EN
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
    'eighteen': '18', 'nineteen': '19', 'twenty': '20',
    # FR
    'zéro': '0', 'un': '1', 'une': '1', 'deux': '2', 'trois': '3',
    'quatre': '4', 'cinq': '5', 'sept': '7', 'huit': '8', 'neuf': '9',
    'dix': '10',
}


def _word_to_num(text):
    for word, n in _WORD_MAP.items():
        text = re.sub(rf'\b{word}\b', n, text, flags=re.IGNORECASE)
    return text


def _to_text(raw_html):
    """Strip HTML to plain text, preserving block boundaries as newlines."""
    # Decode entities (&nbsp; → space, &amp; → & etc.) before stripping tags
    h = _html.unescape(raw_html)
    # Remove zero-width / invisible Unicode that survive tag stripping
    h = re.sub(r'[­​-‏  ‪-‮﻿]', '', h)
    # Replace non-breaking space (U+00A0) with regular space
    h = h.replace(' ', ' ')
    # Block-level tags → newlines (handles both <p> and <p class="...">)
    for tag in ('h1', 'h2', 'h3', 'h4', 'p', 'div', 'li', 'tr', 'br', 'td'):
        h = re.sub(rf'</?{tag}(?:\s[^>]*)?>',  '\n', h, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', h)
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def _flatten(text):
    """Collapse whitespace and normalise numbers for regex matching."""
    t = re.sub(r'\s+', ' ', text).strip()
    t = _word_to_num(t)
    t = re.sub(r'(\d)[,\s](\d{3})\b', r'\1\2', t)   # 5,768 / 5 768 → 5768
    return t


def _int(s):
    return int(re.sub(r'[^\d]', '', str(s)))


# ── Link extraction ───────────────────────────────────────────────────────────

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        for name, val in attrs:
            if name == 'href' and val and '/disease-outbreak-news/item/' in val:
                href = val if val.startswith('http') else f'https://www.who.int{val}'
                if href not in self.links:
                    self.links.append(href)


# ── Bulletin parsing ──────────────────────────────────────────────────────────

def _parse_don(html):
    """
    Parse a WHO DON page and return structured epidemiological data.

    Patterns are built from observed DON603–DON607 phrasing. The data lives in
    narrative prose (no HTML tables), so we use targeted regex. Word-number
    substitution is applied before matching so early bulletins ("nine deaths")
    are handled the same as later ones ("9 deaths").
    """
    text = _flatten(_to_text(html))
    result = {'drc': {}, 'uga': {}}

    # Primary "as of" date (may differ per country section; take the first)
    m = re.search(r'as of (\d{1,2} \w+ \d{4})', text, re.IGNORECASE)
    if m:
        result['asOf'] = m.group(1)

    # ── DRC: confirmed + deaths ───────────────────────────────────────────────
    drc = result['drc']

    # Try combined patterns first (confirmed and deaths in same clause)
    # DON603/605: "total of 83 confirmed cases including 9 deaths (CFR 11%)"
    m = re.search(
        r'total of (\d[\d]*)\s+confirmed cases?\s+including\s+(\d+)\s+deaths?',
        text, re.IGNORECASE,
    )
    if m:
        drc['confirmed'] = _int(m.group(1))
        drc['deaths']    = _int(m.group(2))

    # DON606: "total of 515 confirmed cases, with 91 deaths among these confirmed cases"
    if 'confirmed' not in drc:
        m = re.search(
            r'total of (\d[\d]*)\s+confirmed cases?,\s+with\s+(\d+)\s+deaths?\s+among',
            text, re.IGNORECASE,
        )
        if m:
            drc['confirmed'] = _int(m.group(1))
            drc['deaths']    = _int(m.group(2))

    # DON607 and fallback: standalone confirmed count ("676 confirmed cases")
    if 'confirmed' not in drc:
        m = re.search(r'(\d[\d]*)\s+confirmed cases?', text, re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if v > 50:   # guard: DRC is always >> Uganda (≤ 20 so far)
                drc['confirmed'] = v

    # Standalone death count when not already captured
    if 'deaths' not in drc:
        for pat in [
            # "including 136 deaths (CFR…)" — DON607 main sentence
            r'including\s+(\d+)\s+deaths?\s*\(CFR',
            # "136 deaths (Case Fatality Rate…)" or "(CFR…)"
            r'(\d+)\s+deaths?\s+\(C(?:ase Fatality|FR)\b',
            # "with 136 deaths" — broad but catches variants
            r'with\s+(\d+)\s+deaths?\b',
            # "91 deaths among these confirmed cases" — must say "confirmed"
            r'(\d+)\s+deaths?\s+among\s+(?:these\s+)?confirmed\s+cases?',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = _int(m.group(1))
                # Sanity: confirmed deaths must be less than confirmed cases
                if v < drc.get('confirmed', 100_000):
                    drc['deaths'] = v
                    break

    # ── DRC: suspected ────────────────────────────────────────────────────────
    m = re.search(r'(\d[\d]*)\s+suspected cases?', text, re.IGNORECASE)
    if m:
        drc['suspected'] = _int(m.group(1))

    # Deaths among suspected (DON603/605)
    for pat in [
        r'(\d+)\s+deaths?\s+among\s+suspected\s+cases?',
        r'suspected cases?,\s+including\s+(\d+)\s+deaths?',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            drc['suspectedDeaths'] = _int(m.group(1))
            break

    # ── DRC: recovered ────────────────────────────────────────────────────────
    # DON607: "32 recovered patients" / DON606: "12 patients have recovered"
    for pat in [
        r'(\d+)\s+recovered\s+patients?',
        r'(\d+)\s+patients?\s+have\s+recovered',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            drc['recovered'] = _int(m.group(1))
            break

    # ── DRC: contacts ─────────────────────────────────────────────────────────
    for pat in [
        r'(\d[\d]*)\s+contacts\b.*?(?:identified|under\s+follow)',
        r'(\d[\d]*)\s+identified\s+contacts',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            drc['contacts'] = _int(m.group(1))
            break

    # ── DRC: health zones and province breakdown ───────────────────────────────
    m = re.search(r'(\d+)\s+health zones?', text, re.IGNORECASE)
    if m:
        drc['zonesAffected'] = _int(m.group(1))

    provs = {}

    # Per-province affected zone counts
    # DON605/606: "Ituri (7/36 HZ)", "North Kivu (7/35 HZ)", "South Kivu Provinces (1/34 HZ)"
    # DON607:     "Ituri (19 zones)", "North Kivu (9 zones)", "South Kivu (1 zone)"
    for slug, patterns in [
        ('ituri', [
            r'Ituri\s*\((\d+)\s+zones?\)',
            r'Ituri\s*\((\d+)/\d+\s*HZ\)',
        ]),
        ('northKivu', [
            r'North\s+Kivu\s*\((\d+)\s+zones?\)',
            r'North\s+Kivu\s*\((\d+)/\d+\s*HZ\)',
            r'Nord.Kivu\s*\((\d+)/\d+\s*HZ\)',
        ]),
        ('southKivu', [
            r'South\s+Kivu\s+Provinces?\s*\((\d+)/\d+\s*HZ\)',  # "South Kivu Provinces (1/34 HZ)"
            r'South\s+Kivu\s*\((\d+)\s+zones?\)',                 # "South Kivu (1 zone)"
            r'Sud.Kivu\s+Provinces?\s*\((\d+)/\d+\s*HZ\)',
        ]),
    ]:
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                provs.setdefault(slug, {})['zonesAffected'] = _int(m.group(1))
                break

    # Ituri confirmed cases — try patterns in order of specificity
    for pat in [
        # DON607: "Ituri Province: 629 cases (93% of total) with 109 deaths"
        r'Ituri Province[:\s]{1,5}(\d+)\s+cases?',
        # DON606: "Ituri Province, which accounts for 94% (487) of confirmed cases"
        r'Ituri Province[^(]{0,60}\((\d+)\)\s+of confirmed',
        # DON605/alt: "88% (110) of confirmed cases" (percentage then count in parens)
        r'\d+%\s+\((\d+)\)\s+of confirmed',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            count = _int(m.group(m.lastindex))
            # Ituri holds ~85-95% of DRC total; reject implausibly small values
            if count > drc.get('confirmed', 0) * 0.2:
                provs.setdefault('ituri', {})['confirmed'] = count
                break

    # Ituri deaths
    for pat in [
        # DON607: "Ituri Province: 629 cases (93% of total) with 109 deaths"
        r'Ituri Province:[^.]*?with\s+(\d+)\s+deaths?',
        # DON606: "CFR in Ituri is 15% (74/487)"
        r'CFR in Ituri is \d+%\s*\((\d+)/(\d+)\)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            provs.setdefault('ituri', {})['deaths'] = _int(m.group(1))
            # CFR sentence also gives confirmed: (deaths/confirmed)
            if m.lastindex == 2 and 'confirmed' not in provs.get('ituri', {}):
                provs['ituri']['confirmed'] = _int(m.group(2))
            break

    # North Kivu CFR sentence: "CFR in North Kivu which is 64% (16/25)"
    m = re.search(r'North Kivu which is \d+%\s*\((\d+)/(\d+)\)', text, re.IGNORECASE)
    if m:
        provs.setdefault('northKivu', {})
        provs['northKivu']['deaths']    = _int(m.group(1))
        provs['northKivu']['confirmed'] = _int(m.group(2))

    # Top health zones by name (all in Ituri Province)
    # DON605+: "Bunia (37 cases), Rwampara (33 cases), Mongbwalu (20 cases), Nyankunde (10 cases)"
    top_zones = {}
    for hzone, slug in [
        ('Bunia',     'bunia'),
        ('Rwampara',  'rwampara'),
        ('Mongbwalu', 'mongbwalu'),
        ('Nyankunde', 'nyankunde'),
    ]:
        m = re.search(rf'{hzone}\s*\((\d+)\s+cases?\)', text, re.IGNORECASE)
        if m:
            top_zones[slug] = _int(m.group(1))
    if top_zones:
        drc['topHealthZones'] = top_zones

    # Healthcare workers — number must be within 80 chars of "health...workers"
    # to avoid grabbing the total DRC confirmed count from a different sentence
    m = re.search(
        r'(\d+)\s+confirmed cases?[^.]{0,80}health(?:\s+and\s+care)?\s+workers?',
        text, re.IGNORECASE,
    )
    if m:
        v = _int(m.group(1))
        if v < 500:   # HCW count is never in the thousands
            drc['healthcareWorkers'] = v

    if provs:
        drc['provinces'] = provs

    # ── Uganda ────────────────────────────────────────────────────────────────
    # All patterns require "Uganda" or "imported" proximity to avoid mis-attributing
    # DRC figures. Uganda count is always < 200, DRC always > 50.
    uga = result['uga']

    # Confirmed
    # DON603: "total of 2 confirmed cases ... in Kampala, Uganda"
    # DON605: "9 confirmed cases including 1 death" (Uganda section)
    # DON606/607: "19 confirmed cases including 2 deaths in imported cases"
    for pat in [
        r'total of (\d+)\s+confirmed cases?[^.]*?Uganda',
        r'In Uganda[^.]*?(\d+)\s+confirmed cases?',
        r'(\d+)\s+confirmed cases?[^.]*?(?:in imported|in Uganda)\b',
        r'Uganda[^.]*?(\d+)\s+confirmed',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if 0 < v < 200:
                uga['confirmed'] = v
                break

    # Deaths
    # DON603: "including 1 death have been reported in Kampala, Uganda"
    # DON606/607: "including 2 deaths in imported cases"
    for pat in [
        r'confirmed cases?[^.]*?including\s+(\d+)\s+deaths?[^.]*?Uganda',
        r'including\s+(\d+)\s+deaths?\s+in\s+imported',
        r'(\d+)\s+deaths?[^.]*?in\s+(?:imported|Uganda)',
        r'Uganda[^.]*?(\d+)\s+deaths?',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = _int(m.group(1))
            if 0 < v < 50:
                uga['deaths'] = v
                break

    # Probable deaths ("one probable case who has died")
    m = re.search(r'(\d+)\s+probable\s+(?:case[s]?\s+)?(?:who\s+(?:has|have)\s+)?died', text, re.IGNORECASE)
    if m:
        uga['probableDeaths'] = _int(m.group(1))

    # Recoveries — Uganda uses "recoveries" (plural noun), DRC uses "recovered" (verb/adj)
    # DON606: "5 recoveries have been reported"
    m = re.search(r'(\d+)\s+recoveries?\s+(?:have\s+been\s+)?reported', text, re.IGNORECASE)
    if m:
        uga['recovered'] = _int(m.group(1))

    # Districts with case counts: "Kampala (n=8) and Wakiso (n=1)"
    districts = {}
    for district in ['Kampala', 'Wakiso']:
        m = re.search(rf'{district}\s*\(n=(\d+)\)', text, re.IGNORECASE)
        if m:
            districts[district.lower()] = _int(m.group(1))
    if districts:
        uga['districts'] = districts

    return result


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_seen():
    try:
        with open(SEEN_FILE, encoding='utf-8') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen(seen):
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def _load_existing():
    try:
        with open(OUT_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _fetch(url):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode('utf-8', errors='replace')


# ── Public API ────────────────────────────────────────────────────────────────

def poll_and_update():
    """
    Check the WHO emergency events page for new DON bulletins.
    Parse any found, persist results to who_don.json, return latest data or None.
    """
    seen = _load_seen()

    try:
        events_html = _fetch(EMERGENCY_EVENTS_URL)
    except (URLError, OSError) as e:
        print(f'WHO DON: could not reach events page — {e}')
        return _load_existing()

    parser = _LinkParser()
    parser.feed(events_html)
    new_links = [lnk for lnk in parser.links if lnk not in seen]

    if not new_links:
        print(f'WHO DON: no new bulletins (seen {len(seen)}).')
        return _load_existing()

    print(f'WHO DON: {len(new_links)} new bulletin(s) found.')

    latest = None
    for url in new_links:
        don_id = url.rstrip('/').split('/')[-1]
        try:
            html = _fetch(url)
            data = _parse_don(html)
            data['url']       = url
            data['donId']     = don_id
            data['fetchedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            drc, uga = data['drc'], data['uga']
            print(
                f'  {don_id} (as of {data.get("asOf", "?")}): '
                f'DRC confirmed={drc.get("confirmed", "?")} deaths={drc.get("deaths", "?")} '
                f'suspected={drc.get("suspected", "?")} | '
                f'UGA confirmed={uga.get("confirmed", "?")} deaths={uga.get("deaths", "?")}'
            )
            # Keep highest DON number (lexically comparable: 2026-DON607 > 2026-DON606)
            if latest is None or don_id > latest['donId']:
                latest = data
            seen.add(url)
        except Exception as e:  # noqa: BLE001
            print(f'  {don_id}: parse error — {e}')

    _save_seen(seen)

    if latest is not None:
        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(latest, f, ensure_ascii=False, indent=2)
        return latest

    return _load_existing()


if __name__ == '__main__':
    import sys
    result = poll_and_update()
    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print('No WHO DON data available.', file=sys.stderr)
        sys.exit(1)
