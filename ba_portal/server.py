"""BA Submission Portal backend (FastAPI). API contract: SPEC_PORTAL.md (+ ADDENDUM v2)."""
import asyncio, collections, csv, hashlib, html, io, json, logging, os, re, secrets, sys, time, unicodedata, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(HERE / ".env")

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

import db as dbm  # noqa: E402
import engines  # noqa: E402
import preprocess  # noqa: E402
import storage  # noqa: E402
import ba_core  # noqa: E402  (path added by engines)
import keystore  # noqa: E402

log = logging.getLogger("ba_portal")


def env_int(k, d):
    try:
        return int(os.environ.get(k, d))
    except ValueError:
        return d


DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = dbm.DB(os.environ.get("DATABASE_URL") or os.environ.get("DB_PATH", str(DATA / "ba_portal.db")))
try:
    if not DB.pg:
        os.chmod(DB.path, 0o600)
except OSError:
    pass
KS = keystore.KeyStore(DB, keystore.ensure_secret(HERE / ".env"))
engines.KEY_LOOKUP = KS.get              # hot reload: every job/health builds layers with the current keys
engines.CALLS_TODAY = DB.layer_calls_today
DITUNDA_MSG = "Antrean, layanan sedang penuh, coba lagi otomatis."
UNCLEAR_MSG = "Tidak terbaca jelas, upload ulang."
RULES_VERSION = "v7"
FRONTEND = Path(os.environ.get("FRONTEND_DIR", HERE / "frontend"))
MAX_BYTES = env_int("MAX_UPLOAD_MB", 20) * 1024 * 1024
PAGE_CAP = env_int("PAGE_CAP", 8)
MAX_PDF_PAGES = env_int("MAX_PDF_PAGES", 40)
CONCURRENCY = env_int("LLM_CONCURRENCY", 2)
QUOTA = env_int("QUOTA_PER_CLIENT_PER_DAY", 30)
GLOBAL_QUOTA = env_int("GLOBAL_UPLOADS_PER_DAY", 1000)
MAX_QUEUE = env_int("MAX_QUEUE", 200)
MAX_CALLS_DAY = env_int("MAX_LLM_CALLS_PER_DAY", 500)
RETENTION_DAYS = env_int("RETENTION_DAYS", 14)
RETRY_MAX_MIN = env_int("RETRY_MAX_MIN", 60)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LOW_CONF = 0.6

LABELS = {"1_identitas": "Identitas HL (NIK, Nama, Jabatan, Lokasi)", "2_periode": "Periode kekurangan gaji",
          "3_hk_diajukan": "Jumlah HK diajukan", "4_ttd": "TTD HL dan Pejabat",
          "5_kehadiran": "Lampiran kehadiran (DMS)", "6_transaksi": "Lampiran transaksi (DMS)"}
LABEL3 = {"kurang_hk": "HK diajukan", "kurang_nominal": "Nominal kekurangan", "single_tanggal": "Tanggal yang diajukan",
          "lainnya": "HK diajukan"}
MAP = {"OK": "VALID", "MISSING": "KOSONG", "PARTIAL": "INVALID", "INVALID": "INVALID"}  # API vocabulary: 3 values only
RANK = {"VALID": 0, "KOSONG": 1, "INVALID": 2}

app = FastAPI(title="BA Portal", docs_url=None, redoc_url=None, openapi_url=None)
queue: asyncio.Queue = asyncio.Queue()
pending = collections.deque()          # ids waiting, in order (for queue_position)
running = set()
_health = {"t": 0, "llm": "down"}
_admin_fail = collections.defaultdict(list)  # ip -> [timestamps of failed admin attempts]


# ------------------------------------------------------------------ security headers

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
       "connect-src 'self' https://cdnjs.cloudflare.com; worker-src 'self' blob: https://cdnjs.cloudflare.com; "
       "frame-src 'self' blob:; object-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ processing

REREAD_ALL_MAX = env_int("REREAD_ALL_MAX", 25)
NOTE_MAX = 120  # v5: one short line per criterion note
UNSURE_WHAT = {"1_identitas": "identitas", "2_periode": "periode", "3_hk_diajukan": "jumlah HK",
               "4_ttd": "TTD", "5_kehadiran": "tanggal kehadiran", "6_transaksi": "transaksi"}
CEK = "tidak terbaca, cek manual"


def _cap1(t):
    return t[:1].upper() + t[1:] if t else t


def _names(ws, n=3):
    names = [w.get("nama") or w.get("nik") or "?" for w in ws]
    return ", ".join(names[:n]) + (f" +{len(names) - n} lainnya" if len(names) > n else "")


