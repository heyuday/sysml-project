"""Persist semantic review decisions with concrete reasons and aggregate RQ1 evidence."""
from analyze_full_rq1 import *
import copy
import difflib

def schema_details():
    path = OUT/'schema-details.json'
    if path.exists():
        return read(path)
    details = {}
    for database in sorted(sql_by_db):
        with psycopg.connect(host=settings.pg_host,port=5432,user=settings.pg_user,
                             password=settings.pg_password,dbname=database+'_template') as c:
            c.execute('SET TRANSACTION READ ONLY')
            cols = c.execute("""SELECT cl.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
                FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace
                JOIN pg_attribute a ON a.attrelid=cl.oid WHERE n.nspname='public'
                AND cl.relkind='r' AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
            constraints = c.execute("""SELECT cl.relname,conname,pg_get_constraintdef(co.oid)
                FROM pg_constraint co JOIN pg_class cl ON co.conrelid=cl.oid
                JOIN pg_namespace n ON cl.relnamespace=n.oid WHERE n.nspname='public'""").fetchall()
            details[database] = dict(columns=cols,constraints=constraints)
    save('schema-details.json',details)
    return details

if __name__=='__main__':
    details=schema_details()
    reviewed=read(OUT/'pair-evidence.json')
    decisions={
        34:(False,'Join path changes from account.accindex = securitydetection.sectechref to a join through profile.profkey; these identifiers are not constrained equal.'),
        35:(False,'One query additionally requires coordscore > 0.8 and postfreq > 0, so it can exclude rows retained by the other.'),
        36:(False,'The filters use coordscore/postfreq versus a population percentile of netinflscore; they select different conditions.'),
        43:(False,'Two SELECT statements with additional output fields are compared with a single SELECT on a different join.'),
        117:(True,'Only bound relation aliases change (c/e to t1/t2); every reference, result column and sort expression is preserved.'),
        123:(False,'The left query guarantees ORDER BY vulnerability_score DESC, whereas the right query has no output ordering guarantee.'),
        126:(True,'jsonb_to_record defines the same named fields and types in a different order. Only clientref is projected, and two pure text equality predicates exchange order.'),
        128:(False,'chaninvdatablock has no JSON-object check constraint. For an array value, jsonb_to_record raises an error while ->> extraction can return NULL. Error behavior differs.'),
        130:(False,'The unrestricted jsonb column permits arrays. jsonb_to_record errors on an array while ->> extraction can return NULL; error behavior differs.'),
        147:(False,'The right query replaces the profile-based account join with moderationaction.modactkey = account.accindex, changing which accounts contribute.'),
        148:(True,'Both COUNT expressions count each inner-join row because both referenced keys are non-null on matched rows. DECIMAL and NUMERIC are PostgreSQL synonyms, with identical joins, grouping and ordering.'),
        180:(True,'For the inner joins, signals.signalregistry = signalclassification.signalref makes either side equivalent in the join to signaldynamics.signalref. All three keys have the same character(36) type.'),
        196:(False,'The selected averages change from pointdense and traitcount to traitcount and traitcount*coverpct*aream2, with extra joins.'),
        197:(True,'Within each non-null zoneref group, the original inner join repeats every scanfeatures row equally for each matching scanpointcloud row. Both averages are over integer columns, and splitting the traitcount average preserves its value and output metadata.'),
        198:(False,'The left averages traitcount and a product with coverpct/aream2. The right averages pointdense and traitcount.'),
        202:(False,'Only one query guarantees ordering by credit_score. Their output ordering contracts differ.'),
        209:(False,'EXPLAIN ANALYZE returns a plan and execution statistics, whereas SELECT returns JSON data.'),
        210:(False,'EXPLAIN ANALYZE returns a plan and execution statistics, whereas SELECT returns JSON data.'),
        214:(True,'Only relation aliases change from df/rm/dp to t1/t2/t3; expressions, output metadata, ordering and LIMIT are preserved.'),
        219:(True,'vendortrace is a non-null primary key, so COUNT(DISTINCT vendortrace) equals COUNT(*) in the denominator; the rest of the SQL is preserved.'),
        252:(False,'One query orders by riskanalysis.txchainlen and the other by transactions.payamtusd, which are distinct attributes.'),
        283:(True,'transportregistry and hubregistry are primary keys, so splitting the CASE into a CTE and rejoining on transportregistry preserves multiplicity. vehiclebreakrate is numeric(4,1), making /100 and /100.0 numeric division here.'),
        298:(False,'Different filters (impactval non-null versus a JSON risk value), joins, sorting and LIMIT change the result contract.'),
        299:(False,'impactval filtering and LIMIT 10 are replaced by account-status/action filters and a different sort.'),
        300:(False,'The join changes from account.accindex = moderationaction.macntref to modactkey, and filtering, extra joins and LIMIT are removed.'),
        301:(False,'The queries use different JSON paths and predicates, add a profile join, and have different sort/limit behavior.'),
        302:(False,'A JSON risk predicate and LIMIT are replaced by account-status and action predicates.'),
        303:(False,'The join key and filters differ, and the right query drops the JSON risk ordering and LIMIT.'),
        304:(False,'The right query changes the account/moderation join key and drops the JSON risk predicate and profile/security joins.'),
        305:(False,'The right query changes the account/moderation join key and drops active-status/action predicates and additional joins.'),
        326:(False,'Different JSON paths, thresholds, manipulation filters and join paths select different trader populations.'),
        345:(True,'JSON ->> returns text already. Removing ::text from those operands preserves every CASE branch and the aggregate.'),
        363:(False,'The tie-break sort changes from ab_test_group to percentage_in_month DESC.'),
        439:(False,'The sort changes from lexical text order to numeric order. For example, text 9 sorts above 10 but integer 10 sorts above 9.'),
    }
    for index,(equivalent,reason) in decisions.items():
        assert reviewed[index]['equivalent'] is None
        reviewed[index].update(equivalent=equivalent,reason=reason,review_method='SQL inspection with schema constraints')
    assert all(r['equivalent'] is not None for r in reviewed)
    by_sql={(d['database'],q['sql']):q for f in OUT.glob('*-queries.json')
            for d in [read(f)] for q in d['queries'].values()}

    def single_read(sql):
        parsed=json.loads(parser.parse_sql_json(sql))['stmts']
        kinds={k for k,_ in identity.nodes(parsed)}
        return len(parsed)==1 and 'SelectStmt' in parsed[0]['stmt'] and not kinds & {
            'InsertStmt','UpdateStmt','DeleteStmt','MergeStmt','IntoClause','CreateStmt','DropStmt'}

    excluded=[]
    groups=defaultdict(list)
    valid_calls=[]
    totals=defaultdict(Counter)
    for attempt in attempts:
        sim=attempt.get('simulator','qwen3.5-4b')
        task=attempt['result']['task_id']
        inventory=totals[sim]
        inventory.update(attempts=1,phase1=int(attempt['result']['phase1_passed']),phase2=int(attempt['result']['phase2_passed']))
        retained=[]
        potentially_changed=False
        for event in attempt['events']:
            if event['kind']!='read': continue
            inventory['endpoint_calls']+=1
            try:
                parsed=json.loads(parser.parse_sql_json(event['sql']))
                may_write=bool({k for k,_ in identity.nodes(parsed)} & {'InsertStmt','UpdateStmt','DeleteStmt','MergeStmt'})
            except Exception:
                may_write=False
            if may_write and event['sql'].lstrip().upper().startswith(('WITH','SELECT','EXPLAIN')):
                potentially_changed=True
            if not event.get('successful'):
                inventory['failed_calls']+=1
                continue
            inventory['successful_calls']+=1
            if not event['scope'].endswith('/0'):continue
            inventory['successful_before_submit']+=1
            if not single_read(event['sql']):
                excluded.append(dict(task=task,wave=attempt['wave'],simulator=sim,sql=event['sql']))
                continue
            assert not potentially_changed, 'A read after a possible write requires snapshot review'
            retained.append(event)
            valid_calls.append((task,sim,event['database'],event['sql']))
        inventory['single_select_calls']+=len(retained)
        inventory['attempts_with_read']+=int(bool(retained))
        groups[task,sim].append(dict(wave=attempt['wave'],reads=retained))
    pair_labels={r['id']:r['equivalent'] for r in reviewed}
    def pair_label(task,left,right):
        return pair_labels[digest([task,*sorted([left,right])])]
    for row in reviewed:
        row['included']=single_read(row['left']) and single_read(row['right'])
    save('reviewed-pairs.json',reviewed)
    save('excluded-calls.json',excluded)
    summary={'cohorts':{},'pairs':{},'methods':{},'exclusions':len(excluded)}
    for sim in sorted(totals):
        count=totals[sim]
        reasons=Counter()
        distinct=set()
        for (task,group_sim),runs in groups.items():
            if group_sim!=sim:continue
            previous=set()
            count['task_groups']+=1
            count['groups_with_comparable_reads']+=int(sum(bool(r['reads']) for r in runs)>=2)
            for run in sorted(runs,key=lambda r:r['wave']):
                for event in run['reads']:
                    sql=event['sql']; distinct.add((task,sql))
                    decision=by_sql[event['database'],sql]['decisions']['N0']
                    reasons[decision['reason'] or 'accepted']+=1
                    if previous:
                        count['later_comparable_reads']+=1
                        if sql in previous: count['exact_reuses']+=1
                        elif any(pair_label(task,sql,prior) for prior in previous):count['equivalent_reuses']+=1
                        else:count['different_reads']+=1
                previous.update(e['sql'] for e in run['reads'])
        count['distinct_texts']=len(distinct)
        summary['cohorts'][sim]={'counts':dict(count),'rejection_reasons':dict(reasons)}
    included=[r for r in reviewed if r['included']]
    summary['pairs']={'original':len(reviewed),'included':len(included),'labels':dict(Counter(str(r['equivalent']) for r in included)),
                      'by_cohort':{sim:dict(Counter(str(r['equivalent']) for r in included if r['simulator']==sim)) for sim in totals},
                      'evidence':dict(Counter(r.get('review_method',r['reason']) for r in included))}
    parent={}
    def representative(key):
        parent.setdefault(key,key)
        if parent[key]!=key:
            parent[key]=representative(parent[key])
        return parent[key]
    for task,sim,database,sql in valid_calls:
        representative((task,sim,sql))
    for row in included:
        if row['equivalent']:
            left,right=[(row['task'],row['simulator'],row[side]) for side in ['left','right']]
            parent[representative(left)]=representative(right)
            a,b=[by_sql[row['database'],row[side]] for side in ['left','right']]
            assert a.get('bag_digest')==b.get('bag_digest') and a.get('bag_digest') is not None
    for row in included:
        if not row['equivalent']:
            assert representative((row['task'],row['simulator'],row['left']))!=representative((row['task'],row['simulator'],row['right']))
    summary['reviewed_key_classes']=dict(raw=len(parent),equivalent_classes=len({representative(k) for k in parent}))
    for policy in ['N0','N1','N2','N3']:
        keys=set();accepts=0;reuses=0
        for (task,sim),runs in groups.items():
            previous=set()
            for run in sorted(runs,key=lambda r:r['wave']):
                this=set()
                for event in run['reads']:
                    key=by_sql[event['database'],event['sql']]['decisions'][policy]['key']
                    if key:
                        keys.add((task,sim,key));accepts+=1;reuses+=key in previous;this.add(key)
                previous.update(this)
        summary['methods'][policy]=dict(accepted=accepts,keys=len(keys),chronological_reuses=reuses,
            true_merges=sum(r['merged'][policy] and r['equivalent'] for r in included),
            false_merges=sum(r['merged'][policy] and not r['equivalent'] for r in included),
            missed_merges=sum(not r['merged'][policy] and r['equivalent'] for r in included))
    save('summary.json',summary)
    print(json.dumps(summary,indent=2))
