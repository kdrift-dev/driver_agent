#!/usr/bin/env python3
"""Build bounded unique in-tree Kbuild source-unit candidates.

Evaluation/dataset helper. Target driver diffs are used only to construct
candidate paths, not as Driver-Agent input.
"""
from __future__ import annotations
import argparse, csv, subprocess
from pathlib import Path

SOURCE_SUFFIXES={'.c','.h','.rs','.S','.s'}
EXCLUDE_PARTS={'staging'}
EXCLUDE_PREFIXES=('drivers/gpu/drm/amd/','drivers/gpu/drm/nouveau/')

def git(repo:Path,args:list[str],check=False)->str:
    p=subprocess.run(['git','-C',str(repo),*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed\n{p.stderr}")
    return p.stdout

def exists(repo:Path,ref:str,path:str)->bool:
    p=subprocess.run(['git','-C',str(repo),'cat-file','-e',f'{ref}:{path}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return p.returncode==0

def list_paths(repo:Path,ref:str,prefix='drivers')->list[str]:
    return [x for x in git(repo,['ls-tree','-r','--name-only',ref,'--',prefix]).splitlines() if x]

def show(repo:Path,ref:str,path:str)->str:
    return git(repo,['show',f'{ref}:{path}'],check=False)

def nearest_unit(file_path:str,unit_dirs:set[str])->str|None:
    p=Path(file_path).parent
    while p.as_posix() and p.as_posix()!='drivers':
        rel=p.as_posix()
        if rel in unit_dirs:
            return rel
        p=p.parent
    return None

def grep_loc(repo:Path,ref:str,unit:str)->int:
    out=git(repo,['grep','-I','-n','-e','[^[:space:]]',ref,'--',unit],check=False)
    n=0
    prefix=ref+':'
    for line in out.splitlines():
        if not line.startswith(prefix):
            continue
        rest=line[len(prefix):]
        path=rest.split(':',1)[0]
        if Path(path).suffix in SOURCE_SUFFIXES:
            n+=1
    return n

def diff_numstat(repo:Path,old_ref:str,new_ref:str,path:str):
    out=git(repo,['diff','--numstat',f'{old_ref}..{new_ref}','--',path])
    add=delete=changed=0
    for line in out.splitlines():
        parts=line.split('\t')
        if len(parts)<3: continue
        try:
            a=int(parts[0]); d=int(parts[1])
        except ValueError:
            continue
        add+=a; delete+=d; changed+=1
    return add,delete,changed

def parent_includes(repo:Path,ref:str,path:str)->bool:
    p=Path(path); parent=p.parent.as_posix(); base=p.name
    mf=show(repo,ref,f'{parent}/Makefile') if parent else ''
    kc=show(repo,ref,f'{parent}/Kconfig') if parent else ''
    return (base in mf) or (path in kc) or (f'{path}/Kconfig' in kc)

def build(args):
    repo=Path(args.repo); out=Path(args.out_dir); out.mkdir(parents=True,exist_ok=True)
    new_paths=list_paths(repo,args.new_ref,'drivers')
    make_dirs={str(Path(x).parent) for x in new_paths if Path(x).name=='Makefile'}
    kconf_dirs={str(Path(x).parent) for x in new_paths if Path(x).name=='Kconfig'}
    unit_dirs=make_dirs & kconf_dirs
    changed=[x for x in git(repo,['diff','--name-only',f'{args.old_ref}..{args.new_ref}','--','drivers']).splitlines() if x]
    units={}
    for f in changed:
        if any(part in EXCLUDE_PARTS for part in Path(f).parts): continue
        if any(f.startswith(pref) for pref in EXCLUDE_PREFIXES): continue
        if Path(f).suffix not in SOURCE_SUFFIXES and Path(f).name not in {'Makefile','Kconfig'}: continue
        u=nearest_unit(f,unit_dirs)
        if not u: continue
        if not exists(repo,args.old_ref,u): continue
        units.setdefault(u,[]).append(f)
    old_paths_cache={}
    rows=[]
    for u,ch in sorted(units.items()):
        files=list_paths(repo,args.old_ref,u)
        src=[x for x in files if Path(x).suffix in SOURCE_SUFFIXES]
        if not src: continue
        source_count=len(src)
        if source_count>args.max_source_files: continue
        if len({Path(x).parent.as_posix() for x in files})>args.max_subdirs: continue
        add,delete,changed_count=diff_numstat(repo,args.old_ref,args.new_ref,u)
        modified=add+delete
        if modified<=0 or modified>args.max_modified_loc: continue
        loc=grep_loc(repo,args.old_ref,u)
        if loc<=0 or loc>args.max_code_loc: continue
        rows.append({
            'module_path':u,'modified_loc':modified,'code_loc':loc,'source_file_count':source_count,
            'changed_files':changed_count,'status':'existing','candidate_kind':'lowest_makefile_kconfig_unit',
            'parent_includes':str(parent_includes(repo,args.new_ref,u)),'changed_file_samples':';'.join(ch[:12]),
        })
    rows.sort(key=lambda r:(int(r['source_file_count']),int(r['code_loc']),int(r['modified_loc']),r['module_path']))
    fields=['module_path','modified_loc','code_loc','source_file_count','changed_files','status','candidate_kind','parent_includes','changed_file_samples']
    with (out/'unique_intree_candidates.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    print(f"[unique-candidates] old={args.old_ref} new={args.new_ref} rows={len(rows)} out={out/'unique_intree_candidates.csv'}", flush=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--repo',default='/mnt/disk7/jw_ai4os/linux_source_code/linux.git')
    ap.add_argument('--old-ref',required=True)
    ap.add_argument('--new-ref',default='v7.0')
    ap.add_argument('--out-dir',required=True)
    ap.add_argument('--max-code-loc',type=int,default=5000)
    ap.add_argument('--max-source-files',type=int,default=12)
    ap.add_argument('--max-modified-loc',type=int,default=5000)
    ap.add_argument('--max-subdirs',type=int,default=1)
    build(ap.parse_args())
if __name__=='__main__': main()
