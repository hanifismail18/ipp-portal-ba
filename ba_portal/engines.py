"""Extraction engines. Both return the ba_core extraction dict (SCHEMA_DOC) plus a usage list.

v3 (ZERO MONEY): chain = gemini direct -> gemini2 -> openrouter (:free only) -> groq -> 9router rotation (free models only).
Every layer is an OpenAI-compatible endpoint, ONE call per BA, images inline. Keys come from the admin key manager
(KEY_LOOKUP, hot-reloaded) or .env. `claude -p` is disabled unless ALLOW_CLAUDE_CLI=1 (it burns the Claude subscription).
"""
import base64, io, json, os, re, sys, time
from pathlib import Path

import httpx

TOOL_DIR = Path(__file__).resolve().parent.parent / "ba_tool"
sys.path.insert(0, str(TOOL_DIR))
import ba_core  # noqa: E402
import preprocess  # noqa: E402

OPENAI_PROMPT = """You extract data from an Indonesian "Berita Acara (BA) kekurangan gaji" PDF for an HL (harian lepas) worker.
The PDF has {pages} page(s). Attached below are page images (each preceded by "PAGE N"); pages not attached have a text layer only
(no embedded images, so no handwriting/signature on them).
{text_block}
Return ONLY one JSON object (no markdown fences, no prose) following this schema (// comments explain fields):
{schema}
Additionally include top-level "page_quality": [{{"page": int, "legible": "good"|"poor", "note": str}}] for every attached image
(poor = text you need is blurred/cut/too faint to read with confidence).
Rules: copy values exactly as printed; never invent or guess - use null / [] / "unclear" when not visible or not legible.
Dates as ISO YYYY-MM-DD; when an attachment shows only day numbers, take month/year from its header or the BA period and say so in note;
if month/year is truly unknown give the bare day number "DD".
Attendance days_present: only dates with an explicit present mark (H, 1, check, clock-in); a blank, 0, "-", L/libur or A cell is NOT
present; len(days_present) must equal the printed total when one is shown. TRX count null when the cell is blank.
Signatures: "yes" only if an actual handwritten mark or signature image is visible above/at that slot; a printed name alone is "no".
Output minified JSON (no indentation, no comments).{chunk_note}
"""
CHUNK_NOTE = """
PARTIAL CALL {i}/{k}: only page images {first}-{last} of {pages} are attached in this call (other pages: text layer only).
List every worker of the BA (from the text layer and these pages); fill att/trx/signatures only from what these pages
or the text layer show (unknown -> null/[]/"unclear"). Other parts are extracted in separate calls and merged."""


_COOLDOWN = {}  # (layer, model) -> unix time until which it is skipped (rate-limited / overloaded)
_LAYER_CD = {}  # layer -> unix time until which the whole layer is skipped (402 = exhausted)
KEY_LOOKUP = lambda provider: None  # noqa: E731  set by server: admin-managed key (decrypted) or None
CALLS_TODAY = lambda layer: 0  # noqa: E731  set by server: attempts logged today for this layer (daily guard)
FREE_RE = re.compile(r"^[\w.\-]+/[\w.\-/]+:free$")  # OpenRouter free model ids


def cooldown_secs(text):
    """Seconds to skip a model after 429/503. Honors Google retryDelay / 'retry in Ns' / 9router 'reset after Ns';
    a PerDay quota (Google free tier is per model per project) waits until midnight Pacific (quota reset)."""
    text = text or ""
    if "PerDay" in text:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        return int((nxt - now).total_seconds())
    m = re.search(r'retryDelay"?\s*:\s*"(\d+(?:\.\d+)?)s|retry in (\d+(?:\.\d+)?)s|reset after (\d+)\s*([sm])', text)
    if m:
        if m.group(1) or m.group(2):
            return int(float(m.group(1) or m.group(2))) + 2
        return int(m.group(3)) * (60 if m.group(4) == "m" else 1) + 1
    return 300 if "exceeded your current quota" in text else 60


def reset_layer(name):
    """Forget cooldowns of a layer (called when its key is replaced/removed)."""
    _LAYER_CD.pop(name, None)
    for k in [k for k in _COOLDOWN if k[0] == name]:
        _COOLDOWN.pop(k, None)


class EngineError(Exception):
    pass


def _env(k, default=None):
    v = os.environ.get(k, default)
    return v.strip() if isinstance(v, str) else v


