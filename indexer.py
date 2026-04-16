import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────
with open('config.json') as f:
    CONFIG = json.load(f)

CACHE_FILE           = 'known_urls.json'
SERVICE_ACCOUNT_FILE = 'service_account.json'
DELAY                = 0.12       # ~500 req/min, well under 600 limit
DAILY_LIMIT          = 1900       # buffer below Google's 2000/day cap

INDEXING_SCOPES = ['https://www.googleapis.com/auth/indexing']
GSC_SCOPES      = ['https://www.googleapis.com/auth/webmasters.readonly']

# ── Auth ──────────────────────────────────────────────────────
def get_services():
    indexing_creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=INDEXING_SCOPES)
    gsc_creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=GSC_SCOPES)
    return (
        build('indexing', 'v3', credentials=indexing_creds),
        build('searchconsole', 'v1', credentials=gsc_creds)
    )

# ── Cache ─────────────────────────────────────────────────────
def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

# ── Sitemap fetcher (handles sitemap index files too) ─────────
def get_urls_from_sitemap(sitemap_url, visited=None):
    if visited is None:
        visited = set()
    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)
    try:
        r = requests.get(sitemap_url, timeout=15,
                         headers={'User-Agent': 'GSC-AutoIndexer/1.0'})
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠ Could not fetch sitemap {sitemap_url}: {e}")
        return []

    root = ET.fromstring(r.content)
    ns   = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    urls = []

    # Sitemap index → recurse
    for child in root.findall('sm:sitemap/sm:loc', ns):
        urls.extend(get_urls_from_sitemap(child.text.strip(), visited))

    # Regular sitemap → collect
    for loc in root.findall('sm:url/sm:loc', ns):
        urls.append(loc.text.strip())

    return urls

# ── URL Inspection ────────────────────────────────────────────
def inspect_url(gsc_svc, url, site_url):
    try:
        resp       = gsc_svc.urlInspection().index().inspect(body={
            'inspectionUrl': url,
            'siteUrl':       site_url
        }).execute()
        result     = resp.get('inspectionResult', {})
        index_info = result.get('indexStatusResult', {})
        return {
            'verdict':       index_info.get('verdict', 'UNKNOWN'),
            'coverageState': index_info.get('coverageState', ''),
            'lastCrawlTime': index_info.get('lastCrawlTime', ''),
            'indexingState': index_info.get('indexingState', ''),
            'mobile':        result.get('mobileUsabilityResult', {}).get('verdict', 'N/A'),
        }
    except Exception as e:
        return {'verdict': 'ERROR', 'error': str(e)}

# ── Indexing request ──────────────────────────────────────────
def request_indexing(indexing_svc, url):
    try:
        indexing_svc.urlNotifications().publish(
            body={'url': url, 'type': 'URL_UPDATED'}
        ).execute()
        return True
    except Exception as e:
        print(f"    ❌ Indexing API error: {e}")
        return False

# ── Process a list of URLs ────────────────────────────────────
def process_urls(urls, label, site_url, cache, indexing_svc, gsc_svc, quota):
    submitted = already = errors = 0

    for url in urls:
        if quota[0] >= DAILY_LIMIT:
            print(f"  ⚠ Daily quota reached ({DAILY_LIMIT}), stopping.")
            break

        status  = inspect_url(gsc_svc, url, site_url)
        quota[0] += 1

        entry = cache.get(url, {
            'site':       site_url,
            'first_seen': datetime.now().isoformat(),
        })
        entry['verdict']       = status['verdict']
        entry['coverageState'] = status.get('coverageState', '')
        entry['last_checked']  = datetime.now().isoformat()

        if status['verdict'] == 'PASS':
            already += 1
            print(f"  ✅ Indexed    : {url}")
        else:
            ok = request_indexing(indexing_svc, url)
            if ok:
                entry['submitted'] = datetime.now().isoformat()
                submitted += 1
                icon = '🚀 Submitted' if label == 'NEW' else '🔄 Re-submitted'
                print(f"  {icon}: {url}")
            else:
                errors += 1
                print(f"  ❌ Failed    : {url}")

        cache[url] = entry
        time.sleep(DELAY)

    return submitted, already, errors

# ── Main ──────────────────────────────────────────────────────
def run():
    print(f"\n{'='*60}")
    print(f"  GSC Auto-Indexer  —  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    indexing_svc, gsc_svc = get_services()
    cache = load_cache()
    quota = [0]   # mutable so process_urls can update it

    grand_new = grand_submitted = grand_already = grand_errors = 0

    for site_url in CONFIG['sites']:
        sitemap_url = CONFIG['sitemaps'].get(site_url)
        if not sitemap_url:
            print(f"⚠ No sitemap for {site_url}, skipping.\n")
            continue

        print(f"🌐  {site_url}")
        all_urls = get_urls_from_sitemap(sitemap_url)
        known    = set(cache.keys())
        new_urls = [u for u in all_urls if u not in known]

        # URLs still pending from previous runs
        pending_urls = [
            u for u, v in cache.items()
            if v.get('site') == site_url and v.get('verdict') not in ('PASS',)
        ]

        print(f"    Sitemap URLs  : {len(all_urls)}")
        print(f"    New URLs      : {len(new_urls)}")
        print(f"    Still pending : {len(pending_urls)}")
        print()

        # Re-check pending first
        if pending_urls:
            print(f"  — Re-checking pending URLs —")
            s, a, e = process_urls(pending_urls, 'PENDING', site_url, cache,
                                   indexing_svc, gsc_svc, quota)
            grand_submitted += s; grand_already += a; grand_errors += e

        # Then handle new URLs
        if new_urls:
            print(f"  — Processing new URLs —")
            s, a, e = process_urls(new_urls, 'NEW', site_url, cache,
                                   indexing_svc, gsc_svc, quota)
            grand_new += len(new_urls)
            grand_submitted += s; grand_already += a; grand_errors += e

        print()

    save_cache(cache)

    # ── Final report ──────────────────────────────────────────
    print(f"{'='*60}")
    print(f"  Run complete  —  {datetime.now().strftime('%H:%M UTC')}")
    print(f"{'='*60}")
    print(f"  New URLs found        : {grand_new}")
    print(f"  Already indexed       : {grand_already}")
    print(f"  Submitted for indexing: {grand_submitted}")
    print(f"  Errors                : {grand_errors}")
    print(f"  API calls used        : {quota[0]} / {DAILY_LIMIT}")
    print(f"  Total URLs in cache   : {len(cache)}")
    print()

if __name__ == '__main__':
    run()
