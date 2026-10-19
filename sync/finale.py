"""Finale Inventory pull for the FJ dashboard.

Finale is Funky Junque's source of truth for warehouse stock and pushes the FBM quantity to Amazon.
Its Amazon push task (checked 2026-10-19) lists "Remaining after reservations (include BOM)" with
stock source = Northbrook. This module reproduces that number per product:

    fbm(p) = max(0, on hand at Northbrook - reserved at Northbrook)          (own stock)
           + min over BOM components c of floor(own(c) / qty(c))             (bundles buildable)

and adds open purchase orders (on order qty + earliest expected receive date).

REST calls per load (Finale limit: 300 collection requests / hour):
  inventoryitem/  (~3 MB, every load)
  order/?filter=PURCHASE_ORDER  (~7 MB, every load)
  product/  (~130 MB full; cached in Supabase sync_state 'finale_products', refreshed incrementally
             by lastUpdatedDate, full re-pull weekly) -> BOM + ASIN per product

Env: FINALE_API_KEY + FINALE_API_SECRET, or one FINALE_API_CONNECTION holding both
     ("key:secret", the two on separate lines / separated by spaces, or with "key:" / "secret:" labels);
     FINALE_ACCOUNT (default funkyjunque).
Everything degrades: if the secrets are missing or Finale errors, load() returns None and the sync
keeps Amazon's FBM numbers.
"""
import base64, datetime, json, math, os, re, time, urllib.request, urllib.error

ACCOUNT = os.environ.get('FINALE_ACCOUNT', 'funkyjunque').strip() or 'funkyjunque'
BASE = f'https://app.finaleinventory.com/{ACCOUNT}/api/'
# Northbrook (100000) and everything under it (Northbrook Sub 100001, inactive FBM 100005).
# Stock source of Finale's "Push stock levels to Amazon" task.
WAREHOUSE_ROOT = '100000'
FULL_PRODUCT_REFRESH_DAYS = 7


_LABEL = re.compile(r'^(finale|api|key|secret|api[_ -]?key|api[_ -]?secret|key[_ -]?secret|account|funkyjunque)[:=]?$', re.I)

def _creds():
    """(key, secret) or a ready 'Basic ...' header string, or None. Never logs the values."""
    k, sct = os.environ.get('FINALE_API_KEY', '').strip(), os.environ.get('FINALE_API_SECRET', '').strip()
    if k and sct: return (k, sct)
    raw = os.environ.get('FINALE_API_CONNECTION', '').strip()
    if not raw: return None
    if raw.lower().startswith('basic '): return raw
    if ':' in raw and not re.search(r'\s', raw):
        a, b = raw.split(':', 1)
        if a and b: return (a, b)
    # labelled or whitespace / comma separated: "API key: X\nSecret: Y", "X Y", "X,Y"
    parts = [t for t in re.split(r'[\s,;]+|(?<=[A-Za-z]):(?=\s|$)', raw) if t]
    parts = [t.rstrip(':=') if _LABEL.match(t) else t for t in parts]
    vals = [t for t in parts if t and not _LABEL.match(t)]
    vals = [t.split('=', 1)[1] if re.match(r'^(key|secret|api_?key|api_?secret)=', t, re.I) else t for t in vals]
    if len(vals) == 2: return (vals[0], vals[1])
    print(f'finale: FINALE_API_CONNECTION is set but could not be split into a key and a secret '
          f'(found {len(vals)} value(s), {len(raw)} chars). Use "key:secret".', flush=True)
    return None

def enabled():
    return _creds() is not None


def _auth():
    c = _creds()
    if isinstance(c, str): return c if c.startswith('Basic ') else 'Basic ' + c.split(None, 1)[1]
    return 'Basic ' + base64.b64encode(f'{c[0]}:{c[1]}'.encode()).decode()


def get(path, params=None, tries=4):
    url = BASE + path
    if params:
        url += ('&' if '?' in url else '?') + '&'.join(f'{k}={v}' for k, v in params.items())
    for i in range(tries):
        req = urllib.request.Request(url, headers={'Authorization': _auth(), 'Accept': 'application/json',
                                                   'User-Agent': 'fj-dashboard-sync'})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode('utf-8', 'replace')
            if e.code == 429 or e.code >= 500:
                wait = 20 * (i + 1)
                print(f'  finale {path}: HTTP {e.code}, retry in {wait}s', flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f'finale {path}: HTTP {e.code} {body}')
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1: raise
            time.sleep(10 * (i + 1))
    raise RuntimeError(f'finale {path}: gave up after {tries} tries')