def _multi(res, poor):
    """v5 multi-worker messages. A problem shared by every worker is said ONCE at BA level; workers[].problems holds
    only that worker's own issues. Unreadable-only fields become one 'Cek manual' line (never a fake rule failure)."""
    n = len(res)
    rows = []  # per worker: {k: (status, reason, unsure)}
    for r in res:
        row = {}
        for k in ba_core.CRITERIA:
            st = MAP.get(r["criteria"][k], "INVALID")
            c = min(r.get("confidence", {}).get(k, 0.8), 0.4 if poor else 1.0)
            if st != "VALID":
                row[k] = (st, r["reason"].get(k) or "", False)
            elif c < LOW_CONF:
                row[k] = ("INVALID", CEK, True)
            else:
                row[k] = ("VALID", "", False)
        if row["5_kehadiran"][2] and row["3_hk_diajukan"][2]:  # HK unsure only because attendance is unreadable
            row["3_hk_diajukan"] = ("INVALID", CEK + " (kehadiran)", True)
        rows.append(row)
    shared = {k for k in ba_core.CRITERIA
              if all(row[k][0] != "VALID" for row in rows) and len({(row[k][1], row[k][2]) for row in rows}) == 1}
    warns = [[p for p in r["problems"] if not p.startswith("[")] for r in res]
    common_w = [w for w in (warns[0] if warns else []) if all(w in ws for ws in warns)] if n > 1 else []
    workers = []
    for r, row, ws in zip(res, rows, warns):
        own = [_cap1(row[k][1]) for k in ba_core.CRITERIA if k not in shared and row[k][0] != "VALID" and not row[k][2]]
        cek = [UNSURE_WHAT[k] for k in ba_core.CRITERIA if k not in shared and row[k][2] and "(kehadiran)" not in row[k][1]]
        if cek:
            own.append("Cek manual: " + ", ".join(cek) + " tidak terbaca")
        own += [w for w in ws if w not in common_w and not w.lower().startswith("kehadiran:")]
        wc = {k: row[k][0] for k in ba_core.CRITERIA}
        workers.append({**{k: r.get(k) for k in ("nik", "nama", "jabatan", "lokasi", "periode", "hk_diajukan")},
                        "status": "VALID" if all(v == "VALID" for v in wc.values()) else "INVALID",
                        "problems": list(dict.fromkeys(own)), "criteria": wc})
    criteria = []
    for k in ba_core.CRITERIA:
        sts = [row[k][0] for row in rows]
        worst = max(sts, key=lambda x: RANK[x])
        bad = [(w, row[k][1]) for w, row in zip(workers, rows) if row[k][0] != "VALID" and not row[k][2]]
        uns = [w for w, row in zip(workers, rows) if row[k][2]]
        if not bad and not uns:
            note = f"Semua {n} pekerja OK"
        elif k in shared:
            note = _cap1(rows[0][k][1])
        else:  # real problems first (name: reason), then one 'cek manual' tail for unreadable-only workers
            nm = lambda w: w.get("nama") or w.get("nik") or "?"  # noqa: E731
            whys = {y for _, y in bad}
            heads = [[f"{nm(w)}: {y}" for w, y in bad]]
            if len(whys) == 1:
                heads.append([f"{_cap1(next(iter(whys)))}: {_names([w for w, _ in bad])}"])
            heads.append([f"{len(bad)} dari {n} pekerja: {_names([w for w, _ in bad])}"] if bad else [])
            tails = [[f"cek manual: {_names(uns)}"], [f"cek manual: {len(uns)} pekerja"]] if uns else [[]]
            cands = ["; ".join(h + t) for h in heads for t in tails]
            note = next((c for c in cands if len(c) <= NOTE_MAX), cands[-1])
            if len(note) > NOTE_MAX:
                note = note[:NOTE_MAX - 1].rstrip() + "…"
            note = _cap1(note)
        criteria.append((k, worst, note))
    alerts = list(common_w)
    cek = [(w, [UNSURE_WHAT[k] for k in ba_core.CRITERIA if row[k][2] and "(kehadiran)" not in row[k][1]])
           for w, row in zip(workers, rows)]
    cek = [(w, what) for w, what in cek if what]
    if cek:
        whats = sorted({x for _, what in cek for x in what}, key=list(UNSURE_WHAT.values()).index)
        alerts.append(f"Cek manual: {', '.join(whats)} tidak terbaca ({_names([w for w, _ in cek])})"
                      if len(cek) < n else f"Cek manual: {', '.join(whats)} tidak terbaca")
    bad = [w for w in workers if w["status"] != "VALID"]
    if 0 < len(bad) < n:  # mixed only: one short line
        alerts.insert(0, f"{len(bad)} dari {n} pekerja bermasalah: {_names(bad)}")
    return workers, criteria, alerts


def build_result(ex, prep):
    res = ba_core.evaluate(ex, "")
    jenis = res[0].get("jenis", "kurang_hk") if res else "kurang_hk"
    poor = prep["quality"] == "poor"
    multi = len(res) > 1
    conf = {k: round(min(min(r.get("confidence", {}).get(k, 0.8) for r in res), 0.4 if poor else 1.0), 2)
            for k in ba_core.CRITERIA}  # internal only
    label = {k: LABEL3.get(jenis, LABELS[k]) if k == "3_hk_diajukan" else LABELS[k] for k in ba_core.CRITERIA}
    alerts = list(prep["alerts"]) + list(ex.get("_alerts") or [])
    blank = {m["page"] for m in prep["metrics"] if m.get("blank")}
    for pq in ex.get("page_quality") or []:
        if str(pq.get("legible")).lower() == "poor" and pq.get("page") not in blank:
            alerts.append(f"Hal. {pq.get('page')} sulit dibaca ({pq.get('note') or 'kurang jelas'}); foto kurang jelas, upload ulang.")
    if multi:
        workers, crit3, extra = _multi(res, poor)
        criteria = [{"key": k, "label": label[k], "status": st, "note": note} for k, st, note in crit3]
        alerts += extra
    else:  # single worker: v3 behaviour unchanged
        r = res[0]
        wc = {k: MAP.get(r["criteria"][k], "INVALID") for k in ba_core.CRITERIA}
        probs = list(r.get("short_problems") or [])
        criteria = []
        for k in ba_core.CRITERIA:
            worst, note = wc[k], r["reason"].get(k) or ""
            if conf[k] < LOW_CONF:  # low confidence = plain INVALID + 'upload ulang' (v3)
                if worst == "VALID":
                    worst = wc[k] = "INVALID"
                    probs.append(f"{ba_core.SHORT[k]}: {UNCLEAR_MSG}")
                note = f"{note}. {UNCLEAR_MSG}" if note else UNCLEAR_MSG
                alerts.append(f"{label[k]}: {UNCLEAR_MSG}")
            criteria.append({"key": k, "label": label[k], "status": worst, "note": note})
        workers = [{**{k: r.get(k) for k in ("nik", "nama", "jabatan", "lokasi", "periode", "hk_diajukan")},
                    "status": "VALID" if all(v == "VALID" for v in wc.values()) else "INVALID",
                    "problems": probs, "criteria": wc}]
        alerts += [p for p in r["problems"] if not p.startswith("[")]
    alerts = list(dict.fromkeys(alerts))
    verdict = "VALID" if all(c["status"] == "VALID" for c in criteria) and all(w["status"] == "VALID" for w in workers) \
        else "INVALID"
    per_worker = [{**w, "jenis": r.get("jenis"), "verdict": w["status"]} for w, r in zip(workers, res)]
    first = res[0] if res else {}
    return {"workers": workers, "criteria": criteria, "alerts": alerts, "verdict": verdict, "quality": prep["quality"],
            "jenis": jenis, "tanggal": first.get("tanggal") or [], "nominal": first.get("nominal"),
            "pages": prep["pages"], "rules": RULES_VERSION, "_internal": {"confidence": conf},
            "page_notes": [{"page": p.get("page"), "what": p.get("what")} for p in (ex.get("page_notes") or [])
                           if isinstance(p, dict)],
            "per_worker": per_worker, "metrics": prep["metrics"],
            "signature_slots": [{k: x.get(k) for k in ("role", "worker", "page", "bbox", "signed")}
                                for x in (ex.get("signature_slots") or []) if isinstance(x, dict)],
            "issue_marks": [{k: x.get(k) for k in ("criterion", "worker", "page", "bbox", "label")}
                            for x in (ex.get("issue_marks") or []) if isinstance(x, dict)],
            "missing_attachments": missing_attachments(res)}


