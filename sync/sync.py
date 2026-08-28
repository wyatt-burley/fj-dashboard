"""Funky Junque dashboard sync — runs in GitHub Actions.

MODE=fast (default, every 6h):
  FBA inventory summaries + planning report + FBM listings +
  Sales & Traffic 1-day / 7-day / 30-day (all-channel) reports,
  merge with sellery meta / monthly trend / images from Supabase sync_state,
  rebuild rows, upsert dashboard_snapshot. Refresh images for new ASINs.

MODE=monthly (weekly):
  12 monthly Sales & Traffic reports -> sync_state.monthly.

Env: LWA_CLIENT_ID, LWA_CLIENT_SECRET, LWA_REFRESH_TOKEN,
     SUPABASE_URL, SUPABASE_SERVICE_KEY  (optional: MARKETPLACE_ID, SELLER_ID, MODE)
"""
import calendar, datetime, json, os, re, statistics, time, urllib.request
import spapi

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SVC = os.environ['SUPABASE_SERVICE_KEY']
MODE = os.environ.get('MODE', 'fast')

def sb_req(path, method='GET', body=None, prefer=None):
    headers = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    if prefer:
        headers['Prefer'] = prefer
    req = urllib.request.Request(SUPABASE_URL + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req) as r:
        raw = r.read()
    return json.loads(raw) if raw else None

def state_get(key):
    rows = sb_req(f'/rest/v1/sync_state?key=eq.{key}&select=data')
    return rows[0]['data'] if rows else None

def state_put(key, data):
    sb_req('/rest/v1/sync_state?on_conflict=key', 'POST',
           {'key': key, 'data': data, 'updated_at': datetime.datetime.utcnow().isoformat() + 'Z'},
           prefer='resolution=merge-duplicates')

def num(v):
    try: return float(v)
    except (ValueError, TypeError): return 0.0

def tsv_rows(raw, encoding='utf-8'):
    lines = raw.decode(encoding, 'replace').splitlines()
    col = {h: i for i, h in enumerate(lines[0].split('\t'))}
    for ln in lines[1:]:
        row = ln.split('\t')
        yield lambda n, row=row: (row[col[n]] if n in col and col[n] < len(row) else '')

def st_units(raw):
    """Sales&Traffic JSON -> ({asin: units}, {asin: revenue})."""
    d = json.loads(raw)
    units, rev = {}, {}
    for e in d.get('salesAndTrafficByAsin', []):
        a = e.get('childAsin')
        s = e.get('salesByAsin', {})
        units[a] = units.get(a, 0) + s.get('unitsOrdered', 0)
        rev[a] = rev.get(a, 0) + (s.get('orderedProductSales', {}) or {}).get('amount', 0)
    return units, rev

# ---------------------------------------------------------------- monthly mode
def run_monthly():
    today = datetime.date.today()
    months = []
    y, m = today.year, today.month
    for i in range(12):
        mm, yy = m - i, y
        while mm < 1: mm += 12; yy -= 1
        months.append((yy, mm))
    months.reverse()
    monthly, keys = {}, []
    for yy, mm in months:
        start = datetime.date(yy, mm, 1)
        end = datetime.date(yy, mm, calendar.monthrange(yy, mm)[1])
        if end >= today: end = today - datetime.timedelta(days=2)
        if end < start: continue
        key = f'{yy}-{mm:02d}'
        rid = spapi.create_report('GET_SALES_AND_TRAFFIC_REPORT',
            {'dateGranularity': 'MONTH', 'asinGranularity': 'CHILD'},
            start.isoformat() + 'T00:00:00Z', end.isoformat() + 'T23:59:59Z')
        raw = spapi.fetch_report(rid, poll=12, polls=40)
        if raw:
            u, _ = st_units(raw)
            for a, n in u.items():
                monthly.setdefault(a, {})[key] = monthly.setdefault(a, {}).get(key, 0) + n
            keys.append(key)
            print(key, 'done', flush=True)
        else:
            print(key, 'FAILED', flush=True)
        time.sleep(50)
    units365 = {a: sum(mm.values()) for a, mm in monthly.items()}
    state_put('monthly', {'monthly': monthly, 'monthKeys': keys, 'units365': units365})
    print('monthly state saved:', len(monthly), 'ASINs,', len(keys), 'months')

