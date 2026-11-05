"""Score controlled labels and held-out BIRD attempts; latency is not an endpoint."""
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import combinations
import json
import os
from pathlib import Path
import time

import httpx
import psycopg
from sysml import db, identity
from sysml.common import digest
from sysml.evaluate import normalization
from rq1_repeats import ROOT, UPSTREAM, save

POLICIES=("N0","N1","N2","N3")


def ratio(a,b):
    return a/b if b else None


def scores(votes):
    counts=Counter((predicted,actual) for predicted,actual in votes if actual is not None)
    tp,fp,fn,tn=(counts[k] for k in [(True,True),(True,False),(False,True),(False,False)])
    return dict(true_merges=tp,false_merges=fp,missed_merges=fn,true_separations=tn,
                precision=ratio(tp,tp+fp),recall=ratio(tp,tp+fn),accuracy=ratio(tp+tn,tp+fp+fn+tn))


def cached_proposals():
    original=identity.propose
    def proposal(sql):
        path=ROOT/"n3-proposals"/(digest([os.environ["SYSML_N3_MODEL"],sql])+".json")
        if not path.exists():
            for retry in range(6):
                try:
                    candidate=original(sql)
                    break
                except httpx.HTTPError as exc:
                    response=getattr(exc,"response",None)
                    code=response.status_code if response is not None else None
                    if retry==5 or code in {400,401,403,404}:
                        raise RuntimeError(f"N3 request failed (HTTP {code}); scoring can resume") from exc
                    time.sleep(2**retry)
            save(path,{"proposal":candidate})
        return json.loads(path.read_bytes())["proposal"]
    identity.propose=proposal


def analyze_task(attempts,catalog,labels):
    first=attempts[0]["result"]
    task=first["task_id"]
    context={"database":first["database"],"scope":task+"/initial"}
    @lru_cache(None)
    def key(sql,policy):
        return identity.analyze(sql,[],context,policy,catalog).key
    waves={}
    within=[]
    inventory=[]
    for attempt in attempts:
        wave=attempt["wave"]
        events=attempt["events"]
        reads=[e for e in events if e["kind"]=="read" and e.get("successful")]
        eligible_initial=[e["sql"] for e in reads if e["scope"].rsplit("/",1)[-1]=="0"]
        waves[wave]=eligible_initial
        for policy in POLICIES:
            seen=set()
            hits=eligible=0
            for e in reads:
                value=key(e["sql"],policy)
                if value:
                    scoped=(e["scope"],value)
                    hits+=scoped in seen
                    seen.add(scoped)
                    eligible+=1
            within.append(dict(wave=wave,policy=policy,successful_reads=len(reads),eligible=eligible,hits=hits))
        inventory.append(dict(wave=wave,task_error=attempt["result"].get("error"),
                              reward=attempt["result"].get("total_reward"),calls=len(events),
                              successful_reads=len(reads),initial_state_reads=len(eligible_initial),
                              failed_reads=sum(e["kind"]=="read" and not e.get("successful") for e in events)))

    def pair_id(a,b):
        return digest([task,*sorted([a,b])])
    pairs=[]
    sqls=sorted({s for wave in waves.values() for s in wave})
    # Enumerate pairs independently of every tested method. Unresolved labels stay null.
    origins={s:{w for w,reads in waves.items() if s in reads} for s in sqls}
    for left,right in combinations(sqls,2):
        if len(origins[left]|origins[right])<2:
            continue
        pid=pair_id(left,right)
        pairs.append(dict(id=pid,task=task,database=first["database"],left=left,right=right,
                          equivalent=labels.get(pid)))
    accuracy={p:scores([(bool(key(row["left"],p)) and key(row["left"],p)==key(row["right"],p),
                        row["equivalent"]) for row in pairs]) for p in POLICIES}
    folds=[]
    overlap=[]
    for (i,a),(j,b) in combinations(sorted(waves.items()),2):
        left,right=set(a),set(b)
        overlap.append(dict(waves=[i,j],shared_exact_queries=len(left & right),
                            distinct_exact_queries=len(left | right),
                            jaccard=ratio(len(left & right),len(left | right))))
    for w,reads in sorted(waves.items()):
        prior={s for other,group in waves.items() if other!=w for s in group}
        for policy in POLICIES:
            prior_keys={key(s,policy) for s in prior}-{None}
            hits=extra=safe=incorrect=unreviewed=0
            for sql in reads:
                k=key(sql,policy)
                if k not in prior_keys:
                    continue
                hits+=1
                if sql in prior:
                    continue
                extra+=1
                judgments=[labels.get(pair_id(sql,s)) for s in prior if key(s,policy)==k]
                if False in judgments:
                    incorrect+=1
                elif all(j is True for j in judgments):
                    safe+=1
                else:
                    unreviewed+=1
            folds.append(dict(held_out_wave=w,policy=policy,reads=len(reads),
                              exact_text_seen=sum(s in prior for s in reads),
                              eligible=sum(key(s,policy) is not None for s in reads),
                              candidate_hits=hits,additional_candidates=extra,
                              reviewed_safe_extra=safe,unsafe_extra=incorrect,unresolved_extra=unreviewed))
    return (dict(task=task,database=first["database"],attempts=inventory,within_attempt=within,
                 distinct_initial_sql=len(sqls),exact_query_overlap=overlap,
                 cross_attempt=folds,labeled_pair_accuracy=accuracy),pairs)


