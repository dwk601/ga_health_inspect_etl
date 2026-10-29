import json
import time
from urllib.parse import quote, urljoin, urlsplit, parse_qs

from scrapling.fetchers import Fetcher

BASE = 'https://ga.healthinspections.us/stateofgeorgia/'
API = BASE + 'API/index.cfm/'


class Source:
    def __init__(self):
        self.last_request = 0.0

    def get(self, url):
        for attempt in range(3):
            time.sleep(max(0, 0.5 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = Fetcher.get(url, timeout=30, retries=1, follow_redirects=False)
                if response.status >= 500:
                    raise RuntimeError(f'HTTP {response.status} for {url}')
                if response.status != 200:
                    raise ValueError(f'HTTP {response.status} for {url}')
                return response
            except ValueError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def search(self, page):
        path = quote(json.dumps({'keyword': ''}, separators=(',', ':')), safe='')
        return self.json(API + f'search/{path}/{page}')

    def inspections(self, facility_id):
        return self.json(API + 'inspectionsData/' + quote(facility_id, safe=''))

    def json(self, url):
        data = json.loads(self.get(url).body)
        if not isinstance(data, list):
            raise ValueError('Expected API list')
        return data


def report_url(path):
    if not isinstance(path, str):
        raise ValueError('Missing printablePath')
    url = urljoin(BASE, path)
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'ga.healthinspections.us' or not parts.path.startswith('/_templates/'):
        raise ValueError('Invalid report URL')
    templates = parse_qs(parts.query).get('rtype', [])
    if len(templates) != 1 or not templates[0]:
        raise ValueError('Missing report template')
    return url, templates[0]