def _b64filter(obj):
    return base64.b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode()


def _cols(d, key):
    """Finale returns column-major tables; missing columns come back as a list of None."""
    v = d.get(key)
    n = len(next(iter(d.values()))) if d else 0
    return v if isinstance(v, list) else [None] * n


def _tail(url):
    return (url or '').rstrip('/').split('/')[-1]


def _facility_tree():
    d = get('facility')
    parent = {_tail(u): _tail(p) for u, p in zip(_cols(d, 'facilityUrl'), _cols(d, 'parentFacilityUrl'))}
    names = {_tail(u): n for u, n in zip(_cols(d, 'facilityUrl'), _cols(d, 'facilityName'))}
    def under(fid, root):
        seen = 0
        while fid and seen < 10:
            if fid == root: return True
            fid, seen = parent.get(fid), seen + 1
        return False
    return under, names


def _products(cache):
    """BOM + ASIN per product id, cached. cache = {'stamp': iso, 'full': iso, 'p': {pid: [asin, active, [[comp, qty]...]]}}"""
    now = datetime.datetime.utcnow()
    cache = cache if isinstance(cache, dict) and cache.get('p') else None
    full_due = (not cache or not cache.get('full')
                or now - datetime.datetime.fromisoformat(cache['full']) > datetime.timedelta(days=FULL_PRODUCT_REFRESH_DAYS)
                or now - datetime.datetime.fromisoformat(cache['stamp']) > datetime.timedelta(days=25))
    if full_due:
        print('  finale: full product pull (BOM + ASIN)...', flush=True)
        d = get('product/')
        prods = {}
    else:
        since = (datetime.datetime.fromisoformat(cache['stamp']) - datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
        d = get('product/', {'filter': _b64filter({'lastUpdatedDate': [since, None]})})
        prods = dict(cache['p'])
    ids = _cols(d, 'productId')
    for pid, asin, st, assoc in zip(ids, _cols(d, 'amazonStandardIdentificationNumber'),
                                    _cols(d, 'statusId'), _cols(d, 'productAssocList')):
        if not pid: continue
        bom = []
        for a in assoc or []:
            if a.get('productAssocTypeId') == 'MANUF_COMPONENT':
                for it in a.get('productAssocItemList') or []:
                    if it.get('productId') and (it.get('quantity') or 0) > 0:
                        bom.append([it['productId'], it['quantity']])
        prods[pid] = [(asin or '').strip().upper(), st == 'PRODUCT_ACTIVE', bom]
    print(f"  finale: products {'full' if full_due else 'incremental'}: {len(ids)} fetched, {len(prods)} cached", flush=True)
    stamp = now.isoformat(timespec='seconds')
    return {'stamp': stamp, 'full': stamp if full_due else cache['full'], 'p': prods}


def load(state_get, state_put):
    """Pull Finale and return {'fbm': {pid: qty}, 'own': {pid: qty}, 'po': {pid: [onOrder, etaISO, etaQty]},
    'products': {pid: [asin, active, bom]}, 'asinProducts': {asin: [pid...]}, 'at': iso}. None on failure."""
    if not enabled():
        print('finale: no usable Finale credentials (FINALE_API_KEY/SECRET or FINALE_API_CONNECTION) - keeping Amazon FBM numbers', flush=True)
        return None
    try:
        t0 = time.time()
        under, names = _facility_tree()
        inv = get('inventoryitem/')
        qoh, res = {}, {}
        po_qty = {}           # pid -> {orderId: qty on order}
        for pid, fac, oh, oo, rs, ou in zip(_cols(inv, 'productId'), _cols(inv, 'facilityUrl'),
                _cols(inv, 'quantityOnHand'), _cols(inv, 'quantityOnOrder'),
                _cols(inv, 'quantityReserved'), _cols(inv, 'orderUrl')):
            if not pid: continue
            f = _tail(fac)
            if under(f, WAREHOUSE_ROOT):
                qoh[pid] = qoh.get(pid, 0) + (oh or 0)
                res[pid] = res.get(pid, 0) + (rs or 0)
            if (oo or 0) > 0:
                po_qty.setdefault(pid, {})
                o = _tail(ou)
                po_qty[pid][o] = po_qty[pid].get(o, 0) + oo

        # purchase orders -> expected receive dates
        po = get('order/', {'filter': _b64filter({'orderTypeId': ['PURCHASE_ORDER', 'PURCHASE_ORDER']})})
        recv = {_tail(u): (rd or '')[:10] for u, rd in zip(_cols(po, 'orderUrl'), _cols(po, 'receiveDate'))}

        cache = _products(state_get('finale_products'))
        state_put('finale_products', cache)
        prods = cache['p']

        own = {p: max(0, int(qoh.get(p, 0) - res.get(p, 0))) for p in set(qoh) | set(res)}
        memo = {}
        def total(p, depth=0):
            """own stock + units buildable from components (nested BOMs followed, depth-limited)."""
            if p in memo: return memo[p]
            base = own.get(p, 0)
            bom = (prods.get(p) or [None, None, []])[2]
            build = 0
            if bom and depth < 4:
                build = min(math.floor(total(c, depth + 1) / q) for c, q in bom)
            memo[p] = base + max(0, build)
            return memo[p]
        fbm = {p: total(p) for p in set(own) | {p for p, v in prods.items() if v[2]}}

        po_out = {}
        for p, orders in po_qty.items():
            dated = sorted(((recv.get(o) or '9999'), q) for o, q in orders.items())
            eta, eta_q = dated[0]
            po_out[p] = [int(sum(orders.values())), '' if eta == '9999' else eta, int(eta_q)]

        asin_products = {}
        for p, (asin, active, _) in prods.items():
            if asin: asin_products.setdefault(asin, []).append(p)

        print(f"finale: {len(own)} products with Northbrook stock ({sum(own.values()):,} units after reservations) | "
              f"{sum(1 for p, v in prods.items() if v[2])} bundles (BOM) | {len(po_out)} products on {len({o for x in po_qty.values() for o in x})} open POs "
              f"({sum(v[0] for v in po_out.values()):,} units) | {time.time() - t0:.0f}s", flush=True)
        return {'fbm': fbm, 'own': own, 'po': po_out, 'products': prods, 'asinProducts': asin_products,
                'at': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'}
    except Exception as e:
        print('finale: pull FAILED, keeping Amazon FBM numbers:', str(e)[:300], flush=True)
        return None


def match_products(asin, skus, fin):
    """Finale product ids for a dashboard row: its Amazon SKUs that are Finale product ids,
    else the Finale products whose ASIN field is this ASIN."""
    hit = [s for s in skus if s in fin['products'] or s in fin['own']]
    if hit: return sorted(set(hit)), 'sku'
    by_asin = [p for p in fin['asinProducts'].get((asin or '').upper(), []) if fin['products'][p][1]] \
              or fin['asinProducts'].get((asin or '').upper(), [])
    return sorted(set(by_asin)), ('asin' if by_asin else '')


def row_values(asin, skus, fin):
    """(fbm, onOrder, poEta, poEtaQty, productIds) for one ASIN, or None when Finale has no product for it.
    Several Finale products on one ASIN are distinct stock and are summed."""
    pids, how = match_products(asin, skus, fin)
    if not pids: return None
    fbm = sum(fin['fbm'].get(p, 0) for p in pids)
    pos = [fin['po'][p] for p in pids if p in fin['po']]
    on_order = sum(x[0] for x in pos)
    dated = sorted((x[1], x[2]) for x in pos if x[1])
    eta, eta_q = (dated[0] if dated else ('', 0))
    return {'fbm': fbm, 'onOrder': on_order, 'poEta': eta, 'poEtaQty': eta_q if eta else 0,
            'finaleIds': pids, 'how': how}


def bundle_units(units_by_asin, fin):
    """Units of each component ASIN consumed by bundle sales: bundle units x component qty.
    units_by_asin = {asin: units sold}. Returns {component asin: units}."""
    out = {}
    for p, (asin, _, bom) in fin['products'].items():
        if not bom or not asin: continue
        u = units_by_asin.get(asin, 0)
        if not u: continue
        for c, q in bom:
            ca = (fin['products'].get(c) or [''])[0]
            if ca and ca != asin:
                out[ca] = out.get(ca, 0) + u * q
    return {a: int(round(v)) for a, v in out.items()}