def main():
    cached_proposals()
    catalog=json.loads((UPSTREAM/"inputs/11-stable/calibrated.jsonl.catalog.json").read_bytes())
    controlled=[]
    for report in normalization([],catalog,POLICIES,controlled=True):
        pairs=report["controlled_pairs"]
        controlled.append(dict(policy=report["policy"],scores=scores([(p["merged"],p["expected"]) for p in pairs]),
                               pairs=pairs))
    save(ROOT/"controlled-accuracy.json",controlled)
    labels_file=ROOT/"reviewed-labels.json"
    labels=json.loads(labels_file.read_bytes()) if labels_file.exists() else {}
    if not isinstance(labels,dict) or any(type(v) is not bool for v in labels.values()):
        raise ValueError("Reviewed labels must map pair ID to JSON true or false")
    history_file=ROOT/"simulator-transition.json"
    history=json.loads(history_file.read_bytes()) if history_file.exists() else {}
    legacy=set(history.get("previous_attempts", []))
    groups=defaultdict(list)
    for file in sorted((ROOT/"attempts").glob("*/*.json")):
        row=json.loads(file.read_bytes())
        simulator=row.get("simulator","qwen3.5-4b")
        if history:
            expected=history["previous"] if file.relative_to(ROOT).as_posix() in legacy else history["new"]
            if expected!=simulator:
                raise ValueError(f"Simulator provenance mismatch for {file.name}")
        groups[row["result"]["task_id"],simulator].append(row)
    output=[]
    review=[]
    for (task,simulator),attempts in groups.items():
        database=attempts[0]["result"]["database"]
        with psycopg.connect(dbname=database+"_template") as connection:
            cat=db.catalog(connection,tracked=False)
        report, pairs=analyze_task(attempts,cat,labels)
        report["simulator"]=simulator
        for pair in pairs:
            pair["simulator"]=simulator
        output.append(report)
        review.extend(pairs)
    save(ROOT/"pair-review.json",review)
    summary={}
    for policy in POLICIES:
        folds=[f for t in output for f in t["cross_attempt"] if f["policy"]==policy]
        counts={k:sum(f[k] for f in folds) for k in (
                "reads","eligible","exact_text_seen","candidate_hits","additional_candidates",
                "reviewed_safe_extra","unsafe_extra","unresolved_extra")}
        summary[policy]={**counts,"candidate_hit_fraction":ratio(counts["candidate_hits"],counts["reads"]),
                         "extra_candidate_fraction":ratio(counts["additional_candidates"],counts["reads"])}
    save(ROOT/"query-variation.json",dict(tasks=output,attempts=sum(len(a) for a in groups.values()),
         pooled_held_out_reads=summary,real_pairs=len(review),reviewed=sum(r["equivalent"] is not None for r in review),
         cross_attempt_scope="Successful reads before the first submission on identical starting clones, compared within simulator model."))


if __name__=="__main__":
    main()
