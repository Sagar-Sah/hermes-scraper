"""Stream a read-only snapshot of saved HTTP table evidence, never product rows.

This module has no network/browser APIs. CSV entries are observed base-table
prices, not full selected configurations or independently verified prices.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import parse_qsl, urlsplit

COLUMNS = ('source_product_id', 'quantity', 'raw_markup_price', 'currency', 'response_evidence_id', 'product_ids', 'product_titles', 'product_urls')
_BASE = re.compile(r'/service/rest/v1/products/([1-9]\d*)/prices\Z')
_CATALOG = re.compile(r'/service/rest/v1/products/([1-9]\d*)/options\Z')
_OPTION = re.compile(r'/service/rest/v1/products/([1-9]\d*)/options/([1-9]\d*)/prices\Z')
_MONEY = re.compile(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\Z')


def family(path):
    if _BASE.fullmatch(path): return 'base_price_tables'
    if _CATALOG.fullmatch(path): return 'option_catalogs'
    if _OPTION.fullmatch(path): return 'option_price_tables'
    if path == '/service/rest/v1/products': return 'product_lookup_or_listing'
    return 'other'


def _raw_price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value)
    return text if len(text) <= 100 and _MONEY.fullmatch(text) else None


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return int(value) if str(value).isdigit() and 0 < int(value) <= 10**12 else None


def _scalar(value):
    """Only selected public evidence fields, bounded even for bad source shapes."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return str(value)[:150] if value is not None else None
    return {'unsupported_type': type(value).__name__}


def _request_matches(request, url):
    parsed = urlsplit(url or '')
    context = request.get('context', {})
    params = request.get('params', {})
    if not isinstance(context, dict) or not isinstance(params, dict):
        return False
    expected = [(str(k), str(v).lower() if isinstance(v, bool) else str(v)) for k,v in params.items()]
    actual = parse_qsl(parsed.query, keep_blank_values=True)
    return (request.get('method') == 'GET' and request.get('body') is None
            and context.get('origin') == 'https://www.printograph.com'
            and f'{parsed.scheme}://{parsed.netloc}' == context['origin']
            and parsed.path == request.get('path') and not parsed.fragment
            and len(actual) == len(dict(actual)) and sorted(actual) == sorted(expected))


