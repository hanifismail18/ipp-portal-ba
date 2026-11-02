"""PDF -> text layer + preprocessed page images (deskew, contrast, downscale, JPEG) + quality metrics.

Token control: pages with a text layer and no embedded images (no signature/scan/screenshot) are sent as text only.
v4: every page is prepared (no page cap here; the engine splits images into chunks of PAGE_CAP), images are cropped to
their ink bounding box before downscaling, and text-layer pages with an embedded image (screenshot tables) render at
IMG_DPI_EMBED so small table text stays legible after the crop.
"""
import base64, io, os, re, subprocess, tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

DPI_EMBED = os.environ.get("IMG_DPI_EMBED", "220")
MAX_SIDE = int(os.environ.get("IMG_MAX_SIDE", "1600"))
JPEG_Q = int(os.environ.get("IMG_JPEG_QUALITY", "80"))
# quality thresholds (calibrated on the 18 samples: all real samples pass)
BLUR_MIN = float(os.environ.get("QUALITY_BLUR_MIN", "150"))      # variance of Laplacian at 1200px
CONTRAST_MIN = float(os.environ.get("QUALITY_CONTRAST_MIN", "30"))  # p99-p1 gray range


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def page_count(pdf):
    m = re.search(r"Pages:\s+(\d+)", sh(["pdfinfo", str(pdf)]).stdout)
    return int(m.group(1)) if m else 0


def page_texts(pdf, n):
    out = []
    for p in range(1, n + 1):
        t = sh(["pdftotext", "-layout", "-f", str(p), "-l", str(p), str(pdf), "-"]).stdout
        out.append(t if len(re.sub(r"\s", "", t)) > 40 else "")
    return out


def pages_with_images(pdf):
    """Pages carrying a non-trivial embedded image (signature, scan, screenshot). Tiny cell fills are ignored."""
    pages = set()
    for line in sh(["pdfimages", "-list", str(pdf)]).stdout.splitlines()[2:]:
        f = line.split()
        if len(f) > 4 and f[2] == "image":
            try:
                w, h = int(f[3]), int(f[4])
            except ValueError:
                continue
            if w >= 40 and h >= 30 and w * h >= 4000:
                pages.add(int(f[0]))
    return pages


def estimate_skew(gray):
    """Projection-profile skew estimate in degrees (-6..6). Works on text documents."""
    small = cv2.resize(gray, None, fx=800 / max(gray.shape), fy=800 / max(gray.shape)) if max(gray.shape) > 800 else gray
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = bw.shape
    best, best_a = -1, 0.0
    for a in np.arange(-6, 6.01, 0.25):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
        rot = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        s = np.var(rot.sum(axis=1))
        if s > best:
            best, best_a = s, a
    return float(best_a)


def autocrop(img, gray, pad=12):
    """Crop white margins (ink bounding box + pad). Saves tokens and raises effective resolution after downscale."""
    bg = float(np.median(gray))
    ys, xs = np.where(gray < bg - 40)
    if len(xs) < 50:
        return img, gray
    y0, y1 = np.percentile(ys, [0.05, 99.95]).astype(int)
    x0, x1 = np.percentile(xs, [0.05, 99.95]).astype(int)
    y0, x0 = max(0, y0 - pad), max(0, x0 - pad)
    y1, x1 = min(gray.shape[0], y1 + pad + 1), min(gray.shape[1], x1 + pad + 1)
    if (y1 - y0) * (x1 - x0) > 0.92 * gray.size:
        return img, gray
    return img[y0:y1, x0:x1], gray[y0:y1, x0:x1]


