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
import subprocess
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
OUT_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'feed.json')
OUT_JS_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'feed.js')
HIGH_WATER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'high_water.json')
MAX_ITEMS = 100

# ── Qualifier words that precede approximate numbers ─────────────────────────
# EN / FR / ES / PT / AR
_Q = (
    r'(?:more than|over|at least|nearly|almost|about|around|up to|approximately|already|'
    r'plus de|environ|au moins|près de|presque|déjà|à peu près|'
    r'más de|al menos|casi|alrededor de|unos?|unas?|cerca de|aproximadamente|'
    r'mais de|pelo menos|quase|'
    r'أكثر من|على الأقل|نحو|حوالي|ما يزيد على)'
)


def fetch(url):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text)


def normalize_numerals(text):
    """Convert Arabic-Indic (٠–٩) and Persian (۰–۹) digits to ASCII."""
    for i, c in enumerate('٠١٢٣٤٥٦٧٨٩'):   # U+0660–U+0669
        text = text.replace(c, str(i))
    for i, c in enumerate('۰۱۲۳۴۵۶۷۸۹'):   # U+06F0–U+06F9
        text = text.replace(c, str(i))
    return text


def classify(title, description):
    text = (title + ' ' + description).lower()
    if any(w in text for w in [
        'pheic', 'public health emergency', 'urgence de santé',
        'emergency of international', 'urgencia sanitaria',
        'urgência sanitária', 'طوارئ صحية دولية',
    ]):
        return 'pheic'
    if any(w in text for w in [
        'cases', 'casos', 'deaths', 'muertos', 'fallecidos',
        'décès', 'suspected', 'confirmed', 'sospechosos',
        'suspeitos', 'cas suspects', 'حالات', 'وفيات',
    ]):
        return 'cases'
    if any(w in text for w in [
        'response', 'respuesta', 'réponse', 'alert', 'alerte',
        'deployed', 'preparedness', 'airlines', 'border', 'travel',
        'resposta', 'استجابة',
    ]):
        return 'response'
    return 'analysis'


