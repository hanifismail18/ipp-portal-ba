#!/usr/bin/env python3
"""BA (Berita Acara) kekurangan gaji validator.

Usage:
    python3 ba_validate.py <pdf|folder> [--out DIR] [--jobs N] [--model sonnet] [--no-cache]

Pipeline per PDF:
  1. text layer via pdftotext (if present) + page PNGs via pdftoppm
  2. one `claude -p` vision call (sonnet) -> strict JSON extraction (cached by PDF hash)
  3. deterministic rule checks in code -> 6 criteria + verdict
Outputs: <out>/rekap.csv, <out>/results.json, <out>/reports/<slug>.md
"""
import argparse, csv, json, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ba_core import (PROMPT_VERSION, CRITERIA, SCHEMA_DOC, PROMPT, sh, slugify, file_hash, text_layer,
                     parse_json, evaluate)

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"

def extract(pdf, model, use_cache=True):
    """Vision+text extraction via `claude -p`; cached by PDF hash + prompt version."""
    CACHE.mkdir(exist_ok=True)
    key = f"{file_hash(pdf)}_{PROMPT_VERSION}"
    cf = CACHE / f"{key}.json"
    if use_cache and cf.exists():
        return json.loads(cf.read_text())
    text = text_layer(pdf)
    dpi = "110" if text else "140"
    imgdir = CACHE / f"img_{key}"
    imgdir.mkdir(exist_ok=True)
    if not list(imgdir.glob("*.png")):
        sh(["pdftoppm", "-r", dpi, "-png", str(pdf), str(imgdir / "p")])
    images = sorted(imgdir.glob("p-*.png"), key=lambda p: int(re.findall(r"(\d+)\.png$", p.name)[0]))
    text_block = ("Text layer extracted by pdftotext (may miss images/handwriting):\n<<<\n" + text[:8000] + "\n>>>"
                  if text else "The PDF has no text layer (scan); rely on the images.")
    prompt = PROMPT.format(images=", ".join(str(i) for i in images), text_block=text_block, schema=SCHEMA_DOC)
    last_err = None
    for _ in range(2):
        r = sh(["claude", "-p", "--model", model, "--setting-sources", "", "--strict-mcp-config",
                "--allowedTools", "Read", "--no-session-persistence", "--output-format", "json", prompt],
               stdin=subprocess.DEVNULL, cwd=str(imgdir), timeout=900)
        try:
            out = json.loads(r.stdout)
            data = parse_json(out["result"])
            data["_meta"] = {"pages": len(images), "text_layer": bool(text), "dpi": dpi,
                             "cost_usd": out.get("total_cost_usd"), "model": model}
            cf.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            return data
        except Exception as e:  # retry once
            last_err = f"{e}: {r.stdout[-400:]} {r.stderr[-400:]}"
    raise RuntimeError(f"extraction failed for {pdf}: {last_err}")


# ---------------------------------------------------------------- rules

def report_md(fname, ex, results):
    L = [f"# {fname}", ""]
    m = ex.get("_meta", {})
    L.append(f"Halaman: {m.get('pages')} | text layer: {m.get('text_layer')}")
    for r in results:
        L += ["", f"## {r['nama']} (NIK {r['nik']}) -> **{r['verdict']}**", "",
              f"Jabatan: {r['jabatan']} | Lokasi: {r['lokasi']} | Periode: {r['periode']} | HK diajukan: {r['hk_diajukan']}", "",
              "| Kriteria | Status | Alasan |", "|---|---|---|"]
        for k in CRITERIA:
            L.append(f"| {k} | {r['criteria'][k]} | {r['reason'][k]} |")
        if r["problems"]:
            L += ["", "Masalah:"] + [f"- {p}" for p in r["problems"]]
    if ex.get("notes"):
        L += ["", f"Catatan ekstraksi: {ex['notes']}"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    t = Path(a.target)
    pdfs = sorted(t.glob("*.pdf")) if t.is_dir() else [t]
    out = Path(a.out)
    (out / "reports").mkdir(parents=True, exist_ok=True)

    def run(p):
        try:
            ex = extract(p, a.model, not a.no_cache)
            return p, ex, evaluate(ex, p.name), None
        except Exception as e:
            return p, None, None, str(e)

    allres = []
    with ThreadPoolExecutor(a.jobs) as pool:
        for p, ex, res, err in pool.map(run, pdfs):
            if err:
                print(f"ERROR {p.name}: {err}", file=sys.stderr)
                res = [{"file": p.name, "criteria": {k: "ERROR" for k in CRITERIA}, "reason": {}, "verdict": "ERROR",
                        "problems": [err[:300]]}]
            else:
                (out / "reports" / f"{slugify(p.name)}.md").write_text(report_md(p.name, ex, res))
            for r in res:
                print(f"{r['verdict']:8} {p.name}  " + " ".join(f"{k[0]}:{r['criteria'][k]}" for k in CRITERIA))
            allres += res
    (out / "results.json").write_text(json.dumps(allres, ensure_ascii=False, indent=1))
    cols = ["file", "nik", "nama", "jabatan", "lokasi", "periode", "hk_diajukan"] + CRITERIA + ["verdict", "problems"]
    with open(out / "rekap.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in allres:
            wr.writerow([r.get(c) for c in cols[:7]] + [r["criteria"].get(k) for k in CRITERIA] +
                        [r["verdict"], " | ".join(r["problems"])])
    print(f"\n{sum(r['verdict'] == 'VALID' for r in allres)}/{len(allres)} VALID -> {out / 'rekap.csv'}")


if __name__ == "__main__":
    main()
