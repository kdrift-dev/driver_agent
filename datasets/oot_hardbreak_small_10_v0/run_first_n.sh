#!/usr/bin/env bash
set -euo pipefail
ROOT=/mnt/disk7/jw_ai4os/driver-agent/workflow
MANIFEST="$ROOT/datasets/oot_hardbreak_small_10_v0/manifest.csv"
N=${1:-3}
OUT=${2:-$ROOT/runs/oot_hardbreak_small_10_v0_first${N}_$(date +%Y%m%d_%H%M%S)}
mkdir -p "$OUT"
python3 - <<'INNERPY' "$MANIFEST" "$OUT" "$N"
import csv, json, subprocess, sys, time
from pathlib import Path
manifest=Path(sys.argv[1]); out=Path(sys.argv[2]); n=int(sys.argv[3])
rows=list(csv.DictReader(manifest.open()))[:n]
for i,r in enumerate(rows,1):
    case_out=out/r['case_id']; case_out.mkdir(parents=True, exist_ok=True)
    level='load_unload' if r.get('qemu_test_level')=='load_unload' else 'interface_smoke'
    cmd=['sudo','-n','/usr/bin/python3',str(Path('/mnt/disk7/jw_ai4os/driver-agent/workflow/pregen_driver_agent.py')),
      '--old-ref',r['old_ref'],'--new-ref',r['new_ref'],'--out-dir',str(case_out),'--jobs','8',
      '--enable-llm','--llm-provider','deepseek','--llm-model','deepseek-v4-pro',
      '--config-ini','/mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini','--llm-timeout-sec','300','--llm-max-retries','1','--llm-max-tokens','12000',
      '--patch-candidates','1','--oot-max-patch-iterations','2','--enable-qemu','--qemu-timeout-sec','1800','--cleanup-worktrees',
      '--oot-pipeline-mode','integrated','--oot-repo-url',r['oot_repo_url'],'--oot-repo-ref',r['oot_repo_ref'],'--oot-source-subdir',r['oot_source_subdir'],
      '--oot-module-ko',r['oot_module_ko'],'--oot-module-name',r['oot_module_name'],'--oot-qemu-test-mode','auto','--oot-qemu-test-level',level]
    if r.get('device_node'):
        cmd += ['--oot-device-node', r['device_node']]
    if 'lddbus' in r['case_id']:
        cmd += ['--oot-qemu-test-mode','custom','--oot-qemu-check-script','/mnt/disk7/jw_ai4os/driver-agent/workflow/datasets/oot_qemu_cases/lddbus_sysfs_check.sh']
    (case_out/'cmd.json').write_text(json.dumps(cmd,indent=2)+'\n')
    print(f"[RUN] {i}/{len(rows)} {r['case_id']} {r['oot_source_subdir']} {r['old_ref']}->{r['new_ref']}", flush=True)
    st=time.time(); p=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=14400)
    (case_out/'run.log').write_text(p.stdout,encoding='utf-8',errors='replace')
    print(f"[DONE] {r['case_id']} rc={p.returncode} sec={time.time()-st:.1f}", flush=True)
INNERPY
