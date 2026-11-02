"""ba_core: shared extraction prompt/schema + deterministic BA rule checks (SPEC.md v2).

Imported by ba_validate.py (CLI, claude -p backend) and the BA portal (_work/ba_portal, OpenAI-compatible backend).
"""
import datetime as dt, hashlib, json, re, subprocess
from pathlib import Path

PROMPT_VERSION = "v5"
CRITERIA = ["1_identitas", "2_periode", "3_hk_diajukan", "4_ttd", "5_kehadiran", "6_transaksi"]

SCHEMA_DOC = r'''{
 "jenis": "kurang_hk"|"kurang_nominal"|"single_tanggal"|"lainnya",   // kind of shortfall, decide from the BA body:
      // kurang_hk = some work DAYS were not paid/counted; kurang_nominal = days were paid but at a lower RATE than due
      // (rate/upah seharusnya vs diterima); single_tanggal = the BA disputes exactly ONE specific date;
      // DEFAULT is kurang_hk for any BA that lists work days or HK counts; lainnya ONLY if it is not about unpaid days/rate/date at all
 "jenis_reason": str,
 "page_notes": [ {"page": int, "what": "BA"|"kehadiran"|"transaksi"|"lain"} ],   // what each page is
 "confidence": {"identitas": 0-1, "periode": 0-1, "hk": 0-1, "ttd": 0-1, "kehadiran": 0-1, "transaksi": 0-1},
      // your certainty that you read each part correctly (low when blurred, cut off, ambiguous)
 "workers": [ {                       // one entry per HL worker the BA is about (NOT the preparer/kordel)
   "nik": str|null, "nama": str|null, "jabatan": str|null, "lokasi": str|null,   // lokasi = penempatan / cabang / DC / toko
   "periode_text": str|null,           // period of the pay shortfall exactly as printed in the BA body/header (null if the BA itself states none)
   "periode_start": "YYYY-MM-DD"|null, "periode_end": "YYYY-MM-DD"|null,   // keep impossible dates as printed text only, e.g. 31 Sept -> end null
   "hk_figures": [ {"label": str, "value": int} ],   // EVERY day-count printed in the BA body for this worker, label copied verbatim
                                                     // (e.g. "Jumlah hari yang dibayarkan", "Total HK", "HK kurang", "Total masuk hari", "selisih")
   "hk_claimed": int|null,             // number of days the shortfall concerns (diajukan / Total HK / jumlah hari kurang); null if no figure is explicitly the claim.
                                       // If the BA disputes ONE date but prints a period total (e.g. "Jumlah HK: 15"), hk_claimed = 1
   "days_listed": ["YYYY-MM-DD"],      // work dates explicitly listed in the BA body (not attachments); [] if none listed
   "tanggal_diajukan": ["YYYY-MM-DD"],  // date(s) the shortfall is about, as stated anywhere in the BA text (e.g. "tidak absen di tgl 9 Oktober")
   "nominal": {"rate_seharusnya": int|null, "rate_diterima": int|null, "hk_nominal": int|null,   // rupiah integers; hk_nominal = days the rate applies to
               "nominal_seharusnya": int|null, "nominal_diterima": int|null, "nominal_kurang": int|null},   // all null if no amounts printed
   // ---- the 5 fields below ONLY when the BA lists 2+ workers (table of workers); omit them for a single-worker BA ----
   "hk_kerja": int|null, "hk_dibayar": int|null,   // this worker's 'Hari Kerja' and 'Sudah dibayar' cells; hk_claimed = 'Kekurangan' cell
   "ttd_hl": "yes"|"no"|"unclear",     // THIS worker's own signature cell (e.g. column 'Tanda tangan Mitra'); "no" if the cell is blank
   "att": {"found": bool, "nik_shown": str|null, "days_present": ["YYYY-MM-DD"], "total": int|null},
        // THIS worker's rows in the attendance attachment(s) (match by NIK/name): every date shown for him, and his printed total
        // (e.g. rekap 'Jumlah absensi'); found=false if he is not in the attachment
   "trx": {"found": bool, "days": [ {"date": "YYYY-MM-DD", "count": int|null} ], "total": int|null}
        // THIS worker's rows in the transaction (TRX/order) attachment; found=false if absent
 } ],
 // MANY WORKERS: list EVERY worker row of the table (no truncation, tables may continue on later pages); shared BA facts
 // apply to every worker: periode; jabatan = the role the BA gives the group (e.g. "mitra HL", "PHL") if no per-row column;
 // lokasi = site/DC named in the BA, else this worker's clock-in location in the attendance attachment (e.g. "SCLJKT - SIRCLO JAKARTA").
 // Top-level attach_* then describe the attachment as a whole.
 "other_persons": [ {"nama": str, "nik": str|null, "role": str} ],  // e.g. preparer/kordel in a header block
 "signatures": {
   "hl": "yes"|"no"|"unclear",          // handwritten ink / signature image above the HL worker's name
   "pejabat_slots": [ {"role": str, "name": str|null, "signed": "yes"|"no"|"unclear"} ],  // every official signer slot (supervisor, AM, kordel, mengetahui, menyetujui...)
   "evidence": str                      // page + what you saw
 },
 "attach_kehadiran": {                  // attendance attachment (absensi / presensi / rekap kehadiran) from the DMS app or a recap of it
   "present": bool, "pages": [int],
   "format": "per_day"|"summary_only"|"none",   // per_day = shows which individual dates were worked (grid with H/1, presensi rows, clock-in list...)
   "nik_shown": str|null, "nama_shown": str|null,
   "days_present": ["YYYY-MM-DD"],      // dates marked as worked/present
   "total": int|null, "note": str
 },
 "attach_transaksi": {                  // transaction attachment (TRX / order / delivery counts per day)
   "present": bool, "pages": [int],
   "format": "per_day_table"|"text_line"|"empty"|"none",  // empty = TRX table/row exists but has no counts; text_line = a single free-text order line, not a table
   "nik_shown": str|null,
   "days": [ {"date": "YYYY-MM-DD", "count": int|null} ],   // every day column/row shown with its count (null if blank)
   "total": int|null, "note": str
 },
 "notes": str
}'''