def reread_unsure(ex, pdf_path, log_usage):
    """v5 (multi-worker only): ONE targeted high-res re-read of the attendance pages (non-lite Flash first) for all
    workers (<= REREAD_ALL_MAX), else only those whose read looks wrong (same date twice, count != printed total,
    HK != attendance, not found).
    A consistent re-read replaces that worker's dates; otherwise the worker stays 'cek manual' (never a fake failure)."""
    ws, kh = ex.get("workers") or [], ex.get("attach_kehadiran") or {}
    if len(ws) < 2 or not kh.get("present"):
        return ex
    res = ba_core.evaluate(ex, "")
    sus = [w for w, r in zip(ws, res) if isinstance(w.get("att"), dict) and ba_core.norm_nik(w.get("nik")) and (
        "5_kehadiran" in r.get("unsure", []) or r["criteria"]["5_kehadiran"] != "OK" or
        (r["criteria"]["3_hk_diajukan"] != "OK" and "kehadiran" in r["reason"]["3_hk_diajukan"]))]
    # every multi-worker BA (<= REREAD_ALL_MAX workers) gets ONE re-read of all workers: flash-lite often copies the
    # BA NIK into the DMS NIK and misreads dense date columns; bigger BAs re-read only the suspicious workers
    targets = [w for w in ws if isinstance(w.get("att"), dict) and ba_core.norm_nik(w.get("nik"))] \
        if len(ws) <= REREAD_ALL_MAX else sus
    if not targets:
        return ex
    pages = [p for p in (kh.get("pages") or []) if isinstance(p, int)] or \
        [p.get("page") for p in (ex.get("page_notes") or []) if isinstance(p, dict) and p.get("what") == "kehadiran"]
    pages = sorted({p for p in pages if isinstance(p, int)})[:4]
    if not pages:
        return ex
    got = engines.reread_attendance(pdf_path, pages, [{"nik": w.get("nik"), "nama": w.get("nama")} for w in targets],
                                    log_usage)
    fixed, left, model = [], [], None
    for w in targets:
        rr = got.get(ba_core.norm_nik(w.get("nik")))
        days = sorted({x for x in (rr or {}).get("dates", [])})
        tot = w["att"].get("total")
        if rr and rr["sure"] and len(ba_core.norm_nik(rr.get("nik_shown"))) == 16:
            w["att"] = {**w["att"], "nik_shown": rr["nik_shown"]}  # the high-res read of the DMS NIK wins
        if rr and rr["sure"] and days and (not isinstance(tot, int) or tot == len(days)):
            if sorted(set(w["att"].get("days_present") or [])) != days:
                fixed.append(w.get("nama"))
            w["att"] = {**w["att"], "found": True, "days_present": days}
        elif w in sus:
            left.append(w.get("nama"))
    model = next((v.get("model") for v in got.values()), None)
    ex["_reread"] = {"model": model, "pages": pages, "workers": len(targets), "fixed": fixed, "unresolved": left}
    return ex


def _pages_of(ex, what, key):
    a = (ex.get(key) or {}).get("pages") or []
    ps = [p for p in a if isinstance(p, int)] or [p.get("page") for p in (ex.get("page_notes") or [])
                                                  if isinstance(p, dict) and p.get("what") == what]
    return sorted({p for p in ps if isinstance(p, int)})


def issue_targets(res):
    """v7: field-level problems that have a place on the page (NIK cell, HK cell, periode text, attendance/transaction
    row) -> [{criterion, worker, label, where, find}]. Absent attachments have no place (see missing_attachments)."""
    multi, out = len(res) > 1, []
    for r in res:
        c, rs, pr, who = r["criteria"], r["reason"], r["problems"], r.get("nama") if len(res) > 1 else None
        nik = r.get("nik") or "?"

        ref = {"nik": ba_core.norm_nik(r.get("nik")), "name": r.get("nama"), "hk": r.get("hk_diajukan"),
               "periode": r.get("periode")}

        def add(k, label, where, find):
            out.append({"criterion": k, "worker": who, "label": label, "where": where, "find": find, "ref": ref})
        m = re.search(r"NIK (\d+) digit", rs["1_identitas"])
        if c["1_identitas"] != "OK" and m:
            add("1_identitas", f"NIK {m.group(1)} digit", "BA", f"the NIK value {nik} in the BA")
        elif any(p.startswith("NIK di BA") for p in pr):
            add("1_identitas", "NIK beda dengan DMS", "BA", f"the NIK value {nik} in the BA")
        if c["2_periode"] == "PARTIAL" or any(p.startswith("Periode ") and " hanya " in p for p in pr):
            add("2_periode", "Periode salah", "BA", f"the periode text '{r.get('periode') or '?'}' in the BA")
        if c["3_hk_diajukan"] == "PARTIAL":
            nom = r.get("jenis") == "kurang_nominal"
            add("3_hk_diajukan", "Nominal tidak cocok" if nom else "HK tidak cocok", "BA",
                "the nominal / amount calculation in the BA" if nom else
                f"the HK / Kekurangan HK value ({r.get('hk_diajukan')}) claimed in the BA")
        k5 = rs["5_kehadiran"]
        if c["5_kehadiran"] == "PARTIAL" and "tidak ada di lampiran" not in k5:
            add("5_kehadiran", "Tanggal tidak ada di kehadiran" if "tidak menunjukkan" in k5 else "Hanya rekap total",
                "kehadiran (attendance/DMS)", "this worker's row in the attendance table")
        elif any(p.startswith("Hari di BA vs lampiran") for p in pr):
            add("5_kehadiran", "Hari beda dengan kehadiran", "kehadiran (attendance/DMS)",
                "this worker's row in the attendance table")
        k6 = rs["6_transaksi"]
        if (c["6_transaksi"] == "PARTIAL" and "tidak ada di lampiran" not in k6) or "tanpa jumlah" in k6:
            add("6_transaksi", "Tanpa jumlah per hari" if "tanpa jumlah" in k6 else "Transaksi kurang",
                "transaksi (DMS transaction)", "this worker's row in the transaction table")
    seen, uniq = set(), []
    for i in out:
        key = (i["criterion"], i["worker"], i["label"])
        if key not in seen:
            seen.add(key)
            uniq.append({**i, "id": len(uniq) + 1})
    return uniq


