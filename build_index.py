#!/usr/bin/env python3
"""
Build the data files for OpenNeuro Full-Text Search.

Source: OpenNeuro's GitHub mirror (github.com/OpenNeuroDatasets), one git repo per dataset.
For each dataset it reads dataset_description.json, the README, the file tree (names only,
via a tree-only clone; no imaging data) and the TracerName of one *_pet.json sidecar.

Incremental: every dataset's current commit is checked with `git ls-remote`; only new or
changed datasets are re-read. Per-dataset records are cached in --cache.

Output (--out):
  datasets.json  one JSON array, one dataset record per line
  meta.json      {"built": ISO-8601 UTC time, "n": ..., "max_id": ..., "added": ..., "updated": ...}

Usage:  python build_index.py --cache cache --out site
Needs:  Python 3.8+, git. Standard library only.
"""
import argparse, json, math, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

RAW = "https://raw.githubusercontent.com/OpenNeuroDatasets/{id}/{ref}/{path}"
GIT = "https://github.com/OpenNeuroDatasets/{id}.git"
DATATYPES = {"anat", "func", "dwi", "fmap", "perf", "pet", "eeg", "meg", "ieeg", "beh",
             "nirs", "motion", "micr", "mrs"}
NORMALIZE = [(re.compile(r"sub-[A-Za-z0-9]+"), "sub-*"), (re.compile(r"ses-[A-Za-z0-9]+"), "ses-*"),
             (re.compile(r"run-\d+"), "run-*"), (re.compile(r"echo-\d+"), "echo-*"),
             (re.compile(r"(chunk|split|part)-\d+"), r"\1-*"), (re.compile(r"\d{3,}"), "#")]
MAX_PATTERNS, MAX_README, MAX_MEASURES = 250, 20000, 80000
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
RECORD_VERSION = 2  # bump to force every cached record to be rebuilt


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def head_of(ds, tries=3):
    """Commit sha of the dataset's default branch, None if the repo doesn't exist,
    'ERR' if it couldn't be checked (network trouble)."""
    for i in range(tries):
        r = subprocess.run(["git", "ls-remote", GIT.format(id=ds), "HEAD"],
                           capture_output=True, text=True, timeout=60, env=GIT_ENV)
        if r.returncode == 0:
            return r.stdout.split()[0] if r.stdout.strip() else None
        err = r.stderr.lower()
        if "not found" in err or "could not read username" in err or "authentication" in err:
            return None
        time.sleep(2 * (i + 1))
    return "ERR"


def fetch(ds, ref, path, tries=3, max_bytes=None):
    url = RAW.format(id=ds, ref=ref, path=urllib.parse.quote(path))
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes=0-{max_bytes - 1}"} if max_bytes else {})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(max_bytes or -1).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def file_tree(ds):
    with tempfile.TemporaryDirectory() as tmp:
        ok = subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none",
                             "--no-checkout", GIT.format(id=ds), f"{tmp}/r"],
                            capture_output=True, env=GIT_ENV).returncode == 0
        if not ok:
            return None
        out = subprocess.run(["git", "-C", f"{tmp}/r", "ls-tree", "-r", "--name-only", "HEAD"],
                             capture_output=True, text=True).stdout
        return out.splitlines()


def tracer_of(ds, ref, files):
    pets = sorted((f for f in files if f.endswith("pet.json")), key=len)
    if not pets:
        return ""
    try:
        j = json.loads(fetch(ds, ref, pets[0]) or "")
    except Exception:
        return ""
    parts = [str(j.get(k, "")).strip() for k in ("TracerName", "TracerRadionuclide", "ModeOfAdministration")]
    return "" if not parts[0] or parts[0] == "?" else " | ".join(p for p in parts if p)


def read_json(ds, ref, path, max_bytes=2_000_000):
    try:
        return json.loads(fetch(ds, ref, path, max_bytes=max_bytes) or "")
    except Exception:
        return None


def describe_columns(sidecar, prefix):
    """Lines like 'participants: bdi_total = Beck Depression Inventory total score' from a BIDS sidecar."""
    lines = []
    if not isinstance(sidecar, dict):
        return lines
    tool = sidecar.get("MeasurementToolMetadata")
    if isinstance(tool, dict) and tool.get("Description"):
        lines.append(f"{prefix}: {str(tool['Description'])[:300]}")
    for col, meta in sidecar.items():
        if col == "MeasurementToolMetadata":
            continue
        if isinstance(meta, dict):
            bits = [str(meta.get(k, "")) for k in ("LongName", "Description") if meta.get(k)]
            levels = meta.get("Levels")
            if isinstance(levels, dict):
                bits.append("levels: " + "; ".join(f"{k}={v}" for k, v in list(levels.items())[:12]))
            if meta.get("Units"):
                bits.append(f"units: {meta['Units']}")
            lines.append(f"{prefix}: {col} = " + " | ".join(bits)[:300] if bits else f"{prefix}: {col}")
        elif isinstance(meta, str):
            lines.append(f"{prefix}: {col} = {meta[:300]}")
    return lines


