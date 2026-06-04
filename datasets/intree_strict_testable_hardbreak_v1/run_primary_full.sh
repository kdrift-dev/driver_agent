#!/usr/bin/env bash
set -euo pipefail
ROOT=/mnt/disk7/jw_ai4os/driver-agent/workflow
MANIFEST="$ROOT/datasets/intree_strict_testable_hardbreak_v1/manifest.csv"
OUT=${1:-$ROOT/runs/intree_strict_testable_hardbreak_v1_full_$(date +%Y%m%d_%H%M%S)}
mkdir -p "$OUT"
python - <<'PY' "$MANIFEST" "$OUT"
import csv, json, subprocess, sys, time
from pathlib import Path
manifest=Path(sys.argv[1]); out=Path(sys.argv[2])
rows=[r for r in csv.DictReader(manifest.open()) if r.get('role')=='primary']
print('[dataset]', manifest)
print('[out]', out)
for i,r in enumerate(rows,1):
    case_out=out/r['case_id']; case_out.mkdir(parents=True, exist_ok=True)
    cmd=[
        'sudo','-n','/usr/bin/python3',str(Path('/mnt/disk7/jw_ai4os/driver-agent/workflow/pregen_driver_agent.py')),
        '--repo','/mnt/disk7/jw_ai4os/linux_source_code/linux.git',
        '--old-ref',r['old_ref'], '--new-ref',r['new_ref'], '--module-path',r['module_path'],
        '--out-dir',str(case_out), '--jobs','8', '--kernel-config-target','allmodconfig',
        '--enable-llm','--llm-provider','deepseek','--llm-model','deepseek-v4-pro',
        '--config-ini','/mnt/disk7/jw_ai4os/driver-agent/workflow/config.ini',
        '--llm-timeout-sec','300','--llm-max-retries','1','--llm-max-tokens','12000',
        '--patch-candidates','1','--max-patch-iterations','2',
        '--patch-source-mode','focused','--patch-prompt-strength','strict',
        '--enable-qemu','--qemu-timeout-sec','1800','--cleanup-worktrees'
    ]
    (case_out/'cmd.json').write_text(json.dumps(cmd,indent=2)+'\n')
    print(f"[RUN] {i}/{len(rows)} {r['case_id']} {r['old_ref']}->{r['new_ref']} {r['module_path']}", flush=True)
    start=time.time()
    p=subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=14400)
    (case_out/'run.log').write_text(p.stdout, encoding='utf-8', errors='replace')
    print(f"[DONE] {r['case_id']} rc={p.returncode} sec={time.time()-start:.1f}", flush=True)
PY
