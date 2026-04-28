import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

with open('config.json') as f:
    CONFIG = json.load(f)

CACHE_FILE = 'known_urls.json'


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)


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

    for child in root.findall('sm:sitemap/sm:loc', ns):
        urls.extend(get_urls_from_sitemap(child.text.strip(), visited))

    for loc in root.findall('sm:url/sm:loc', ns):
        urls.append(loc.text.strip())

    return urls


def ping_sitemap(sitemap_url):
    try:
        r = requests.get(
            'https://www.google.com/ping',
            params={'sitemap': sitemap_url},
            timeout=10,
            headers={'User-Agent': 'GSC-AutoIndexer/1.0'}
        )
        if r.status_code == 200:
            print(f"  📡 Pinged     : {sitemap_url}")
            return True
        else:
            print(f"  ⚠ Ping failed ({r.status_code}): {sitemap_url}")
            return False
    except Exception as e:
        print(f"  ⚠ Ping error  : {sitemap_url}: {e}")
        return False


def run():
    print(f"\n{'='*60}")
    print(f"  GSC Auto-Indexer  —  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    cache = load_cache()
    grand_new = grand_pinged = 0

    for site_url in CONFIG['sites']:
        sitemap_url = CONFIG['sitemaps'].get(site_url)
        if not sitemap_url:
            print(f"⚠ No sitemap for {site_url}, skipping.\n")
            continue

        print(f"🌐  {site_url}")

        if ping_sitemap(sitemap_url):
            grand_pinged += 1

        all_urls = get_urls_from_sitemap(sitemap_url)
        known    = set(cache.keys())
        new_urls = [u for u in all_urls if u not in known]

        print(f"    Sitemap URLs  : {len(all_urls)}")
        print(f"    New URLs      : {len(new_urls)}")
        print()

        for url in new_urls:
            cache[url] = {
                'site':       site_url,
                'first_seen': datetime.now().isoformat(),
            }
            print(f"  ➕ New URL     : {url}")

        grand_new += len(new_urls)
        print()

    save_cache(cache)

    print(f"{'='*60}")
    print(f"  Run complete  —  {datetime.now().strftime('%H:%M UTC')}")
    print(f"{'='*60}")
    print(f"  New URLs found        : {grand_new}")
    print(f"  Sitemaps pinged       : {grand_pinged} / {len(CONFIG['sites'])}")
    print(f"  Total URLs in cache   : {len(cache)}")
    print()


if __name__ == '__main__':
    run()