class OpenAIEngine:
    """prefix LLM_ = primary endpoint (Gemini direct or 9router); LLM2_ = optional 2nd endpoint (e.g. OpenRouter direct)."""

    def __init__(self, prefix="LLM_", name="openai", base=None, key=None, models=None, allow=None, daily_cap=None,
                 probe="/models"):
        self.name = name
        self.probe = probe
        self.base = (base or _env(prefix + "BASE_URL") or "").rstrip("/")
        self.key = key if key is not None else (_env(prefix + "API_KEY") or "")
        self.models = [m.strip() for m in (models or _env(prefix + "MODEL") or "").split(",") if m.strip()]
        self.refused = [m for m in self.models if allow and not allow(m)]  # never call a non-free model
        self.models = [m for m in self.models if m not in self.refused]
        self.daily_cap = daily_cap
        self.timeout = float(_env("LLM_TIMEOUT", "180"))
        self.retries = int(_env("LLM_RETRIES", "3"))
        self.max_tokens = int(_env("LLM_MAX_TOKENS", "6000"))  # OpenRouter reserves credit for max_tokens: keep it bounded
        self.max_tokens_big = int(_env("LLM_MAX_TOKENS_BIG", "24000"))  # v4 ceiling for many-worker BAs
        self.wait = True  # False inside the chain: do not sleep on cooldown, hand over to the next engine

    def configured(self):
        return bool(self.base and self.key and self.models and "PASTE" not in self.key)

    def exhausted(self):
        """None if usable now, else 'daily-cap' / 'limited'."""
        if self.daily_cap is not None and CALLS_TODAY(self.name) >= self.daily_cap:
            return "daily-cap"
        if _LAYER_CD.get(self.name, 0) > time.time() or all(self._cd(m) > time.time() for m in self.models):
            return "limited"
        return None

    def _cd(self, model):
        return _COOLDOWN.get((self.name, model), 0)

    def health(self):
        return self.status() == "ok"

    def status(self):
        """ok | missing-key | no-free-model | daily-cap | limited (429 cooldown / 402 exhausted) | down"""
        if not (self.base and self.key and "PASTE" not in self.key):
            return "missing-key"
        if not self.models:
            return "no-free-model"
        ex = self.exhausted()
        if ex:
            return ex
        try:
            r = httpx.get(self.base + self.probe, headers={"Authorization": f"Bearer {self.key}"}, timeout=4)
            return "ok" if r.status_code == 200 else "down"
        except Exception:
            return "down"

    def extract(self, pdf_path, prep, log_usage):
        """prep = preprocess.prepare(...) output. One logical call per BA (retries/fallback models logged separately)."""
        text = prep["text"]
        text_block = ("Text layer extracted by pdftotext (may miss images/handwriting):\n<<<\n" + text[:40000] + "\n>>>"
                      if text else "The PDF has no text layer (scan); rely on the images.")
        content = [{"type": "text", "text": OPENAI_PROMPT.format(pages=prep["pages"], text_block=text_block, schema=ba_core.SCHEMA_DOC,
                                                                 chunk_note=prep.get("chunk_note", ""))}]
        for img in prep["images"]:
            content.append({"type": "text", "text": f"PAGE {img['page']}"})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img["b64"]}})
        # v4: output grows ~450 tokens per worker; estimate workers from NIK-like numbers in the text layer
        est = max(len(set(re.findall(r"(?<!\d)\d{14,17}(?!\d)", text))), len(prep["images"]) * 6 if not text else 0)
        body = {"messages": [{"role": "user", "content": content}], "stream": False, "temperature": 0,
                "max_tokens": max(self.max_tokens, min(self.max_tokens_big, 2000 + 450 * est))}
        last = None
        deadline = time.time() + float(_env("LLM_MAX_WAIT", "300"))
        while time.time() < deadline:
            ex = self.exhausted()
            if ex == "daily-cap" or _LAYER_CD.get(self.name, 0) > time.time():
                raise EngineError(f"{self.name}: {ex}")
            ready = [m for m in self.models if self._cd(m) <= time.time()]
            if not ready and not self.wait:
                raise EngineError(f"{self.name}: semua model sedang cooldown ({last or 'rate limit'})")
            if not ready:  # every model rate-limited: wait for the earliest reset (shared across workers)
                time.sleep(max(1.0, min(min(self._cd(m) for m in self.models) - time.time(), 60)))
                continue
            model = ready[0]
            for attempt in range(1, self.retries + 1):
                t0 = time.time()
                status, usage, note, full = "error", {}, "", ""
                try:
                    r = httpx.post(self.base + "/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                                   json={**body, "model": model}, timeout=self.timeout)
                    if r.status_code == 200:
                        js = r.json()
                        usage = js.get("usage") or {}
                        try:
                            data = ba_core.parse_json(js["choices"][0]["message"]["content"])
                            status = "ok"
                        except Exception as e:
                            status, note = "bad_json", str(e)[:200]
                            if js["choices"][0].get("finish_reason") == "length":  # truncated: retry with a bigger budget
                                note = "truncated (max_tokens) " + note
                                body["max_tokens"] = min(self.max_tokens_big, body["max_tokens"] * 2)
                    else:
                        status, note, full = f"http_{r.status_code}", r.text[:300], r.text
                except Exception as e:
                    status, note = "exception", f"{type(e).__name__}: {e}"[:200]
                log_usage(engine=self.name, model=model, attempt=attempt, status=status,
                          prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                          images=len(prep["images"]), latency_ms=int((time.time() - t0) * 1000), note=note)
                if status == "ok":
                    data["_meta"] = {"engine": self.name, "model": model, "pages": prep["pages"], "text_layer": bool(text)}
                    if model != self.models[0]:
                        data.setdefault("_alerts", []).append(f"Diperiksa dengan model cadangan ({model}); hasil mungkin kurang akurat.")
                    return data
                last = f"{model}: {status} {note[:120]}"
                if status == "http_402":  # payment required = this layer is exhausted; never pay, skip the layer
                    _LAYER_CD[self.name] = time.time() + float(_env("LAYER_402_COOLDOWN", "3600"))
                    raise EngineError(f"{self.name}: exhausted (402)")
                if status in ("http_429", "http_503"):
                    secs = cooldown_secs(full)  # per-model quota: honor retryDelay / PerDay reset
                    _COOLDOWN[(self.name, model)] = time.time() + secs
                    break
                if status.startswith("http_4"):  # bad model/request/key: disable this model for 1h
                    _COOLDOWN[(self.name, model)] = time.time() + 3600
                    break
                if not self.wait and status != "bad_json":  # in the chain: timeout / 5xx -> next model / next layer now
                    _COOLDOWN[(self.name, model)] = time.time() + 60
                    break
                time.sleep(min(30, 2 ** attempt))  # timeout / 5xx / bad json: backoff and retry same model
            else:
                _COOLDOWN[(self.name, model)] = time.time() + 60
        raise EngineError(f"LLM gagal: {last}")


class ClaudeEngine:
    name = "claude"

    def __init__(self):
        self.model = _env("CLAUDE_MODEL", "sonnet")

    def configured(self):
        return True

    def health(self):
        import shutil
        return bool(shutil.which("claude"))

    def status(self):
        return "ok" if self.health() else "down"

    def exhausted(self):
        return None

    def extract(self, pdf_path, prep, log_usage):
        import ba_validate
        t0 = time.time()
        cache_file = ba_validate.CACHE / f"{ba_core.file_hash(pdf_path)}_{ba_core.PROMPT_VERSION}.json"
        cached = cache_file.exists()
        data = ba_validate.extract(Path(pdf_path), self.model, True)
        if not cached:
            log_usage(engine=self.name, model=self.model, attempt=1, status="ok", images=prep["pages"],
                      latency_ms=int((time.time() - t0) * 1000), cost_usd=(data.get("_meta") or {}).get("cost_usd"),
                      note="claude -p")
        data.setdefault("_meta", {})["engine"] = self.name
        return data


class ChainEngine:
    """Fallback chain (first layer that answers wins). Unkeyed / exhausted / daily-capped layers are skipped
    without a call. If every layer fails -> EngineError (server parks the BA as 'ditunda' and retries later)."""
    name = "chain"

    def __init__(self, engines):
        self.engines = engines
        for e in engines:
            e.wait = False

    def configured(self):
        return any(e.configured() for e in self.engines)

    def health(self):
        return any(v == "ok" for v in self.health_detail().values())

    def health_detail(self):
        return {e.name: e.status() for e in self.engines}

    def extract(self, pdf_path, prep, log_usage):
        errors = []
        for e in self.engines:
            if not e.configured():
                continue
            ex = e.exhausted()
            if ex:
                errors.append(f"{e.name}: {ex}")
                continue
            try:
                return e.extract(pdf_path, prep, log_usage)
            except Exception as ex:  # next layer
                errors.append(f"{e.name}: {ex}"[:200])
        raise EngineError("Semua engine gagal: " + " | ".join(errors))


def _merge_sig(a, b):
    rank = {"yes": 0, "no": 1, "unclear": 2}
    a, b = str(a or "unclear").lower(), str(b or "unclear").lower()
    return a if rank.get(a, 3) <= rank.get(b, 3) else b


def _wkeys(w):
    """Match keys of a worker across chunks: NIK and normalized name (a NIK typo in one chunk must not split him)."""
    nik = ba_core.norm_nik(w.get("nik"))
    name = re.sub(r"[^a-z]", "", str(w.get("nama") or "").lower())
    return [k for k in (("nik", nik) if len(nik) >= 10 else None, ("nama", name) if name else None) if k]


def merge_chunks(parts):
    """Merge per-chunk extractions of one BA: workers by NIK/name (union of attachment rows), signatures best-of
    (yes > no > unclear), attachments union, confidence min."""
    out = json.loads(json.dumps(parts[0]))
    for k in ("page_notes", "page_quality", "other_persons", "_alerts"):
        out[k] = [x for p in parts for x in (p.get(k) or [])]
    conf = {}
    for p in parts:
        for k, v in (p.get("confidence") or {}).items():
            conf[k] = min(conf.get(k, 1.0), ba_core._conf(v))
    out["confidence"] = conf
    workers, index = [], {}
    for p in parts:
        for w in p.get("workers") or []:
            keys = _wkeys(w)
            hit = next((index[k] for k in keys if k in index), None)
            if hit is None:
                workers.append(json.loads(json.dumps(w)))
                for k in keys:
                    index[k] = len(workers) - 1
                continue
            m = workers[hit]
            for k in keys:
                index.setdefault(k, hit)
            for f, v in w.items():
                if m.get(f) in (None, "", []) and v not in (None, "", []):
                    m[f] = v
            m["ttd_hl"] = _merge_sig(m.get("ttd_hl"), w.get("ttd_hl")) if (m.get("ttd_hl") or w.get("ttd_hl")) else None
            for f, dk in (("att", "days_present"), ("trx", "days")):
                a, b = m.get(f), w.get(f)
                if isinstance(a, dict) and isinstance(b, dict):
                    a["found"] = bool(a.get("found")) or bool(b.get("found"))
                    seen = {json.dumps(x, sort_keys=True) for x in a.get(dk) or []}
                    a[dk] = (a.get(dk) or []) + [x for x in b.get(dk) or [] if json.dumps(x, sort_keys=True) not in seen]
                    a["total"] = a.get("total") if a.get("total") is not None else b.get("total")
                    a["nik_shown"] = a.get("nik_shown") or b.get("nik_shown")
    out["workers"] = workers
    sig = out.setdefault("signatures", {})
    slots = {}
    for p in parts:
        s = p.get("signatures") or {}
        sig["hl"] = _merge_sig(sig.get("hl"), s.get("hl"))
        for sl in s.get("pejabat_slots") or []:
            k = str(sl.get("role") or sl.get("name") or "").lower()
            slots[k] = {**sl, "signed": _merge_sig((slots.get(k) or {}).get("signed"), sl.get("signed"))}
    sig["pejabat_slots"] = list(slots.values())
    sig["evidence"] = " | ".join(str((p.get("signatures") or {}).get("evidence") or "") for p in parts)
    for att, dk in (("attach_kehadiran", "days_present"), ("attach_transaksi", "days")):
        hit = [p.get(att) for p in parts if isinstance(p.get(att), dict) and p[att].get("present")]
        if hit:
            a = json.loads(json.dumps(hit[0]))
            for b in hit[1:]:
                a["pages"] = sorted(set(a.get("pages") or []) | set(b.get("pages") or []))
                a[dk] = (a.get(dk) or []) + [x for x in b.get(dk) or [] if x not in (a.get(dk) or [])]
                if a.get("format") in ("none", "empty", "summary_only") and b.get("format") not in ("none", "empty"):
                    a["format"] = b.get("format")
                a["total"] = a.get("total") if a.get("total") is not None else b.get("total")
            out[att] = a
    out["_meta"] = {**(parts[-1].get("_meta") or {}), "chunks": len(parts)}
    return out


def extract_chunked(eng, pdf_path, prep, log_usage, chunk_pages, cache_dir):
    """v4: ONE call when the BA's page images fit in chunk_pages, else one call per chunk of chunk_pages images
    (every chunk also gets the full text layer). Each chunk result is cached on disk by file hash + prompt version, so a
    BA parked as 'ditunda' half-way does not repeat finished chunks."""
    imgs = prep["images"]
    if len(imgs) <= chunk_pages:
        return eng.extract(pdf_path, prep, log_usage)
    chunks = [imgs[i:i + chunk_pages] for i in range(0, len(imgs), chunk_pages)]
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = ba_core.file_hash(pdf_path)
    parts = []
    for i, ch in enumerate(chunks, 1):
        cf = cache_dir / f"{h}_{ba_core.PROMPT_VERSION}_{i}of{len(chunks)}.json"
        if cf.exists():
            parts.append(json.loads(cf.read_text()))
            continue
        sub = {**prep, "images": ch, "chunk_note": CHUNK_NOTE.format(i=i, k=len(chunks), first=ch[0]["page"],
                                                                     last=ch[-1]["page"], pages=prep["pages"])}
        data = eng.extract(pdf_path, sub, log_usage)
        cf.write_text(json.dumps(data, ensure_ascii=False))
        parts.append(data)
    return merge_chunks(parts)


REREAD_PROMPT = """You re-read the ATTENDANCE attachment (DMS absensi) of an Indonesian payroll document, nothing else.
The images are high-resolution horizontal strips of attachment page(s) {pages}, top to bottom. Neighbouring strips overlap
by a few pixels: a row cut by a strip edge and repeated in the next strip is ONE row.
For EACH worker below, find EVERY attendance row that belongs to that worker (match by NIK or name; the name may be
abbreviated or in different case) and copy the date printed in each row. Indonesian dates are day-first
(dd/mm/yy or dd-mm-yyyy; yy 26 = 2026). Read every digit carefully; do not guess, do not copy a neighbour row's date.
Workers:
{workers}
Return ONLY minified JSON: {{"workers":[{{"nik":"<NIK as given above>","nik_shown":"<NIK printed in that worker's rows, or null>",
"dates":["YYYY-MM-DD", ... one entry per row ...],"rows":<number of rows found>,"sure":true|false}}]}}"""


def _reread_order(models):
    """Re-read prefers a non-lite Flash model (more precise on dense tables); lite models stay as fallback."""
    return sorted(models, key=lambda m: 0 if ("flash" in m and "lite" not in m) else 1)


def _trusted(model):
    return "flash" in model and "lite" not in model


def _vision_answers(content, max_tokens, n_images, log_usage, tag, need=1, stop_on_trusted=False):
    """One vision prompt through the FULL free fallback list on 429/503/errors, in tiers: non-lite Flash of every layer,
    then lite Flash, then everything else (gemma via Gemini API, OpenRouter :free, groq, 9router free). Per-model
    cooldown honors retryDelay. Collects up to `need` answers from DIFFERENT models (stops at the first trusted one when
    stop_on_trusted). Returns [(parsed JSON, model)]."""
    body = {"messages": [{"role": "user", "content": content}], "stream": False, "temperature": 0,
            "reasoning_effort": "low", "max_tokens": max_tokens}
    ls = layers()
    tiers = [lambda m: _trusted(m), lambda m: "flash" in m and "lite" in m, lambda m: "flash" not in m]
    tries = [(e, m) for t in tiers for e in ls for m in _reread_order(e.models) if t(m)]
    answers = []
    for e, model in tries:
        if not e.configured() or e.exhausted() or e._cd(model) > time.time():
            continue
        t0, status, usage, note, data, full = time.time(), "error", {}, "", None, ""
        try:
            r = httpx.post(e.base + "/chat/completions", headers={"Authorization": f"Bearer {e.key}"},
                           json={**body, "model": model}, timeout=e.timeout)
            if r.status_code == 200:
                js = r.json()
                usage = js.get("usage") or {}
                data = ba_core.parse_json(js["choices"][0]["message"]["content"])
                status = "ok"
            else:
                status, note, full = f"http_{r.status_code}", r.text[:200], r.text
                if r.status_code == 400 and "reasoning" in r.text.lower():  # endpoint rejects the param: drop it
                    body.pop("reasoning_effort", None)
        except Exception as ex:
            status, note = "exception", f"{type(ex).__name__}: {ex}"[:200]
        log_usage(engine=e.name, model=model, attempt=1, status=status, prompt_tokens=usage.get("prompt_tokens"),
                  completion_tokens=usage.get("completion_tokens"), images=n_images,
                  latency_ms=int((time.time() - t0) * 1000), note=f"{tag} {note}".strip())
        if status == "ok":
            answers.append((data, model))
            if len(answers) >= need or (stop_on_trusted and _trusted(model)):
                break
            continue
        if status == "http_402":
            _LAYER_CD[e.name] = time.time() + float(_env("LAYER_402_COOLDOWN", "3600"))
        elif status in ("http_429", "http_503"):
            _COOLDOWN[(e.name, model)] = time.time() + cooldown_secs(full)
        elif status.startswith("http_4"):
            _COOLDOWN[(e.name, model)] = time.time() + 3600
        else:
            _COOLDOWN[(e.name, model)] = time.time() + 60
    return answers


def _vision_json(content, max_tokens, n_images, log_usage, tag, lite_ok=True):
    """First answer of the full fallback list -> (parsed JSON, model) or (None, None). (lite_ok kept for callers; unused)"""
    a = _vision_answers(content, max_tokens, n_images, log_usage, tag)
    return a[0] if a else (None, None)


def reread_attendance(pdf_path, pages, targets, log_usage):
    """v5: ONE targeted call for the workers whose attendance read looked wrong. targets = [{nik, nama}].
    Returns {norm_nik: {"dates": [...], "nik_shown": ..., "sure": bool, "model": ...}} or {} if no layer answered."""
    imgs = preprocess.strips(pdf_path, pages)
    if not imgs or not targets:
        return {}
    wl = "\n".join(f"- NIK {t.get('nik') or '?'} | {t.get('nama') or '?'}" for t in targets)
    content = [{"type": "text", "text": REREAD_PROMPT.format(pages=", ".join(map(str, pages)), workers=wl)}]
    for im in imgs:
        content.append({"type": "text", "text": f"PAGE {im['page']} strip {im['part']}"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + im["b64"]}})
    # reasoning_effort=low: Flash 3 otherwise thinks 5-13k tokens and hits max_tokens before the JSON (finish=length)
    data, model = _vision_json(content, min(24000, 6000 + 300 * len(targets)), len(imgs), log_usage, "reread")
    out = {}
    for w in (data or {}).get("workers") or []:
        k = ba_core.norm_nik(w.get("nik"))
        if k:
            out[k] = {"dates": [x for x in (w.get("dates") or []) if ba_core.d(x)],
                      "nik_shown": w.get("nik_shown"), "sure": w.get("sure") is not False, "model": model}
    return out


