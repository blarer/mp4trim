"""One-shot runner for the real-clip test suites.

    py -3.12 tests/run_all.py [--fast] <clip.mp4>

Runs each suite in its own subprocess with a timeout, captures output,
and prints an aligned PASS/FAIL table plus the tail of any failure.
--fast skips the two slow export suites. Stdlib only.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv):
    args = [a for a in argv if a != "--fast"]
    fast = "--fast" in argv
    if not args:
        print(__doc__)
        return 2
    clip = args[0]

    shots = os.path.join(os.environ.get("TEMP", str(HERE)), "mp4trim_shots")
    # (script, timeout_s, extra args)
    suites = [
        ("check_update.py", 120, []),
        ("check_v21.py", 180, [clip]),
        ("check_glass.py", 180, [clip]),
        ("check_scrub.py", 300, [clip]),
        ("check_misc.py", 600, [clip]),
        ("ui_smoke.py", 300, [clip, shots]),
    ]
    if not fast:
        suites += [
            ("check_exports.py", 1200, [clip]),
            ("check_tiers.py", 1200, [clip]),
        ]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MP4TRIM_NO_UPDATE"] = "1"

    results = []
    for script, timeout, extra in suites:
        cmd = [sys.executable, str(HERE / script)] + extra
        print(f"=== {script} ===", flush=True)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                               errors="replace", env=env, timeout=timeout)
            out = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0
        except subprocess.TimeoutExpired as e:
            out = ((e.stdout or "") if isinstance(e.stdout, str) else "")
            out += f"\n*** TIMEOUT after {timeout}s ***"
            ok = False
        secs = time.time() - t0
        print(("PASS" if ok else "FAIL") + f" in {secs:.1f}s", flush=True)
        results.append((script, ok, secs, out))

    width = max(len(s) for s, *_ in results)
    print("\n" + "-" * (width + 18))
    print(f"{'suite':<{width}}  {'result':<6}  seconds")
    for script, ok, secs, _ in results:
        print(f"{script:<{width}}  {'PASS' if ok else 'FAIL':<6}  {secs:7.1f}")
    print("-" * (width + 18))

    failed = [r for r in results if not r[1]]
    for script, _, _, out in failed:
        tail = out.strip().splitlines()[-15:]
        print(f"\n--- tail of {script} ---")
        for line in tail:
            print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