def to_years(v, units):
    u = (units or "").lower()
    if "month" in u:
        return v / 12
    if "week" in u:
        return v / 52.18
    if "day" in u:
        return v / 365.25
    return v


def participants_info(ds, ref, files):
    """Column names, age range and sex counts from participants.tsv; descriptions from participants.json."""
    fs = set(files)
    info, text = {}, []
    side = read_json(ds, ref, "participants.json") if "participants.json" in fs else None
    if "participants.tsv" in fs:
        tsv = fetch(ds, ref, "participants.tsv", max_bytes=5_000_000) or ""
        rows = [r.split("\t") for r in tsv.splitlines() if r.strip()]
        if rows:
            head = [h.strip() for h in rows[0]]
            low = [h.lower() for h in head]
            info["cols"] = head[:300]
            text.append("participants columns: " + " ".join(head[:300]))
            pid = low.index("participant_id") if "participant_id" in low else 0
            first = {}
            for r in rows[1:]:
                if len(r) == len(head) and r[pid].strip() not in first:
                    first[r[pid].strip()] = r
            info["n"] = len(first)
            age_i = next((low.index(c) for c in ("age", "age_years", "age_at_scan", "ageatscan", "age_at_visit")
                          if c in low), next((i for i, h in enumerate(low) if h.startswith("age")), None))
            if age_i is not None:
                units = side.get(head[age_i], {}).get("Units") if isinstance(side, dict) and isinstance(side.get(head[age_i]), dict) else None
                ages = []
                for r in first.values():
                    try:
                        v = float(r[age_i])
                    except ValueError:
                        continue
                    if math.isfinite(v):
                        ages.append(to_years(v, units))
                # Without stated units, values over 120 suggest months or days: don't guess.
                # Negative values mean a standardized score, not years.
                if ages and min(ages) >= 0 and (units or max(ages) <= 120):
                    info["age"] = [round(min(ages), 1), round(max(ages), 1), round(sum(ages) / len(ages), 1), len(ages)]
            sex_i = next((low.index(c) for c in ("sex", "gender", "biological_sex", "sex_at_birth") if c in low), None)
            if sex_i is not None:
                meta = side.get(head[sex_i]) if isinstance(side, dict) else None
                levels = {str(k).strip().lower(): str(v).strip().lower()
                          for k, v in (meta.get("Levels") or {}).items()} if isinstance(meta, dict) and isinstance(meta.get("Levels"), dict) else {}
                counts, ambiguous = {"F": 0, "M": 0, "O": 0}, False
                for r in first.values():
                    v = r[sex_i].strip().lower()
                    v = levels.get(v, v)
                    if v in ("f", "female", "w", "woman", "girl"):
                        counts["F"] += 1
                    elif v in ("m", "male", "man", "boy"):
                        counts["M"] += 1
                    elif v in ("n/a", "na", "nan", "", "unknown", "u", "not reported"):
                        continue
                    elif re.fullmatch(r"-?\d+(\.\d+)?", v):
                        ambiguous = True  # numeric code with no Levels: don't guess the coding
                        break
                    else:
                        counts["O"] += 1
                if not ambiguous and sum(counts.values()):
                    info["sex"] = counts
    text += describe_columns(side, "participants")
    return info, text


def phenotype_info(ds, ref, files):
    """Phenotype measure files, their column headers and sidecar descriptions."""
    tsvs = sorted(f for f in files if f.startswith("phenotype/") and f.endswith(".tsv"))[:60]
    out, text = [], []
    for f in tsvs:
        name = f.split("/")[-1][:-4]
        head = (fetch(ds, ref, f, max_bytes=65536) or "").split("\n", 1)[0]
        cols = [c.strip() for c in head.split("\t") if c.strip()][:400]
        out.append({"file": name, "ncol": len(cols)})
        text.append(f"phenotype {name}: " + " ".join(cols))
        side_path = f[:-4] + ".json"
        if side_path in files:
            text += describe_columns(read_json(ds, ref, side_path), f"phenotype {name}")
    return out, text


