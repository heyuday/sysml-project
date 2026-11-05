"""Checkpointed RQ2/RQ3 live batch. Run from the project venv; no work on import."""
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sysml import db
from sysml.common import DSN, append, quantiles, records
from sysml.evaluate import calibrate, live_replay
from sysml.policies import Config
from sysml.simulation import simulate
from sysml.workloads import generate

PROJECT=Path(__file__).resolve().parents[1]
ROOT=PROJECT/'artifacts'/'low-concurrency-20261104'
DESIGN=json.loads((PROJECT/'configs'/'design.json').read_bytes())
SEEDS=DESIGN['seeds']
LEVELS=[1,8]
CELLS={
    'stable':['I1','I0','I2','I3','I4','R1','R2','R7','direct'],
    'shift_ro':['E1','E2','E3','E6','E8','direct'],
    'burst':['R0','R2','R7'],
}

def now():
    return datetime.now(timezone.utc).isoformat()

def save(path,data):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(data,indent=2,allow_nan=False),encoding='utf8')
    os.replace(temp,path)

def plan(levels=LEVELS):
    jobs=[]
    rng=random.Random(1104)
    for seed in SEEDS:
        pairs=[(s,p) for s,ps in CELLS.items() for p in ps]
        rng.shuffle(pairs)
        if seed==SEEDS[0]:
            pairs.remove(('stable','I1'))
            pairs.insert(0,('stable','I1'))
        for ix,(s,p) in enumerate(pairs):
            for clients in levels if (ix+SEEDS.index(seed))%2==0 else reversed(levels):
                jobs.append(dict(seed=seed,scenario=s,policy=p,clients=clients,
                                 id=f'{seed}-{s}-{p}-c{clients}'))
    return jobs

@contextmanager
def clone():
    target='sysml_low_'+uuid.uuid4().hex[:16]
    admin=make_conninfo(DSN,dbname='postgres')
    with db.connect(admin) as c:
        c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE chbenchmark').format(sql.Identifier(target)))
    try:
        yield make_conninfo(DSN,dbname=target)
    finally:
        with db.connect(admin) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))

def materialize(job):
    folder=ROOT/'inputs'/f"{job['seed']}-{job['scenario']}"
    if (folder/'ready.json').exists():return folder
    folder.parent.mkdir(parents=True,exist_ok=True)
    stage=folder.with_name(folder.name+'-prep-'+uuid.uuid4().hex[:8])
    stage.mkdir()
    s=job['scenario']
    # Generate once at eight lanes, including the burst pattern. Only lane assignment
    # changes during replay, so one and eight clients have identical offered arrivals.
    ops=list(generate(n=2000,seed=job['seed'],clients=8,write=0 if s=='shift_ro' else .05,
                      skew=1.4,repetition='high',locality='hot',
                      pattern={'stable':'stationary','shift_ro':'shift','burst':'burst'}[s],
                      mix='mixed',rate=25,write_column='referenced'))
    for o in ops: append(stage/'input.jsonl',o)
    with clone() as dsn:calibrate(ops,stage/'calibrated.jsonl',dsn=dsn)
    measured=list(records(stage/'calibrated.jsonl'))
    assert len(measured)==2000
    assert all(x['committed'] and x['rows_changed']==1 for x in measured if x['kind']=='write')
    schema=json.loads((stage/'calibrated.jsonl.catalog.json').read_bytes())
    capacity=None
    if s=='shift_ro':
        # Capacity chosen only using the initial 40%; identical for every policy/client level.
        peak=simulate(measured[:800],schema,Config())['peak_value_bytes']
        capacity=int(peak*.25)
        assert capacity>0
    save(stage/'ready.json',dict(capacity=capacity,requests=len(ops),
            input_hash=hashlib.sha256((stage/'input.jsonl').read_bytes()).hexdigest()))
    stage.rename(folder)
    return folder

