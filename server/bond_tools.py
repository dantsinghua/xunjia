"""Four bounded analysis-only tools; no account data or sending capability."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, localcontext
import base64
import difflib
import hashlib
import json
import re
from uuid import uuid4

from .domain import goal
from .reference_data import ReferenceDataUnavailable, ReferenceSnapshot, UnconfiguredReferenceData


MASTER_FIELDS = ('remaining_days', 'tenor_basis', 'maturity_date', 'exercise_date', 'balance',
                 'balance_unit', 'balance_date', 'coupon_type', 'coupon_rate_pct', 'issuer_id',
                 'bond_type', 'industry', 'bond_rating', 'issuer_rating')
VALUATION_FIELDS = ('provider', 'basis', 'yield_pct', 'clean_price', 'dirty_price', 'duration',
                    'valuation_date', 'published_at')
ISSUER_FIELDS = ('uscc', 'full_name', 'short_name', 'aliases', 'former_names', 'registered_region',
                 'region_code', 'ownership', 'industry', 'issuer_rating')
QUOTE_FIELDS = ('side', 'quote_type', 'raw_value', 'yield_pct', 'yield_basis', 'volume', 'volume_unit',
                'broker_id', 'broker_name', 'firmness', 'settlement_date', 'settlement_text',
                'source_updated_at', 'captured_at', 'last_confirmed_active_at', 'timestamp_scope',
                'status', 'freshness', 'quote_id', 'version', 'source_ref', 'quality')
COMPARE_FIELDS = ('remaining_days', 'balance', 'coupon_rate_pct', 'valuation_yield_pct',
                  'bid_yield_pct', 'ofr_yield_pct', 'bond_rating', 'issuer_rating', 'industry', 'bond_type')
CONVENTION = 'ACT/365F-compounded-v1'


class InputError(ValueError):
    pass


def closed(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise InputError('未知字段或对象格式无效')
    if any(key not in value for key in required):
        raise InputError('缺少必填请求字段')


def text_value(value, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InputError('字符串格式无效')
    return value


def number(value, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,40})?', value):
        raise InputError('数值须为有限十进制字符串')
    result = Decimal(value)
    if positive and result <= 0:
        raise InputError('数值必须为正')
    return result


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise InputError('日期须为ISO日期')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError('日期无效') from None


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError('整数超出允许范围')
    return value


def enum(value, values):
    if value not in values:
        raise InputError('枚举值无效')
    return value


def strings(value, allowed=None):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise InputError('数组须含1至20项')
    for item in value:
        text_value(item)
        if allowed is not None:
            enum(item, allowed)
    if len(set(value)) != len(value):
        raise InputError('数组不允许重复项')


def decimal_text(value):
    return format(value, 'f')


def public_fact_value(field, value):
    """Supplier values may be malformed; only declared public types survive."""
    try:
        if field in ('balance', 'coupon_rate_pct', 'yield_pct', 'clean_price', 'dirty_price', 'duration'):
            number(value)
        elif field == 'remaining_days':
            integer(value, 0, 365250)
        elif field in ('industry', 'bond_rating', 'issuer_rating'):
            if not isinstance(value, dict):
                return None
            keys = ('scheme', 'level', 'code') if field == 'industry' else ('agency', 'scale', 'value', 'rating_date')
            result = {k: value.get(k) if isinstance(value.get(k), str) else None for k in keys}
            if field.endswith('rating') and result['rating_date'] is not None:
                iso_date(result['rating_date'])
            return result
        elif field in ('aliases', 'former_names'):
            if not isinstance(value, list):
                return None
            return [item if isinstance(item, str) else {k: item.get(k) if isinstance(item.get(k), str) else None for k in ('name', 'valid_from', 'valid_to')}
                    for item in value if isinstance(item, (str, dict))]
        else:
            text_value(value, 500)
            if field.endswith('_date'):
                iso_date(value)
    except InputError:
        return None
    return value


class BondTools:
    def __init__(self, provider=None):
        self.provider = provider if provider is not None else UnconfiguredReferenceData()

    def invoke(self, name, arguments):
        response = dict(schema_version='1', status='ok', request_id=str(uuid4()), snapshot_id=None,
                        generated_at=datetime.now(timezone.utc).isoformat(), as_of=None, data=None,
                        missing_fields=[], warnings=[], sources=[], usage='analysis_only')
        try:
            if not isinstance(arguments, dict) or arguments.get('schema_version') != '1':
                raise InputError('schema_version须为1')
            if name not in ('bond_info', 'similar_bonds', 'issuer_info', 'bond_calc'):
                raise InputError('未知工具')
            request = deepcopy(arguments)
            if name == 'bond_calc':
                self._calc(request, response)
            else:
                self._validate_query(name, request)
                snapshot = self._snapshot(response)
                if name == 'similar_bonds':
                    self._similar(request, snapshot, response)
                else:
                    self._info(name, request, snapshot, response)
        except InputError as exc:
            response.update(status='invalid_input', data=None)
            response['warnings'].append(dict(code='INVALID_INPUT', fields=[], message=str(exc)))
        except ReferenceDataUnavailable as exc:
            response.update(status='unavailable', data=None)
            code = 'NOT_CONFIGURED' if str(exc) == 'NOT_CONFIGURED' else 'SOURCE_UNAVAILABLE'
            response['warnings'].append(dict(code=code, fields=[]))
        except (DecimalException, OverflowError):
            response.update(status='unsupported', data=None)
            response['warnings'].append(dict(code='NUMERIC_DOMAIN', fields=[]))
        return response

    def _snapshot(self, response):
        try:
            snapshot = deepcopy(self.provider.snapshot())
        except ReferenceDataUnavailable:
            raise
        except Exception:
            raise ReferenceDataUnavailable('SOURCE_UNAVAILABLE') from None
        if (not isinstance(snapshot, ReferenceSnapshot) or not isinstance(snapshot.snapshot_id, str)
                or not snapshot.snapshot_id or not isinstance(snapshot.as_of, str)):
            raise ReferenceDataUnavailable('INVALID_SNAPSHOT')
        try:
            iso_date(snapshot.as_of[:10])
            if not isinstance(snapshot.sources, list) or not all(isinstance(s, dict) and isinstance(s.get('id'), str) and s['id'] for s in snapshot.sources):
                raise ValueError('invalid sources')
            for rows, id_key in ((snapshot.bonds, 'bond_id'), (snapshot.issuers, 'issuer_id')):
                if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get(id_key), str) and row[id_key] for row in rows):
                    raise ValueError('invalid records')
            for row in snapshot.bonds:
                if any(section in row and not isinstance(row[section], dict) for section in ('master', 'valuation')):
                    raise ValueError('invalid public facts')
                if 'quotes' in row and (not isinstance(row['quotes'], list) or not all(isinstance(q, dict) for q in row['quotes'])):
                    raise ValueError('invalid quotes')
            if not isinstance(snapshot.terms, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in snapshot.terms.items()):
                raise ValueError('invalid terms')
        except (ValueError, TypeError):
            raise ReferenceDataUnavailable('INVALID_SNAPSHOT') from None
        response.update(snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of)
        response['sources'] = [{key: source.get(key) if isinstance(source.get(key), str) else None for key in
                                ('id', 'origin', 'transport', 'dataset_version', 'as_of')}
                               for source in snapshot.sources]
        return snapshot

    def _validate_query(self, name, request):
        if name != 'similar_bonds':
            identity = 'bond_id' if name == 'bond_info' else 'issuer_id'
            allowed = {'schema_version', 'mode', 'query', identity, 'limit', 'cursor'}
            if name == 'bond_info':
                allowed |= {'market', 'include'}
            closed(request, allowed, ['mode'])
            enum(request['mode'], ('search', 'get'))
            field = 'query' if request['mode'] == 'search' else identity
            forbidden = identity if field == 'query' else 'query'
            text_value(request.get(field), 80 if field == 'query' else 100)
            if forbidden in request:
                raise InputError('search/get参数冲突')
            if 'market' in request:
                enum(request['market'], ('IB', 'SH', 'SZ', 'BJ'))
            if 'include' in request:
                strings(request['include'], ('master', 'valuation', 'quotes'))
            if request['mode'] == 'get' and any(k in request for k in ('limit', 'cursor')):
                raise InputError('get不支持分页')
        else:
            closed(request, ('schema_version', 'anchor_bond_id', 'as_of_date', 'tenor_basis',
                'remaining_days', 'issuer_scope', 'industry', 'bond_types', 'rating',
                'compare_fields', 'sort', 'limit', 'cursor'),
                ('anchor_bond_id', 'tenor_basis', 'remaining_days', 'issuer_scope', 'compare_fields'))
            text_value(request['anchor_bond_id'])
            enum(request['tenor_basis'], ('maturity', 'exercise'))
            enum(request['issuer_scope'], ('same', 'different', 'any'))
            closed(request['remaining_days'], ('min', 'max'), ('min', 'max'))
            low = integer(request['remaining_days']['min'], 0, 365250)
            integer(request['remaining_days']['max'], low, 365250)
            strings(request['compare_fields'], COMPARE_FIELDS)
            if 'as_of_date' in request:
                iso_date(request['as_of_date'])
            if 'bond_types' in request:
                strings(request['bond_types'])
            if 'industry' in request:
                closed(request['industry'], ('scheme', 'level', 'codes'), ('scheme', 'level', 'codes'))
                text_value(request['industry']['scheme']); text_value(request['industry']['level'])
                strings(request['industry']['codes'])
            if 'rating' in request:
                closed(request['rating'], ('scope', 'agency', 'scale', 'allowed_values'), ('scope', 'agency', 'scale', 'allowed_values'))
                enum(request['rating']['scope'], ('bond', 'issuer'))
                text_value(request['rating']['agency']); text_value(request['rating']['scale'])
                strings(request['rating']['allowed_values'])
            if request.get('sort', ['tenor_distance', 'code']) not in (['tenor_distance', 'code'], ['code']):
                raise InputError('排序无效')
        integer(request.get('limit', 10), 1, 20)
        if 'cursor' in request:
            text_value(request['cursor'], 1024)

    def _fact(self, raw, path, response):
        empty = dict(value=None, quality='missing', source_ref=None, as_of=None, missing_reason='NOT_PROVIDED')
        if not isinstance(raw, dict) or raw.get('value') is None:
            response['missing_fields'].append(path)
            return empty
        result = {key: raw.get(key) if isinstance(raw.get(key), str) else None for key in empty}
        result['value'] = public_fact_value(path.rsplit('.', 1)[-1], raw['value'])
        if result['value'] is None:
            response['missing_fields'].append(path)
            return empty
        if raw.get('quality') not in ('verified', 'source_unverified', 'derived', 'user_supplied'):
            response['missing_fields'].append(path)
            return empty
        source_ids = {s['id'] for s in response['sources']}
        if result['source_ref'] not in source_ids:
            result.update(quality='source_unverified', source_ref=None)
            response['warnings'].append(dict(code='MISSING_SOURCE', fields=[path]))
        return result

    def _identity(self, row, issuer=False):
        keys = ('issuer_id', 'full_name', 'short_name') if issuer else ('bond_id', 'code', 'name', 'market')
        result = {}
        for key in keys:
            value = row.get(key)
            if issuer and isinstance(value, dict):
                value = value.get('value')
            result[key] = value if isinstance(value, str) else None
        return result

    def _project(self, row, response, include):
        result = {'identity': self._identity(row)}
        for section, fields in (('master', MASTER_FIELDS), ('valuation', VALUATION_FIELDS)):
            if section in include:
                values = row.get(section) or {}
                result[section] = {key: self._fact(values.get(key), f'{section}.{key}', response) for key in fields}
        if 'quotes' in include:
            quotes = []
            for quote in row.get('quotes', []):
                if quote.get('side') not in ('bid', 'ofr'):
                    continue
                public = {key: quote.get(key) if isinstance(quote.get(key), str) else None for key in QUOTE_FIELDS}
                # A clean-price raw value is never a yield fallback.
                if public['quote_type'] not in ('yield', 'clean_price', 'dirty_price', 'spread', 'unknown'):
                    public['quote_type'] = 'unknown'
                for key in ('raw_value', 'yield_pct', 'volume'):
                    try:
                        number(public[key])
                    except InputError:
                        public[key] = None
                if public['quality'] != 'verified' or public['source_ref'] not in {s['id'] for s in response['sources']}:
                    public['yield_pct'] = None
                quotes.append(public)
            result['quotes'] = quotes
            result['best'] = self._best(quotes)
        return result

    @staticmethod
    def _best(quotes):
        groups = {}
        for q in quotes:
            timestamp = q.get('last_confirmed_active_at') or q.get('source_updated_at')
            try:
                valid_time = isinstance(timestamp, str) and datetime.fromisoformat(timestamp.replace('Z', '+00:00')).utcoffset() is not None
            except ValueError:
                valid_time = False
            if (q.get('status') != 'active' or q.get('freshness') != 'fresh' or q.get('yield_pct') is None
                    or not q.get('yield_basis') or not q.get('settlement_date') or not valid_time):
                continue
            key = (q['yield_basis'], q['settlement_date'])
            group = groups.setdefault(key, dict(yield_basis=key[0], settlement_date=key[1], bid=None, ofr=None))
            current = group[q['side']]
            if current is None or ((Decimal(q['yield_pct']) < Decimal(current['yield_pct'])) if q['side'] == 'bid'
                                   else (Decimal(q['yield_pct']) > Decimal(current['yield_pct']))):
                group[q['side']] = q
        values = list(groups.values())
        return dict(bid=values[0]['bid'] if len(values) == 1 else None,
                    ofr=values[0]['ofr'] if len(values) == 1 else None, groups=values)

    def _page(self, rows, request, snapshot):
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in request.items() if k != 'cursor'},
                                                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        offset = 0
        if 'cursor' in request:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(request['cursor'].encode()))
                if cursor['snapshot'] != snapshot.snapshot_id or cursor['query'] != fingerprint:
                    raise InputError('分页游标快照或条件已变化')
                offset = integer(cursor['offset'], 0, len(rows))
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise InputError('分页游标无效') from None
        end = offset + request.get('limit', 10)
        more = end < len(rows)
        cursor = base64.urlsafe_b64encode(json.dumps(dict(snapshot=snapshot.snapshot_id, query=fingerprint, offset=end)).encode()).decode() if more else None
        return rows[offset:end], more, cursor

    def _info(self, name, request, snapshot, response):
        issuer = name == 'issuer_info'
        id_key = 'issuer_id' if issuer else 'bond_id'
        if request['mode'] == 'get':
            rows = snapshot.lookup_issuers(request[id_key]) if issuer else snapshot.lookup_bonds(request[id_key])
            if 'market' in request:
                rows = [r for r in rows if r.get('market') == request['market']]
            if len(rows) != 1:
                response['status'] = 'ambiguous' if rows else 'not_found'
                response['data'] = {'candidates': [self._identity(r, issuer) for r in rows]}
                return
            if issuer:
                response['data'] = {'identity': self._identity(rows[0], True), **{key: self._fact(rows[0].get(key), f'issuer.{key}', response) for key in ISSUER_FIELDS}}
            else:
                response['data'] = self._project(rows[0], response, request.get('include', ['master', 'valuation', 'quotes']))
            if response['missing_fields'] or response['warnings']:
                response['status'] = 'partial'
            return
        query = request['query'].strip().casefold()
        matches = []
        rows = snapshot.search_issuers() if issuer else snapshot.search_bonds()
        for row in rows:
            if request.get('market') and row.get('market') != request['market']:
                continue
            keys = ('issuer_id', 'full_name', 'short_name', 'aliases', 'former_names') if issuer else ('bond_id', 'code', 'name', 'aliases')
            hits = []
            for key in keys:
                value = row.get(key)
                if isinstance(value, dict):
                    value = value.get('value')
                values = value if isinstance(value, list) else [value]
                for candidate in values:
                    if isinstance(candidate, dict):
                        candidate = candidate.get('name')
                    if not isinstance(candidate, str):
                        continue
                    normalized = candidate.casefold()
                    kind = 'exact' if normalized == query else 'substring' if query in normalized else 'fuzzy' if difflib.SequenceMatcher(None, query, normalized).ratio() >= .7 else None
                    if kind:
                        hits.append(dict(field=key, text=candidate, match_kind=kind))
            if hits:
                rank = min(('exact', 'substring', 'fuzzy').index(h['match_kind']) for h in hits)
                matches.append({**self._identity(row, issuer), 'match_kind': ('exact', 'substring', 'fuzzy')[rank], 'matches': hits})
        matches.sort(key=lambda r: (('exact', 'substring', 'fuzzy').index(r['match_kind']), r.get('code') or '', r[id_key] or ''))
        # A sole fuzzy/substring candidate remains a suggestion, never confirmation.
        exact = [match for match in matches if match['match_kind'] == 'exact']
        resolved = exact[0][id_key] if len(exact) == 1 else None
        page, more, cursor = self._page(matches, request, snapshot)
        response['status'] = 'not_found' if not matches else 'ok' if resolved is not None else 'ambiguous'
        response['data'] = dict(candidates=page, total=len(matches), has_more=more, cursor=cursor,
                                **{f'resolved_{id_key}': resolved})

    @staticmethod
    def _known(row, field, snapshot, as_of):
        raw = (row.get('master') or {}).get(field)
        if (not isinstance(raw, dict) or raw.get('quality') != 'verified' or not isinstance(raw.get('source_ref'), str)
                or raw['source_ref'] not in {s.get('id') for s in snapshot.sources}):
            return None
        observed = raw.get('as_of')
        try:
            if not isinstance(observed, str) or iso_date(observed[:10]) > as_of:
                return None
        except InputError:
            return None
        return public_fact_value(field, raw.get('value'))

    def _similar(self, request, snapshot, response):
        anchors = snapshot.lookup_bonds(request['anchor_bond_id'])
        if len(anchors) != 1:
            response['status'] = 'ambiguous' if anchors else 'not_found'
            return
        anchor = anchors[0]
        as_of = iso_date(request.get('as_of_date', snapshot.as_of[:10]))
        date_field = 'maturity_date' if request['tenor_basis'] == 'maturity' else 'exercise_date'
        def days(row):
            value = self._known(row, date_field, snapshot, as_of)
            try:
                return (iso_date(value) - as_of).days if value else None
            except InputError:
                return None
        anchor_days = days(anchor)
        anchor_issuer = self._known(anchor, 'issuer_id', snapshot, as_of)
        excluded, unknown, candidates = {}, {}, []
        for row in snapshot.search_bonds():
            if row.get('bond_id') == request['anchor_bond_id']:
                continue
            tenor = days(row)
            checks = {'remaining_days': None if tenor is None else request['remaining_days']['min'] <= tenor <= request['remaining_days']['max']}
            if request['issuer_scope'] != 'any':
                issuer = self._known(row, 'issuer_id', snapshot, as_of)
                checks['issuer_id'] = None if issuer is None or anchor_issuer is None else (issuer == anchor_issuer if request['issuer_scope'] == 'same' else issuer != anchor_issuer)
            if 'bond_types' in request:
                value = self._known(row, 'bond_type', snapshot, as_of)
                checks['bond_type'] = None if value is None else value in request['bond_types']
            if 'industry' in request:
                value, condition = self._known(row, 'industry', snapshot, as_of), request['industry']
                checks['industry'] = None if not isinstance(value, dict) or any(value.get(k) is None for k in ('scheme', 'level', 'code')) else value['scheme'] == condition['scheme'] and value['level'] == condition['level'] and value['code'] in condition['codes']
            if 'rating' in request:
                condition = request['rating']
                field = condition['scope'] + '_rating'
                value = self._known(row, field, snapshot, as_of)
                checks[field] = None if not isinstance(value, dict) or any(value.get(k) is None for k in ('agency', 'scale', 'value', 'rating_date')) else value['agency'] == condition['agency'] and value['scale'] == condition['scale'] and value['value'] in condition['allowed_values'] and iso_date(value['rating_date']) <= as_of
            for field, passed in checks.items():
                counts = unknown if passed is None else excluded if not passed else None
                if counts is not None:
                    counts[field] = counts.get(field, 0) + 1
            if not all(value is True for value in checks.values()):
                continue
            public = self._project(row, response, ['master', 'valuation', 'quotes'])
            comparisons = {}
            for field in request['compare_fields']:
                comparisons[field] = self._compare(field, anchor, row, snapshot, as_of, response, anchor_days, tenor)
            candidates.append({**self._identity(row), 'hard_filters': checks, 'remaining_days': tenor,
                               'tenor_distance': abs(tenor - anchor_days) if anchor_days is not None else None,
                               'comparisons': comparisons, 'facts': public})
        if request.get('sort') == ['code']:
            candidates.sort(key=lambda r: (r['code'] or '', r['bond_id']))
        else:
            candidates.sort(key=lambda r: (r['tenor_distance'] is None, r['tenor_distance'] or 0, r['code'] or '', r['bond_id']))
        page, more, cursor = self._page(candidates, request, snapshot)
        response['data'] = dict(anchor=self._identity(anchor), criteria_applied={k: v for k, v in request.items() if k not in ('cursor', 'schema_version')},
            as_of_date=as_of.isoformat(), candidates=page, total=len(candidates), has_more=more, cursor=cursor,
            excluded_counts=excluded, unknown_counts=unknown, relaxation_options=[])
        if unknown or response['missing_fields'] or response['warnings']:
            response['status'] = 'partial'
        response['missing_fields'] = sorted(set(response['missing_fields']))

    def _compare(self, field, anchor, row, snapshot, as_of, response, anchor_days, tenor):
        left = right = None
        reason = 'MISSING_OR_UNVERIFIED'
        if field == 'remaining_days':
            left, right = anchor_days, tenor
        elif field in MASTER_FIELDS:
            left, right = self._known(anchor, field, snapshot, as_of), self._known(row, field, snapshot, as_of)
            if field == 'balance':
                if any(self._known(anchor, key, snapshot, as_of) is None or self._known(anchor, key, snapshot, as_of) != self._known(row, key, snapshot, as_of)
                       for key in ('balance_unit', 'balance_date')):
                    left = right = None
                    reason = 'BALANCE_UNIT_OR_DATE_NOT_COMPARABLE'
            if field in ('industry', 'bond_rating', 'issuer_rating') and left is not None and right is not None:
                keys = ('scheme', 'level') if field == 'industry' else ('agency', 'scale', 'rating_date')
                if any(left.get(k) is None or left.get(k) != right.get(k) for k in keys):
                    left = right = None
                    reason = 'CLASSIFICATION_NOT_COMPARABLE'
        elif field == 'valuation_yield_pct':
            a, b = anchor.get('valuation', {}), row.get('valuation', {})
            required = ('provider', 'basis', 'valuation_date', 'yield_pct')
            valid = all(isinstance(values.get(k), dict) and values[k].get('quality') == 'verified'
                        and isinstance(values[k].get('source_ref'), str)
                        and values[k].get('source_ref') in {s['id'] for s in response['sources']}
                        and values[k].get('value') is not None for values in (a, b) for k in required)
            if valid and all(a[k]['value'] == b[k]['value'] for k in required[:-1]) and a['basis']['value'] != 'unknown':
                try:
                    if iso_date(a['valuation_date']['value']) <= as_of:
                        left = public_fact_value('yield_pct', a['yield_pct']['value'])
                        right = public_fact_value('yield_pct', b['yield_pct']['value'])
                except InputError:
                    pass
            else:
                reason = 'VALUATION_DATE_OR_BASIS_NOT_COMPARABLE'
        else:
            side = 'bid' if field == 'bid_yield_pct' else 'ofr'
            a = self._project(anchor, response, ['quotes'])['best'][side]
            b = self._project(row, response, ['quotes'])['best'][side]
            if a and b and all(a[k] == b[k] for k in ('yield_basis', 'settlement_date')):
                left, right = a['yield_pct'], b['yield_pct']
            else:
                reason = 'QUOTE_DATE_OR_BASIS_NOT_COMPARABLE'
        comparable = left is not None and right is not None
        result = dict(comparable=comparable, anchor_value=left, candidate_value=right, reason=None if comparable else reason, difference=None)
        if comparable and field in ('remaining_days', 'balance', 'coupon_rate_pct', 'valuation_yield_pct', 'bid_yield_pct', 'ofr_yield_pct'):
            try:
                result['difference'] = decimal_text(Decimal(str(right)) - Decimal(str(left)))
                if field.endswith('yield_pct'):
                    result['difference_bp'] = decimal_text((Decimal(right) - Decimal(left)) * 100)
            except DecimalException:
                result.update(comparable=False, reason='INVALID_NUMERIC_FACT')
        return result

    def _calc(self, request, response):
        closed(request, ('schema_version', 'operation', 'inputs'), ('operation', 'inputs'))
        operation = enum(request['operation'], ('yield_gap', 'clean_dirty', 'current_yield', 'price_from_yield', 'yield_from_price'))
        inputs = request['inputs']
        common = {'hypothetical', 'bond_id', 'terms_snapshot_id'}
        fields = {
            'yield_gap': {'our_side', 'limit_yield_pct', 'peer_side', 'peer_yield_pct', 'same_basis', 'quote_type', 'yield_basis', 'peer_yield_basis', 'settlement_date', 'peer_settlement_date', 'source_ref', 'authorization_ref'},
            'clean_dirty': {'clean_price_per_100', 'dirty_price_per_100', 'accrued_interest_per_100', 'settlement_date', 'accrued_interest_settlement_date'},
            'current_yield': {'annual_coupon_per_100', 'clean_price_per_100', 'coupon_basis'},
        }
        terms_fields = {'bond_type', 'settlement_date', 'cashflows', 'convention', 'compounding_frequency', 'yield_basis', 'settlement_cashflow', 'accrued_interest_per_100', 'accrued_interest_settlement_date'}
        fields['price_from_yield'] = terms_fields | {'yield_pct'}
        fields['yield_from_price'] = terms_fields | {'price_type', 'price_per_100'}
        closed(inputs, fields[operation] | common)
        if 'hypothetical' in inputs and type(inputs['hypothetical']) is not bool:
            raise InputError('hypothetical须为布尔值')
        if inputs.get('hypothetical') is True and any(k in inputs for k in ('bond_id', 'terms_snapshot_id')):
            raise InputError('假设计算不能声称真实条款快照')
        response['data'] = dict(method=operation, version='1', inputs_used=deepcopy(inputs), result=None,
            units={}, assumptions=[], missing_inputs=[], usage='analysis_only', hypothetical=inputs.get('hypothetical', False),
            quality='user_supplied' if inputs.get('hypothetical') else 'derived')
        def missing(keys):
            absent = [key for key in keys if key not in inputs or inputs[key] is None]
            if absent:
                response['status'] = 'partial'
                response['missing_fields'] = absent
                response['data']['missing_inputs'] = absent
                response['warnings'].append(dict(code='MISSING_INPUTS', fields=absent))
            return bool(absent)
        if operation == 'yield_gap':
            if missing(('our_side', 'limit_yield_pct', 'peer_side', 'peer_yield_pct', 'same_basis')):
                return
            enum(inputs['our_side'], ('BUY', 'SELL')); enum(inputs['peer_side'], ('bid', 'ofr'))
            number(inputs['limit_yield_pct']); number(inputs['peer_yield_pct'])
            if type(inputs['same_basis']) is not bool:
                raise InputError('same_basis须为布尔值')
            if 'quote_type' in inputs:
                enum(inputs['quote_type'], ('yield', 'clean_price', 'dirty_price', 'spread', 'unknown'))
            for key in ('yield_basis', 'peer_yield_basis'):
                if key in inputs:
                    enum(inputs[key], ('maturity', 'exercise', 'recommended', 'unknown'))
            for key in ('settlement_date', 'peer_settlement_date'):
                if key in inputs:
                    iso_date(inputs[key])
            for key in ('source_ref', 'authorization_ref'):
                if key in inputs:
                    text_value(inputs[key])
            comparable = inputs['same_basis'] and inputs.get('quote_type', 'yield') == 'yield'
            if inputs.get('yield_basis') == 'unknown' or inputs.get('peer_yield_basis') == 'unknown':
                comparable = False
            for a, b in (('yield_basis', 'peer_yield_basis'), ('settlement_date', 'peer_settlement_date')):
                if a in inputs or b in inputs:
                    comparable = comparable and inputs.get(a) is not None and inputs.get(a) == inputs.get(b)
            try:
                result = goal(inputs['our_side'], inputs['limit_yield_pct'], inputs['peer_side'], inputs['peer_yield_pct'], valid=comparable)
            except ValueError as exc:
                raise InputError(str(exc)) from None
            response['data'].update(result=result, units={'raw_gap_bp': 'BP', 'remaining_gap_bp': 'BP'})
            if result['raw_gap_bp'] is None:
                response['status'] = 'partial'
                response['warnings'].append(dict(code='INCOMPARABLE', fields=['peer_side', 'same_basis']))
            return
        if not inputs.get('hypothetical'):
            if missing(('bond_id', 'terms_snapshot_id')):
                return
            text_value(inputs['bond_id']); text_value(inputs['terms_snapshot_id'])
            snapshot = self._snapshot(response)
            terms = snapshot.load_bond_terms(inputs['bond_id'], inputs['terms_snapshot_id'])
            if terms is None or terms.get('quality') != 'verified' or terms.get('source_ref') not in {s['id'] for s in response['sources']}:
                response['status'] = 'partial'
                response['missing_fields'] = ['verified_bond_terms']
                response['data']['missing_inputs'] = ['verified_bond_terms']
                return
            locked = terms_fields | {'annual_coupon_per_100', 'coupon_basis'}
            if any(k in inputs and k in terms and inputs[k] != terms[k] for k in locked):
                raise InputError('输入与已验收条款快照冲突')
            # Verified metadata alone does not authorize a caller to invent absent terms.
            required_terms = {
                'price_from_yield': ('bond_type', 'cashflows', 'convention', 'compounding_frequency', 'yield_basis', 'settlement_cashflow'),
                'yield_from_price': ('bond_type', 'cashflows', 'convention', 'compounding_frequency', 'yield_basis', 'settlement_cashflow'),
                'clean_dirty': ('accrued_interest_per_100', 'accrued_interest_settlement_date'),
                'current_yield': ('annual_coupon_per_100', 'coupon_basis'),
            }[operation]
            absent = [key for key in required_terms if terms.get(key) is None]
            if absent:
                response['status'] = 'partial'
                response['missing_fields'] = absent
                response['data']['missing_inputs'] = absent
                response['warnings'].append(dict(code='MISSING_INPUTS', fields=absent))
                return
            if any(key in inputs and key not in terms for key in ('accrued_interest_per_100', 'accrued_interest_settlement_date')):
                raise InputError('真实券应计利息须来自已验收条款快照')
            inputs = {**{k: deepcopy(v) for k, v in terms.items() if k in fields[operation]}, **inputs}
            response['data']['inputs_used'] = deepcopy(inputs)
        if operation == 'current_yield':
            if missing(('annual_coupon_per_100', 'clean_price_per_100', 'coupon_basis')):
                return
            enum(inputs['coupon_basis'], ('fixed_annual',))
            coupon = number(inputs['annual_coupon_per_100'])
            if coupon < 0:
                raise InputError('年息不得为负')
            result = coupon / number(inputs['clean_price_per_100'], True) * 100
            response['data'].update(result={'current_yield_pct': decimal_text(result), 'name': '当期收益率'}, units={'current_yield_pct': 'percent'})
            return
        if operation == 'clean_dirty':
            if ('clean_price_per_100' in inputs) == ('dirty_price_per_100' in inputs):
                raise InputError('净价或全价须且只能提供一个')
            if missing(('settlement_date', 'accrued_interest_per_100', 'accrued_interest_settlement_date')):
                return
            accrued = self._accrued(inputs)
            if 'clean_price_per_100' in inputs:
                clean = number(inputs['clean_price_per_100'], True); dirty = clean + accrued
            else:
                dirty = number(inputs['dirty_price_per_100'], True); clean = dirty - accrued
            if clean <= 0 or dirty <= 0:
                raise InputError('换算后价格必须为正')
            response['data'].update(result={'clean_price_per_100': decimal_text(clean), 'dirty_price_per_100': decimal_text(dirty), 'accrued_interest_per_100': decimal_text(accrued)}, units={'prices': 'price_per_100'})
            return
        required = ['bond_type', 'settlement_date', 'cashflows', 'convention', 'compounding_frequency', 'yield_basis', 'settlement_cashflow']
        required += ['yield_pct'] if operation == 'price_from_yield' else ['price_type', 'price_per_100']
        if missing(required):
            return
        if inputs['bond_type'] not in ('fixed', 'zero_coupon') or inputs['convention'] != CONVENTION or inputs['yield_basis'] != 'maturity':
            response['status'] = 'unsupported'
            response['warnings'].append(dict(code='UNSUPPORTED_CONVENTION_OR_BOND', fields=['bond_type', 'convention', 'yield_basis']))
            return
        frequency = integer(inputs['compounding_frequency'], 1, 12)
        settlement = iso_date(inputs['settlement_date'])
        enum(inputs['settlement_cashflow'], ('include', 'exclude'))
        if not isinstance(inputs['cashflows'], list) or not 1 <= len(inputs['cashflows']) <= 1000:
            raise InputError('现金流须为1至1000项完整数组')
        cashflows = []
        for flow in inputs['cashflows']:
            closed(flow, ('payment_date', 'amount_per_100'), ('payment_date', 'amount_per_100'))
            when, amount = iso_date(flow['payment_date']), number(flow['amount_per_100'], True)
            days = (when - settlement).days
            if days < 0:
                continue
            if days == 0 and inputs['settlement_cashflow'] == 'exclude':
                continue
            cashflows.append((days, amount))
        if not cashflows or not any(days > 0 for days, amount in cashflows):
            raise InputError('结算日后无有效现金流')
        accrued = None
        if inputs.get('accrued_interest_per_100') is not None:
            if missing(('accrued_interest_settlement_date',)):
                return
            accrued = self._accrued(inputs)
        with localcontext() as context:
            context.prec = 40
            def present_value(yield_pct):
                base = 1 + yield_pct / (100 * frequency)
                if base <= 0:
                    raise InputError('复利基数须为正')
                return sum((amount / context.power(base, Decimal(days) * frequency / 365) for days, amount in cashflows), Decimal(0))
            if operation == 'price_from_yield':
                y = number(inputs['yield_pct'])
                dirty = present_value(y)
            else:
                enum(inputs['price_type'], ('clean', 'dirty'))
                dirty = number(inputs['price_per_100'], True)
                if inputs['price_type'] == 'clean':
                    if accrued is None:
                        missing(('accrued_interest_per_100',)); return
                    dirty += accrued
                # Positive future cashflows imply one monotone root when price exceeds immediate cashflow.
                immediate = sum((amount for days, amount in cashflows if days == 0), Decimal(0))
                if dirty <= immediate:
                    response['status'] = 'unsupported'
                    response['warnings'].append(dict(code='NO_LEGAL_ROOT', fields=['price_per_100']))
                    return
                low, high = Decimal(-100 * frequency) + Decimal('0.00000001'), Decimal(100)
                for _ in range(64):
                    if present_value(high) <= dirty:
                        break
                    high *= 2
                else:
                    response['status'] = 'unsupported'
                    response['warnings'].append(dict(code='NO_BRACKET', fields=['price_per_100']))
                    return
                if present_value(low) < dirty:
                    response['status'] = 'unsupported'
                    response['warnings'].append(dict(code='NO_BRACKET', fields=['price_per_100']))
                    return
                for _ in range(256):
                    y = (low + high) / 2
                    value = present_value(y)
                    if abs(value - dirty) <= Decimal('0.0000000001') and high - low <= Decimal('0.000000001'):
                        break
                    if value > dirty:
                        low = y
                    else:
                        high = y
                else:
                    response['status'] = 'unsupported'
                    response['warnings'].append(dict(code='NO_CONVERGENCE', fields=[]))
                    return
            clean = dirty - accrued if accrued is not None else None
            if clean is not None and clean <= 0:
                raise InputError('净价必须为正')
            result = dict(dirty_price_per_100=decimal_text(dirty), clean_price_per_100=decimal_text(clean) if clean is not None else None,
                          accrued_interest_per_100=decimal_text(accrued) if accrued is not None else None,
                          yield_pct=decimal_text(y), solver_status='converged' if operation == 'yield_from_price' else 'not_needed')
        response['data'].update(method=CONVENTION, result=result, units={'prices': 'price_per_100', 'yield_pct': 'percent'},
            assumptions=['Explicit supplied cashflows; ACT/365F; periodic compounding; no taxes or transaction fees.'])
        if accrued is None:
            response['status'] = 'partial'
            response['data']['missing_inputs'] = ['accrued_interest_per_100']
            response['missing_fields'] = ['clean_price_per_100', 'accrued_interest_per_100']

    @staticmethod
    def _accrued(inputs):
        if iso_date(inputs['settlement_date']) != iso_date(inputs['accrued_interest_settlement_date']):
            raise InputError('应计利息结算日不一致')
        return number(inputs['accrued_interest_per_100'])