SIG_VERSION = "s1"
SIG_PROMPT = """Locate the SIGNATURE SLOTS on these pages of an Indonesian "Berita Acara" (BA). Each image is ONE whole
page (not cropped). A signature slot is a place where someone must sign: an official block (heading such as Dibuat /
Mengetahui / Menyetujui / Disetujui oleh / Diketahui oleh + a title such as SPV / AM / Supervisor / Asisten Manajer, and
usually a printed name or dotted line below), the worker's (HL / mitra / pekerja) own block, and, if the BA has a table
with a signature column (e.g. "Tanda tangan Mitra" / "TTD"), EVERY cell of that column (one slot per worker row).
A block can be split across pages (heading on one page, signatures and names on the next): report it where the
signing space and the printed name are.
For each slot return:
- page: the page number given before the image
- role: the heading and title as printed, short, e.g. "Menyetujui (AM)", "Dibuat (SPV)", "Disetujui oleh Supervisor",
  "Pekerja (Wahyu Tri Basuki)"; for a table cell use "Tanda tangan Mitra - <worker name in that row>"
- worker: the worker's name for a table cell or the worker's own block, else null
- signed: true if a handwritten/drawn signature, initials or signature image is in the slot; false if it is empty,
  only a dotted line, only a printed name, or only a stamp without a signature
- box_2d: [ymin, xmin, ymax, xmax] on a 0-1000 scale of that page image: the signing space of the slot (the area
  between the heading and the printed name, or the whole table cell), tight but covering where the signature is/should be
Return ONLY minified JSON: {"slots":[{"page":1,"role":"...","worker":null,"signed":false,"box_2d":[0,0,0,0]}]}"""