def process_image(png_path, scanned):
    img = cv2.imread(str(png_path))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = {}
    if scanned:
        a = estimate_skew(gray)
        m["skew_deg"] = a
        if abs(a) >= 0.5:
            h, w = gray.shape
            M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
            img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    p_lo, p99 = np.percentile(gray, [0.5, 99])
    m["contrast"] = float(p99 - p_lo)
    bg = float(np.median(gray))
    m["ink"] = float((gray < bg - 25).mean())  # share of pixels clearly darker than paper; ~0 = (near) blank page
    if scanned and m["contrast"] < 200:  # stretch low contrast
        lo, hi = np.percentile(gray, [1, 99.5])
        img = np.clip((img.astype(np.float32) - lo) * 255.0 / max(1, hi - lo), 0, 255).astype(np.uint8)
        m["contrast_stretched"] = True
    g2 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)  # sharpness measured AFTER enhancement (what the LLM sees)
    g12 = cv2.resize(g2, None, fx=1200 / max(g2.shape), fy=1200 / max(g2.shape))
    m["blur"] = float(cv2.Laplacian(g12, cv2.CV_64F).var())
    m["blank"] = m["contrast"] < 6 and m["ink"] < 0.003
    if not m["blank"]:  # metrics above stay on the full page (calibrated thresholds); only the sent image is cropped
        img, _ = autocrop(img, g2)
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if max(pil.size) > MAX_SIDE:
        pil.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=JPEG_Q, optimize=True)
    m["poor"] = bool(scanned and not m["blank"] and (m["blur"] < BLUR_MIN or m["contrast"] < CONTRAST_MIN))
    return base64.b64encode(buf.getvalue()).decode(), m, buf.getvalue()


def prepare(pdf, page_cap=8, save_dir=None):
    """page_cap is kept for the call signature; it is the chunk size used by engines.extract_chunked (v4)."""
    n = page_count(pdf)
    texts = page_texts(pdf, n)
    img_pages = pages_with_images(pdf)
    alerts = []
    images, metrics = [], []
    with tempfile.TemporaryDirectory() as td:
        for p in range(1, n + 1):
            scanned = not texts[p - 1]
            if not scanned and p not in img_pages:
                continue  # text-only page: no image needed
            dpi = "150" if scanned else DPI_EMBED
            sh(["pdftoppm", "-r", dpi, "-png", "-singlefile", "-f", str(p), "-l", str(p), str(pdf), f"{td}/p"])
            b64, m, raw = process_image(f"{td}/p.png", scanned)
            m["page"] = p
            metrics.append(m)
            if m.get("blank"):
                continue  # blank page: nothing to read, don't spend tokens
            images.append({"page": p, "b64": b64})
            if save_dir:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
                (Path(save_dir) / f"p{p}.jpg").write_bytes(raw)
    poor = [m["page"] for m in metrics if m.get("poor")]
    if poor:
        alerts.append(f"Foto/scan kurang jelas (hal. {', '.join(map(str, poor))}), mohon upload ulang dengan scan yang lebih jelas.")
    text = "\n".join(f"--- PAGE {i + 1} ---\n{t}" for i, t in enumerate(texts) if t)
    return {"pages": n, "text": text, "images": images, "metrics": metrics,
            "quality": "poor" if poor else "good", "alerts": alerts}


def strips(pdf, pages, dpi=300, max_w=2000, strip_h=1000, overlap=70, max_strips=12):
    """v5 targeted re-read: attachment page(s) at high DPI, ink-cropped and cut into overlapping horizontal strips
    (each <= max_w x strip_h), so dense DMS tables keep ~12px+ glyphs when the model tiles the image."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        for p in pages:
            sh(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "-f", str(p), "-l", str(p), str(pdf), f"{td}/s"])
            img = cv2.imread(f"{td}/s.png")
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            img, _ = autocrop(img, gray)
            if img.shape[1] > max_w:
                img = cv2.resize(img, None, fx=max_w / img.shape[1], fy=max_w / img.shape[1], interpolation=cv2.INTER_AREA)
            y, part = 0, 1
            while y < img.shape[0] and len(out) < max_strips:
                tile = img[y:y + strip_h]
                ok, buf = cv2.imencode(".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 88])
                out.append({"page": p, "part": part, "b64": base64.b64encode(buf.tobytes()).decode()})
                y += strip_h - overlap
                part += 1
    return out


def page_jpeg(pdf, page, dpi=110, quality=85):
    """v6: one WHOLE page (no crop, no deskew) as base64 JPEG, so model box coordinates are page coordinates."""
    with tempfile.TemporaryDirectory() as td:
        sh(["pdftoppm", "-r", str(dpi), "-jpeg", "-jpegopt", f"quality={quality}", "-singlefile", "-f", str(page),
            "-l", str(page), str(pdf), f"{td}/p"])
        f = Path(td) / "p.jpg"
        return base64.b64encode(f.read_bytes()).decode() if f.exists() else None