def config(job,inputs):
    p=job['policy']
    cfg=dict(normal='N2',invalid='I1',refresh='R0',evict='E1',tau=2,horizon=3)
    if p=='direct':cfg['direct']=True
    elif p.startswith('I'):cfg['invalid']=p
    elif p.startswith('R'):cfg.update(refresh=p,refresh_budget=None if p=='R1' else 4)
    elif p.startswith('E'):cfg.update(evict=p,capacity=inputs['capacity'])
    return cfg

def serve(directory,port):
    import uvicorn
    from sysml.live import app
    server=uvicorn.Server(uvicorn.Config(app(Config(**json.loads((directory/'config.json').read_bytes())),
                            str(directory/'broker.jsonl')),host='127.0.0.1',port=port,log_level='warning'))
    def stop():
        sys.stdin.buffer.read()
        server.should_exit=True
    threading.Thread(target=stop,daemon=True).start()
    server.run()

def trial(job,folder,d):
    inputs=json.loads((folder/'ready.json').read_bytes())
    cfg=config(job,inputs)
    save(d/'config.json',cfg)
    ops=[{**r,'client':int(r['id'])%job['clients']} for r in records(folder/'input.jsonl')]
    with clone() as dsn, (d/'server.log').open('w') as log:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        child=subprocess.Popen([sys.executable,'-B',__file__,'serve',str(d),str(port)],
                env=dict(os.environ,SYSML_DSN=dsn),stdin=subprocess.PIPE,stdout=log,stderr=log)
        try:
            with httpx.Client(base_url=url,trust_env=False,timeout=3) as client:
                for _ in range(160):
                    if child.poll() is not None:raise RuntimeError('broker exited; see '+str(log.name))
                    try:
                        if client.get('/stats').status_code==200:break
                    except httpx.ConnectError:pass
                    time.sleep(.15)
                else:raise TimeoutError('broker failed to start')
            live_replay(ops,url,d/'requests.jsonl',job['clients'])
        finally:
            child.stdin.close()
            try:child.wait(timeout=30)
            except subprocess.TimeoutExpired:child.kill();child.wait()
    rows=list(records(d/'requests.jsonl'))
    stats=json.loads((d/'requests.jsonl.stats.json').read_bytes())
    if len(rows)!=2000 or {x['id'] for x in rows}!={x['id'] for x in ops}:raise ValueError('incomplete trace')
    if stats['cdc_error'] or stats.get('refresh_errors'):raise ValueError('CDC/refresh error')
    if cfg.get('capacity') is not None and stats['peak_value_bytes']>cfg['capacity']:raise ValueError('capacity exceeded')
    ref=list(records(folder/'calibrated.jsonl'))
    rw=[r for r in ref if r['kind']=='write']
    lw=sorted((r for r in rows if r['type']=='write'),key=lambda x:x['version'])
    if {w['id'] for w in rw}!={w['id'] for w in lw} or not all(w['committed'] and w['rows_changed']==1 for w in lw):
        raise ValueError('writes did not commit as expected')
    observed=set();expected=set();comparable={0}
    for a,b in zip(lw,rw):
        observed.add(a['id']);expected.add(b['id'])
        if observed==expected and a['version']==b['version']:comparable.add(a['version'])
    refmap={r['id']:r for r in ref if r['kind']=='read'}
    reads=[r for r in rows if r['type']=='read']
    checked=0
    for r in reads:
        baseline=refmap[r['id']]
        if r['version'] in comparable and baseline['version']==r['version']:
            if r['digest']!=baseline['digest']:raise ValueError('digest mismatch at '+r['id'])
            checked+=1
    slices={}
    for label,low,high in [('all',0,2000),('initial',0,800),('heldout',800,2000),
                           ('before_shift',800,1000),('after_shift',1000,1200)]:
        part=[r for r in reads if low<=int(r['id'])<high]
        slices[label]=dict(reads=len(part),hits=sum(r['hit'] for r in part),
                latency_ms=quantiles([r['client_wait_ms'] for r in part]),
                scheduled_latency_ms=quantiles([r['schedule_to_completion_ms'] for r in part]),
                cumulative_wait_ms=sum(r['client_wait_ms'] for r in part),demand_db_ms=sum(r['db_ms'] for r in part))
    return dict(job=job,config=cfg,completed=now(),trace_hash=inputs['input_hash'],matched_digests=checked,
                unmatched_versions=len(reads)-checked,metrics=slices,broker=stats,requests=2000)

