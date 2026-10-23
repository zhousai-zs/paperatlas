"""Measure a local verification command, retaining the process tree peak RSS."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import psutil


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    start = time.perf_counter()
    process = subprocess.Popen(command)
    peak = 0
    while process.poll() is None:
        try:
            parent = psutil.Process(process.pid)
            memory = sum(p.memory_info().rss for p in [parent] + parent.children(recursive=True) if p.is_running())
            peak = max(memory, peak)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        time.sleep(.2)
    report = dict(command=command, elapsed_seconds=time.perf_counter()-start,
                  peak_process_tree_rss_mb=peak/1024**2, exit_code=process.returncode,
                  note='Sampled sum of process-tree working sets; shared pages may be counted twice.')
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    raise SystemExit(process.returncode)
