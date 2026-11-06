"""Reproduce RQ1 decisions and gather independent, read-only pair evidence."""
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT = Path('D:/Projects/sysml-project')
BIRD = PROJECT.parent / 'BIRD-Interact/BIRD-Interact-ADK'
ROOT = PROJECT / 'artifacts/rq1-command-a-full-lite'
OUT = Path(__file__).resolve().parent / 'full-rq1-analysis'
OUT.mkdir(exist_ok=True)
sys.path[:0] = [str(PROJECT), str(BIRD)]
import psycopg
from pglast import parser
from types import SimpleNamespace
from sysml import db, identity
from sysml.common import digest, fingerprint, result

def read(path):
    return json.loads(path.read_bytes())

def save(name, data):
    target = OUT / name
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf8')
    temporary.replace(target)

def stored_proposal(sql):
    path = ROOT / 'n3-proposals' / (digest(['command-a-03-2025', sql]) + '.json')
    return read(path)['proposal']

identity.propose = stored_proposal
pg_values = dict(PG_HOST='127.0.0.1', PG_USER='root', PG_PASSWORD='123123')
for line in (BIRD/'.env').read_text(encoding='utf8').splitlines():
    key, separator, value = line.partition('=')
    if separator and key.strip() in pg_values:
        pg_values[key.strip()] = value.strip().strip('\"\'')
settings = SimpleNamespace(**{k.lower(): v for k,v in pg_values.items()})
pairs = read(ROOT / 'pair-review.json')
attempts = [read(f) for f in sorted((ROOT/'attempts').glob('*/*.json'))]
assert len({(a['result']['task_id'], a['wave']) for a in attempts}) == 1500
sql_by_db = defaultdict(set)
for attempt in attempts:
    for event in attempt['events']:
        if event['kind'] == 'read' and event.get('successful'):
            sql_by_db[event['database']].add(event['sql'])

def audit_database(item):
    database, sqls = item
    cached = OUT / (database+'-queries.json')
    if cached.exists():
        data = read(cached)
        assert set(data['queries']) == {digest(sql) for sql in sqls}
        if data.get('version') == 2:
            return data
    data = {'version': 2, 'database': database, 'queries': {}}
    with psycopg.connect(host=settings.pg_host, port=5432, user=settings.pg_user,
                         password=settings.pg_password, dbname=database+'_template') as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        connection.execute("SET LOCAL timezone='UTC'")
        connection.execute('SET LOCAL search_path=pg_catalog,public')
        connection.execute("SET LOCAL datestyle='ISO,YMD'")
        connection.execute("SET LOCAL statement_timeout='15000ms'")
        data['snapshot_date'] = str(connection.execute('SELECT CURRENT_DATE').fetchone()[0])
        catalog = db.catalog(connection, tracked=False)
        data['catalog'] = catalog
        for sql in sorted(sqls):
            evidence = {'sql': sql, 'decisions': {}}
            for policy in ['N0', 'N1', 'N2', 'N3']:
                analysis = identity.analyze(sql, [], {'database': database, 'scope': 'audit'}, policy, catalog)
                evidence['decisions'][policy] = dict(key=analysis.key, reason=analysis.reason, proposal=analysis.proposal)
            try:
                ast = identity.tree(sql)
                evidence['tree_digest'] = digest(ast)
                evidence['ordered'] = bool(ast['SelectStmt'].get('sortClause'))
            except Exception as error:
                evidence['parse_error'] = type(error).__name__
                data['queries'][digest(sql)] = evidence
                continue
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(sql)
                        value = result(cursor)
                        evidence['columns'] = value['columns']
                        evidence['rows'] = len(value['rows'])
                        evidence['bag_digest'] = fingerprint(value, ordered=False)
                        evidence['values_digest'] = fingerprint({'columns': [], 'rows': value['rows']}, ordered=False)
                        evidence['ordered_digest'] = fingerprint(value, ordered=True)
            except Exception as error:
                evidence['execution_error'] = type(error).__name__
            data['queries'][digest(sql)] = evidence
    save(database+'-queries.json', data)
    print(database, len(sqls), 'audited', flush=True)
    return data

if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=3) as pool:
        databases = list(pool.map(audit_database, sorted(sql_by_db.items())))
    lookup = {(d['database'], q['sql']): q for d in databases for q in d['queries'].values()}
    reviewed = []
    for pair in pairs:
        left, right = [lookup[pair['database'], pair[side]] for side in ['left','right']]
        label = None
        reason = 'Requires semantic review'
        if left.get('tree_digest') and left.get('tree_digest') == right.get('tree_digest'):
            label, reason = True, 'Identical PostgreSQL syntax tree after removing source positions'
        elif left.get('columns') is not None and right.get('columns') is not None:
            if left['columns'] != right['columns']:
                label, reason = False, 'Different output column names, types or type modifiers'
            elif left['bag_digest'] != right['bag_digest']:
                label, reason = False, 'Different row multisets on a common starting snapshot'
        reviewed.append({**pair, 'equivalent': label, 'reason': reason,
                         'merged': {p: bool(left['decisions'][p]['key']) and left['decisions'][p]['key']==right['decisions'][p]['key'] for p in ['N0','N1','N2','N3']}})
    save('pair-evidence.json', reviewed)
    print('Labels', Counter(r['equivalent'] for r in reviewed), flush=True)
    print('Reasons', Counter(r['reason'] for r in reviewed), flush=True)