def run(all_clients=False):
    # Exclusive OS file lock is released on process exit, including after interruption.
    import msvcrt
    ROOT.mkdir(parents=True,exist_ok=True)
    lock=open(ROOT/'campaign.lock','a+b')
    lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    low_jobs=plan()
    low_manifest={'seeds':SEEDS,'clients':LEVELS,'cells':CELLS,'requests_per_trial':2000,
              'offered_rps':25,'stages':low_jobs,'settings':'N2; I1/R0 unless varied; refresh budget 4 except R1-all; tau=2s horizon=3s; eviction 25% of training unlimited peak',
              'reporting':'first 800 and held-out 1200 separately, continuous cache state; shift at request 1000'}
    mf=ROOT/'manifest.json'
    previous=json.loads(mf.read_bytes()) if mf.exists() else None
    manifest=low_manifest
    if all_clients or (previous and previous['clients']!=LEVELS):
        extra_levels=[n for n in DESIGN['clients'] if n not in LEVELS]
        manifest={**low_manifest,'clients':LEVELS+extra_levels,'stages':low_jobs+plan(extra_levels)}
    if previous is not None and previous!=low_manifest and previous!=manifest:
        raise ValueError('manifest changed')
    jobs=manifest['stages']
    save(mf,manifest)
    done=sum((ROOT/'trials'/j['id']/'result.json').exists() for j in jobs)
    def status(state,job=None,**kw):
        data=dict(state=state,updated=now(),worker_pid=os.getpid(),completed=done,total=len(jobs),current=job,**kw)
        save(ROOT/'status.json',data)
        mirror=os.environ.get('SYSML_STATUS_MIRROR')
        if mirror:
            try:save(Path(mirror),data)
            except OSError:pass
    current=None
    try:
        with db.connect(DSN) as c:
            if db.watermark(c)!=0:raise ValueError('template modified')
        for current in jobs:
            dest=ROOT/'trials'/current['id']
            if (dest/'result.json').exists():continue
            status('preparing',current)
            folder=materialize(current)
            dest.mkdir(parents=True,exist_ok=True)
            attempts=1+len(list(dest.glob('attempt-*')))
            for repeat in range(2):
                attempt=dest/f'attempt-{attempts+repeat}'
                attempt.mkdir()
                status('running',current,attempt=str(attempt),requests_log=str(attempt/'requests.jsonl'))
                try:
                    result=trial(current,folder,attempt)
                    break
                except (httpx.TransportError,TimeoutError):
                    (attempt/'error.txt').write_text(traceback.format_exc())
                    if repeat:raise
            save(dest/'result.json',dict(result,attempt=attempt.name))
            done+=1
            status('checkpointed',current)
            print(now(),done,'/',len(jobs),current['id'],flush=True)
        status('complete')
    except BaseException:
        status('failed',current,error=traceback.format_exc())
        raise
    finally:
        if os.environ.get('SYSML_STOP_STACK')=='1':
            subprocess.run(['docker','compose','stop','postgres','redis'],cwd=PROJECT,check=False,timeout=90)
        lock.close()

if __name__=='__main__':
    if sys.argv[1:] and sys.argv[1]=='serve':serve(Path(sys.argv[2]),int(sys.argv[3]))
    else:run(all_clients='--all-clients' in sys.argv[1:])