def missing_attachments(res):
    """v7: attachments that are absent (no place to mark): the frontend shows a red placeholder page instead."""
    out = []
    if any(r["criteria"]["5_kehadiran"] == "MISSING" for r in res):
        out.append("kehadiran")
    if any(r["criteria"]["6_transaksi"] == "MISSING" and "tidak ada" in r["reason"]["6_transaksi"] for r in res):
        out.append("transaksi")
    return out


def _page_words(pdf_path, page):
    """(W, H, [(x0, y0, x1, y1, text)]) of a page's text layer (pdftotext -bbox), or None if it has no text."""
    import subprocess
    try:
        t = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-bbox", pdf_path, "-"],
                           capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    m = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', t)
    ws = [(float(a), float(b), float(c), float(d), html.unescape(w)) for a, b, c, d, w in re.findall(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', t)]
    return (float(m.group(1)), float(m.group(2)), ws) if m and len(ws) > 20 else None


def _tok(x):
    return re.sub(r"[^0-9a-z\-]", "", str(x).lower())


def text_marks(ex, pdf_path, ba_pages, targets):
    """v7: exact boxes from the PDF text layer (0 LLM calls) for NIK / periode / HK / attendance-row targets.
    Returns {id: {page, bbox}}; targets not found here (scans, odd layouts) go to the LLM box call."""
    pages = {"BA": ba_pages, "kehadiran": _pages_of(ex, "kehadiran", "attach_kehadiran")[:3],
             "transaksi": _pages_of(ex, "transaksi", "attach_transaksi")[:3]}
    cache, got = {}, {}

    def words(p):
        if p not in cache:
            cache[p] = _page_words(pdf_path, p)
        return cache[p]

    def norm(p, W, H, bs, pad=2):
        x0, y0 = min(b[0] for b in bs) - pad, min(b[1] for b in bs) - pad
        x1, y1 = max(b[2] for b in bs) + pad, max(b[3] for b in bs) + pad
        return {"page": p, "bbox": [round(max(0, x0 / W), 4), round(max(0, y0 / H), 4),
                                    round(min(1, x1 / W), 4), round(min(1, y1 / H), 4)]}

    def line(ws, w, tol=4):
        yc = (w[1] + w[3]) / 2
        return sorted((v for v in ws if abs((v[1] + v[3]) / 2 - yc) < tol), key=lambda v: v[0])

    def name_word(ws, name):
        tk = [_tok(x) for x in str(name or "").split() if _tok(x)]
        if not tk:
            return None
        for i, w in enumerate(ws):  # first + second name token in reading order (pdftotext word order)
            if _tok(w[4]) == tk[0] and (len(tk) < 2 or any(_tok(v[4]) == tk[1] for v in ws[i + 1:i + 3])):
                return w
        return None
    for t in targets:
        ref, k = t["ref"], t["criterion"]
        where = "kehadiran" if k == "5_kehadiran" else "transaksi" if k == "6_transaksi" else "BA"
        for p in pages[where]:
            pw = words(p)
            if not pw:
                continue
            W, H, ws = pw
            hit = None
            if k == "1_identitas" and ref["nik"]:
                hit = [w for w in ws if re.sub(r"\D", "", w[4]) == ref["nik"]][:1]
            elif k == "2_periode" and ref["periode"]:
                tk = [_tok(x) for x in str(ref["periode"]).split() if _tok(x)]
                for i in range(len(ws)):
                    run = ws[i:i + len(tk)]
                    if len(tk) >= 2 and [_tok(w[4]) for w in run[:2]] == tk[:2]:
                        hit = [w for j, w in enumerate(run) if j < 2 or _tok(w[4]) == tk[j]]
                        break
            elif k == "3_hk_diajukan" and isinstance(ref["hk"], int):
                v = str(ref["hk"])
                if t["worker"]:  # table row: the right-most cell with the claimed value on that worker's row
                    nw = name_word(ws, ref["name"])
                    row = line(ws, nw) if nw else []
                    vs = [w for w in row if _tok(w[4]) == v]
                    if vs:
                        hit = [vs[-1]] + [w for w in row if 0 < w[0] - vs[-1][2] < 15 and _tok(w[4]) in ("hari", "hk")][:1]
                else:  # 'Total HK : 4 HK' / 'Jumlah hari yang dibayarkan: 4 hari' / 'Kekurangan HK : 2 hari'
                    key = ("hk", "jumlah", "total", "kekurangan", "dibayar")
                    for w in ws:
                        ln = line(ws, w)
                        unit = [x for x in ln if 0 < x[0] - w[2] < 15 and _tok(x[4]) in ("hari", "hk")][:1]
                        if _tok(w[4]) == v and (unit or any(_tok(x[4]).startswith("hk") for x in ln if x is not w)) \
                                and any(_tok(x[4]).startswith(key) for x in ln if x is not w):
                            hit = [w] + unit
                            break
            elif k in ("5_kehadiran", "6_transaksi"):
                anchor = [w for w in ws if ref["nik"] and re.sub(r"\D", "", w[4]) == ref["nik"]][:1] or \
                    ([name_word(ws, ref["name"])] if name_word(ws, ref["name"]) else [])
                hit = line(ws, anchor[0]) if anchor else None
            if hit:
                got[t["id"]] = norm(p, W, H, hit)
                break
    return got


def locate_sig(ex, pdf_path, log_usage):
    """v6: boxes of the signature slots for the preview overlay. Cheap path: ONE small call on the BA page(s) only,
    and only when the TTD criterion fails for someone (nothing to highlight otherwise). Cached per file hash.
    v7: the same call also locates field-level problems (issue_marks); it then also gets the attachment pages."""
    if ex.get("issue_marks") is not None:
        return ex
    res = ba_core.evaluate(ex, "")
    need_sig = ex.get("signature_slots") is None and \
        not all(r["criteria"]["4_ttd"] == "OK" and "4_ttd" not in r.get("unsure", []) for r in res)
    targets = issue_targets(res)
    if not targets:
        ex["issue_marks"] = []
        if not need_sig:
            return ex
    ba_pages = sorted({p.get("page") for p in (ex.get("page_notes") or [])
                       if isinstance(p, dict) and p.get("what") == "BA" and isinstance(p.get("page"), int)})[:4] or [1]
    if not targets:  # v6 path, unchanged (same cache file)
        cf = DATA / "sig_cache" / f"{ba_core.file_hash(pdf_path)}_{engines.SIG_VERSION}.json"
        if cf.exists():
            ex["signature_slots"] = json.loads(cf.read_text())
            return ex
        slots = engines.locate_signatures(pdf_path, ba_pages, log_usage)
        if slots is not None:
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(slots, ensure_ascii=False))
            ex["signature_slots"] = slots
        return ex
    pages = set(ba_pages)
    if any(t["where"].startswith("kehadiran") for t in targets):
        pages |= set(_pages_of(ex, "kehadiran", "attach_kehadiran")[:3])
    if any(t["where"].startswith("transaksi") for t in targets):
        pages |= set(_pages_of(ex, "transaksi", "attach_transaksi")[:3])
    pages = sorted(pages)[:6]
    sig = hashlib.sha1(json.dumps([need_sig, pages, targets], sort_keys=True).encode()).hexdigest()[:10]
    cf = DATA / "sig_cache" / f"{ba_core.file_hash(pdf_path)}_v7_{sig}.json"
    if cf.exists():
        got = json.loads(cf.read_text())
    else:
        found = text_marks(ex, pdf_path, ba_pages, targets)  # exact, free; the LLM only gets what text can't find
        rest = [t for t in targets if t["id"] not in found]
        r = engines.locate_signatures(pdf_path, pages, log_usage, issues=rest, need_sig=need_sig) \
            if rest or need_sig else ([], {})
        if r is None:
            return ex  # no layer answered: no marks, retried on the next processing
        got = {"slots": r[0], "issues": {**{str(k): v for k, v in r[1].items()},
                                         **{str(k): {**v, "src": "text"} for k, v in found.items()}}}
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(got, ensure_ascii=False))
    if need_sig:
        ex["signature_slots"] = got["slots"]
    ex["issue_marks"] = [{"criterion": t["criterion"], "worker": t["worker"], "label": t["label"],
                          **got["issues"][str(t["id"])]} for t in targets if str(t["id"]) in got["issues"]]
    return ex