def _load_body(path, root, maximum):
    target = Path(path or '')
    if not target.is_absolute(): target = root / target
    target = target.resolve()
    if not target.is_relative_to(root / 'objects'):
        raise ValueError('object_path_outside_store')
    if target.stat().st_size > maximum:
        raise ValueError('response_body_exceeds_report_limit')
    raw = target.read_bytes()
    if raw.startswith(b'\x1f\x8b'):
        with gzip.open(target, 'rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum: raise ValueError('decompressed_body_exceeds_report_limit')
    value = json.loads(raw)
    semantic = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return value, semantic


def export_stage(db_path, objects_root, job_id, output_dir, *, max_body_bytes=64*1024*1024, filename_prefix="base-price"):
    """One read transaction; bounded per-response memory and streamed CSV/diagnostics."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', filename_prefix):
        raise ValueError('Filename prefix must be a bounded plain filename stem')
    root, output = Path(objects_root).resolve(), Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    job = db.execute('SELECT id,run_id,scope,status,used,budget FROM source_domain_jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        db.close()
        raise LookupError('Source domain job not found')
    counts = Counter({r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM source_domain_tasks WHERE job_id=? GROUP BY status', (job_id,))})
    attempt_statuses, attempt_phases, attempt_families = Counter(), Counter(), Counter()
    attempt_count = 0
    for attempt in db.execute('SELECT phase,public_url,status FROM source_domain_attempts WHERE job_id=?', (job_id,)):
        attempt_count += 1
        attempt_statuses[str(attempt['status'])] += 1
        attempt_phases[attempt['phase']] += 1
        attempt_families[family(urlsplit(attempt['public_url']).path)] += 1
    def product_ids(raw):
        try: value = json.loads(raw)
        except (TypeError, ValueError): return []
        return sorted(set(value)) if isinstance(value,list) and all(isinstance(x,str) and 0<len(x)<=128 for x in value) else []
    products = {}
    for task in db.execute('SELECT products,status FROM source_domain_tasks WHERE job_id=?', (job_id,)):
        for ident in product_ids(task['products']):
            if ident not in products:
                metadata = db.execute('SELECT title,url FROM products WHERE id=?', (ident,)).fetchone()
                products[ident] = {'product_id':ident,'title':metadata['title'] if metadata else None,
                    'product_url':metadata['url'] if metadata else None, 'task_counts':Counter(),
                    'families':Counter(), 'base_price_entries':0,'quantity_tier_entry_count':0,'option_catalog_entries':0,
                    'metadata_available':metadata is not None}
            products[ident]['task_counts'][task['status']] += 1
    report = {'schema_version':2, 'generated_at':datetime.now(timezone.utc).isoformat(),
        'job':{k:job[k] for k in ('id','run_id','scope','status')},
        'requests_made_by_report':0, 'snapshot_semantics':'One read-only database transaction; rerun for later saved results.',
        'evidence_only':True, 'catalog_complete':False, 'final_configuration_count':None,
        'verified':False, 'product_rows_inserted':0,
        'task_counts':dict(counts), 'planned_unique_requests':sum(counts.values()),
        'succeeded_unique_requests':counts['succeeded'],
        'source_attempts':{'durable_used':job['used'],'attempt_budget':job['budget'],'ledger_count':attempt_count,
            'statuses':dict(sorted(attempt_statuses.items())),'phases':dict(attempt_phases),'families':dict(attempt_families)},
        'observation_status':'no_source_attempts' if attempt_count == 0 else 'saved_evidence_snapshot',
        'quantity_tier_entry_count':0, 'exported_base_price_entries':0, 'option_catalog_entry_count':0,
        'option_price_entry_count':0,
        'families':{}, 'products':[], 'product_attribution_scope':'Each product receives its attributed task/entry counts. Shared tasks are counted once globally; per-product counts can overlap. Source attempts are not guessed per product.', 'diagnostics':{'count':0,'codes':{},'samples':[]},
        'limits':['Base-price entries omit selected option/service charges, shipping, tax and independent verification.',
                  'Successful request-stage completion does not establish the full valid catalog.',
                  'Status 0/null indicates no completed HTTP response, not success.',
                  'Unsupported entries are retained as explicit diagnostics; no prices or currency are inferred.']}
    metrics = defaultdict(lambda: {'succeeded_tasks':0,'supported_response_bodies':0,'source_product_ids':set(),'semantic_bodies':set()})
    codes = Counter()
    csv_name, diag_name = filename_prefix+'-evidence.csv', filename_prefix+'-diagnostics.jsonl'
    temporary = []
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=output, delete=False) as csv_file, tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=output, delete=False) as diag_file:
            temporary.extend([Path(csv_file.name), Path(diag_file.name)])
            writer = csv.DictWriter(csv_file, fieldnames=COLUMNS)
            writer.writeheader()
            def diagnose(code, **fields):
                value = {'code':code, **fields}
                codes[code] += 1
                report['diagnostics']['count'] += 1
                if len(report['diagnostics']['samples']) < 50:
                    report['diagnostics']['samples'].append(value)
                diag_file.write(json.dumps(value, allow_nan=False)+'\n')
            if attempt_count != job['used']:
                diagnose('attempt_ledger_and_budget_counter_differ', ledger_count=attempt_count, durable_used=job['used'])
            for task in db.execute('''SELECT t.id task_id,t.request_key,t.request,t.products,t.response_id,r.status response_status,r.url,r.object_path
                FROM source_domain_tasks t LEFT JOIN responses r ON r.id=t.response_id
                WHERE t.job_id=? AND t.status='succeeded' ORDER BY t.id''', (job_id,)):
                consumers = product_ids(task['products'])
                refs = {'task_id':task['task_id'],'response_evidence_id':task['response_id'],'product_ids':consumers}
                if not consumers:
                    diagnose('product_attribution_missing', **refs)
                attribution = {'product_ids':json.dumps(consumers,separators=(',',':')),
                    'product_titles':json.dumps([products[p]['title'] for p in consumers],separators=(',',':')),
                    'product_urls':json.dumps([products[p]['product_url'] for p in consumers],separators=(',',':'))}
                try:
                    request = json.loads(task['request'])
                    if not isinstance(request,dict): raise ValueError('request_not_object')
                except (TypeError, ValueError):
                    diagnose('invalid_saved_request', **refs); continue
                path = request.get('path', '')
                group = family(path)
                stats = metrics[group]
                stats['succeeded_tasks'] += 1
                for ident in consumers: products[ident]['families'][group] += 1
                if task['response_status'] != 200:
                    diagnose('successful_task_missing_http_200_response', **refs); continue
                if not _request_matches(request, task['url']):
                    diagnose('request_response_scope_mismatch', **refs); continue
                try:
                    payload, semantic = _load_body(task['object_path'], root, max_body_bytes)
                except (OSError, ValueError, TypeError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[a-z_]+', str(exc)) else 'unreadable_response_body'
                    diagnose(reason, **refs); continue
                stats['semantic_bodies'].add(semantic)
                match = (_BASE.fullmatch(path) if group == 'base_price_tables' else
                         _OPTION.fullmatch(path) if group == 'option_price_tables' else _CATALOG.fullmatch(path))
                if match: stats['source_product_ids'].add(match.group(1))
                items = payload.get('items') if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    diagnose('unsupported_response_shape_items_not_list', family=group, **refs); continue
                if group == 'option_catalogs':
                    for index, item in enumerate(items):
                        option_type = item.get('type') if isinstance(item,dict) else None
                        if not isinstance(item,dict) or not str(item.get('id','')).isdigit() or not isinstance(option_type,dict) or not str(option_type.get('id','')).isdigit():
                            diagnose('unsupported_option_catalog_entry', index=index, **refs)
                        else:
                            report['option_catalog_entry_count'] += 1
                            for ident in consumers: products[ident]['option_catalog_entries'] += 1
                    stats['supported_response_bodies'] += 1
                    continue
                if group == 'option_price_tables':
                    stats['supported_response_bodies'] += 1
                    seen = set()
                    for index, item in enumerate(items):
                        if not isinstance(item, dict):
                            diagnose('unsupported_option_price_entry', index=index, **refs)
                            continue
                        quantity = _quantity(item.get('quantity'))
                        price = _raw_price(item.get('markupPrice'))
                        if quantity is not None:
                            report['quantity_tier_entry_count'] += 1
                            for ident in consumers: products[ident]['quantity_tier_entry_count'] += 1
                        if quantity is None or price is None:
                            diagnose('unsupported_quantity_or_markup_price', index=index,
                                     quantity=_scalar(item.get('quantity')), raw_markup_price=_scalar(item.get('markupPrice')), **refs)
                            continue
                        if quantity in seen:
                            diagnose('duplicate_quantity_in_option_table', index=index, quantity=quantity, **refs)
                        seen.add(quantity)
                        report['option_price_entry_count'] += 1
                        for ident in consumers:
                            products[ident]['option_price_entries'] = products[ident].get('option_price_entries', 0) + 1
                    continue
                if group != 'base_price_tables':
                    diagnose('response_family_not_exported', family=group, **refs); continue
                stats['supported_response_bodies'] += 1
                currency = request.get('context',{}).get('currency')
                if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):
                    currency = ''
                    diagnose('request_currency_unknown', **refs)
                seen = set()
                for index, item in enumerate(items):
                    if not isinstance(item,dict):
                        diagnose('unsupported_base_price_entry', index=index, **refs); continue
                    quantity = _quantity(item.get('quantity'))
                    if quantity is not None:
                        report['quantity_tier_entry_count'] += 1
                        for ident in consumers: products[ident]['quantity_tier_entry_count'] += 1
                    price = _raw_price(item.get('markupPrice'))
                    if quantity is None or price is None:
                        diagnose('unsupported_quantity_or_markup_price', index=index,
                                 quantity=_scalar(item.get('quantity')), raw_markup_price=_scalar(item.get('markupPrice')), **refs)
                        continue
                    if quantity in seen:
                        diagnose('duplicate_quantity_in_base_table', index=index, quantity=quantity, **refs)
                    seen.add(quantity)
                    writer.writerow({'source_product_id':match.group(1),'quantity':quantity,'raw_markup_price':price,
                                     'currency':currency,'response_evidence_id':task['response_id'], **attribution})
                    report['exported_base_price_entries'] += 1
                    for ident in consumers: products[ident]['base_price_entries'] += 1
            csv_file.flush(); os.fsync(csv_file.fileno())
            diag_file.flush(); os.fsync(diag_file.fileno())
        db.rollback(); db.close()
        for name, stats in sorted(metrics.items()):
            report['families'][name] = {'succeeded_tasks':stats['succeeded_tasks'],
                'supported_response_bodies':stats['supported_response_bodies'],
                'distinct_source_product_ids':len(stats['source_product_ids']),
                'distinct_full_semantic_response_bodies':len(stats['semantic_bodies'])}
        for ident, product in sorted(products.items()):
            product['planned_unique_requests'] = sum(product['task_counts'].values())
            product['succeeded_unique_requests'] = product['task_counts']['succeeded']
            product['task_counts'] = dict(product['task_counts'])
            product['families'] = dict(product['families'])
            report['products'].append(product)
        report['diagnostics']['codes'] = dict(codes)
        report['base_price_entries_per_source_attempt'] = report['exported_base_price_entries']/attempt_count if attempt_count else None
        base_attempts = attempt_families['base_price_tables']
        report['base_price_entries_per_base_price_attempt'] = report['exported_base_price_entries']/base_attempts if base_attempts else None
        report['ratio_definition'] = 'Exported base-table entries divided by recorded HTTP attempts; entries are not complete product configurations.'
        report['files'] = {'csv':csv_name,'diagnostics':diag_name}
        for temp, name in zip(temporary, (csv_name,diag_name)):
            temp.replace(output/name)
        for name, text in [('source-domain-stage-report.json',json.dumps(report,indent=2,allow_nan=False)+'\n'),
                           ('source-domain-stage-report.md',markdown(report))]:
            with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=output,delete=False) as handle:
                handle.write(text); target=Path(handle.name)
            target.replace(output/name)
        return report
    finally:
        try: db.close()
        except Exception: pass
        for temp in temporary: temp.unlink(missing_ok=True)


def markdown(report):
    attempts = report['source_attempts']
    ratio = report['base_price_entries_per_source_attempt']
    rate = 'Undefined: no recorded source attempts' if ratio is None else f'{ratio:.2f}'
    lines = ['# Source table evidence by product','',f"Job `{report['job']['id']}` · {report['job']['scope']} · **{report['job']['status']}**.",
        f"Generated {report['generated_at']}. Zero source requests made by this export.",'',
        f"**{report['succeeded_unique_requests']} / {report['planned_unique_requests']} known requests succeeded**. HTTP attempts: {attempts['ledger_count']} (durable counter {attempts['durable_used']}, budget {attempts['attempt_budget']}). Statuses: `{attempts['statuses']}`.",
        f"Base-price entries exported: **{report['exported_base_price_entries']}**. Quantity-tier entries encountered: {report['quantity_tier_entry_count']}. Supported option catalog entries: {report['option_catalog_entry_count']}.",
        f"Base-price entries per source attempt: **{rate}**. These are base-price entries, not complete product configurations.",'',
        'If no HTTP attempts are recorded, this is a zero-attempt checkpoint, not a successful table collection.', '',
        '| Response family | Succeeded tasks | Distinct full semantic bodies | Distinct product IDs |', '|---|---:|---:|---:|']
    for family_name, item in report['families'].items():
        lines.append(f"| {family_name} | {item['succeeded_tasks']} | {item['distinct_full_semantic_response_bodies']} | {item['distinct_source_product_ids']} |")
    lines += ['', '| Product | Known requests succeeded / planned | Base-price entries | Option catalog entries |', '|---|---:|---:|---:|']
    for product in report['products']:
        title = str(product['title'] or product['product_id']).replace('|','\\|').replace('\n',' ')
        lines.append(f"| {title} | {product['succeeded_unique_requests']} / {product['planned_unique_requests']} | {product['base_price_entries']} | {product['option_catalog_entries']} |")
    lines += ['', report['product_attribution_scope'], '',f"Diagnostics: **{report['diagnostics']['count']}** (`{report['diagnostics']['codes']}`). The JSONL companion preserves every diagnostic with task/response evidence references.",'',
              'The CSV preserves supported raw markup prices and source quantities. Unknown currency stays blank with an explicit diagnostic. Unsupported entry shapes are excluded from CSV and recorded in diagnostics. Duplicate quantity entries remain separate evidence and are flagged.', '',
              'This export is evidence only. It inserts no current product rows, marks no prices verified, and establishes neither catalog completeness nor a final configuration count.', '',
              'Rerun:', '', '```sh', 'python -m crawler.source_domain_report --db /absolute/path/atlas.db --objects-root /absolute/path/atlas-data --job-id JOB_ID --output-dir /absolute/path/report-directory', '```','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--objects-root',required=True)
    parser.add_argument('--job-id',required=True);parser.add_argument('--output-dir',required=True)
    parser.add_argument('--filename-prefix',default='base-price',help='Plain evidence filename stem; e.g. roll-labels-base-price for earlier Roll filenames')
    args=parser.parse_args()
    result=export_stage(args.db,args.objects_root,args.job_id,args.output_dir,filename_prefix=args.filename_prefix)
    print(json.dumps({k:result[k] for k in ('observation_status','succeeded_unique_requests','exported_base_price_entries','quantity_tier_entry_count','base_price_entries_per_source_attempt')}))


if __name__ == '__main__': main()
