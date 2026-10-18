"""Independent supplement for the four unchanged public-tool measurement cases.

This checks saved provenance and substantive answers, not latency or live-server
attestation. The original 47-case gate and its public-text validator still apply.
"""
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path
import re

CASES = ('tool-bond-info', 'tool-issuer-info', 'tool-similar-bonds', 'tool-bond-calc')


def scalar_text(value):
    if isinstance(value, dict):
        return ' '.join(str(key)+' '+scalar_text(item) for key, item in value.items())
    if isinstance(value, list):
        return ' '.join(scalar_text(item) for item in value)
    return str(value) if value is not None else ''


def has_number(text, expected):
    return any(Decimal(match[0]) == Decimal(str(expected))
               for match in re.finditer(r'(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])', text))


def fact_value(value):
    return value.get('value') if isinstance(value, dict) else value


def number_equal(value, expected):
    if isinstance(value, bool): return False
    try:
        return Decimal(str(value)) == Decimal(str(expected))
    except (ArithmeticError, ValueError, TypeError):
        return False


def check_record(record, case_id):
    errors = []
    def require(condition, reason):
        if not condition: errors.append(reason)
    require(record.get('case_id') == case_id, 'CASE_ID_MISMATCH')
    require(record.get('success') is True, 'ORIGINAL_CASE_NOT_SUCCESSFUL')
    reply = record.get('reply') or {}
    proposal = reply.get('proposal') or {}
    public = proposal.get('text', '')
    answer = public+' '+scalar_text(reply.get('trader_view', {}))
    require(reply.get('action') == 'SEND' and isinstance(public, str) and bool(public.strip()), 'NO_FINAL_PUBLIC_REPLY')
    require((record.get('decision_commit') or {}).get('state') == 'QUEUED', 'NO_VALIDATED_REPLY_COMMAND')
    inputs = record.get('reply_inputs') or [record.get('reply_input') or {}]
    context = inputs[-1]
    inherited = context.get('analysis_tool_results', [])
    local = context.get('tool_results', [])
    require(isinstance(inherited, list) and isinstance(local, list), 'INVALID_TOOL_RESULT_ARRAYS')
    if not isinstance(inherited, list): inherited = []
    if not isinstance(local, list): local = []
    committed = (record.get('fact_commit') or {}).get('public_tool_results', [])
    if committed or inherited:
        require(inherited == committed, 'A_PERSISTED_RESULTS_NOT_DELIVERED_TO_B')
        source = context.get('analysis_tool_source') or {}
        require(bool(source.get('analysis_id')) and source.get('through_revision') ==
                (record.get('fact_commit') or {}).get('through_revision'), 'A_RESULT_SOURCE_WATERMARK_MISMATCH')
    receipts = [{'request': {'name': row.get('tool'), 'arguments': row.get('arguments')}, 'result': row.get('result')}
                for row in record.get('evidence', []) if row.get('tool')]
    results = inherited + local
    require(bool(results), 'B_RECEIVED_NO_TOOL_RESULTS')
    for result in results:
        require(result in receipts, 'B_TOOL_RESULT_HAS_NO_ACTUAL_INVOCATION_RECEIPT')
    def usable(name, *, mode=None):
        return [row['result'].get('data') or {} for row in results
                if isinstance(row, dict) and isinstance(row.get('request'), dict)
                and row['request'].get('name') == name
                and (mode is None or row['request'].get('arguments', {}).get('mode') == mode)
                and isinstance(row.get('result'), dict) and row['result'].get('status') in ('ok', 'partial')]

    if case_id == 'tool-bond-info':
        data = usable('bond_info', mode='get')
        issuers = [fact_value(row.get('master', {}).get('issuer_id')) for row in data
                   if row.get('identity', {}).get('bond_id') == 'SYN-A']
        require('SYN-ISSUER' in issuers, 'BOND_SEARCH_IS_NOT_ISSUER_CONFIRMATION')
        # The requested issuer can be stated using its actual public ID or a
        # name backed by a separately received issuer result.
        names = ['SYN-ISSUER']
        for row in usable('issuer_info'):
            identity = row.get('identity', {})
            if identity.get('issuer_id') == 'SYN-ISSUER':
                names.extend(str(identity[k]) for k in ('full_name', 'short_name') if identity.get(k))
        require(any(name in public for name in names), 'REPLY_DOES_NOT_CONFIRM_THE_REQUESTED_ISSUER')
    elif case_id == 'tool-issuer-info':
        data = [row for row in usable('issuer_info', mode='get')
                if row.get('identity', {}).get('issuer_id') == 'SYN-ISSUER']
        require(bool(data), 'ISSUER_SEARCH_IDENTITY_IS_NOT_ISSUER_DETAILS')
        names = ['SYN-ISSUER']
        details = []
        for row in data:
            identity = row.get('identity', {})
            names.extend(str(identity[k]) for k in ('full_name', 'short_name') if identity.get(k))
            for key in ('registered_region', 'ownership', 'issuer_rating', 'industry', 'uscc'):
                value = fact_value(row.get(key))
                if isinstance(value, dict): value = value.get('value') or value.get('code')
                if isinstance(value, str) and value:
                    details.append((key, value))
        require(any(name in public for name in names), 'REPLY_DOES_NOT_IDENTIFY_THE_REQUESTED_ISSUER')
        require(any(value in answer for _key, value in details), 'ANSWER_HAS_NO_SOURCED_ISSUER_DETAIL')
    elif case_id == 'tool-similar-bonds':
        candidates = []
        for row in usable('similar_bonds'):
            criteria = row.get('criteria_applied', {})
            if (criteria.get('anchor_bond_id') == 'SYN-A' and criteria.get('issuer_scope') == 'same'
                    and criteria.get('tenor_basis') == 'maturity'
                    and criteria.get('remaining_days') == {'min': 0, 'max': 365}):
                candidates.extend(row.get('candidates', []))
        candidates = [row for row in candidates if row.get('bond_id') == 'SYN-B']
        require(bool(candidates), 'REQUESTED_SIMILAR_BOND_RESULT_MISSING')
        require('SYN-B' in answer, 'ANSWER_OMITS_THE_ACTUAL_CANDIDATE')
        require(bool(re.search(r'相似|候选|同发行人|同主体', public)), 'PUBLIC_REPLY_IGNORES_SIMILAR_BOND_REQUEST')
        if candidates:
            comparisons = candidates[0].get('comparisons', {})
            tenor = comparisons.get('remaining_days', {})
            value = comparisons.get('valuation_yield_pct', {})
            require(tenor.get('comparable') is True and value.get('comparable') is True,
                    'REQUESTED_COMPARISONS_NOT_AVAILABLE')
            tenor_answer = has_number(answer, tenor.get('candidate_value', 335)) or (
                has_number(answer, abs(Decimal(str(tenor.get('difference', '-30'))))) and
                bool(re.search(r'短|少|差|difference', answer, re.I)))
            require(tenor_answer and bool(re.search(r'天|期限|remaining_days', answer)), 'ANSWER_OMITS_SUPPORTED_TENOR_COMPARISON')
            valuation_answer = has_number(answer, value.get('candidate_value', '2.9872')) or (
                bool(re.search(r'(?:估值.{0,20}(?:相同|一致|持平)|(?:相同|一致|持平).{0,20}估值)', answer))
                and Decimal(str(value.get('difference_bp', '1'))) == 0)
            require(valuation_answer, 'ANSWER_OMITS_SUPPORTED_VALUATION_COMPARISON')
    elif case_id == 'tool-bond-calc':
        data = [row for row in usable('bond_calc') if row.get('method') == 'yield_gap'
                and row.get('hypothetical') is True
                and number_equal(row.get('inputs_used', {}).get('limit_yield_pct'), '3.20')
                and number_equal(row.get('inputs_used', {}).get('peer_yield_pct'), '3.10')]
        require(any(Decimal(str(row.get('result', {}).get('raw_gap_bp', '-1'))) == 10 for row in data),
                'USER_SUPPLIED_TEN_BP_CALCULATION_NOT_AVAILABLE')
        require(bool(re.search(r'价差|利差|基点|BP|核算|计算', public, re.I)), 'PUBLIC_REPLY_IGNORES_CALCULATION_REQUEST')
        require(bool(re.search(r'(?<![\d.])10(?:\.0+)?\s*(?:BP|基点)', answer, re.I)) or
                (has_number(answer, 10) and bool(re.search(r'raw_gap_bp|差值.{0,8}BP|单位.{0,8}BP', answer, re.I))),
                'ANSWER_OMITS_THE_SUPPORTED_TEN_BP_RESULT')
    else:
        errors.append('UNKNOWN_TOOL_CASE')
    return {'case_id': case_id, 'passed': not errors, 'errors': sorted(set(errors)),
            'inherited_results': len(inherited), 'b_new_results': len(local),
            'public_reply': public, 'trader_view': reply.get('trader_view', {})}


def check_directory(directory):
    directory = Path(directory)
    results = []
    for case_id in CASES:
        path = directory / (case_id+'.json')
        if not path.is_file():
            results.append({'case_id': case_id, 'passed': False, 'errors': ['CASE_ARTIFACT_MISSING']})
            continue
        try:
            result = check_record(json.loads(path.read_text()), case_id)
            result['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            result = {'case_id': case_id, 'passed': False, 'errors': ['MALFORMED_ARTIFACT: '+str(exc)]}
        results.append(result)
    return {'passed': all(row['passed'] for row in results), 'cases': results,
            'scope': 'Four saved public-tool cases; supplements original semantic, source-freeze and latency gates'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = check_directory(args.run_dir)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'passed': result['passed'], 'cases': [
        {'case_id': row['case_id'], 'passed': row['passed'], 'errors': row['errors']} for row in result['cases']]}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
