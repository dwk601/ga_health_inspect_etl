import json
import sys
import time
from urllib.parse import quote, urljoin, urlsplit, parse_qs

from scrapling.fetchers import FetcherSession

BASE = 'https://ga.healthinspections.us/stateofgeorgia/'
API = BASE + 'API/index.cfm/'
# The search API intermittently returns an empty page mid-listing; recheck before treating it as the end.
EMPTY_PAGE_RECHECK_DELAYS = (10, 60, 300)
# The inspections API intermittently returns records that don't belong to the requested facility; refetch.
INSPECTIONS_REFETCH_DELAYS = (5, 30)


class Source:
    def __init__(self):
        self.last_request = 0.0
        self.session_manager = FetcherSession()

    def __enter__(self):
        self.session = self.session_manager.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.session_manager.__exit__(exc_type, exc_value, traceback)

    def get(self, url):
        for attempt in range(3):
            # Space request starts 0.5s apart: at most 2 requests/second, without idling after slow responses.
            time.sleep(max(0, 0.5 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=30, retries=1, follow_redirects=False, discard_cookies=True)
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
        url = API + f'search/{path}/{page}'
        for delay in EMPTY_PAGE_RECHECK_DELAYS:
            facilities = self.json(url)
            if facilities:
                return facilities
            time.sleep(delay)
        return self.json(url)

    def inspections(self, facility_id):
        url = API + 'inspectionsData/' + quote(facility_id, safe='')
        for delay in INSPECTIONS_REFETCH_DELAYS:
            inspections = self.facility_inspections(url)
            mismatched = [i for i in inspections if not belongs_to(i, facility_id)]
            if not mismatched:
                return inspections
            print(f'Refetching inspections for {facility_id}; unexpected record: {json.dumps(mismatched[0])[:500]}',
                  file=sys.stderr, flush=True)
            time.sleep(delay)
        return self.facility_inspections(url)

    def facility_inspections(self, url):
        # Facilities without inspections get a placeholder row instead of an empty list.
        return [i for i in self.json(url) if not is_no_inspections_placeholder(i)]

    def json(self, url):
        data = json.loads(self.get(url).body)
        if not isinstance(data, list):
            raise ValueError('Expected API list')
        return data


def belongs_to(inspection, facility_id):
    return (isinstance(inspection, dict) and isinstance(inspection.get('inspectionId'), int)
            and not isinstance(inspection.get('inspectionId'), bool) and inspection.get('facilityId') == facility_id)


def is_no_inspections_placeholder(inspection):
    return (isinstance(inspection, dict) and inspection.get('inspectionId') == 0
            and inspection.get('printablePath') == '' and inspection.get('violations') == {})


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
