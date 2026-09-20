"""Run the pipeline on a file without the web server: python tests/run_local.py IMAGE [key=value ...]"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.main import Job, _run_job, JOBS_DIR  # noqa: E402

src = Path(sys.argv[1])
opts = dict(a.split("=", 1) for a in sys.argv[2:])
job = Job("0000000000" + src.stem.encode().hex()[:6].ljust(6, "0"), src.name, "", opts)
shutil.rmtree(job.dir, ignore_errors=True)
job.dir.mkdir(parents=True)
dest = job.dir / ("input" + src.suffix.lower())
shutil.copy(src, dest)
job.input_path = str(dest)
job.log = lambda m: (print(m), Job.log(job, m))
_run_job(job, opts)
r = job.result
print(json.dumps({k: r.get(k) for k in ("status", "error", "solution", "calibration", "time", "warnings")}, indent=1, default=str))
print("variables:", len(r.get("variables", [])))
for v in r.get("variables", [])[:12]:
    print(f"  {v['name']:<28} {v['type']:<10} {'<' if v['upper_limit'] else ' '}{v['mag']:.3f} ±{v['err'] or 0:.3f}  vsx {v['max']}-{v['min']} {v['flags']}")
print("candidates:")
for c in r.get("candidates", []):
    print(f"  {c['status']:<13} {c['kind']:<12} {c['ra']:.5f} {c['dec']:.5f} mag {c['mag']:.2f} snr {c['snr']:.0f} fwhm {c['fwhm_px']:.1f}  {c['label']}")
print("minor bodies:", [(m['name'], m['vmag'], m['detected']) for m in r.get("minor_bodies", [])][:10])
print("job dir", job.dir)