ISSUE_PROMPT = """Locate these FIELDS (problems found by the checker) on the pages. Each image is ONE whole page (not
cropped). For each field return one box around the printed value / table cell (or, for an attendance/transaction
item, that worker's whole row in the table; if the table has no per-worker row, the whole table), on the page where it
is printed:
{items}
Return them in "issues": {{"id": <id above>, "page": <page number given before the image>, "box_2d": [ymin, xmin, ymax,
xmax] on a 0-1000 scale of that page image}}. Omit a field you cannot find; never guess a place."""
SIG_NONE = 'Do NOT list signature slots: return "slots": [].'


def locate_signatures(pdf_path, pages, log_usage, dpi=110, issues=None, need_sig=True):
    """v6: signature slots with page-normalized boxes. Full pages (no crop), so box_2d/1000 maps 1:1 onto the page the
    frontend renders. Returns [{role, worker, page, bbox [x0,y0,x1,y1] 0..1, signed}] or None if no layer answered.
    v7: with issues=[{id, where, worker, find}] the SAME call also locates those fields and the return value is
    (slots, {id: {page, bbox}})."""
    text = SIG_PROMPT if need_sig else SIG_NONE
    if issues:
        items = "\n".join(f"- id {i['id']}: {i['find']}" + (f" (worker: {i['worker']})" if i.get("worker") else "") +
                          f" [on a {i['where']} page]" for i in issues)
        text = text.replace('Return ONLY minified JSON: {"slots":[{"page":1,"role":"...","worker":null,"signed":false,'
                            '"box_2d":[0,0,0,0]}]}', "") + "\n\n" + ISSUE_PROMPT.format(items=items) + \
            '\nReturn ONLY minified JSON: {"slots":[{"page":1,"role":"...","worker":null,"signed":false,' \
            '"box_2d":[0,0,0,0]}],"issues":[{"id":1,"page":1,"box_2d":[0,0,0,0]}]}'
    content, n = [{"type": "text", "text": text}], 0
    for p in pages:
        b64 = preprocess.page_jpeg(pdf_path, p, dpi)
        if b64:
            content += [{"type": "text", "text": f"PAGE {p}"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]
            n += 1
    if not n:
        return None
    # v8: a trusted (non-lite Flash) answer stands alone; otherwise two DIFFERENT models must agree (box IoU) or nothing is marked
    ans = _vision_answers(content, 8000 + 200 * len(issues or []), n, log_usage, "sigbox", need=2, stop_on_trusted=True)
    if not ans or (len(ans) == 1 and not _trusted(ans[0][1])):
        return None
    data, model = ans[0]
    other = None if _trusted(model) else ans[1][0]

    def box(x):
        b = x.get("box_2d")
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
            return None
        y0, x0, y1, x1 = [max(0.0, min(1.0, v / 1000)) for v in b]
        if x1 <= x0 or y1 <= y0 or x.get("page") not in pages:
            return None
        return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]
    def iou(a, b):
        w = min(a[2], b[2]) - max(a[0], b[0]); h = min(a[3], b[3]) - max(a[1], b[1])
        if w <= 0 or h <= 0:
            return 0.0
        i = w * h
        return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)

    def agrees(b, pg, cands):
        return other is None or any(c[0] == pg and iou(b, c[1]) >= 0.3 for c in cands)
    o_slots = [(sl.get("page"), box(sl)) for sl in (other or {}).get("slots") or [] if isinstance(sl, dict)]
    o_slots = [(p, b) for p, b in o_slots if b]
    out = []
    for sl in (data.get("slots") or []) if need_sig else []:
        b = box(sl)
        if b and agrees(b, sl["page"], o_slots):
            out.append({"role": str(sl.get("role") or "TTD")[:80], "worker": sl.get("worker") or None,
                        "page": sl["page"], "bbox": b, "signed": sl.get("signed") is True})
    if issues is None:
        return out
    ids = {i["id"] for i in issues}
    got = {}
    for x in data.get("issues") or []:
        b = box(x) if isinstance(x, dict) else None
        if b and x.get("id") in ids and x["id"] not in got and agrees(b, x["page"], [
                (y.get("page"), box(y)) for y in (other or {}).get("issues") or []
                if isinstance(y, dict) and y.get("id") == x["id"] and box(y)]):
            got[x["id"]] = {"page": x["page"], "bbox": b}
    return out, got



GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
GROQ_BASE = "https://api.groq.com/openai/v1"
GEMINI_MODELS = ("gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemini-flash-lite-latest,"
                 "gemini-3-flash-preview,gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash,gemma-4-31b-it,gemma-4-26b-a4b-it")
PROVIDERS = ["gemini", "gemini2", "openrouter", "groq"]  # admin key manager slots


def _key(provider, envname):
    return KEY_LOOKUP(provider) or _env(envname) or ""


def _cap(name, default):
    env = {"9router": "NINEROUTER"}.get(name, name.upper())
    return int(_env(env + "_MAX_CALLS_DAY", str(default)))


def _free_9router(m):
    """9router: Gemini free-tier key models, or OpenRouter ':free' ids. Paid providers (deepseek, ...) refused."""
    return m.startswith("gemini/") or (m.startswith("openrouter/") and bool(FREE_RE.match(m[len("openrouter/"):])))


def layers():
    """Chain order (Boss v3): gemini direct (+ 2nd project key) -> openrouter :free -> groq -> 9router rotation."""
    gm = _env("GEMINI_MODEL") or GEMINI_MODELS
    return [
        OpenAIEngine(name="gemini", base=_env("GEMINI_BASE_URL") or GEMINI_BASE, key=_key("gemini", "GEMINI_API_KEY"),
                     models=gm, daily_cap=_cap("gemini", 400)),
        OpenAIEngine(name="gemini2", base=_env("GEMINI_BASE_URL") or GEMINI_BASE, key=_key("gemini2", "GEMINI2_API_KEY"),
                     models=gm, daily_cap=_cap("gemini2", 400)),
        OpenAIEngine(name="openrouter", base=_env("OPENROUTER_BASE_URL") or OPENROUTER_BASE,
                     key=_key("openrouter", "OPENROUTER_API_KEY"), probe="/key", allow=lambda m: bool(FREE_RE.match(m)),
                     models=_env("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free,nvidia/nemotron-3.5-super-vl:free",
                     daily_cap=_cap("openrouter", 45)),
        OpenAIEngine(name="groq", base=_env("GROQ_BASE_URL") or GROQ_BASE, key=_key("groq", "GROQ_API_KEY"),
                     models=_env("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct", daily_cap=_cap("groq", 300)),
        OpenAIEngine("LLM_", "9router", allow=_free_9router, daily_cap=_cap("9router", 300)),
    ]


def claude_allowed():
    return _env("ALLOW_CLAUDE_CLI", "0") == "1"


def get_engine():
    """ENGINE=chain|auto (default): all layers in order; claude -p appended only when ALLOW_CLAUDE_CLI=1.
    ENGINE=<layer name> selects one layer (tests)."""
    eng = (_env("ENGINE", "auto") or "auto").lower()
    ls = layers()
    if eng == "claude" and claude_allowed():
        return ClaudeEngine()
    one = [e for e in ls if e.name == eng]
    if one:
        return ChainEngine(one)
    chain = list(ls)
    if claude_allowed():
        chain.append(ClaudeEngine())
    return ChainEngine(chain)


def test_key(provider):
    """One tiny text call on the provider's first model -> ok | limited | invalid | missing-key | error."""
    e = next((x for x in layers() if x.name == provider), None)
    if not e or not e.key:
        return {"provider": provider, "result": "missing-key"}
    if not e.models:
        return {"provider": provider, "result": "error", "detail": "no free model configured"}
    try:
        r = httpx.post(e.base + "/chat/completions", headers={"Authorization": f"Bearer {e.key}"}, timeout=30,
                       json={"model": e.models[0], "max_tokens": 5, "stream": False,
                             "messages": [{"role": "user", "content": "Reply OK"}]})
        code = r.status_code
    except Exception as ex:
        return {"provider": provider, "result": "error", "detail": type(ex).__name__}
    if code == 200:
        res = "ok"
    elif code in (429, 402):
        res = "limited"
    elif code in (400, 401, 403):
        res = "invalid"
    else:
        res = "error"
    return {"provider": provider, "result": res, "http": code, "model": e.models[0]}