def process(ba_id):
    row = DB.q("SELECT * FROM ba WHERE id=?", (ba_id,), one=True)
    if not row:
        return
    eng = engines.get_engine()
    try:
        storage.ensure_local(row["path"])
        prep = preprocess.prepare(row["path"], PAGE_CAP)
        if DB.llm_calls_today() >= MAX_CALLS_DAY:
            raise engines.EngineError("daily LLM call cap reached")
        ex = engines.extract_chunked(eng, row["path"], prep, lambda **kw: DB.log_usage(ba_id=ba_id, **kw),
                                     PAGE_CAP, DATA / "chunk_cache")
        try:
            ex = reread_unsure(ex, row["path"], lambda **kw: DB.log_usage(ba_id=ba_id, **kw))
        except Exception as e:  # the re-read is a bonus; never lose the main extraction over it
            log.warning("reread failed ba=%s: %s", ba_id, type(e).__name__)
        try:
            ex = locate_sig(ex, row["path"], lambda **kw: DB.log_usage(ba_id=ba_id, **kw))
        except Exception as e:  # boxes are display-only
            log.warning("sigbox failed ba=%s: %s", ba_id, type(e).__name__)
        used = (ex.get("_meta") or {}).get("engine") or eng.name
        model = (ex.get("_meta") or {}).get("model")
        DB.set_result(ba_id, "done", build_result(ex, prep), ex, engine=f"{used}:{model}" if model else used)
        DB.x("UPDATE ba SET retry_at=NULL, retries=0 WHERE id=?", (ba_id,))
    except engines.EngineError as e:  # every free layer exhausted: park, retry later; never fail, never pay
        n = (row.get("retries") or 0) + 1
        mins = min(RETRY_MAX_MIN, 2 ** n)
        import datetime as dt
        at = (dt.datetime.now().astimezone() + dt.timedelta(minutes=mins)).isoformat(timespec="seconds")
        log.info("ba=%s ditunda (retry %d in %d min)", ba_id, n, mins)
        DB.x("UPDATE ba SET status='ditunda', retry_at=?, retries=?, error=?, updated_at=? WHERE id=?",
             (at, n, str(e)[:500], dbm.now(), ba_id))
    except Exception as e:
        log.warning("processing failed ba=%s: %s", ba_id, type(e).__name__)
        DB.set_result(ba_id, "error", error=f"{type(e).__name__}: {e}"[:500], engine=eng.name)


async def enqueue(ba_id):
    pending.append(ba_id)
    await queue.put(ba_id)


async def worker():
    while True:
        ba_id = await queue.get()
        try:
            pending.remove(ba_id)
        except ValueError:
            pass
        running.add(ba_id)
        try:
            await run_in_threadpool(process, ba_id)
        finally:
            running.discard(ba_id)
            queue.task_done()


def cleanup_once():
    """Delete non-submitted uploads (rows + PDFs) older than RETENTION_DAYS. Usage log is kept."""
    if RETENTION_DAYS <= 0:
        return 0
    import datetime as dt
    cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    rows = DB.q("SELECT id, path FROM ba WHERE created_at < ? AND id NOT IN (SELECT ba_id FROM submission) "
                "AND status != 'processing'", (cutoff,))
    for r in rows:
        Path(r["path"]).unlink(missing_ok=True)
        storage.delete(r["path"])
        DB.x("DELETE FROM ba WHERE id=?", (r["id"],))
    if rows:
        log.info("retention cleanup removed %d uploads", len(rows))
    return len(rows)


async def retry_loop():
    """Re-queue 'ditunda' BAs whose retry_at has passed."""
    while True:
        try:
            for r in DB.q("SELECT id FROM ba WHERE status='ditunda' AND retry_at <= ? ORDER BY created_at", (dbm.now(),)):
                if r["id"] not in pending and r["id"] not in running:
                    DB.x("UPDATE ba SET status='processing', updated_at=? WHERE id=?", (dbm.now(), r["id"]))
                    await enqueue(r["id"])
        except Exception as e:
            log.warning("retry loop failed: %s", e)
        await asyncio.sleep(30)


def reevaluate_old():
    """Results built with older rules are rebuilt from the stored extraction (local only, zero LLM calls)."""
    for r in DB.q("SELECT id, path, extraction_json FROM ba WHERE status='done' AND extraction_json IS NOT NULL "
                  "AND (result_json IS NULL OR result_json NOT LIKE ?)", (f'%"rules": "{RULES_VERSION}"%',)):
        try:
            storage.ensure_local(r["path"])
            prep = preprocess.prepare(r["path"], PAGE_CAP)
            DB.x("UPDATE ba SET result_json=? WHERE id=?",
                 (json.dumps(build_result(json.loads(r["extraction_json"]), prep), ensure_ascii=False), r["id"]))
        except Exception as e:
            log.warning("re-evaluate ba=%s failed: %s", r["id"], type(e).__name__)