def build_record(ds, sha):
    desc_txt = fetch(ds, sha, "dataset_description.json")
    try:
        desc = json.loads(desc_txt) if desc_txt else {}
    except Exception:
        desc = {}
    readme = ""
    for name in ("README", "README.md", "README.txt", "README.rst"):
        t = fetch(ds, sha, name)
        if t is not None:
            readme = t
            break
    files = file_tree(ds)
    if files is None:
        return None
    subs, sess, mods, tasks, trc, pats, deriv = set(), set(), set(), set(), set(), set(), False
    for f in files:
        parts = f.split("/")
        isder = parts[0] == "derivatives"
        deriv = deriv or isder
        if parts[0].startswith("sub-"):
            subs.add(parts[0])
        for q in parts[:-1]:
            if q.startswith("ses-"):
                sess.add(q)
            if q in DATATYPES and not isder:
                mods.add(q)
        base = parts[-1]
        tasks.update(re.findall(r"task-([A-Za-z0-9]+)", base))
        if "_pet" in base or "/pet/" in f:
            trc.update(re.findall(r"_(?:trc|acq)-([A-Za-z0-9]+)", base))
        p = f
        for rx, rep in NORMALIZE:
            p = rx.sub(rep, p)
        pats.add(re.sub(r"^sub-\*/(ses-\*/)?", "", p))
    pats = sorted(pats)
    if len(pats) > MAX_PATTERNS:
        pats = pats[:MAX_PATTERNS] + [f"... ({len(pats) - MAX_PATTERNS} more patterns)"]
    tracer = tracer_of(ds, sha, files)
    if tracer or trc:
        mods.add("pet")
    parts_info, ptext = participants_info(ds, sha, files)
    pheno, phtext = phenotype_info(ds, sha, files)
    if pheno:
        mods.add("phenotype")
    measures = "\n".join(ptext + phtext)[:MAX_MEASURES]
    auth = desc.get("Authors") or []
    auth = [auth] if isinstance(auth, str) else auth
    refs = desc.get("ReferencesAndLinks") or []
    refs = " ".join(map(str, refs)) if isinstance(refs, list) else str(refs)
    return {"id": ds, "name": str(desc.get("Name", ""))[:300], "authors": ", ".join(map(str, auth))[:400],
            "doi": str(desc.get("DatasetDOI", ""))[:120], "refs": refs[:1000], "readme": readme[:MAX_README],
            "nsub": len(subs), "nses": len(sess), "nfiles": len(files), "mods": sorted(mods),
            "tasks": sorted(tasks)[:200], "tracer": tracer, "trc": sorted(trc), "deriv": deriv,
            "files": "\n".join(pats), "participants": parts_info, "pheno": pheno, "measures": measures}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--out", default="site")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--scan-ahead", type=int, default=1500, help="IDs to probe past the highest known one")
    ap.add_argument("--min-scan", type=int, default=10000, help="always probe at least ds000001..this")
    ap.add_argument("--offline", action="store_true", help="skip GitHub; rewrite the output from the cache")
    a = ap.parse_args()
    cache, out = Path(a.cache) / "records", Path(a.out)
    cache.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    cached = {}
    for p in cache.glob("ds*.json"):
        try:
            r = json.loads(p.read_text())
            if r.get("_v") == RECORD_VERSION:
                cached[r["id"]] = r
        except Exception:
            pass
    hi = max([int(k[2:]) for k in cached] + [0])
    ids = [f"ds{n:06d}" for n in range(1, max(a.min_scan, hi + a.scan_ahead) + 1)]
    log(f"{len(cached)} cached records; checking {len(ids)} IDs")

    if a.offline:
        heads = {k: r["_head"] for k, r in cached.items()}
    else:
        with ThreadPoolExecutor(a.workers) as ex:
            heads = dict(zip(ids, ex.map(head_of, ids)))
    live = {k: v for k, v in heads.items() if v and v != "ERR"}
    unreachable = [k for k, v in heads.items() if v == "ERR"]
    todo = [k for k, v in live.items() if k not in cached or cached[k].get("_head") != v]
    log(f"{len(live)} datasets found, {len(todo)} new or changed, {len(unreachable)} unreachable")

    def work(ds):
        rec = build_record(ds, live[ds])
        if rec:
            rec["_head"], rec["_v"] = live[ds], RECORD_VERSION
            (cache / f"{ds}.json").write_text(json.dumps(rec, ensure_ascii=False))
        return ds, rec

    added = updated = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for ds, rec in ex.map(work, todo):
            if rec:
                added += ds not in cached
                updated += ds in cached
                cached[ds] = rec

    # Keep datasets we couldn't check this time; drop ones that no longer exist.
    keep = {k: r for k, r in cached.items() if k in live or k in unreachable}
    # Skip empty placeholder repos (no name, no README, a file or two).
    keep = {k: r for k, r in keep.items() if r["name"] or r["readme"] or r["nfiles"] > 2}
    recs = [{k: v for k, v in keep[k].items() if not k.startswith("_")} for k in sorted(keep)]
    with open(out / "datasets.json", "w") as f:
        f.write("[\n" + ",\n".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False, allow_nan=False) for r in recs) + "\n]\n")
    meta = {"built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "n": len(recs),
            "max_id": recs[-1]["id"] if recs else None, "added": added,
            "updated": updated, "unreachable": len(unreachable)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    log(json.dumps(meta))


if __name__ == "__main__":
    main()
