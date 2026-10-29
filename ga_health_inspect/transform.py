import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .source import report_url


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
    if not isinstance(raw, dict) or not isinstance(raw.get('inspectionId'), int) or isinstance(raw.get('inspectionId'), bool) or raw.get('facilityId') != facility_id:
        raise ValueError('Invalid inspection id or facility')
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


def report(page, template, errors):
    if template not in ('food_2015', 'pool_2017', 'Tourist2014'):
        raise ValueError('Unknown report template: ' + template)
    def text(node):
        return ' '.join(' '.join(node.xpath('.//text()').getall()).replace('\xa0', ' ').split())
    def value(label):
        # Labels are either in their own cell or followed by a span in the same cell.
        nodes = page.xpath('//td[normalize-space(.) and (b or strong)] | //td[not(*)]')
        for node in nodes:
            s = text(node)
            if s.startswith(label):
                tail = s[len(label):].strip()
                if tail:
                    return tail
                siblings = node.xpath('./following-sibling::td')
                for sibling in siblings:
                    candidate = text(sibling)
                    if candidate:
                        return candidate
        return None
    name_label = {'food_2015': 'Establishment Name:', 'pool_2017': 'Facility Name:', 'Tourist2014': 'Accommodation Name:'}[template]
    raw = {k: value(label) for k, label in [('name', name_label), ('address', 'Facility Address:' if template == 'pool_2017' else 'Address:'), ('city', 'City:'), ('permit_number', 'Permit Number:' if template == 'pool_2017' else 'Permit#:'), ('inspection_date', 'Inspection Date:'), ('time_in', 'Time In:'), ('time_out', 'Time Out:')]}
    if template == 'food_2015':
        scores = page.css('#div_finalScore::text').getall()
        grades = page.css('#div_grade::text').getall()
    elif template == 'pool_2017':
        scores = page.xpath('//td[contains(@class,"ArialEighteen") and @rowspan]/strong/text()').getall()
        grades = []
    else:
        scores = page.xpath('//td[contains(@style,"font-size:40pt")]/text()').getall()
        grades = []
    if template == 'food_2015':
        for key, label in [('time_in', 'Time In:'), ('time_out', 'Time Out:')]:
            cells = page.xpath('//b[normalize-space(.)=$label]/parent::td/following-sibling::td[position() <= 4]', label=label)
            parts = [text(cell) for cell in cells]
            raw[key] = f'{parts[0]}:{parts[2]} {parts[3]}' if len(parts) == 4 and parts[1] == ':' and parts[0].isdigit() and parts[2].isdigit() and parts[3] in ('AM', 'PM') else None
    raw['score'] = scores[0].strip() if scores else None
    raw['grade'] = grades[0].strip() if grades else None
    # Tourist labels share cells; extract only through the following labelled field.
    if template == 'Tourist2014':
        for key, label in [('name', 'Accommodation Name:'), ('address', 'Address:'), ('city', 'City:'), ('permit_number', 'Permit#:'), ('inspection_date', 'Inspection Date:'), ('time_in', 'Time In:'), ('time_out', 'Time Out:')]:
            nodes = page.xpath('//strong[normalize-space(.)=$label]/following-sibling::span[1]', label=label)
            raw[key] = text(nodes[0]) if nodes else None
            if nodes and key in ('time_in', 'time_out'):
                suffix = nodes[0].xpath('./following-sibling::sup[1]/text()').get()
                raw[key] = f'{raw[key]} {suffix.strip()}' if suffix and suffix.strip() in ('AM', 'PM') else None
    if not raw['name'] or not raw['inspection_date'] or raw['score'] is None:
        raise ValueError('Report header not recognized')
    fields = dict(raw)
    fields['inspection_date'] = date(raw['inspection_date'], '%m/%d/%Y', errors)
    score = number(raw['score'], errors)
    fields['score'] = str(score) if score is not None else None
    return fields, raw
