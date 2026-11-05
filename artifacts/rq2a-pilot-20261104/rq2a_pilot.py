"""Run a paired CH invalidation pilot on fresh database clones."""
import hashlib
import json
import platform
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from psycopg import sql
from psycopg.conninfo import make_conninfo
from sysml import db
from sysml.common import DSN, append, records
from sysml.evaluate import calibrate
from sysml.policies import Config
from sysml.simulation import simulate
from sysml.workloads import generate


def save(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def main():
    root=Path(sys.argv[1])
    root.mkdir(parents=True,exist_ok=False)
    args=dict(n=240,seed=11,clients=8,write=.05,skew=1.4,repetition='high',locality='hot',pattern='stationary',mix='mixed',rate=25)
    summary=dict(start=datetime.now(timezone.utc).isoformat(),settings=args,arms={})
    reference=list(generate(**args,write_column='referenced'))
    control=list(generate(**args,write_column='unreferenced'))
    assert len(reference)==len(control)==240
    for a,b in zip(reference,control):
        assert a==b if a['kind']=='read' else {**a,'sql':a['sql'].replace('s_quantity','s_ytd')}==b
    with db.connect(DSN) as c:
        summary['postgres']=c.execute('show server_version').fetchone()[0]
        summary['warehouse_rows']=c.execute('select count(*) from warehouse').fetchone()[0]
        summary['stock_rows']=c.execute('select count(*) from stock').fetchone()[0]
        assert db.watermark(c)==0
    summary['python']=platform.python_version()
    summary['policy_comparison']='Sequential pre/post PG oracle; unlimited population of previously observed eligible reads. Replay uses N2, unlimited bytes, R0; wait and hits are modeled.'
    suffix=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    for arm,ops in [('quantity',reference),('ytd',control)]:
        folder=root/arm
        folder.mkdir()
        template_name='sysml_pilot_'+suffix+'_'+arm
        with db.connect(make_conninfo(DSN,dbname='postgres')) as c:
            c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE chbenchmark').format(sql.Identifier(template_name)))
        for op in ops:append(folder/'input.jsonl',op)
        print('Calibrating and checking snapshots:',arm,flush=True)
        calibrate(ops,folder/'calibrated.jsonl',dsn=make_conninfo(DSN,dbname=template_name),oracle=folder/'oracle.jsonl')
        measured=list(records(folder/'calibrated.jsonl'))
        schema=json.loads((folder/'calibrated.jsonl.catalog.json').read_bytes())
        oracle=list(records(folder/'oracle.jsonl'))
        writes=[r for r in measured if r['kind']=='write']
        assert len(writes)==12 and all(r['committed'] and r['rows_changed']==1 for r in writes)
        assert len(oracle)==len(writes)*5
        summary['arms'][arm]=dict(clone=template_name,input_sha256=hashlib.sha256((folder/'input.jsonl').read_bytes()).hexdigest(),
            workload=dict(Counter(r['template'] for r in ops)),policies={},writes_with_actual_changes=sum(bool(r['actual']) for r in oracle if r['policy']=='I0'))
        for i in range(5):
            policy=f'I{i}'
            rows=[r for r in oracle if r['policy']==policy]
            cfg=Config(invalid=policy,normal='N2',refresh='R0',capacity=None)
            replay=simulate(measured,schema,cfg)
            save(folder/(policy+'-replay.json'),replay)
            phases=replay['phases'].values()
            counts={k:sum(len(r[k]) for r in rows) for k in ['actual','selected','unnecessary','missed']}
            counts.update(reads=sum(r.get('reads',0) for r in phases),hits=sum(r.get('hits',0) for r in phases),
                stale_hits=sum(r.get('stale_hits',0) for r in phases),cached_query_write_pairs=sum(v['queries'] for r in rows for v in r['by_shape'].values()),
                median_write_decision_ms=median(r['write_decision_ms'] for r in rows),
                median_oracle_revision_check_ms=median(r['oracle_revision_check_ms'] for r in rows),
                candidates_gt_four=sum(len(r['selected'])>4 for r in rows))
            assert counts['reads']==228
            summary['arms'][arm]['policies'][policy]=counts
        save(root/'summary.json',summary)
        print(arm,{p:(v['actual'],v['selected'],v['missed'],v['stale_hits']) for p,v in summary['arms'][arm]['policies'].items()},flush=True)
    with db.connect(DSN) as c:assert db.watermark(c)==0
    summary['end']=datetime.now(timezone.utc).isoformat()
    save(root/'summary.json',summary)


if __name__=='__main__':
    main()
