"""Minimal SP-API client (env-var driven, for GitHub Actions)."""
import json, os, time, urllib.parse, urllib.request

ENDPOINT = os.environ.get('SPAPI_ENDPOINT', 'https://sellingpartnerapi-na.amazon.com')
MARKETPLACE_ID = os.environ.get('MARKETPLACE_ID', 'ATVPDKIKX0DER')
SELLER_ID = os.environ.get('SELLER_ID', '')

_token_cache = {'token': None, 'exp': 0}

def access_token():
    if _token_cache['token'] and time.time() < _token_cache['exp'] - 120:
        return _token_cache['token']
    data = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'refresh_token': os.environ['LWA_REFRESH_TOKEN'],
        'client_id': os.environ['LWA_CLIENT_ID'],
        'client_secret': os.environ['LWA_CLIENT_SECRET'],
    }).encode()
    req = urllib.request.Request('https://api.amazon.com/auth/o2/token', data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req) as r:
        d = json.load(r)
    _token_cache['token'] = d['access_token']
    _token_cache['exp'] = time.time() + d['expires_in']
    return d['access_token']

def get(path, params=None, retries=4):
    url = ENDPOINT + path
    if params:
        url += '?' + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            'x-amz-access-token': access_token(), 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f'HTTP {e.code} on {path}: {body[:500]}')
    raise RuntimeError(f'exhausted retries on {path}')

def get_all_pages(path, params, list_key, next_token_param='nextToken',
                  payload_key='payload', pause=1.2):
    items, params = [], dict(params or {})
    while True:
        d = get(path, params)
        payload = d.get(payload_key, d) if payload_key else d
        items.extend(payload.get(list_key, []))
        nt = payload.get('nextToken') or d.get('nextToken') or (d.get('pagination') or {}).get('nextToken')
        if not nt:
            return items
        params = {**params, next_token_param: nt}
        time.sleep(pause)

def create_report(report_type, options=None, start=None, end=None, tries=15):
    """Create a report, retrying 429s (createReport is ~1/min)."""
    spec = {'reportType': report_type, 'marketplaceIds': [MARKETPLACE_ID]}
    if options: spec['reportOptions'] = options
    if start: spec['dataStartTime'] = start
    if end: spec['dataEndTime'] = end
    body = json.dumps(spec).encode()
    for t in range(tries):
        try:
            req = urllib.request.Request(ENDPOINT + '/reports/2021-06-30/reports', data=body, method='POST',
                headers={'x-amz-access-token': access_token(), 'Content-Type': 'application/json'})
            return json.load(urllib.request.urlopen(req))['reportId']
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(70); continue
            raise RuntimeError(f'createReport {report_type}: HTTP {e.code} {e.read().decode()[:300]}')
    raise RuntimeError(f'createReport {report_type}: retries exhausted')

def fetch_report(rid, poll=15, polls=50, binary=False):
    """Poll a report to DONE and return its (decompressed) content, or None on failure."""
    import gzip
    doc_id = None
    for _ in range(polls):
        time.sleep(poll)
        r = get(f'/reports/2021-06-30/reports/{rid}')
        r = r.get('payload', r)
        st = r.get('processingStatus')
        if st == 'DONE':
            doc_id = r['reportDocumentId']; break
        if st in ('CANCELLED', 'FATAL'):
            return None
    if not doc_id:
        return None
    doc = get(f'/reports/2021-06-30/documents/{doc_id}')
    doc = doc.get('payload', doc)
    raw = urllib.request.urlopen(doc['url']).read()
    if doc.get('compressionAlgorithm') == 'GZIP':
        raw = gzip.decompress(raw)
    return raw