PROMPT = """You extract data from an Indonesian "Berita Acara (BA) kekurangan gaji" PDF for an HL (harian lepas) worker.
Page images (read EVERY one with the Read tool, in order; page N = file ending -N.png): {images}
{text_block}
Return ONLY one JSON object (no markdown fences, no prose) following this schema (// comments explain fields):
{schema}
Rules: copy values exactly as printed; never invent or guess - use null / [] / "unclear" when not visible.
Dates as ISO YYYY-MM-DD; when an attachment shows only day numbers, take month/year from its header or the BA period and say so in note; if month/year is truly unknown give the bare day number "DD".
Signatures: "yes" only if an actual handwritten mark or signature image is visible above/at that slot; a printed name alone is "no".
"""


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def slugify(name):
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def file_hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:20]


def pdf_pages(pdf):
    m = re.search(r"Pages:\s+(\d+)", sh(["pdfinfo", str(pdf)]).stdout)
    return int(m.group(1)) if m else 0


def text_layer(pdf):
    t = sh(["pdftotext", "-layout", str(pdf), "-"]).stdout
    return t if len(re.sub(r"\s", "", t)) > 60 else ""


def parse_json(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError as e:
        if "Extra data" not in str(e):
            raise
        return json.JSONDecoder().raw_decode(s[i:])[0]  # v6: valid object followed by stray text / a 2nd object


def d(s):
    try:
        return dt.date.fromisoformat(s) if s else None
    except Exception:
        return None


def dkey(s):
    """ISO date -> 'YYYY-MM-DD'; bare day number (month unknown) -> '--DD'; else None."""
    s = str(s or "").strip()
    if d(s):
        return s
    if re.fullmatch(r"\d{1,2}", s):
        return f"--{int(s):02d}"
    return None


def has_day(day, pool):
    return day in pool or any(x[-2:] == day[-2:] for x in pool if x.startswith("--") or day.startswith("--"))


def yes(v):
    return v is True or str(v).lower() in ("yes", "true")


def norm_nik(n):
    return re.sub(r"\D", "", n or "")


JENIS = ("kurang_hk", "kurang_nominal", "single_tanggal", "lainnya")


def _conf(v, default=0.8):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    digits = re.sub(r"[^\d]", "", str(v).split(",")[0])
    return int(digits) if digits else None


def classify(ex, w, problems):
    """LLM proposes jenis; deterministic guards keep it consistent with the extracted facts."""
    jenis = str(ex.get("jenis") or "").strip().lower()
    nom = {k: _int((w.get("nominal") or {}).get(k)) for k in
           ("rate_seharusnya", "rate_diterima", "hk_nominal", "nominal_seharusnya", "nominal_diterima", "nominal_kurang")}
    tanggal = sorted({dkey(x) for x in (w.get("tanggal_diajukan") or []) if d(dkey(x))})
    listed = sorted({dkey(x) for x in (w.get("days_listed") or []) if d(dkey(x))})
    claimed = w.get("hk_claimed")
    if jenis not in JENIS:
        jenis = "lainnya" if jenis else "kurang_hk"
    if jenis == "kurang_nominal" and not (nom["rate_seharusnya"] and nom["rate_diterima"] and nom["rate_seharusnya"] != nom["rate_diterima"]):
        jenis = "kurang_hk"  # no rate difference printed: amounts follow from missing days
    if jenis == "kurang_hk" and claimed == 1 and (len(tanggal) == 1 or len(listed) == 1):
        jenis = "single_tanggal"
    if jenis == "single_tanggal" and not tanggal and len(listed) == 1:
        tanggal = listed
    if jenis == "lainnya" and (listed or w.get("hk_figures") or claimed is not None):
        jenis = "kurang_hk"  # standard template (work days / HK printed) even if the shortfall figure is implicit
    if jenis == "lainnya":
        problems.append("Jenis BA tidak dikenali; diperiksa dengan 6 kriteria standar")
    return jenis, tanggal, nom


def check_single(tanggal, claimed, figs, problems):
    if not tanggal:
        return "MISSING", "BA membahas satu tanggal tetapi tanggalnya tidak disebut"
    if len(tanggal) > 1:
        return "PARTIAL", f"lebih dari satu tanggal disebut: {', '.join(tanggal[:4])}"
    if claimed not in (None, 1):
        problems.append(f"HK diajukan tertulis {claimed}, padahal BA hanya membahas tanggal {tanggal[0]}")
        return "PARTIAL", f"HK diajukan {claimed} != 1 untuk tanggal {tanggal[0]}"
    other = [f for f in figs if f["value"] != 1]
    if other:
        problems.append("Angka HK lain di BA (" + "; ".join(f"{f['label']}={f['value']}" for f in other) +
                        ") dianggap total periode, bukan HK diajukan")
    return "OK", f"tanggal {tanggal[0]} diajukan (1 HK)"


def check_nominal(n, problems):
    s_, r_, k_ = n["nominal_seharusnya"], n["nominal_diterima"], n["nominal_kurang"]
    rs, rd, hk = n["rate_seharusnya"], n["rate_diterima"], n["hk_nominal"]
    if s_ is None and rs and hk:
        s_ = rs * hk
    if r_ is None and rd and hk:
        r_ = rd * hk
    if k_ is None:
        return "MISSING", "nominal kekurangan tidak tercantum"
    if s_ is None or r_ is None:
        return "PARTIAL", f"kurang Rp{k_:,} tercantum, tetapi nominal seharusnya/diterima tidak lengkap"
    if abs((s_ - r_) - k_) > 1:
        problems.append(f"Hitungan nominal tidak cocok: {s_:,} - {r_:,} = {s_ - r_:,}, tertulis kurang {k_:,}")
        return "PARTIAL", f"seharusnya Rp{s_:,} - diterima Rp{r_:,} != kurang Rp{k_:,}"
    if rs and rd and hk and abs((rs - rd) * hk - k_) > 1:
        problems.append(f"(rate {rs:,} - {rd:,}) x {hk} HK = {(rs - rd) * hk:,} != kurang {k_:,}")
        return "PARTIAL", f"selisih rate x {hk} HK tidak sama dengan kurang Rp{k_:,}"
    return "OK", f"seharusnya Rp{s_:,} - diterima Rp{r_:,} = kurang Rp{k_:,}" + (f" ({hk} HK)" if hk else "")


def worker_attach(w, kh, tx):
    """Multi-worker BA (v4): the attendance/TRX rows of THIS worker (w.att / w.trx) replace the whole-attachment day lists.
    A worker missing from an attachment that exists gets an empty day list (-> fails that criterion)."""
    kh, tx = dict(kh), dict(tx)
    a, t = w.get("att"), w.get("trx")
    if isinstance(a, dict) and kh.get("present"):
        found = a.get("found") is not False and (a.get("days_present") or a.get("total") is not None)
        kh.update(days_present=a.get("days_present") or [], total=a.get("total"), nik_shown=a.get("nik_shown"),
                  _not_found=not found)
    if isinstance(t, dict) and tx.get("present") and tx.get("format") not in ("none", "empty"):
        found = t.get("found") is not False and (t.get("days") or t.get("total") is not None)
        tx.update(days=t.get("days") or [], total=t.get("total"), nik_shown=None, _not_found=not found)
    return kh, tx


MONTHS = {m: i for i, ms in enumerate([("januari", "january", "jan"), ("februari", "february", "feb"),
          ("maret", "march", "mar"), ("april", "apr"), ("mei", "may"), ("juni", "june", "jun"), ("juli", "july", "jul"),
          ("agustus", "august", "agu", "aug"), ("september", "sept", "sep"), ("oktober", "october", "okt", "oct"),
          ("november", "nov"), ("desember", "december", "des", "dec")], 1) for m in ms}


def period_day_overflow(text):
    """'16-31 September 2026' -> 'Periode 16-31 September: September hanya 30 hari, cek tanggal' (else None)."""
    m = re.search(r"(\d{1,2})\s*(?:-|–|—|s/?d|sampai|hingga)\s*(\d{1,2})\s+([A-Za-z]+)\.?(?:\s+(\d{4}))?", str(text or ""), re.I)
    if not m or m.group(3).lower() not in MONTHS:
        return None
    mon, year = MONTHS[m.group(3).lower()], int(m.group(4) or dt.date.today().year)
    last = ((dt.date(year, mon, 28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)).day
    if int(m.group(2)) <= last:
        return None
    name = m.group(3)[:1].upper() + m.group(3)[1:].lower()
    return f"Periode {int(m.group(1))}-{int(m.group(2))} {name}: {name} hanya {last} hari, cek tanggal"


def slot_label(s):
    """Signature slot as printed: role + title/name in brackets, e.g. 'Menyetujui (AM)' (v6)."""
    role, name = (s.get("role") or "").strip(), (s.get("name") or "").strip()
    if role and name and name.lower() not in role.lower():
        return f"{role} ({name})"
    return role or name or "Pejabat"


SHORT = {"1_identitas": "Identitas", "2_periode": "Periode", "3_hk_diajukan": "HK diajukan", "4_ttd": "TTD",
         "5_kehadiran": "Kehadiran", "6_transaksi": "Transaksi"}


def evaluate(ex, fname):
    """Deterministic rule checks -> list of per-worker results."""
    workers = ex.get("workers") or [{}]
    sig = ex.get("signatures") or {}
    kh0 = ex.get("attach_kehadiran") or {}
    tx0 = ex.get("attach_transaksi") or {}
    multi = len(workers) > 1
    results = []
    llm_conf = ex.get("confidence") or {}
    for w in workers:
        crit, reason, problems = {}, {}, []
        kh, tx = worker_attach(w, kh0, tx0) if multi else (kh0, tx0)
        conf = {k: _conf(llm_conf.get(g)) for k, g in zip(CRITERIA, ("identitas", "periode", "hk", "ttd", "kehadiran", "transaksi"))}
        jenis, tanggal, nom = classify(ex, w, problems)
        # 1 identitas
        fields = {k: w.get(k) for k in ("nik", "nama", "jabatan", "lokasi")}
        missing = [k for k, v in fields.items() if not v]
        crit["1_identitas"] = "OK" if not missing else ("MISSING" if len(missing) == 4 else "PARTIAL")
        reason["1_identitas"] = "lengkap" if not missing else "tidak ada: " + ", ".join(missing)
        nik = norm_nik(w.get("nik"))
        nik_dms = norm_nik(kh.get("nik_shown")) if kh.get("present") else ""
        if nik and len(nik) != 16:  # v4: a malformed NIK fails identitas (KTP NIK is always 16 digits)
            crit["1_identitas"] = "PARTIAL"  # v5 plain wording; the DMS NIK is named here (no separate mismatch line)
            reason["1_identitas"] = (reason["1_identitas"] + "; " if missing else "") + f"NIK {len(nik)} digit" + (
                f", di DMS {nik_dms}" if nik_dms and nik_dms != nik else f" ({w.get('nik')})")
        for o in ex.get("other_persons") or []:
            if o.get("nama") and o.get("nik") and norm_nik(o.get("nik")) != nik:
                problems.append(f"Header menyebut orang lain: {o.get('nama')} ({o.get('role')}, NIK {o.get('nik')})")

        # work days: listed in BA, else attendance days
        listed = sorted({dkey(x) for x in (w.get("days_listed") or []) if dkey(x)})
        att_days = sorted({dkey(x) for x in (kh.get("days_present") or []) if dkey(x)}) if kh.get("present") else []
        trx_days = {dkey(t.get("date")): t.get("count") for t in (tx.get("days") or []) if dkey(t.get("date"))}
        kt = kh.get("total")
        raw_att = [dkey(x) for x in (kh.get("days_present") or []) if dkey(x)] if kh.get("present") else []
        att_unsure = multi and len(raw_att) != len(set(raw_att))  # v5: the same date read twice for one worker = misread
        if isinstance(kt, int) and att_days and len(att_days) != kt:
            # extraction inconsistency (e.g. a blank cell read as present): reconcile with days that have TRX counts
            with_trx = [x for x in att_days if isinstance(trx_days.get(x), (int, float)) and trx_days[x] > 0]
            if len(with_trx) == kt:
                problems.append(f"Kehadiran: {len(att_days)} tanggal terbaca vs total {kt}; dipakai {kt} hari yang punya transaksi")
                att_days = with_trx
            else:
                if not multi:
                    problems.append(f"Kehadiran: {len(att_days)} tanggal terbaca, total tertulis {kt}; cek manual")
                att_unsure = att_unsure or multi
        conf5 = conf["5_kehadiran"]
        if att_unsure:  # v5 (multi): an unreadable attendance list is a 'cek manual', never a rule failure by itself
            conf["5_kehadiran"] = min(conf["5_kehadiran"], 0.5)
        elif isinstance(kt, int) and att_days and len(att_days) != kt:
            conf["5_kehadiran"] = min(conf["5_kehadiran"], 0.5)
        work_days = listed or att_days
        if jenis == "single_tanggal" and tanggal:
            work_days = tanggal[:1]

        # 2 periode
        ps, pe = d(w.get("periode_start")), d(w.get("periode_end"))
        pend = pe or ((ps.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1) if ps else None)
        if not (w.get("periode_text") or ps):
            crit["2_periode"], reason["2_periode"] = "MISSING", "periode tidak tercantum di BA"
        else:
            crit["2_periode"], reason["2_periode"] = "OK", w.get("periode_text") or f"{ps} s/d {pe}"
            bad_day = period_day_overflow(w.get("periode_text"))
            if bad_day:  # v6: '16-31 September' -> one plain alert; verdict unchanged
                problems.append(bad_day)
            elif w.get("periode_text") and not pe:
                problems.append(f"Periode '{w.get('periode_text')}' tidak bisa diparse / tanggal tidak valid")
            evid = sorted(x for x in set(att_days) | {k for k, v in trx_days.items() if v} if d(x))
            if ps and evid:
                end = pend
                outside = [x for x in evid if not (ps <= d(x) <= end)]
                if len(outside) == len(evid):
                    crit["2_periode"] = "PARTIAL"
                    reason["2_periode"] = f"periode BA {ps}..{end} tapi semua tanggal lampiran di luar periode ({outside[0]}..)"
                    problems.append("Periode BA tidak cocok dengan tanggal di lampiran")
                elif outside:
                    problems.append(f"{len(outside)} tanggal lampiran di luar periode BA")

        # 3 HK diajukan (rule set depends on jenis)
        figs = [f for f in (w.get("hk_figures") or []) if isinstance(f.get("value"), int)]
        claimed = w.get("hk_claimed")
        if jenis == "single_tanggal":
            crit["3_hk_diajukan"], reason["3_hk_diajukan"] = check_single(tanggal, claimed, figs, problems)
        elif jenis == "kurang_nominal":
            crit["3_hk_diajukan"], reason["3_hk_diajukan"] = check_nominal(nom, problems)
        ref = len(listed) if listed else (len(att_days) if att_days else (kh.get("total") if kh.get("present") else None))
        ref_src = "hari di daftar BA" if listed else "hari di lampiran kehadiran"
        if jenis in ("single_tanggal", "kurang_nominal"):
            pass
        elif not figs and claimed is None:
            if listed:
                crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "PARTIAL", f"tidak ada angka HK, hanya {len(listed)} tanggal terdaftar"
            else:
                crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "MISSING", "tidak ada jumlah HK"
        elif ref is None:
            v = claimed if claimed is not None else figs[0]["value"]
            crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "OK", f"HK {v} tercantum (tidak ada daftar hari untuk dicek)"
        else:
            vals = {f["value"] for f in figs} | ({claimed} if claimed is not None else set())
            hk_txt = claimed if claimed is not None else "/".join(str(f["value"]) for f in figs)
            if ref in vals:
                crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "OK", f"HK {ref}, sesuai {ref_src}"
            elif att_unsure and not listed and isinstance(kt, int) and kt in vals:
                # v5: day rows disagree with the printed attendance total, but the total (rekap) equals the HK claimed
                crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "OK", f"HK {kt}, sesuai rekap kehadiran"
                conf["5_kehadiran"] = conf5
            elif att_unsure and not listed:  # v5: compared against a misread attendance list -> cek manual, not INVALID
                crit["3_hk_diajukan"], reason["3_hk_diajukan"] = "OK", f"HK {hk_txt}; kehadiran tidak terbaca, cek manual"
                conf["3_hk_diajukan"] = min(conf["3_hk_diajukan"], 0.5)
            else:
                crit["3_hk_diajukan"] = "PARTIAL"
                reason["3_hk_diajukan"] = f"HK tertulis {hk_txt}, {ref_src} hanya {ref}" if ref < max(vals) else \
                    f"HK tertulis {hk_txt}, {ref_src} {ref}"
        hk_k, hk_b = _int(w.get("hk_kerja")), _int(w.get("hk_dibayar"))
        if multi and jenis == "kurang_hk" and None not in (hk_k, hk_b, _int(claimed)) and hk_k - hk_b != _int(claimed):
            crit["3_hk_diajukan"] = "PARTIAL"  # v4 table arithmetic: Hari Kerja - Sudah dibayar must equal Kekurangan
            reason["3_hk_diajukan"] = f"kekurangan tertulis {claimed}, seharusnya {hk_k - hk_b} (hari kerja {hk_k}, sudah dibayar {hk_b})"

        # 4 TTD (v3 strict): OK only if EVERY slot (HL + all officials) is signed; anything else is INVALID
        hl = str(w.get("ttd_hl") if multi and w.get("ttd_hl") else sig.get("hl", "unclear")).lower()
        slots = [{"role": "HL", "signed": hl}] + [
            {"role": slot_label(s), "signed": str(s.get("signed")).lower()}
            for s in (sig.get("pejabat_slots") or [])]
        unsigned = [s["role"] for s in slots if not yes(s["signed"])]
        unclear = [s["role"] for s in slots if s["signed"] == "unclear"]
        if len(slots) == 1:  # no official slot detected at all
            unsigned.append("Pejabat (slot tidak ditemukan)")
        crit["4_ttd"] = "OK" if not unsigned else "INVALID"
        if not unsigned:
            reason["4_ttd"] = f"Semua slot bertanda tangan ({len(slots)}/{len(slots)})"
        else:
            reason["4_ttd"] = ("Tidak ada TTD. " if len(unsigned) >= len(slots) else "") + "Belum TTD: " + ", ".join(map(str, unsigned))
            if unclear:
                reason["4_ttd"] += " (tidak terbaca jelas, upload ulang: " + ", ".join(map(str, unclear)) + ")"
        if unclear:
            conf["4_ttd"] = min(conf["4_ttd"], 0.5)

        # 5 kehadiran
        if not kh.get("present"):
            crit["5_kehadiran"], reason["5_kehadiran"] = "MISSING", "lampiran kehadiran tidak ada"
        elif kh.get("format") == "per_day" and (att_days or kh.get("total")):
            crit["5_kehadiran"] = "OK"
            reason["5_kehadiran"] = f"hal {kh.get('pages')}: {len(att_days) or kh.get('total')} hari hadir"
        else:
            crit["5_kehadiran"] = "PARTIAL"
            reason["5_kehadiran"] = f"hal {kh.get('pages')}: hanya rekap total ({kh.get('total')}), tanpa rincian harian"
        if kh.get("_not_found"):
            crit["5_kehadiran"], reason["5_kehadiran"] = "PARTIAL", "nama/NIK tidak ada di lampiran kehadiran"
        if jenis == "single_tanggal" and tanggal and kh.get("present") and crit["5_kehadiran"] != "MISSING":
            if att_days and not has_day(tanggal[0], att_days):
                crit["5_kehadiran"] = "PARTIAL"
                reason["5_kehadiran"] = f"lampiran kehadiran tidak menunjukkan tanggal {tanggal[0]}"
            elif att_days:
                crit["5_kehadiran"], reason["5_kehadiran"] = "OK", f"hal {kh.get('pages')}: hadir pada {tanggal[0]}"
        if kh.get("present") and nik_dms and nik and nik_dms != nik and len(nik) == 16:
            problems.append(f"NIK di BA {nik}, di DMS {nik_dms}")
        diff = sorted([x for x in listed if not has_day(x, att_days)] + [x for x in att_days if not has_day(x, listed)])
        if listed and att_days and diff:
            problems.append(f"Hari di BA vs lampiran kehadiran beda: {', '.join(diff[:6])}")

        # 6 transaksi
        fmt = tx.get("format")
        with_cnt = {k for k, v in trx_days.items() if isinstance(v, (int, float)) and v > 0}
        if not tx.get("present") or fmt == "none":
            crit["6_transaksi"], reason["6_transaksi"] = "MISSING", "lampiran transaksi DMS tidak ada"
        elif fmt == "empty" or not with_cnt:
            crit["6_transaksi"], reason["6_transaksi"] = "MISSING", "tabel transaksi ada tapi tanpa jumlah per hari"
        else:
            nocnt = [x for x in work_days if not has_day(x, with_cnt)]
            dated = [x for x in with_cnt if d(x)]
            if ps and dated and all(not (ps <= d(x) <= pend) for x in dated):
                crit["6_transaksi"] = "PARTIAL"
                reason["6_transaksi"] = f"semua tanggal transaksi ({min(dated)}..{max(dated)}) di luar periode BA"
            elif not work_days:
                kt = kh.get("total") if kh.get("present") else None
                if isinstance(kt, int) and len(with_cnt) < kt:
                    crit["6_transaksi"] = "PARTIAL"
                    reason["6_transaksi"] = f"transaksi hanya {len(with_cnt)} hari, kehadiran {kt} hari"
                else:
                    crit["6_transaksi"] = "OK"
                    reason["6_transaksi"] = f"{len(with_cnt)} hari dengan jumlah transaksi" + (" (baris order teks)" if fmt == "text_line" else "")
            elif nocnt:
                crit["6_transaksi"] = "PARTIAL"
                reason["6_transaksi"] = f"tidak ada transaksi pada {len(nocnt)}/{len(work_days)} hari kerja: {', '.join(nocnt[:6])}"
            else:
                crit["6_transaksi"] = "OK"
                reason["6_transaksi"] = f"transaksi ada di semua {len(work_days)} hari kerja (total {tx.get('total') or sum(int(v) for v in trx_days.values() if v)})"
        if tx.get("_not_found") and crit["6_transaksi"] != "MISSING":
            crit["6_transaksi"], reason["6_transaksi"] = "PARTIAL", "nama/NIK tidak ada di lampiran transaksi"
        if tx.get("present") and tx.get("nik_shown") and nik and norm_nik(tx["nik_shown"]) != nik:
            problems.append(f"NIK di BA {nik}, di lampiran transaksi {norm_nik(tx['nik_shown'])}")

        for k in CRITERIA:
            if crit[k] != "OK":
                problems.insert(0, f"[{k}] {crit[k]}: {reason[k]}")
        verdict = "VALID" if all(crit[k] == "OK" for k in CRITERIA) else "INVALID"
        hk_out = w.get("hk_claimed") if w.get("hk_claimed") is not None else (figs[0]["value"] if figs else None)
        if jenis == "single_tanggal":
            hk_out = 1 if tanggal else hk_out
        elif jenis == "kurang_nominal" and nom.get("hk_nominal"):
            hk_out = nom["hk_nominal"]
        results.append({"file": fname, "nik": w.get("nik"), "nama": w.get("nama"), "jabatan": w.get("jabatan"),
                        "lokasi": w.get("lokasi"), "periode": w.get("periode_text") or
                        (f"{w.get('periode_start')} s/d {w.get('periode_end')}" if w.get("periode_start") else None),
                        "hk_diajukan": hk_out, "jenis": jenis, "tanggal": tanggal if jenis == "single_tanggal" else listed,
                        "nominal": nom if any(v is not None for v in nom.values()) else None,
                        "hk_hari_terdaftar": len(listed), "criteria": crit, "reason": reason,
                        "confidence": {k: round(conf[k], 2) for k in CRITERIA},
                        "verdict": verdict, "problems": problems,
                        "unsure": [k for k in CRITERIA if conf[k] < 0.6],
                        "short_problems": [reason[k] if SHORT[k].lower() in reason[k].lower() else f"{SHORT[k]}: {reason[k]}"
                                           for k in CRITERIA if crit[k] != "OK"]})  # v6: no 'TTD: Belum TTD: …' 
    return results