async def cleanup_loop():
    while True:
        try:
            await run_in_threadpool(cleanup_once)
        except Exception as e:  # never crash the loop
            log.warning("cleanup failed: %s", e)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup():
    for _ in range(CONCURRENCY):
        asyncio.create_task(worker())
    # results from an older result format (no 'jenis') are re-evaluated once (extraction cache makes this cheap)
    DB.x("UPDATE ba SET status='processing' WHERE status='done' AND (result_json IS NULL OR result_json NOT LIKE '%\"jenis\"%')")
    for r in DB.q("SELECT id FROM ba WHERE status='processing' ORDER BY created_at"):
        await enqueue(r["id"])  # restart-safe: re-queue unfinished jobs
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(retry_loop())
    asyncio.get_running_loop().run_in_executor(None, reevaluate_old)
    if ADMIN_TOKEN in ("", "CHANGE_ME") or len(ADMIN_TOKEN) < 8:
        log.warning("ADMIN_TOKEN not set or too short: admin endpoints disabled")


# ------------------------------------------------------------------ helpers

def client_id(req: Request):
    if TRUST_PROXY and req.headers.get("x-forwarded-for"):
        return req.headers["x-forwarded-for"].split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def base_url(req: Request):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = req.headers.get("x-forwarded-proto") if TRUST_PROXY else None
    host = req.headers.get("x-forwarded-host") if TRUST_PROXY else None
    return f"{proto or req.url.scheme}://{host or req.headers.get('host') or req.url.netloc}"


def safe_filename(name):
    name = Path(name or "ba.pdf").name
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\w .()\-]", "_", name).strip(" .") or "ba.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[-120:]


def link_for(ba_id):
    return DB.q("SELECT * FROM public_link WHERE ba_id=? AND revoked_at IS NULL", (ba_id,), one=True)


def public(row, req: Request):
    sub = DB.q("SELECT 1 FROM submission WHERE ba_id=?", (row["id"],), one=True)
    link = link_for(row["id"]) if sub else None
    out = {"id": row["id"], "filename": row["filename"], "status": row["status"], "error": None,
           "workers": [], "criteria": [], "alerts": [], "verdict": "INVALID", "can_submit": False,
           "submitted": bool(sub), "quality": "good", "jenis": None, "tanggal": [], "nominal": None,
           "pages": row["pages"], "page_notes": [], "retry_at": None,
           "public_url": f"{base_url(req)}/f/{link['token']}" if link else None, "queue_position": None}
    if row["status"] == "processing":
        ids = list(pending)
        out["queue_position"] = 0 if row["id"] in running else (ids.index(row["id"]) + 1 if row["id"] in ids else None)
    if row["status"] == "done" and row["result_json"]:
        r = json.loads(row["result_json"])
        for k in ("workers", "criteria", "alerts", "verdict", "quality", "jenis", "tanggal", "nominal",
                  "pages", "page_notes", "signature_slots", "issue_marks", "missing_attachments"):
            if k in r:
                out[k] = r[k]
        out.setdefault("signature_slots", [])  # v6: [] = no unsigned slot located (or not needed)
        out.setdefault("issue_marks", [])  # v7: red boxes for field-level problems
        out.setdefault("missing_attachments", [])  # v7: 'transaksi' / 'kehadiran' absent -> red placeholder page
        out["criteria"] = [{k: v for k, v in c.items() if k in ("key", "label", "status", "note")} for c in out["criteria"]]
        out["alerts"] = [a for a in out["alerts"] if "keyakinan" not in a]  # pre-v3 results
        out["can_submit"] = r["verdict"] == "VALID" and r["quality"] == "good" and not out["submitted"]
    if row["status"] == "ditunda":
        out["alerts"] = [DITUNDA_MSG]
        out["retry_at"] = row.get("retry_at")
    if row["status"] == "error":
        out["error"] = "Gagal memproses BA."
        out["alerts"] = ["Gagal memproses BA. Coba upload ulang beberapa saat lagi."]
    return out


def get_row(ba_id):
    if not re.fullmatch(r"[0-9a-f\-]{36}", ba_id or ""):
        raise HTTPException(404, "BA tidak ditemukan.")
    row = DB.q("SELECT * FROM ba WHERE id=?", (ba_id,), one=True)
    if not row:
        raise HTTPException(404, "BA tidak ditemukan.")
    return row


def do_submit(row, submitter, ip, req):
    """Returns (submitted, reason, public_url). Never submits INVALID / unfinished / poor-quality BAs."""
    pub = public(row, req)
    if not pub["submitted"]:
        if row["status"] != "done":
            return False, "BA belum selesai diperiksa.", None
        if pub["verdict"] != "VALID":
            return False, "BA belum lengkap/valid, perlu revisi sebelum submit.", None
        if pub["quality"] != "good":
            return False, "Foto/scan kurang jelas, mohon upload ulang.", None
        DB.x("INSERT OR IGNORE INTO submission(ba_id, submitter, submitter_ip, submitted_at) VALUES (?,?,?,?)",
             (row["id"], submitter, ip, dbm.now()))
    if not link_for(row["id"]) and not DB.q("SELECT 1 FROM public_link WHERE ba_id=?", (row["id"],), one=True):
        DB.x("INSERT INTO public_link(token, ba_id, created_at) VALUES (?,?,?)",
             (secrets.token_urlsafe(18), row["id"], dbm.now()))  # 18 bytes -> 24 url-safe chars
    link = link_for(row["id"])
    return True, None, (f"{base_url(req)}/f/{link['token']}" if link else None)


# ------------------------------------------------------------------ API