# ------------------------------------------------------------------- fast mode
def run_fast():
    today = datetime.date.today()

    print('inventory summaries...', flush=True)
    items = spapi.get_all_pages('/fba/inventory/v1/summaries', {
        'granularityType': 'Marketplace', 'granularityId': spapi.MARKETPLACE_ID,
        'marketplaceIds': spapi.MARKETPLACE_ID, 'details': 'true'}, 'inventorySummaries', pause=0.6)
    skus = {}
    for it in items:
        d = it.get('inventoryDetails', {}) or {}
        unf = d.get('unfulfillableQuantity') or {}
        skus[it['sellerSku']] = {
            'asin': it.get('asin', ''), 'productName': it.get('productName', ''),
            'fulfillable': d.get('fulfillableQuantity', 0) or 0,
            'inboundWorking': d.get('inboundWorkingQuantity', 0) or 0,
            'inboundShipped': d.get('inboundShippedQuantity', 0) or 0,
            'inboundReceiving': d.get('inboundReceivingQuantity', 0) or 0,
            'unfulfillable': unf.get('totalUnfulfillableQuantity', 0) or 0,
        }
    print(' ', len(skus), 'SKUs', flush=True)

    # -- create all reports up front (429-paced), then collect
    print('creating reports...', flush=True)
    rid_planning = spapi.create_report('GET_FBA_INVENTORY_PLANNING_DATA')
    rid_fbm = spapi.create_report('GET_MERCHANT_LISTINGS_ALL_DATA')
    day = (today - datetime.timedelta(days=2)).isoformat()
    rid_1d = spapi.create_report('GET_SALES_AND_TRAFFIC_REPORT',
        {'dateGranularity': 'DAY', 'asinGranularity': 'CHILD'},
        day + 'T00:00:00Z', day + 'T23:59:59Z')
    end30 = today - datetime.timedelta(days=2)
    start30 = end30 - datetime.timedelta(days=29)
    rid_30 = spapi.create_report('GET_SALES_AND_TRAFFIC_REPORT',
        {'dateGranularity': 'DAY', 'asinGranularity': 'CHILD'},
        start30.isoformat() + 'T00:00:00Z', end30.isoformat() + 'T23:59:59Z')
    start7 = end30 - datetime.timedelta(days=6)
    rid_7 = spapi.create_report('GET_SALES_AND_TRAFFIC_REPORT',
        {'dateGranularity': 'DAY', 'asinGranularity': 'CHILD'},
        start7.isoformat() + 'T00:00:00Z', end30.isoformat() + 'T23:59:59Z')

    print('planning report...', flush=True)
    planning = {}
    raw = spapi.fetch_report(rid_planning)
    if raw:
        for g in tsv_rows(raw):
            sku = g('sku')
            if not sku: continue
            planning[sku] = {
                'aged': {'d0_90': int(num(g('inv-age-0-to-90-days'))),
                         'd91_180': int(num(g('inv-age-91-to-180-days'))),
                         'd181_270': int(num(g('inv-age-181-to-270-days'))),
                         'd271_365': int(num(g('inv-age-271-to-365-days'))),
                         'd365plus': int(num(g('inv-age-365-plus-days')))},
                'units7d': int(num(g('units-shipped-t7'))),
                'units30d': int(num(g('units-shipped-t30'))),
                'ais': sum(num(g(c)) for c in ['estimated-ais-181-210-days','estimated-ais-211-240-days',
                        'estimated-ais-241-270-days','estimated-ais-271-300-days','estimated-ais-301-330-days',
                        'estimated-ais-331-365-days','estimated-ais-365-plus-days']),
                'price': num(g('your-price')),
            }
    print(' ', len(planning), 'rows', flush=True)

    print('FBM listings report...', flush=True)
    fbm_by_sku = {}
    raw = spapi.fetch_report(rid_fbm)
    if raw:
        for g in tsv_rows(raw, 'cp1252'):
            sku = g('seller-sku')
            if not sku or g('fulfillment-channel').startswith('AMAZON'): continue
            q = g('quantity')
            fbm_by_sku[sku] = int(float(q)) if q.strip() else 0
    print(' ', len(fbm_by_sku), 'FBM listings', flush=True)

    print('sales & traffic 1d/7d/30d...', flush=True)
    raw = spapi.fetch_report(rid_1d)
    day_units = st_units(raw)[0] if raw else {}
    raw = spapi.fetch_report(rid_30)
    ac30u, ac30r = st_units(raw) if raw else ({}, {})
    raw = spapi.fetch_report(rid_7)
    ac7u = st_units(raw)[0] if raw else {}
    print(f'  1d:{len(day_units)} 30d:{len(ac30u)} 7d:{len(ac7u)} ASINs', flush=True)

    print('loading state from Supabase...', flush=True)
    meta = state_get('sellery_meta') or {}
    mon = state_get('monthly') or {}
    images = state_get('images') or {}
    monthly, months, units365 = mon.get('monthly', {}), mon.get('monthKeys', []), mon.get('units365', {})

    # -- assemble ASIN rows (same logic as local build_rows_v2)
    cost_by_sku = {s: m['c'] for s, m in meta.items() if m.get('c') is not None}
    cost_by_asin = {}
    for s, cost in cost_by_sku.items():
        a = meta.get(s, {}).get('a')
        if a: cost_by_asin.setdefault(a, []).append(cost)

    WINTER = re.compile(r'beanie|winter|knit|scarf|glove|mitten|skull|pom|fleece|ear ?warm|snow|slouch', re.I)
    by_asin = {}
    for sku, inv in skus.items():
        a = inv['asin'] or meta.get(sku, {}).get('a')
        if not a: continue
        r = by_asin.setdefault(a, {'asin': a, 'name': inv['productName'], 'inv': 0, 'inbound': 0,
            'u7': 0, 'u30': 0, 'fba30': 0, 'sales': 0.0, 'price': None, 'aged': [0,0,0,0,0],
            'inbW': 0, 'inbS': 0, 'inbR': 0,
            'ais': 0.0, 'fbm': 0, 'costs': [], 'kw': set(), 'cats': set(), 'categories': set(),
            'skus': set(), 'added': None})
        r['name'] = r['name'] or inv['productName']
        r['inv'] += inv['fulfillable']
        r['inbW'] += inv['inboundWorking']; r['inbS'] += inv['inboundShipped']; r['inbR'] += inv['inboundReceiving']
        r['inbound'] += inv['inboundWorking'] + inv['inboundShipped'] + inv['inboundReceiving']
        r['skus'].add(sku)
        p = planning.get(sku)
        if p:
            r['fba30'] += p['units30d']
            r['u30'] += p['units30d']; r['u7'] += p['units7d']
            if p['price']: r['price'] = max(r['price'] or 0, p['price'])
            r['sales'] += p['units30d'] * (p['price'] or 0)
            for i, k in enumerate(['d0_90','d91_180','d181_270','d271_365','d365plus']):
                r['aged'][i] += p['aged'][k]
            r['ais'] += p['ais']
        if sku in cost_by_sku: r['costs'].append(cost_by_sku[sku])
        r['fbm'] += fbm_by_sku.get(sku, 0) or 0
        m = meta.get(sku)
        if m:
            if m.get('k'): r['kw'].add(m['k'])
            if m.get('t'): r['cats'].add(m['t'])
            if m.get('d'): r['added'] = min(r['added'] or m['d'], m['d'])
            for part in str(m.get('g') or '').split(','):
                part = part.strip()
                if part: r['categories'].add(part)

    rows = []
    for a, r in by_asin.items():
        if a in ac30u: r['u30'] = ac30u[a]
        if ac30r.get(a, 0) > 0: r['sales'] = ac30r[a]
        if a in ac7u: r['u7'] = ac7u[a]
        u365 = units365.get(a, 0) or 0
        # include every "active" ASIN: any sales in 12mo, or any stock anywhere
        if not (r['sales'] > 0 or r['u30'] > 0 or u365 > 0 or r['inv'] > 0
                or r['fbm'] > 0 or r['inbound'] > 0):
            continue
        daily = r['u30'] / 30
        u1 = day_units.get(a, 0)
        fba_days = round(r['inv'] / daily, 1) if daily > 0 else (999 if r['inv'] else 0)
        comb_days = round((r['inv'] + r['fbm']) / daily, 1) if daily > 0 else (999 if r['inv']+r['fbm'] else 0)
        incl_inb = round((r['inv'] + r['inbound']) / daily, 1) if daily > 0 else 999
        # FBM-only listing: no FBA stock, inbound, shipped units or age history,
        # but FBM stock covers it -> not an FBA stockout, don't flag it.
        fbm_only = (r['inv'] == 0 and r['inbound'] == 0 and r['fbm'] > 0
                    and r['fba30'] == 0 and sum(r['aged']) == 0)
        tier = 'OUT' if r['inv'] == 0 else 'CRITICAL' if fba_days < 7 else 'LOW' if fba_days < 14 else 'OK'
        if fbm_only:
            tier, fba_days = 'OK', None
        risk = 'OK' if tier == 'OK' else ('AT_RISK' if comb_days < 14 and incl_inb < 14 else 'BUFFERED')
        cost = statistics.median(r['costs']) if r['costs'] else (statistics.median(cost_by_asin[a]) if cost_by_asin.get(a) else None)
        asp = r['sales'] / r['u30'] if r['u30'] else None
        gm = round((asp - cost) / asp, 3) if (asp and cost and asp > 0) else None
        is_winter = bool(WINTER.search(r['name'] or '')) or any(str(k or '').upper().startswith('W') for k in r['cats']) \
                    or any(WINTER.search(k) for k in r['kw'] if k)
        trend = [monthly.get(a, {}).get(m, 0) for m in months]
        rows.append({'asin': a, 'name': (r['name'] or '')[:160], 'img': images.get(a),
            'sales': round(r['sales'],2), 'u1': u1, 'u7': r['u7'], 'u30': r['u30'], 'u365': u365,
            'daily': round(daily,1), 'inv': r['inv'], 'fbaDays': fba_days, 'fbm': r['fbm'],
            'inbound': r['inbound'], 'combDays': comb_days, 'tier': tier, 'risk': risk,
            'cost': cost and round(cost,2), 'price': r['price'] and round(r['price'],2), 'gm': gm,
            'kw': ' '.join(sorted(k for k in r['kw'] if k))[:120],
            'category': ', '.join(sorted(r['categories'], key=str.lower))[:120],
            'tags': ('winter' if is_winter else ''), 'skuList': ' '.join(sorted(r['skus']))[:400],
            'added': r['added'], 'winter': is_winter, 'trend': trend,
            'top': r['sales'] >= 1000, 'fbmOnly': fbm_only,
            'aged': r['aged'], 'ais': round(r['ais'], 1),
            'inbW': r['inbW'], 'inbS': r['inbS'], 'inbR': r['inbR']})
    rows.sort(key=lambda r: (-r['sales'], -r['inv']))

    # -- image refresh for new ASINs (catalog batch of 20; '' marks known-missing
    #    so unfetchable ASINs aren't re-requested every run)
    missing = [r['asin'] for r in rows if not r['img'] and r['asin'] not in images]
    if missing:
        print('fetching images for', len(missing), 'new ASINs...', flush=True)
        for i in range(0, len(missing), 20):
            batch = missing[i:i+20]
            try:
                d = spapi.get('/catalog/2022-04-01/items', {
                    'identifiers': ','.join(batch), 'identifiersType': 'ASIN',
                    'marketplaceIds': spapi.MARKETPLACE_ID, 'includedData': 'images'})
                for it in d.get('items', []):
                    imgs = (it.get('images') or [{}])[0].get('images') or []
                    small = min(imgs, key=lambda im: im.get('width', 9999), default=None)
                    if small: images[it['asin']] = small['link']
                for a in batch:
                    images.setdefault(a, '')
            except Exception as e:
                print('  image batch failed:', str(e)[:120], flush=True)
            time.sleep(1.2)
        state_put('images', images)
        for r in rows:
            r['img'] = r['img'] or images.get(r['asin']) or None

    # KPI cards keep their "top sellers ($1K+/30d)" meaning; the scope toggle in
    # the app recomputes chip counts client-side for the all-ASIN view.
    top = [r for r in rows if r['top']]
    priced = [r for r in top if r['gm'] is not None]
    cutoff6mo = (today - datetime.timedelta(days=183)).isoformat()
    aged_totals = [sum(p['aged'][k] for p in planning.values()) for k in
                   ['d0_90','d91_180','d181_270','d271_365','d365plus']]
    summary = {
      'generated': today.strftime('%b %d, %Y'), 'windowDays': 30, 'salesThreshold': 1000,
      'coverFlagDays': 14, 'criticalDays': 7, 'dayDate': day, 'newCutoff': cutoff6mo,
      'topCount': len(top), 'allCount': len(rows),
      'flaggedCount': sum(1 for r in top if r['tier'] != 'OK'),
      'outCount': sum(1 for r in top if r['tier'] == 'OUT'),
      'criticalCount': sum(1 for r in top if r['tier'] == 'CRITICAL'),
      'lowCount': sum(1 for r in top if r['tier'] == 'LOW'),
      'okCount': sum(1 for r in top if r['tier'] == 'OK'),
      'atRiskCount': sum(1 for r in top if r['risk'] == 'AT_RISK'),
      'bufferedCount': sum(1 for r in top if r['risk'] == 'BUFFERED'),
      'revAtRisk': round(sum(r['sales'] for r in top if r['risk'] == 'AT_RISK'), 2),
      'revBuffered': round(sum(r['sales'] for r in top if r['risk'] == 'BUFFERED'), 2),
      'blendedGM': round(sum(r['sales']*r['gm'] for r in priced)/sum(r['sales'] for r in priced), 3) if priced else None,
      'marginAtRisk': round(sum(r['sales']*r['gm'] for r in priced if r['risk']=='AT_RISK'), 2),
      'inboundTotal': sum(s['inboundWorking']+s['inboundShipped']+s['inboundReceiving'] for s in skus.values()),
      'flaggedWithInbound': sum(1 for r in top if r['tier']!='OK' and r['inbound']>0),
    }
    data = {'summary': summary, 'rows': rows, 'months': months,
            'aged': aged_totals, 'ltsf': round(sum(p['ais'] for p in planning.values()), 1),
            'inboundStages': [['Working', sum(s['inboundWorking'] for s in skus.values())],
                              ['Shipped', sum(s['inboundShipped'] for s in skus.values())],
                              ['Receiving', sum(s['inboundReceiving'] for s in skus.values())]]}

    sb_req('/rest/v1/dashboard_snapshot?on_conflict=id', 'POST',
           {'id': 1, 'data': data, 'updated_at': datetime.datetime.utcnow().isoformat() + 'Z'},
           prefer='resolution=merge-duplicates')
    print('snapshot upserted:', len(rows), 'rows |', summary['flaggedCount'], 'flagged |',
          '$' + format(summary['revAtRisk'], ',.0f'), 'at risk')

if __name__ == '__main__':
    if MODE == 'monthly':
        run_monthly()
    else:
        run_fast()
