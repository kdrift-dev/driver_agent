#!/usr/bin/env python3
from __future__ import annotations
import os, re, json, subprocess, time, csv, shutil
from pathlib import Path
ROOT=Path('/mnt/disk7/jw_ai4os/driver-agent/workflow')
EXT=ROOT/'external_repos'
OLD=ROOT/'runs/oot_p0_screen_v61_to_v70_20260526_232117/work/linux-v6.1'
NEW=ROOT/'runs/oot_p0_screen_v61_to_v70_20260526_232117/work/linux-v7.0'
OUT=ROOT/'runs/oot_small_p0_scan_20260601_233000'
OUT.mkdir(parents=True, exist_ok=True)
SRC_SUFFIX={'.c','.h'}
MAKE_RE=re.compile(r'^\s*obj-m\s*[:+]?=\s*(.+)$')
BLACKLIST_DIR_PARTS={'.git','.github','docs','doc','tests','test','examples','utils','udev','img','extern','ci','RTOS','Microcontrollers','Unit_Testing'}

def git_url(repo:Path)->str:
    p=subprocess.run(['git','-C',str(repo),'config','--get','remote.origin.url'],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    return p.stdout.strip()

def head(repo:Path)->str:
    p=subprocess.run(['git','-C',str(repo),'rev-parse','HEAD'],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    return p.stdout.strip()

def count_loc(files):
    n=0
    for f in files:
        try:
            n+=sum(1 for line in f.read_text(errors='ignore').splitlines() if line.strip())
        except Exception: pass
    return n

def parse_modules(makefile:Path):
    mods=[]
    try: lines=makefile.read_text(errors='ignore').splitlines()
    except Exception: return []
    for line in lines:
        if line.strip().startswith('#'): continue
        m=MAKE_RE.match(line)
        if not m: continue
        rhs=m.group(1).split('#',1)[0]
        for tok in re.split(r'\s+',rhs.strip()):
            tok=tok.strip()
            if tok.endswith('.o') and '/' not in tok:
                mods.append(tok[:-2])
    return list(dict.fromkeys(mods))

def run(cmd,cwd=None,timeout=90):
    st=time.time()
    p=subprocess.run(cmd,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    return {'returncode':p.returncode,'ok':p.returncode==0,'stdout':p.stdout[-2000:],'stderr':p.stderr[-5000:],'duration_sec':round(time.time()-st,3)}

def build(kern, d, extra=None):
    cmd=['make','-j4','-C',str(kern),f'M={d}','modules']
    if extra: cmd += extra
    try: return run(cmd,timeout=120)
    except subprocess.TimeoutExpired:
        return {'returncode':124,'ok':False,'stdout':'','stderr':'timeout','duration_sec':120}

def classify_fail(stderr):
    s=stderr
    labels=[]
    if 'too many arguments to function' in s and 'class_create' in s: labels.append('class_create_signature')
    if 'proc_create' in s and ('incompatible pointer type' in s or 'struct file_operations' in s): labels.append('proc_ops_signature')
    if 'setup_timer' in s or 'implicit declaration of function' in s and 'setup_timer' in s: labels.append('timer_setup_migration')
    if 'implicit declaration of function' in s: labels.append('missing_function')
    if 'no member named' in s or 'has no member named' in s: labels.append('struct_field_change')
    if 'incompatible-pointer-types' in s or 'incompatible pointer type' in s: labels.append('signature_mismatch')
    return ';'.join(dict.fromkeys(labels)) or 'unknown_compile_error'

cands=[]
for repo in sorted(EXT.iterdir()):
    if not repo.is_dir() or not (repo/'.git').exists(): continue
    for mf in repo.rglob('Makefile'):
        rel=mf.parent.relative_to(repo)
        if any(part in BLACKLIST_DIR_PARTS for part in rel.parts): continue
        mods=parse_modules(mf)
        if not mods: continue
        files=[p for p in mf.parent.iterdir() if p.is_file() and p.suffix in SRC_SUFFIX]
        cfiles=[p for p in files if p.suffix=='.c']
        if not cfiles or len(files)>8: continue
        loc=count_loc(files)
        if loc<20 or loc>2500: continue
        cands.append((repo,mf.parent,mods,files,loc))

print('candidates',len(cands))
rows=[]
for i,(repo,d,mods,files,loc) in enumerate(cands,1):
    old=build(OLD,d)
    new=build(NEW,d)
    p0=bool(old['ok'] and not new['ok'])
    row={
        'idx':i,
        'repo_dir':repo.name,
        'repo_url':git_url(repo),
        'repo_ref':head(repo),
        'source_subdir':d.relative_to(repo).as_posix() if d!=repo else '.',
        'module_candidates':';'.join(mods),
        'default_module_ko':mods[0]+'.ko',
        'default_module_name':mods[0].replace('-','_'),
        'source_file_count':len(files),
        'code_loc':loc,
        'old_ref':'v6.1',
        'new_ref':'v7.0',
        'old_build_ok':old['ok'],
        'new_build_ok':new['ok'],
        'p0_hard_break':p0,
        'failure_kind': classify_fail(new['stderr']) if not new['ok'] else '',
        'new_stderr_excerpt':new['stderr'][-1000:].replace('\n','\\n'),
        'old_stderr_excerpt':old['stderr'][-500:].replace('\n','\\n'),
    }
    rows.append(row)
    if p0:
        detail=OUT/'details'/f"{repo.name}__{row['source_subdir'].replace('/','__').replace('.','root')}.json"
        detail.parent.mkdir(exist_ok=True)
        detail.write_text(json.dumps({'row':row,'old':old,'new':new},indent=2),encoding='utf-8')
        row['evidence_artifact']=detail.relative_to(ROOT).as_posix()
        print('P0', row['repo_dir'], row['source_subdir'], row['failure_kind'], 'loc', loc, flush=True)

fields=['idx','repo_dir','repo_url','repo_ref','source_subdir','module_candidates','default_module_ko','default_module_name','source_file_count','code_loc','old_ref','new_ref','old_build_ok','new_build_ok','p0_hard_break','failure_kind','evidence_artifact','new_stderr_excerpt','old_stderr_excerpt']
for r in rows: r.setdefault('evidence_artifact','')
with (OUT/'oot_small_p0_candidates.csv').open('w',newline='',encoding='utf-8') as f:
    w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
summary={'out_dir':OUT.as_posix(),'candidate_count':len(rows),'p0_count':sum(1 for r in rows if r['p0_hard_break']),'by_failure_kind':{}}
for r in rows:
    if r['p0_hard_break']:
        summary['by_failure_kind'][r['failure_kind']]=summary['by_failure_kind'].get(r['failure_kind'],0)+1
(OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