def official_weight(text):
    """Score source authority. 3=WHO/OMS, 2=MoH direct, 1=Africa CDC/national CDC, 0=media."""
    t = text.lower()
    if any(x in t for x in [
        # EN — both US ('z') and UK ('s') spellings
        'world health organization', 'world health organisation',
        'who declared', 'who has declared', 'who said', 'who says',
        'who published', 'who reports', 'who warned', 'who warns',
        'according to the who', 'according to who', 'tedros',
        # FR
        'organisation mondiale de la santé', 'oms a déclaré', 'oms déclare',
        'oms a publié', "selon l'oms", "d'après l'oms",
        # ES
        'organización mundial de la salud', 'oms ha declarado',
        'según la oms', 'la oms informó', 'la oms señaló',
        # PT
        'organização mundial da saúde', 'segundo a oms',
        # AR
        'منظمة الصحة العالمية',
    ]):
        return 3
    if any(x in t for x in [
        # EN
        'minister of health', 'ministry of health',
        # FR
        'ministre', 'ministère de la santé', 'minister congolais',
        'kamba', 'roger kamba',
        # ES
        'ministerio de salud',
        # PT
        'ministério da saúde',
        # AR
        'وزارة الصحة',
        # Named officials
        'minister ugandais', 'ugandan health minister',
    ]):
        return 2
    if any(x in t for x in [
        'africa cdc', 'africa centres for disease', 'us cdc', 'cdc said',
        'ncdc', 'centre for disease control',
        'المركز الأفريقي لمكافحة',
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
    """Pull epi figures from article text (EN/FR/ES/PT/AR). Returns dict with found fields."""
    text = normalize_numerals(word_to_num(title + ' ' + desc))
    text = re.sub(r'(\d)[,\s](\d{3})\b', r'\1\2', text)  # 1,234 / 1 234 -> 1234

    result = {}

    # ── Deaths ────────────────────────────────────────────────────────────────
    _D = (
        r'deaths?|dead|fatalities|killed|'                           # EN
        r'décès|morts?|personnes?\s+mortes?|victimes?\s+mortelles?|' # FR
        r'muertos?|fallecidos?|muertes?|víctimas?|decesos?|'         # ES
        r'mortes?|óbitos?|falecidos?|'                               # PT
        r'وفيات|وفاة|ضحايا|قتلى|متوفى'                             # AR
    )
    for pat in [
        r'(\d+)\s*(?:probable[s]?\s+)?(?:' + _D + r')\b',
        _Q + r'\s+(\d+)\s*(?:' + _D + r')',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 10_000 and v > result.get('deaths', 0):
                result['deaths'] = v

    # ── Suspected / total cases ───────────────────────────────────────────────
    for pat in [
        # EN
        r'(\d+)\s+(?:suspected\s+)?cases?\b',
        _Q + r'\s+(\d+)\s+(?:suspected\s+)?(?:cases?|infected)',
        r'(?:infected|sickened)\s+' + _Q + r'\s+(\d+)',
        # FR
        r"(\d+)\s+cas\s+(?:suspects?|déclarés?|potentiels?|présumés?|d'[ée]bola|de\s+malades?)\b",
        r'(\d+)\s+cas\s+suspects?\b',
        r'(\d+)\s+personnes?\s+(?:suspectées?|infectées?|touchées?)\b',
        _Q + r'\s+(\d+)\s+(?:personnes?\s+suspectées?|cas)\b',
        # ES
        r'(\d+)\s+casos?\s+(?:sospechosos?|de\s+[eé]bola|presuntos?|posibles?)\b',
        _Q + r'\s+(\d+)\s+casos?\b',
        # PT
        r'(\d+)\s+casos?\s+suspeitos?\b',
        r'(\d+)\s+pessoas?\s+(?:suspeitas?|infetadas?|doentes?)\b',
        # AR
        r'(\d+)\s+(?:حالات|حالة)\s+(?:مشتبه|مشتبه\s+بها|مريضة|مصابة)',
        _Q + r'\s+(\d+)\s+(?:حالات|حالة)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 100_000 and v > result.get('suspected', 0):
                result['suspected'] = v

    # ── Confirmed cases ───────────────────────────────────────────────────────
    for pat in [
        # EN
        r'(\d+)\s+confirmed\s+cases?\b',
        r'confirmed\s+cases?\s*[:\-–]\s*(\d+)',
        # FR
        r'(\d+)\s+cas\s+confirmés?\b',
        r'cas\s+confirmés?\s*[:\-–]\s*(\d+)',
        # ES / PT
        r'(\d+)\s+casos?\s+confirmados?\b',
        r'confirmados?\s*[:\-–]\s*(\d+)',
        # AR
        r'(\d+)\s+(?:حالات|حالة)\s+مؤكدة',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 100_000 and v > result.get('confirmed', 0):
                result['confirmed'] = v

    # ── Active patients under care ────────────────────────────────────────────
    for pat in [
        # FR
        r'(\d+)\s+malades?\s+(?:qui\s+sont\s+)?activement\s+pris\s+en\s+charge',
        r'(\d+)\s+(?:malades?|patients?)\s+(?:activement|currently|under\s+(?:active\s+)?(?:care|treatment))',
        # EN
        r'(\d+)\s+patients?\s+(?:receiving\s+(?:active\s+)?care|hospitali[sz]ed|in\s+treatment)',
        # ES
        r'(\d+)\s+pacientes?\s+(?:hospitalizados?|en\s+tratamiento|bajo\s+atenci[oó]n)',
        # AR
        r'(\d+)\s+(?:مرضى|مريض)\s+(?:يتلقون|يتلقى|تحت)\s+(?:العلاج|الرعاية)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            v = int(m.group(1))
            if 0 < v < 10_000 and v > result.get('active', 0):
                result['active'] = v

    return result


def extract_uga_cases(items):
    """Extract Uganda-specific case count from items mentioning Uganda (EN/FR/ES/PT/AR).

    Every pattern must require explicit Uganda/Ouganda proximity to the number
    to avoid mistakenly attributing DRC totals or combined figures to Uganda.
    """
    best = 0
    for item in items:
        text = normalize_numerals(word_to_num(item['title'] + ' ' + item.get('desc', '')))
        text = re.sub(r'(\d)[,\s](\d{3})\b', r'\1\2', text)
        if not re.search(r'uganda|ouganda|أوغندا', text, re.IGNORECASE):
            continue
        for pat in [
            # EN — number must be anchored to Uganda as the explicit location.
            # "X cases in Uganda" / "X confirmed cases in Uganda"
            r'(\d+)\s+(?:confirmed\s+)?cases?\s+(?:in|reported\s+in)\s+(?:neighboring\s+)?Uganda\b',
            r'(\d+)\s+(?:confirmed\s+)?cases?\s+imported\s+(?:to|in)\s+Uganda\b',
            r'(\d+)\s+(?:confirmed\s+)?(?:deaths?|cases?)\s+in\s+Uganda\b',
            r'(\d+)\s+in\s+(?:neighboring\s+)?Uganda\b',
            r'confirmed\s+(\d+)\s+cases?\s+(?:in|from)\s+Uganda\b',
            r'(\d+)\s+confirmed\s+cases?\s+(?:in|from)\s+Uganda\b',
            # Qualifier + "X cases in Uganda" — require explicit "in Uganda", not just Uganda nearby
            _Q + r'\s+(\d+)\s+(?:confirmed\s+)?cases?\s+in\s+Uganda\b',
            # FR — "X cas confirmés/suspects en Ouganda" — number before "en Ouganda"
            r'(\d+)\s+cas\s+(?:confirmés?|suspects?|positifs?|importés?)\s+(?:en|au)\s+(?:Ouganda|Uganda)\b',
            r'(\d+)\s+personnes?\s+(?:suspectées?|infectées?|en\s+quarantaine)[^.]{0,20}?en\s+(?:Ouganda|Uganda)\b',
            # ES — "X casos confirmados/sospechosos en Uganda"
            r'(\d+)\s+casos?\s+(?:confirmados?|sospechosos?|positivos?)\s+en\s+Uganda\b',
            # PT — "X casos confirmados/suspeitos em Uganda"
            r'(\d+)\s+casos?\s+(?:confirmados?|suspeitos?)\s+em\s+Uganda\b',
            # AR — number + case word + explicit Uganda preposition
            r'(\d+)\s+(?:حالات|حالة)\s+(?:في|بـ)\s+(?:أوغندا|Uganda)\b',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                if 0 < v < 10_000 and v > best:
                    best = v
        # FR: "d'un autre en Ouganda" = 1 case
        if re.search(r"d'un autre en Ouganda", text, re.IGNORECASE) and best < 1:
            best = 1
    # Ceiling: Uganda has imported cases from DRC — counts approaching DRC totals
    # are almost certainly DRC figures misattributed to Uganda.
    if best > 200:
        return None
    return best or None


def compute_stats(items):
    """Build the most authoritative epi summary, preferring WHO > MoH > CDC > media."""
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

    drc = {}
    sources = {}
    for w in (3, 2, 1, 0):
        for field, (val, src) in by_weight[w].items():
            if field not in drc:
                drc[field] = val
                sources[field] = f'w{w}: {src}'

    # Deaths must be directly WHO-attributed (weight 3).  If no weight-3
    # article extracted a death figure, drop the field so the frontend falls
    # back to the high-water mark rather than showing a media-reported figure.
    if 'deaths' not in by_weight[3]:
        drc.pop('deaths', None)

    uga_cases = extract_uga_cases(items)
    uga_mentioned = any(
        re.search(r'uganda|ouganda|أوغندا', item['title'] + ' ' + item.get('desc', ''), re.IGNORECASE)
        for item in items
    )

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


# ── High-water mark persistence ───────────────────────────────────────────────

def load_high_water():
    """Load persisted high-water marks, or return empty structure."""
    try:
        with open(HIGH_WATER_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'drc': {}, 'uga': {}}


def save_high_water(hw):
    with open(HIGH_WATER_FILE, 'w', encoding='utf-8') as f:
        json.dump(hw, f, ensure_ascii=False, indent=2)


def update_high_water(hw, stats, fetched_at):
    """Merge new stats into high-water marks — values only ever increase."""
    source = stats.get('sourceLabel', 'WHO EIOS RSS')
    drc_suspected = (stats.get('drc') or {}).get('suspected')
    for country, fields in (('drc', ('deaths', 'suspected', 'confirmed', 'active')),
                             ('uga', ('cases',))):
        current = stats.get(country, {})
        stored  = hw.setdefault(country, {})
        for field in fields:
            new_val = current.get(field)
            if new_val is None:
                continue
            # Sanity-check Uganda cases against DRC totals — if a Uganda count
            # approaches the DRC suspected total it was almost certainly a DRC
            # figure misattributed to Uganda by the extractor.
            if country == 'uga' and field == 'cases':
                if new_val > 200:
                    continue
                if drc_suspected and new_val >= drc_suspected * 0.3:
                    continue
            prev     = stored.get(field, {})
            prev_val = prev.get('value') if isinstance(prev, dict) else None
            if prev_val is None or new_val > prev_val:
                stored[field] = {'value': new_val, 'asOf': fetched_at, 'source': source}
    return hw


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

    # Update high-water marks and embed them in the output
    hw = load_high_water()
    hw = update_high_water(hw, data['stats'], data['fetchedAt'])
    save_high_water(hw)
    data['highWater'] = hw

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(OUT_JS_FILE, 'w', encoding='utf-8') as f:
        f.write('window.FEED_DATA = ')
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(';\n')

    s = data['stats']
    d = s.get('drc', {})
    u = s.get('uga', {})
    hw_d = hw.get('drc', {})
    hw_u = hw.get('uga', {})
    summary = (
        f'{data["itemCount"]} items — '
        f'DRC deaths={d.get("deaths","—")} (hw={hw_d.get("deaths",{}).get("value","—")}) '
        f'suspected={d.get("suspected","—")} (hw={hw_d.get("suspected",{}).get("value","—")}) | '
        f'UGA cases={u.get("cases","—")} (hw={hw_u.get("cases",{}).get("value","—")}) | '
        f'Alert={s.get("whoAlert","—")} source={s.get("sourceLabel","—")}'
    )
    print(f'OK — {summary}')
    print(f'Feed built:  {data["lastBuildDate"]}')
    print(f'Fetched at:  {data["fetchedAt"]}')
    log('OK', summary)

    repo_root = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(
        ['git', 'add', 'data/feed.js', 'data/feed.json', 'data/high_water.json'],
        cwd=repo_root, check=True
    )
    result = subprocess.run(
        ['git', 'commit', '-m', 'Update feed data'],
        cwd=repo_root, capture_output=True, text=True
    )
    if result.returncode == 0:
        subprocess.run(['git', 'push'], cwd=repo_root, check=True)
        print('Pushed to remote.')
        log('OK', 'Committed and pushed feed data.')
    else:
        print('No changes to commit.')
        log('OK', 'No changes to commit — feed data unchanged.')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        msg = f'Unhandled exception: {e}\n{traceback.format_exc()}'
        print(msg, file=sys.stderr)
        log('ERROR', f'Unhandled exception: {e}')
        sys.exit(1)
