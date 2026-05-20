#!/usr/bin/env python3
r"""
Fetches the WHO EIOS RSS feed and writes items to data/feed.json.

Schedule with Windows Task Scheduler:
  Action:  python "C:\Users\ADRIAN.CALVO\Webapps\ebola-outbreak-map\fetch_feed.py"
  Trigger: Every 3 hours

Or run manually:  python fetch_feed.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from xml.etree import ElementTree as ET

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fetch_feed.log')


def log(status, message):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f'[{ts}] [{status}] {message}\n')

FEED_URL = (
    'https://eios.who.int/portal/api/api/rssfeed/1779091159764'
    '?pinned=false&token=9092D77B-7AB5-4384-9555-7C541960C506'
)
OUT_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'feed.json')
OUT_JS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'feed.js')
MAX_ITEMS = 50


def fetch(url):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text)


def classify(title, description):
    text = (title + ' ' + description).lower()
    if any(w in text for w in [
        'pheic', 'public health emergency', 'urgence de santé',
        'emergency of international', 'urgencia sanitaria'
    ]):
        return 'pheic'
    if any(w in text for w in [
        'cases', 'casos', 'deaths', 'muertos', 'fallecidos',
        'décès', 'suspected', 'confirmed', 'sospechosos'
    ]):
        return 'cases'
    if any(w in text for w in [
        'response', 'respuesta', 'réponse', 'alert', 'alerte',
        'deployed', 'preparedness', 'airlines', 'border', 'travel'
    ]):
        return 'response'
    return 'analysis'


def official_weight(text):
    """Score source authority. 3=WHO/OMS, 2=MoH direct, 1=Africa CDC/national CDC, 0=media."""
    t = text.lower()
    if any(x in t for x in [
        'world health organization', 'organisation mondiale de la santé',
        'organización mundial de la salud', 'who declared', 'who has declared',
        'oms a déclaré', 'oms déclare', 'tedros', 'who said', 'who published',
    ]):
        return 3
    if any(x in t for x in [
        'ministre', 'minister of health', 'ministry of health',
        'ministère de la santé', 'ministerio de salud',
        'minister congolais', 'kamba', 'roger kamba',
        'minister ugandais', 'ugandan health minister',
    ]):
        return 2
    if any(x in t for x in [
        'africa cdc', 'africa centres for disease', 'us cdc', 'cdc said',
        'ncdc', 'centre for disease control',
    ]):
        return 1
    return 0


def word_to_num(text):
    """Replace small English number words with digits."""
    for word, n in [
        ('zero', '0'), ('one', '1'), ('two', '2'), ('three', '3'),
        ('four', '4'), ('five', '5'), ('six', '6'), ('seven', '7'),
        ('eight', '8'), ('nine', '9'), ('ten', '10'),
    ]:
        text = re.sub(rf'\b{word}\b', n, text, flags=re.IGNORECASE)
    return text


def extract_numbers(title, desc):
    """Pull epi figures from article text. Returns dict with found fields."""
    text = word_to_num(title + ' ' + desc)
    text = re.sub(r'(\d)[, ](\d{3})\b', r'\1\2', text)  # 1,234 -> 1234

    result = {}

    # Deaths / décès / muertos
    for pat in [
        r'(\d+)\s*(?:probable\s+)?(?:deaths?|dead)\b',
        r'(\d+)\s*(?:probable[s]?\s+)?(?:décès|morts?)\b',
        r'(\d+)\s*(?:probable[s]?\s+)?(?:muertos?|fallecidos?|muertes?)\b',
        r'(?:more than|at least|over|nearly|almost|environ|plus de|près de|'
        r'más de|déjà|already|casi)\s+(\d+)\s*(?:deaths?|dead|décès|morts?|muertos?)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 10_000 and v > result.get('deaths', 0):
                result['deaths'] = v

    # Suspected / total cases
    for pat in [
        r'(\d+)\s+(?:suspected\s+)?cases?\b',
        r"(\d+)\s+cas\s+(?:suspects?|d'Ebola)",
        r'(\d+)\s+cas\s+suspects?\b',
        r'(\d+)\s+casos?\s+(?:sospechosos?|de\s+[eé]bola)\b',
        r'(?:environ|plus de|près de|more than|over|at least)\s+(\d+)\s+(?:suspected\s+)?cases?',
        r'(?:environ|plus de|près de)\s+(\d+)\s+cas\b',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 100_000 and v > result.get('suspected', 0):
                result['suspected'] = v

    # Confirmed cases
    for pat in [
        r'(\d+)\s+confirmed\s+cases?\b',
        r'(\d+)\s+cas\s+confirmés?\b',
        r'(\d+)\s+casos?\s+confirmados?\b',
        r'confirmed\s+cases?\s*[:\-–]\s*(\d+)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 100_000 and v > result.get('confirmed', 0):
                result['confirmed'] = v

    # Active patients under care
    for pat in [
        r'(\d+)\s+malades?\s+(?:qui\s+sont\s+)?activement\s+pris\s+en\s+charge',
        r'(\d+)\s+(?:malades?|patients?)\s+(?:activement|currently|under\s+(?:active\s+)?(?:care|treatment))',
        r'(\d+)\s+patients?\s+(?:receiving\s+(?:active\s+)?care|hospitali[sz]ed|in\s+treatment)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 10_000 and v > result.get('active', 0):
                result['active'] = v

    return result


def extract_uga_cases(items):
    """Extract Uganda-specific case count from items mentioning Uganda."""
    best = 0
    for item in items:
        text = word_to_num(item['title'] + ' ' + item.get('desc', ''))
        text = re.sub(r'(\d)[, ](\d{3})\b', r'\1\2', text)
        if not re.search(r'uganda|ouganda', text, re.IGNORECASE):
            continue
        for pat in [
            # "X cases in/imported to Uganda"
            r'(\d+)\s+(?:confirmed\s+)?cases?\s+(?:in|reported in|imported\s+(?:to|in))\s+(?:neighboring\s+)?Uganda',
            # "X in neighboring Uganda"
            r'(\d+)\s+in\s+(?:neighboring\s+)?Uganda',
            # "confirmed X cases" / "X confirmed cases" within Uganda item
            r'confirmed\s+(\d+)\s+cases?',
            r'(\d+)\s+confirmed\s+cases?',
            # "X cases coming from DRC" in Uganda article
            r'(\d+)\s+cases?\s+coming\s+from',
            # general case count near Uganda mention
            r'Uganda[^.]{0,80}?(\d+)\s+(?:confirmed\s+)?cases?',
            r'(\d+)\s+(?:confirmed\s+)?cases?[^.]{0,50}?Uganda',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                if 0 < v < 1000 and v > best:
                    best = v
        # French "d'un autre en Ouganda" = 1 case/death in Uganda
        if re.search(r"d'un autre en Ouganda", text, re.IGNORECASE) and best < 1:
            best = 1
    return best or None


def compute_stats(items):
    """Build the most authoritative epi summary, preferring WHO > MoH > CDC > media."""
    # Collect {weight: {field: (value, source_title)}}
    by_weight = {3: {}, 2: {}, 1: {}, 0: {}}
    has_pheic = any(i['tag'] == 'pheic' for i in items)

    for item in items:
        text = item['title'] + ' ' + item.get('desc', '')
        w = official_weight(text)
        nums = extract_numbers(item['title'], item.get('desc', ''))
        for field, val in nums.items():
            prev_val, _ = by_weight[w].get(field, (0, ''))
            if val > prev_val:
                by_weight[w][field] = (val, item['title'][:80])

    # Merge: highest weight wins per field
    drc = {}
    sources = {}
    for w in (3, 2, 1, 0):
        for field, (val, src) in by_weight[w].items():
            if field not in drc:
                drc[field] = val
                sources[field] = f'w{w}: {src}'

    uga_cases = extract_uga_cases(items)
    uga_mentioned = any(
        re.search(r'uganda|ouganda', item['title'] + ' ' + item.get('desc', ''), re.IGNORECASE)
        for item in items
    )

    # Source label for UI
    weight_label = {3: 'WHO / OMS', 2: 'Ministry of Health (DRC)', 1: 'Africa CDC / national CDC', 0: 'media reports'}
    winning_weight = 0
    for w in (3, 2, 1, 0):
        if any(field in by_weight[w] for field in ('deaths', 'suspected')):
            winning_weight = w
            break

    uga = {}
    if uga_cases:
        uga['cases'] = uga_cases
    if uga_mentioned:
        uga['mentioned'] = True

    return {
        'drc': drc,
        'uga': uga,
        'whoAlert': 'PHEIC' if has_pheic else None,
        'sourceLabel': weight_label[winning_weight],
    }


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    channel = root.find('channel')

    items = []
    for item in channel.findall('item')[:MAX_ITEMS]:
        title = (item.findtext('title') or '').strip()
        link  = (item.findtext('link')  or '').strip()
        desc  = strip_html(item.findtext('description') or '').strip()
        pub   = (item.findtext('pubDate') or '').strip()

        if not title or not link:
            continue

        items.append({
            'title':   title,
            'link':    link,
            'pubDate': pub,
            'desc':    desc[:500],
            'tag':     classify(title, desc),
        })

    return {
        'feedTitle':     (channel.findtext('title') or '').strip(),
        'lastBuildDate': (channel.findtext('lastBuildDate') or '').strip(),
        'fetchedAt':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'itemCount':     len(items),
        'stats':         compute_stats(items),
        'items':         items,
    }


def main():
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

    print('Fetching feed...')
    try:
        xml_bytes = fetch(FEED_URL)
    except (URLError, OSError) as e:
        msg = f'Could not reach feed — {e}'
        print(f'ERROR: {msg}', file=sys.stderr)
        log('ERROR', msg)
        sys.exit(1)

    print('Parsing XML...')
    try:
        data = parse(xml_bytes)
    except ET.ParseError as e:
        msg = f'Could not parse XML — {e}'
        print(f'ERROR: {msg}', file=sys.stderr)
        log('ERROR', msg)
        sys.exit(1)

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(OUT_JS_FILE, 'w', encoding='utf-8') as f:
        f.write('window.FEED_DATA = ')
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(';\n')

    s = data['stats']
    d = s.get('drc', {})
    u = s.get('uga', {})
    summary = (
        f'{data["itemCount"]} items — '
        f'DRC deaths={d.get("deaths","—")} suspected={d.get("suspected","—")} '
        f'confirmed={d.get("confirmed","—")} active={d.get("active","—")} | '
        f'UGA cases={u.get("cases","—")} | '
        f'Alert={s.get("whoAlert","—")} source={s.get("sourceLabel","—")}'
    )
    print(f'OK — {summary}')
    print(f'Feed built:  {data["lastBuildDate"]}')
    print(f'Fetched at:  {data["fetchedAt"]}')
    log('OK', summary)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        msg = f'Unhandled exception: {e}\n{traceback.format_exc()}'
        print(msg, file=sys.stderr)
        log('ERROR', f'Unhandled exception: {e}')
        sys.exit(1)
