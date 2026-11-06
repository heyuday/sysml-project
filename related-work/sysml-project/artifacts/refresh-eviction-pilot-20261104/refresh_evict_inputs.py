"""Calibrate small CH traces. Run on an already provisioned project warehouse."""
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sysml import db
from sysml.common import DSN, append, records
from sysml.evaluate import calibrate
from sysml.workloads import generate

def save(path,obj):
    path.write_text(json.dumps(obj,indent=2,allow_nan=False),encoding='utf8')

def main():
    root=Path(sys.argv[1])
    root.mkdir(parents=True,exist_ok=False)
    old=root.parent/'rq2a-pilot-20261104'/'quantity'
    manifest={}
    with db.connect(DSN) as c:
        assert db.watermark(c)==0 and c.execute('SELECT COUNT(*) FROM warehouse').fetchone()[0]==1
    # Pilot runs share a seed and eight clients. Freeze settings before comparing policies.
    for name,pattern,write,skew,mix in [
        ('stable','stationary',.05,1.4,'mixed'),('shift','shift',.05,1.4,'mixed'),
        ('hotwrites','stationary',.20,1.4,'mixed'),('burst','burst',.05,1.4,'mixed'),
        ('rare','stationary',.05,1.4,'disagreement'),('skew_ro','stationary',0,1.4,'mixed'),
        ('uniform_ro','stationary',0,0,'mixed'),('shift_ro','shift',0,1.4,'mixed'),
        ('rare_ro','stationary',0,1.4,'disagreement')]:
        folder=root/name
        folder.mkdir()
        settings=dict(n=240,seed=11,clients=8,write=write,skew=skew,repetition='high',
                      locality='hot',pattern=pattern,mix=mix,rate=25,write_column='referenced')
        ops=list(generate(**settings))
        if mix=='disagreement':
            # Give expensive aggregates a small, reusable tail; both reads depend on stock.
            # Cheap point reads use its compound PK. Aggregates scan a broad key range.
            for op in ops:
                if op['template']=='controlled-cheap-hot':
                    op['sql']='SELECT s_quantity FROM stock WHERE s_w_id=1 AND s_i_id=$1'
                if op['template']=='controlled-expensive-long-tail':
                    op['sql']='SELECT sum(s_quantity) FROM stock WHERE s_i_id >= $1'
                    op['params']=[1000*(op['params'][0]%10)+1]
        for op in ops:append(folder/'input.jsonl',op)
        if name=='stable':
            assert ops==list(records(old/'input.jsonl'))
            for f in ['calibrated.jsonl','calibrated.jsonl.catalog.json']:
                shutil.copy2(old/f,folder/f)
        else:
            target='sysml_repilot_'+name
            admin_dsn=make_conninfo(DSN,dbname='postgres')
            dsn=DSN
            if write:
                with db.connect(admin_dsn) as admin:
                    admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE chbenchmark').format(sql.Identifier(target)))
                dsn=make_conninfo(DSN,dbname=target)
            try:
                calibrate(ops,folder/'calibrated.jsonl',dsn=dsn,profile=bool(write))
            finally:
                if write:
                    with db.connect(admin_dsn) as admin:
                        admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))
        measured=list(records(folder/'calibrated.jsonl'))
        assert len(measured)==len(ops)
        writes=[r for r in measured if r['kind']=='write']
        assert all(r['committed'] and r['rows_changed']==1 for r in writes)
        manifest[name]={'settings':settings,'templates':dict(Counter(r['template'] for r in measured)),
                        'sha256':hashlib.sha256((folder/'calibrated.jsonl').read_bytes()).hexdigest()}
        save(root/'inputs.json',manifest)
        print('calibrated',name,len(ops),'ops',len(writes),'writes',flush=True)
    with db.connect(DSN) as c:assert db.watermark(c)==0

if __name__=='__main__':main()
