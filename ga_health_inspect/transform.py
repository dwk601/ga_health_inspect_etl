import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .source import belongs_to, report_url


def labels(columns):
    if not isinstance(columns, dict) or not all(isinstance(v, str) for v in columns.values()):
        raise ValueError('Invalid columns')
    return {v.split(':', 1)[0].strip().lower(): v.split(':', 1)[1].strip()
            for v in columns.values() if ':' in v}


def date(value, fmt, errors):
    if not value or value.strip().upper() == 'NA':
        return None
    try:
        parsed = datetime.strptime(value.strip(), fmt)
        if parsed.strftime(fmt) != value.strip():
            raise ValueError('noncanonical date')
        return parsed.date().isoformat()
    except (ValueError, TypeError):
        errors[0] += 1
        return None


def number(value, errors):
    if value is None or str(value).strip().upper() in ('', 'NA', 'N/A'):
        return None
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite():
            raise InvalidOperation
        return n
    except InvalidOperation:
        errors[0] += 1
        return None


def facility(raw, errors):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']:
        raise ValueError('Missing facility id')
    c = labels(raw.get('columns'))
    return dict(source_id=raw['id'], name=raw.get('name'), address=raw.get('mapAddress'),
                phone=c.get('phone number'), permit_type=c.get('permit type'), permit_number=c.get('permit number'),
                last_inspection_date=date(c.get('last inspection date'), '%m-%d-%Y', errors),
                last_inspection_score=number(c.get('last inspection score'), errors), raw=raw)


def inspection(raw, facility_id, errors):
    if not belongs_to(raw, facility_id):
        found = {k: raw.get(k) for k in ('inspectionId', 'facilityId')} if isinstance(raw, dict) else type(raw).__name__
        raise ValueError(f'Invalid inspection id or facility for {facility_id}: {found}')
    c = labels(raw.get('columns'))
    violations = raw.get('violations')
    if not isinstance(violations, dict) or any(not isinstance(k, str) or not isinstance(v, list) or len(v) < 2 for k, v in violations.items()):
        raise ValueError('Invalid violations')
    url, template = report_url(raw.get('printablePath'))
    digest = hashlib.sha256(json.dumps(violations, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return dict(facility_id=facility_id, source_id=raw['inspectionId'], report_template=template,
                inspection_date=date(c.get('date'), '%m-%d-%Y', errors), purpose=c.get('inspection purpose'),
                score=number(c.get('score'), errors), inspector=c.get('inspector'), printable_url=url,
                raw=raw, violations_hash=digest)


def violation(key, values, errors):
    if any(v is not None and not isinstance(v, str) for v in values):
        raise ValueError('Invalid violation value')
    fields = {}
    for v in values[2:]:
        if not v:
            continue
        if v.lower().startswith('inspector notes'):
            fields['inspector notes'] = v[len('Inspector Notes'):].lstrip(': ')
        elif ':' in v:
            k, val = v.split(':', 1)
            fields[k.lower().strip()] = val.strip()
    def boolean(value):
        if not value:
            return None
        if value.lower() in ('yes', 'no'):
            return value.lower() == 'yes'
        errors[0] += 1
        return None
    return dict(source_key=key, code_title=values[0], regulation=values[1], points=number(fields.get('points'), errors),
                corrected=boolean(fields.get('corrected during inspection?')), repeat=boolean(fields.get('repeat')),
                notes=fields.get('inspector notes'), raw=values)


def text(node):
    return ' '.join(' '.join(node.xpath('.//text()').getall()).replace('\xa0', ' ').split())


def cell_value(page, label):
    nodes = page.xpath('//td[normalize-space(.) and (b or strong)] | //td[not(*)]')
    for node in nodes:
        s = text(node)
        if s.startswith(label):
            tail = s[len(label):].strip()
            if tail:
                return tail
            for sibling in node.xpath('./following-sibling::td'):
                candidate = text(sibling)
                if candidate:
                    return candidate
    return None


def common_cells(page, name_label, address_label, permit_label):
    return {key: cell_value(page, label) for key, label in [
        ('name', name_label), ('address', address_label), ('city', 'City:'),
        ('permit_number', permit_label), ('inspection_date', 'Inspection Date:')]}


def food_report(page):
    raw = common_cells(page, 'Establishment Name:', 'Address:', 'Permit#:')
    for key, label in [('time_in', 'Time In:'), ('time_out', 'Time Out:')]:
        cells = page.xpath('//b[normalize-space(.)=$label]/parent::td/following-sibling::td[position() <= 4]', label=label)
        parts = [text(cell) for cell in cells]
        raw[key] = (f'{parts[0]}:{parts[2]} {parts[3]}'.strip() if len(parts) == 4 and parts[1] == ':' and (parts[0] or parts[2] or parts[3]) else ' '.join(part for part in parts if part and part != ':') or None)
    scores = page.css('#div_finalScore::text').getall()
    grades = page.css('#div_grade::text').getall()
    raw['score'] = scores[0].strip() if scores else None
    raw['grade'] = grades[0].strip() if grades else None
    return raw


def pool_report(page):
    raw = common_cells(page, 'Facility Name:', 'Facility Address:', 'Permit Number:')
    raw.update(time_in=cell_value(page, 'Time In:'), time_out=cell_value(page, 'Time Out:'))
    scores = page.xpath('//td[contains(@class,"ArialEighteen") and @rowspan]/strong/text()').getall()
    raw.update(score=scores[0].strip() if scores else None, grade=None)
    return raw


def tourist_report(page):
    raw = {}
    for key, label in [('name', 'Accommodation Name:'), ('address', 'Address:'), ('city', 'City:'), ('permit_number', 'Permit#:'), ('inspection_date', 'Inspection Date:'), ('time_in', 'Time In:'), ('time_out', 'Time Out:')]:
        nodes = page.xpath('//strong[normalize-space(.)=$label]/following-sibling::span[1]', label=label)
        raw[key] = text(nodes[0]) if nodes else None
        if nodes and key in ('time_in', 'time_out'):
            suffix = nodes[0].xpath('./following-sibling::sup[1]/text()').get()
            if suffix:
                raw[key] = f'{raw[key]} {suffix.strip()}'.strip()
    scores = page.xpath('//td[contains(@style,"font-size:40pt")]/text()').getall()
    raw.update(score=scores[0].strip() if scores else None, grade=None)
    return raw


REPORT_EXTRACTORS = {'food_2015': food_report, 'pool_2017': pool_report, 'Tourist2014': tourist_report}


def report(page, template, errors):
    if template not in REPORT_EXTRACTORS:
        raise ValueError('Unknown report template: ' + template)
    raw = REPORT_EXTRACTORS[template](page)
    if not raw['name'] or not raw['inspection_date'] or raw['score'] is None:
        raise ValueError('Report header not recognized')
    fields = dict(raw)
    fields['inspection_date'] = date(raw['inspection_date'], '%m/%d/%Y', errors)
    score = number(raw['score'], errors)
    fields['score'] = str(score) if score is not None else None
    for key in ('time_in', 'time_out'):
        value = raw[key]
        fields[key] = None
        if value and value.strip().upper() not in ('NA', 'N/A'):
            try:
                fields[key] = datetime.strptime(value.strip(), '%I:%M %p').strftime('%H:%M:%S')
            except ValueError:
                errors[0] += 1
    return fields, raw
