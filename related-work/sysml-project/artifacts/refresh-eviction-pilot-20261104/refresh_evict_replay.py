import csv
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from sysml.common import records,quantiles
from sysml.policies import Config
from sysml.simulation import simulate


def save(p,obj):
    p.write_text(json.dumps(obj,indent=2,allow_nan=False),encoding='utf8')


def replay(root):
    rows=[]
    base=Config(invalid='I1',tau=2,horizon=3)
    evidence=open(root/'decisions.jsonl','x',encoding='utf8')
    for name in ['stable','shift','hotwrites','burst','rare','skew_ro','uniform_ro','shift_ro','rare_ro']:
        folder=root/name
        ops=list(records(folder/'calibrated.jsonl'))
        cat=json.loads((folder/'calibrated.jsonl.catalog.json').read_bytes())
        def run(track,policy,cfg,budget=None,frac=None):
            reads,writes=[],[]
            r=simulate(ops,cat,cfg,details=reads.append,write_details=writes.append)
            s=r['policy']
            def total(key):
                return sum(x.get(key,0) for x in r['phases'].values())
            def window(lo,hi):
                arr=[v for v in reads if lo<=int(v['id'])<hi]
                return sum(v['hit'] for v in arr)/len(arr) if arr else None
            # Foreground modeled DB/fill wait isolated from Python normalization timings.
            waits=[max(0,x['modeled_wait_ms']-x['normal_ms']) for x in reads]
            row=dict(track=track,scenario=name,policy=policy,budget=budget,capacity_fraction=frac,
                capacity_bytes=cfg.capacity,ablation=cfg.ablate,admission=cfg.admission,
                reads=len(reads),hits=total('hits'),coalesced=total('coalesced'),hit_rate=total('hits')/len(reads),
                wait_ms=sum(waits),total_modeled_wait_ms=sum(x['modeled_wait_ms'] for x in reads),
                wait_p95_ms=quantiles(waits)['p95'],db_ms=total('demand_db_ms')+total('refresh_db_ms'),
                avoided_db_ms=total('avoided_db_ms'),refresh_db_ms=total('refresh_db_ms'),
                refreshes=s.get('refresh_runs',0),used=s.get('refresh_used',0),wasted=s.get('refresh_wasted',0)+s.get('refresh_discarded',0),
                used_before_end_unknown=r['pending_unused_refreshes'],wasted_refresh_db_ms=s.get('refresh_wasted_db_ms',0),
                writes=len(writes),choices_gt_4=sum(len(w['invalidated'])>4 for w in writes),evictions=s.get('evictions',0),
                peak_bytes=r['peak_value_bytes'],metadata_bytes=r['metadata_bytes_estimate'],
                pre_shift_hit_rate=window(90,120) if 'shift' in name else None,
                post_shift_hit_rate=window(120,150) if 'shift' in name else None,
                later_hit_rate=window(210,240) if 'shift' in name else None,stale_hits=total('stale_hits'))
            assert not row['stale_hits'] and (cfg.capacity is None or r['peak_value_bytes']<=cfg.capacity)
            assert not track=='RQ2' or row['refreshes']==row['used']+row['wasted']+row['used_before_end_unknown'],row
            rows.append(row)
            print(json.dumps({'track':track,'scenario':name,'policy':policy,'result':r,'writes':writes,
                              'fraction':frac,'budget':budget}),file=evidence)
            return row
        if not name.endswith('_ro'):
            run('RQ2','R0',base)
            run('RQ2','R0',replace(base,admission='reconsider'))
            for i in range(1,8):
                pol='R'+str(i)
                for b in [4,None]:
                    run('RQ2',pol,replace(base,refresh=pol,refresh_budget=b),budget=b)
            if name in {'stable','shift'}:
                for f in base.weights:
                    run('RQ2','R7',replace(base,refresh='R7',ablate=f),budget=4)
        if name.endswith('_ro') or name=='stable':
            peak=simulate(ops,cat,base)['peak_value_bytes']
            for frac in [None,.5,.25,.10,.05,.01]:
                capacity=None if frac is None else int(frac*peak)
                for i in range(9):
                    pol='E'+str(i)
                    run('RQ3',pol,replace(base,evict=pol,capacity=capacity),frac=frac)
                if frac==.25 and name in {'stable','shift_ro'}:
                    for f in base.weights:
                        run('RQ3','E8',replace(base,evict='E8',capacity=capacity,ablate=f),frac=frac)
        evidence.flush()
        print('replays complete:',name,'cumulative',len(rows),flush=True)
    evidence.close()
    save(root/'pilot-summary.json',rows)
    with (root/'pilot-summary.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


if __name__=='__main__':replay(Path(sys.argv[1]))