@app.post("/api/upload", status_code=202)
async def upload(request: Request, file: UploadFile = File(...)):
    buf = bytearray()
    while chunk := await file.read(1 << 20):
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise HTTPException(413, f"File terlalu besar (maks {MAX_BYTES // 1048576}MB).")
    if not bytes(buf[:1024]).lstrip().startswith(b"%PDF-"):
        raise HTTPException(415, "File harus PDF.")
    sha = hashlib.sha256(buf).hexdigest()
    existing = DB.q("SELECT * FROM ba WHERE sha256=?", (sha,), one=True)
    if existing:  # dedupe: zero LLM calls
        if existing["status"] == "error":
            DB.x("UPDATE ba SET status='processing', error=NULL, updated_at=? WHERE id=?", (dbm.now(), existing["id"]))
            await enqueue(existing["id"])
            return {"id": existing["id"], "status": "processing"}
        return JSONResponse({"id": existing["id"], "status": existing["status"]}, status_code=202)
    if len(pending) >= MAX_QUEUE:
        raise HTTPException(503, "Server sedang sibuk, coba lagi beberapa menit lagi.")
    if not DB.bump_quota("__global__", GLOBAL_QUOTA):
        raise HTTPException(429, "Kuota harian server sudah habis, coba lagi besok.")
    if not DB.bump_quota(client_id(request), QUOTA):
        raise HTTPException(429, f"Kuota upload harian ({QUOTA} BA) sudah habis.")
    path = UPLOADS / f"{sha}.pdf"
    path.write_bytes(buf)
    pages = preprocess.page_count(path)
    if pages == 0 or pages > MAX_PDF_PAGES:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "PDF tidak bisa dibaca (rusak/terkunci)." if pages == 0
                            else f"PDF terlalu banyak halaman ({pages}, maks {MAX_PDF_PAGES}).")
    try:
        storage.put(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(503, "Penyimpanan sementara bermasalah, coba lagi.") from e
    ba_id = str(uuid.uuid4())
    t = dbm.now()
    DB.x("INSERT INTO ba(id, sha256, filename, path, pages, status, uploader_ip, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
         (ba_id, sha, safe_filename(file.filename), str(path), pages, "processing", client_id(request), t, t))
    await enqueue(ba_id)
    return {"id": ba_id, "status": "processing"}


@app.get("/api/ba/{ba_id}")
def get_ba(ba_id: str, request: Request):
    return public(get_row(ba_id), request)


@app.get("/api/ba/{ba_id}/file")
def get_file(ba_id: str):
    row = get_row(ba_id)
    storage.ensure_local(row["path"])
    return FileResponse(row["path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{row["sha256"][:12]}.pdf"'})


async def body_json(request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@app.post("/api/ba/{ba_id}/submit")
async def submit(ba_id: str, request: Request):
    row = get_row(ba_id)
    body = await body_json(request)
    ok, reason, url = do_submit(row, str(body.get("submitter") or "").strip()[:120], client_id(request), request)
    if not ok:
        raise HTTPException(409, reason)
    return {"submitted": True, "public_url": url}


@app.post("/api/submit-bulk")
async def submit_bulk(request: Request):
    body = await body_json(request)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or len(ids) > 200:
        raise HTTPException(400, "ids harus list (maks 200).")
    submitter = str(body.get("submitter") or "").strip()[:120]
    results = []
    for i in ids:
        try:
            row = get_row(str(i))
        except HTTPException:
            results.append({"id": i, "submitted": False, "reason": "BA tidak ditemukan.", "public_url": None})
            continue
        ok, reason, url = do_submit(row, submitter, client_id(request), request)
        results.append({"id": i, "submitted": ok, "reason": reason, "public_url": url})
    return {"results": results}


@app.get("/api/health")
def health():
    if time.time() - _health["t"] > 30:
        eng = engines.get_engine()
        detail = eng.health_detail()
        if not engines.claude_allowed():
            detail["claude"] = "disabled"
        _health.update(t=time.time(), llm="ok" if "ok" in detail.values() else "down", engine=eng.name,
                       engines=detail)
    return {"ok": True, "llm": _health["llm"], "engine": _health.get("engine"), "engines": _health.get("engines"),
            "queue": len(pending), "running": len(running),
            "ditunda": DB.q("SELECT COUNT(*) n FROM ba WHERE status='ditunda'", one=True)["n"]}


# ------------------------------------------------------------------ public links

NOINDEX = {"X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "private, max-age=300",
           "Referrer-Policy": "no-referrer"}


@app.get("/f/{token}")
def public_file(token: str, view: int = 0, dl: int = 0):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{22,64}", token):
        raise HTTPException(404, "Tidak ditemukan.")
    link = DB.q("SELECT * FROM public_link WHERE token=? AND revoked_at IS NULL", (token,), one=True)
    row = DB.q("SELECT * FROM ba WHERE id=?", (link["ba_id"],), one=True) if link else None
    if not row or not storage.ensure_local(row["path"]):
        raise HTTPException(404, "Tidak ditemukan.")
    if view:
        res = json.loads(row["result_json"] or "{}")
        ws = res.get("workers") or [{}]
        w = ws[0]
        who = f"{len(ws)} pekerja" if len(ws) > 1 else (w.get("nama") or "")
        title = html.escape(f"BA {who} - {w.get('periode') or ''}".strip(" -"))
        page = f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title>
<style>body{{margin:0;font:15px system-ui,sans-serif;background:#f4f4f5;color:#111}}header{{display:flex;gap:12px;align-items:center;
justify-content:space-between;padding:10px 16px;background:#fff;border-bottom:1px solid #ddd;flex-wrap:wrap}}h1{{font-size:16px;margin:0}}
a.btn{{padding:6px 12px;border:1px solid #2563eb;border-radius:6px;color:#2563eb;text-decoration:none}}
iframe{{border:0;width:100%;height:calc(100vh - 56px);display:block}}</style></head><body>
<header><h1>{title}</h1><span>{html.escape(row['filename'])} &middot; <a class="btn" href="/f/{token}?dl=1">Download PDF</a></span></header>
<iframe src="/f/{token}" title="BA"></iframe></body></html>"""
        return HTMLResponse(page, headers=NOINDEX)
    disp = "attachment" if dl else "inline"
    return FileResponse(row["path"], media_type="application/pdf",
                        headers={**NOINDEX, "Content-Disposition": f'{disp}; filename="{safe_filename(row["filename"])}"'})


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ------------------------------------------------------------------ admin

def require_admin(req: Request):
    ip = client_id(req)
    now_ = time.time()
    fails = [t for t in _admin_fail[ip] if now_ - t < 900]
    _admin_fail[ip] = fails
    if len(fails) >= 10:
        raise HTTPException(429, "Terlalu banyak percobaan kode admin. Coba lagi 15 menit lagi.")
    tok = req.headers.get("x-admin-token", "")
    if ADMIN_TOKEN in ("", "CHANGE_ME") or len(ADMIN_TOKEN) < 8 or not secrets.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
        fails.append(now_)
        raise HTTPException(401, "Kode admin salah.")


def submitted_rows():
    return DB.q("SELECT s.submitted_at, s.submitter, s.submitter_ip, l.token, l.revoked_at, b.* FROM submission s "
                "JOIN ba b ON b.id=s.ba_id LEFT JOIN public_link l ON l.ba_id=b.id ORDER BY s.submitted_at")


@app.get("/api/admin/submissions")
def admin_list(request: Request):
    require_admin(request)
    out = []
    for r in submitted_rows():
        res = json.loads(r["result_json"] or "{}")
        out.append({"id": r["id"], "submitted_at": r["submitted_at"], "submitter": r["submitter"], "file": r["filename"],
                    "jenis": res.get("jenis"), "workers": res.get("per_worker", []), "verdict": res.get("verdict"),
                    "alerts": [a for a in res.get("alerts", []) if "keyakinan" not in a],
                    "public_url": f"{base_url(request)}/f/{r['token']}" if r["token"] and not r["revoked_at"] else None,
                    "revoked": bool(r["revoked_at"])})
    return out


@app.get("/api/admin/export.csv")
def admin_csv(request: Request):
    require_admin(request)
    cols = ["submitted_at", "submitter", "file", "nik", "nama", "jabatan", "lokasi", "periode", "hk_diajukan"] + \
        ba_core.CRITERIA + ["verdict", "alerts", "jenis", "public_url", "worker_status", "worker_problems"]
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(cols)
    for r in submitted_rows():
        res = json.loads(r["result_json"] or "{}")
        url = f"{base_url(request)}/f/{r['token']}" if r["token"] and not r["revoked_at"] else ""
        for pw in res.get("per_worker", []):
            w.writerow([r["submitted_at"], r["submitter"], r["filename"]] +
                       [pw.get(k) for k in ("nik", "nama", "jabatan", "lokasi", "periode", "hk_diajukan")] +
                       [pw["criteria"].get(k) for k in ba_core.CRITERIA] +
                       [res.get("verdict"), " | ".join(res.get("alerts", [])), res.get("jenis"), url,
                        pw.get("status") or pw.get("verdict"), " | ".join(pw.get("problems") or [])])
    return Response("﻿" + f.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="ba_submissions.csv"'})


@app.post("/api/admin/revoke/{ba_id}")
def admin_revoke(ba_id: str, request: Request):
    require_admin(request)
    get_row(ba_id)
    DB.x("UPDATE public_link SET revoked_at=? WHERE ba_id=? AND revoked_at IS NULL", (dbm.now(), ba_id))
    return {"id": ba_id, "revoked": True}


@app.get("/api/admin/usage")
def admin_usage(request: Request, days: int = 30, limit: int = 100):
    require_admin(request)
    daily = DB.q("SELECT substr(ts,1,10) day, COUNT(*) attempts, SUM(status IN ('ok','bad_json')) calls, "
                 "SUM(status='ok') ok, SUM(status NOT IN ('ok','bad_json')) errors, "
                 "COALESCE(SUM(prompt_tokens),0) prompt_tokens, COALESCE(SUM(completion_tokens),0) completion_tokens, "
                 "ROUND(COALESCE(SUM(cost_usd),0),4) cost_usd, COUNT(DISTINCT ba_id) bas "
                 "FROM llm_usage GROUP BY 1 ORDER BY 1 DESC LIMIT ?", (max(1, min(days, 365)),))
    uploads = DB.q("SELECT substr(created_at,1,10) day, COUNT(*) uploads, SUM(status='done') done, SUM(status='error') errors "
                   "FROM ba GROUP BY 1 ORDER BY 1 DESC LIMIT ?", (max(1, min(days, 365)),))
    now = time.time()
    models = DB.q("SELECT engine, model, COUNT(*) attempts, SUM(status IN ('ok','bad_json')) calls, SUM(status='ok') ok, "
                  "SUM(status='http_429') n429, MAX(CASE WHEN status='http_429' THEN ts END) last_429 "
                  "FROM llm_usage WHERE ts >= ? GROUP BY engine, model ORDER BY engine, model", (dbm.today(),))
    for m in models:
        m["cooldown_s"] = max(0, int(engines._COOLDOWN.get((m["engine"], m["model"]), 0) - now))
    return {"today_calls": DB.llm_calls_today(), "cap_per_day": MAX_CALLS_DAY, "models_today": models, "daily": daily, "uploads_daily": uploads,
            "recent": DB.q("SELECT id, ba_id, ts, engine, model, attempt, status, prompt_tokens, completion_tokens, images, "
                           "latency_ms, cost_usd FROM llm_usage ORDER BY id DESC LIMIT ?", (max(1, min(limit, 2000)),))}


# ------------------------------------------------------------------ admin: LLM key manager (v3)

def _provider(p):
    if p not in engines.PROVIDERS:
        raise HTTPException(404, "Provider tidak dikenal.")
    return p


def _key_info(p, probe=True):
    m = KS.meta(p)
    env_key = (os.environ.get(p.upper() + "_API_KEY") or "").strip()
    layer = next((e for e in engines.layers() if e.name == p), None)
    return {"provider": p, "set": bool(m or env_key), "source": "admin" if m else ("env" if env_key else None),
            "last4": m["last4"] if m else (env_key[-4:] if env_key else None), "updated_at": m["updated_at"] if m else None,
            "layer_status": (layer.status() if probe else None) if layer else None}


@app.get("/api/admin/keys")
def admin_keys(request: Request):
    require_admin(request)
    return [_key_info(p) for p in engines.PROVIDERS]


@app.put("/api/admin/keys/{provider}")
async def admin_key_put(provider: str, request: Request):
    require_admin(request)
    _provider(provider)
    key = str((await body_json(request)).get("key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]{16,300}", key):
        raise HTTPException(400, "Format key tidak valid.")
    KS.set(provider, key)
    engines.reset_layer(provider)
    _health["t"] = 0
    log.info("admin: key for %s replaced", provider)
    return _key_info(provider, probe=False)


@app.delete("/api/admin/keys/{provider}")
def admin_key_delete(provider: str, request: Request):
    require_admin(request)
    _provider(provider)
    KS.delete(provider)
    engines.reset_layer(provider)
    _health["t"] = 0
    log.info("admin: key for %s deleted", provider)
    return _key_info(provider, probe=False)


@app.post("/api/admin/keys/{provider}/test")
def admin_key_test(provider: str, request: Request):
    require_admin(request)
    return engines.test_key(_provider(provider))


@app.get("/admin")
def admin_page():
    p = FRONTEND / "admin.html"
    return FileResponse(p, headers={"X-Robots-Tag": "noindex"}) if p.exists() else HTMLResponse("<h1>Admin</h1>")


if not FRONTEND.exists():
    FRONTEND.mkdir(parents=True)
    (FRONTEND / "index.html").write_text("<!doctype html><meta charset=utf-8><title>BA Portal</title><h1>BA Portal</h1><p>Frontend belum tersedia.</p>")
app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
