#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Invoice Extractor
=================
Reads PDF invoice files (digital or scanned) and extracts:

    Date, Supplier, Invoice No., Net Amount (GBP), VAT Amount (GBP), Total Amount (GBP)

Works with invoices of many different layouts by combining:
  * text-layer extraction for digital PDFs (PyMuPDF)
  * OCR for scanned PDFs (RapidOCR, runs locally, no internet needed)
  * label + position based field extraction (works across layouts)
  * cross-checking of Net + VAT == Total to repair OCR digit mistakes
  * multi-page grouping (an invoice that runs over several pages)

Usage:
    python invoice_extractor.py --input "D:\\Project\\PDF Invoice Scanner" \
                                --output invoices_extracted.csv --dpi 300

Outputs (in the output folder):
    invoices_extracted.csv      main results, one row per invoice/document
    extraction_issues.csv       QA list of records missing important fields
    ocr_text/<pdf>/page_XXX.txt per-page OCR text, for manual verification

Requirements:
    pip install pymupdf rapidocr-onnxruntime
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False

try:
    from rapidocr_onnxruntime import RapidOCR
    HAVE_RAPID = True
except Exception:  # pragma: no cover - import may fail if not installed
    HAVE_RAPID = False


# --------------------------------------------------------------------------
# Small OCR/language helpers
# --------------------------------------------------------------------------

MONTHS_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTHS_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

WORDS_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}


def words_to_number(text: str):
    """Convert simple English amounts like 'FORTY POUNDS ONLY' -> 40."""
    if not text:
        return None
    words = re.findall(r"[a-zA-Z]+", text.lower())
    current = 0
    found = False
    for w in words:
        if w not in WORDS_NUM:
            continue
        found = True
        v = WORDS_NUM[w]
        if v == 100:
            current = (current or 1) * 100
        else:
            current += v
    if not found:
        return None
    return float(current) if current else None


def fix_ocr_digits(s: str) -> str:
    """Repair common OCR confusions that affect numbers."""
    if not s:
        return s
    s = s.translate(str.maketrans({
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "．": ".", "，": ",", "：": ":", "：": ":",
    }))
    s = s.replace("口", "0")
    # 'O'/'o'/'E' next to digits often means zero
    s = re.sub(r"[OoEe](?=\d)", "0", s)
    s = re.sub(r"(?<=\d)[OoEe]", "0", s)
    s = re.sub(r"(?<=\d)[LlI](?=\d)", "1", s)
    # leading 'o.'/'O.'/'E.' before digits -> 0.
    s = re.sub(r"^[oOeE](?=[\d.,])", "0", s)
    return s


def parse_number(s: str):
    """Parse an OCR amount like '1,230.00', '91,00', '11:13', '13074'."""
    if not s:
        return None
    t = fix_ocr_digits(s.strip())
    t = re.sub(r"\b(?:GBP|EUR|EUROS?|POUNDS?|USD)\b", " ", t, flags=re.I)
    t = re.sub(r"[£€$]", " ", t)
    t = t.replace(" ", "")
    t = t.replace(":", ".")
    m = re.search(r"[-+]?\d[\d.,]*", t)
    if not m:
        return None
    raw = m.group(0)
    if not re.fullmatch(r"[-+]?\d[\d.,]*", raw):
        return None
    neg = raw.startswith("-")
    body = raw.lstrip("+-")
    if "," in body and "." in body:
        if body.rfind(".") > body.rfind(","):
            body = body.replace(",", "")
        else:
            body = body.replace(".", "").replace(",", ".")
    elif "," in body:
        parts = body.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2 and len(parts[0]) <= 3:
            body = body.replace(",", ".")
        else:
            body = body.replace(",", "")
    try:
        v = float(body)
    except ValueError:
        return None
    if abs(v) > 999_999.99:
        return None
    return -v if neg else v


def extract_amount(text: str):
    """Best-effort amount extraction from an OCR token string."""
    if not text:
        return None
    t = fix_ocr_digits(text)
    # "FORTY POUNDS ONLY" style wording
    if re.search(r"\b(pounds|gbp|euros)\b", t, re.I) and \
       re.search(r"[a-zA-Z]{3,}", t) and not re.search(r"\d", t):
        return words_to_number(t)
    cands = re.findall(r"[-+]?\d[\d.,: ]*", t)
    for c in reversed(cands):
        v = parse_number(c)
        if v is not None:
            return v
    return None


def clean_amount_text(text: str) -> str:
    """Strip currency words/symbols and OCR junk from a value token."""
    if not text:
        return text
    t = re.sub(r"\b(?:GBP|EUR|EUROS?|POUNDS?|USD)\b", " ", text, flags=re.I)
    t = t.replace("£", " ").replace("€", " ").replace("$", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" :：|/-_.,")


# --------------------------------------------------------------------------
# Token model
# --------------------------------------------------------------------------

class Token:
    __slots__ = ("text", "x0", "y0", "x1", "y1", "conf", "sp", "sq")

    def __init__(self, text, x0, y0, x1, y1, conf=1.0):
        self.text = text.strip()
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.conf = float(conf)
        self.sp = re.sub(r"\s+", " ", self.text.lower()).strip()
        self.sq = re.sub(r"[^a-z0-9]", "", self.text.lower())

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


def words_to_tokens(words):
    """Group PyMuPDF 'words' (x0,y0,x1,y1,word,...) into line tokens."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w[1], w[0]))
    lines = []
    for w in words:
        placed = False
        for ln in lines:
            if abs(ln["y"] - (w[1] + w[3]) / 2) <= 8:
                ln["items"].append(w)
                placed = True
                break
        if not placed:
            lines.append({"y": (w[1] + w[3]) / 2, "items": [w]})
    toks = []
    for ln in lines:
        ln["items"].sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in ln["items"])
        x0 = min(w[0] for w in ln["items"])
        y0 = min(w[1] for w in ln["items"])
        x1 = max(w[2] for w in ln["items"])
        y1 = max(w[3] for w in ln["items"])
        toks.append(Token(text, x0, y0, x1, y1, 1.0))
    return toks


def rapid_to_tokens(result):
    toks = []
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        toks.append(Token(text, min(xs), min(ys), max(xs), max(ys), score))
    return toks


# --------------------------------------------------------------------------
# Page loading / OCR with cache
# --------------------------------------------------------------------------

def page_height_scale(page, dpi):
    """Scale factor relative to A4 @ 300 dpi (3508 px tall)."""
    px_h = page.rect.height * dpi / 72.0
    return max(px_h / 842.0, 0.5)


def get_page_tokens(pdf_path, doc, page_idx, ocr, cache_dir, dpi):
    page = doc[page_idx]
    scale = page_height_scale(page, dpi)
    words = page.get_text("words")
    if words:
        return words_to_tokens(words), "text"
    stem = Path(pdf_path).stem
    json_path = cache_dir / f"{stem}_p{page_idx + 1:03d}.json"
    if not json_path.exists():
        png_path = cache_dir / f"{stem}_p{page_idx + 1:03d}.png"
        if not png_path.exists():
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
            pix.save(str(png_path))
        result, _elapse = ocr(str(png_path))
        items = []
        for box, text, score in (result or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append({
                "text": text,
                "x0": round(min(xs), 1),
                "y0": round(min(ys), 1),
                "x1": round(max(xs), 1),
                "y1": round(max(ys), 1),
                "conf": round(float(score), 3),
            })
        json_path.write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
    items = json.loads(json_path.read_text(encoding="utf-8"))
    toks = [Token(it["text"], it["x0"], it["y0"], it["x1"], it["y1"], it["conf"])
            for it in items]
    return toks, "ocr"


# --------------------------------------------------------------------------
# Line grouping (for supplier detection and reading order)
# --------------------------------------------------------------------------

def group_lines(tokens, scale):
    """Group tokens into visual lines using vertical overlap, then split
    far-apart blocks (e.g. a logo and a company name on the same row)."""
    heights = sorted(t.y1 - t.y0 for t in tokens)
    med_h = heights[len(heights) // 2] if heights else 40.0
    lines = []
    for t in sorted(tokens, key=lambda t: (t.y0, t.x0)):
        placed = False
        for ln in lines:
            ov = min(ln["y1"], t.y1) - max(ln["y0"], t.y0)
            if ov > 0.3 * min(med_h, t.y1 - t.y0):
                ln["items"].append(t)
                placed = True
                break
        if not placed:
            lines.append({"y0": t.y0, "y1": t.y1, "y": t.cy, "items": [t]})
    gap_min = max(120, 45 * scale)
    for ln in lines:
        ln["items"].sort(key=lambda t: t.x0)
        blocks = []
        cur = [ln["items"][0]]
        for t in ln["items"][1:]:
            if t.x0 - cur[-1].x1 > gap_min:
                blocks.append(cur)
                cur = [t]
            else:
                cur.append(t)
        blocks.append(cur)
        ln["items"] = [t for b in blocks for t in b]
        ln["text"] = " || ".join(
            " ".join(t.text for t in b) for b in blocks)
    lines.sort(key=lambda ln: ln["y"])
    return lines


# --------------------------------------------------------------------------
# Supplier detection
# --------------------------------------------------------------------------

CUSTOMER_HINTS = (
    "top removals", "topremovals", "perushanov", "unit 76", "unit c1a",
    "roding", "london industrial", "thurrock", "kerry avenue", "purfleet",
    "e6 6ls", "e66ls", "removals", "bill to", "invoice to", "customer:",
    "to:", "ship to", "sold to", "billed to", "delivered to", "from:",
    "sent:", "subject:", "tax invoice for", "invoice for",
)

NOISE_STARTS = (
    "invoice", "receipt", "statement", "vatinvoice", "ukvatinvoice",
    "taxinvoice", "page", "account", "customer", "bill", "due", "terms",
    "payment", "currency", "tax", "vat", "net", "gross", "subtotal",
    "total", "order", "balance", "amount", "paid", "phone", "tel", "fax",
    "www", "email", "e-mail", "website", "support", "help", "sales",
    "services", "service", "reference", "ref:", "no:", "number",
    "invoice no", "invoicenumber", "transaction", "ways to pay",
    "office reference", "account reference", "your reference", "our reference",
    "billing", "statement period", "statement date", "statement number",
    "date:", "document", "identifier", "transfer", "from", "to ",
    "registration", "company id", "company no", "company number",
    "registered", "bank", "sort", "iban", "swift", "bic", "eori", "coc",
    "kvk", "gst", "sold by", "business address", "delivery address",
    "invoice date", "invoice number", "invoice no", "invoice#", "bill to",
    "attn", "please", "note", "note:", "thank", "if", "you", "the",
    "for", "with", "this", "and", "or", "of", "in", "on", "at",
    "original", "plus", "start", "hours",
    "finish", "van", "call", "visit", "review", "sent", "to", "cc",
    "issue", "telephone", "accounts", "store", "man", "men",
    "description", "quantity", "price", "unit cost", "item", "unpaid",
    "stripe", "postpaid", "billing method", "status", "leads",
)

ADDRESS_HINTS = (
    "road", "street", "st ", "ave", "avenue", "lane", "court", "close",
    "boulevard", "walk", "business campus", "centre", "center", "park",
    "trading estate", "industrial", "buildings", "square", "drive",
    "london", "essex", "surrey", "dublin", "ireland", "lreland", "gloucester",
    "derby", "richmond", "hamilton", "southampton", "holland", "zealand",
    "netherlands", "germany", "bulgaria", "varna", "united", "kingdom",
    "britain", "england", "wales", "rm1", "e6", "sw1", "n11", "tw9",
    "ub6", "ig6", "ig8", "de1", "gl1", "so45", "ws5", "n2 9ed", "campus",
    "house", "forum", "zealand",
)

COMPANY_SUFFIX_RE = re.compile(
    r"\b(ltd\.?|limited|gmbh|b\.?v\.?|p\.?l\.?c\.?|l\.?l\.?p\.?|inc\.?|corp\.?|"
    r"co\.?|kg|ab|oy|s\.?p\.?a\.?)\b",
    re.I,
)

SUPPLIER_ALIASES = {
    "octopusenergy": "Octopus Energy",
    "reloadvisor": "ReloAdvisor",
    "reloadvisorbv": "ReloAdvisor BV",
    "allstaronline": "Allstar",
    "allstar": "Allstar",
    "dkveuroservice": "DKV EuroService",
    "dkv": "DKV",
    "ionoscloud": "IONOS Cloud",
    "ionoscloudltd": "IONOS Cloud Ltd",
    "digitalfive": "digitalfive",
    "westminstercitycouncil": "Westminster City Council",
    "checked": "CheckedSafe",
    "michelinconnectedfleet": "Michelin Connected Fleet",
    "michelin": "Michelin",
    "ringcenfral": "RingCentral",
    "ringcentral": "RingCentral",
    "dkveuroservicegmbh+cokg": "DKV EuroService GmbH + Co. KG",
}


def clean_supplier(name: str) -> str:
    if not name:
        return name
    n = re.sub(r"[\"'\u201c\u201d\u2018\u2019\uff02]", "", name.strip())
    n = n.strip(".:,;")
    if " - " in n:
        n = n.split(" - ")[0].strip()
    # remove trailing generic words
    for w in (
        "cash sale", "transaction receipt", "vat invoice", "uk vat invoice",
        "tax invoice", "invoice", "receipt", "statement", "e-summary",
        "e-invoice", "for business", "team", "your halfords", "ltd.",
    ):
        if n.lower().endswith(w):
            n = n[: -len(w)].strip()
    if n.lower().startswith("your "):
        n = n[5:].strip()
    # camel-case split for merged brand names
    n = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])", " ", n)
    # insert space before attached legal suffixes
    n = re.sub(r"(?i)(?<=[A-Z0-9])(?=(?:Ltd|Limited|GmbH|BV|PLC|LLP|Inc|Co\.?|KG)\b)", " ", n)
    # collapse repeated words ("IONOS IONOS Cloud Ltd" -> "IONOS Cloud Ltd")
    for _ in range(3):
        n = re.sub(r"\b(\w+)\s+\1\b", r"\1", n, flags=re.I)
    # cut address text after the legal suffix when separated by a comma
    if "," in n:
        m = re.match(
            r"^(.+?(?:Ltd\.?|Limited|GmbH|BV|PLC|LLP|Inc\.?|Co\.?|KG))[\s,]+",
            n, re.I)
        if m:
            n = m.group(1)
    n = re.sub(r"[.]+$", "", n.strip())
    n = re.sub(r"\s+", " ", n).strip()
    if len(n) <= 2:
        return n
    if re.match(r"^\d+\s*[A-Za-z]", n) and len(n) > 3:
        n = re.sub(r"^\d+\s*", "", n).strip()  # '7Wise'/'7 Wise' -> 'Wise'
    key = n.lower().replace(" ", "").replace(".", "").replace(",", "")
    if n.islower() and len(n) > 2:
        n = n.title()
    return SUPPLIER_ALIASES.get(key, n)


def is_noise_token(t: Token, page_text_lower: str) -> bool:
    """True when an OCR token cannot be a supplier name."""
    text = t.text
    sp = t.sp
    sq = t.sq
    if len(sq) <= 2:
        return True
    if any(h in sp for h in CUSTOMER_HINTS):
        return True
    if re.match(r"^\s*unit\b", sp):
        return True
    if re.fullmatch(r"reg\.?|gbp|eur|euro|net|vat|gross|total|amount|paid|company|"
                    r"invoice|receipt|statement|accounts|terms|details|summary", sp):
        return True
    if re.search(r"\d{4,}", sp) and not re.search(r"[a-z]{3,}", sp):
        return True  # pure numbers / amounts
    if re.match(r"^\d+[\s\-/&]", sp):
        return True  # numbers, dates, ranges, "1&2TheExchange"
    if re.match(r"^\d{1,2}[A-Za-z]{2,}", sp):
        if not re.match(r"^\d{1,2}wise", sp):
            return True  # "08January 2026", "09/01/2026", "01Jan2026"
    if re.match(r"^[A-Za-z]{1,6}\d{3,}$", sp):
        return True  # IDs like G8997857229, DIM048
    if re.match(r"^[A-Za-z]{2,}[\- ]\d{2,}", sp):
        return True  # codes like CSAFE-100936, DE/125747
    if re.match(r"^[A-Za-z]{1,2}\d{1,2}[A-Za-z]{0,2}\d[A-Za-z]{2}$", sp):
        return True  # postcodes: GL11AU, TW93LU, N29ED
    if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d", sp):
        return True  # dates like "January 06, 2026 13:51:25 GMT"
    if re.match(r"^(man|men)\s+and\b", sp):
        return True  # item descriptions "Man and van"
    if "!" in text or "?" in text:
        return True
    if re.search(r"\b(?:hours|finish|start|trading name of|gov\.uk|barclays|"
                 r"account name|funded|paid out|membership|your details|"
                 r"transfer overview|amount converted|exchange rate|ways to pay|"
                 r"billing point|service hours|need help|phone support|help &|"
                 r"a helping hand|statement|summary|ticket|parking|charge|"
                 r"delivery|shipping|payment reference|thank you|paid out|pald out|"
                 r"we now offer|we offer|our bank|our reference)\b", sp):
        return True
    if re.search(r"\b(?:st|rd|sl|ave|dr|lane|court|close|way|park|square|"
                 r"drive|street|road)\b\.?", sp) and \
            not COMPANY_SUFFIX_RE.search(sp):
        return True  # street suffixes
    if any(w in sp for w in (
            "avenue", "road", "street", "lane", "court", "close", "drive",
            "square", "centre", "center", "building", "walk", "boulevard",
            "trading estate", "industrial park")) and \
            not COMPANY_SUFFIX_RE.search(sp):
        return True  # address text merged into one OCR token (KERRYAVENUE)
    if re.search(r"\breg\w*ration\b", sp):
        return True
    if re.match(r"^(?:a|an|the|you|we|please|this)\s", sp):
        return True
    sp_ocr = sp.replace("0", "o")
    if re.match(r"^supp+o?rt", sp_ocr) or re.match(r"^c?call\d", sp):
        return True
    first = re.split(r"[\s:]+", sp, 1)[0]
    if first in NOISE_STARTS or sp.startswith(NOISE_STARTS):
        return True
    if re.match(r"^(?:no|ref|#)\s*[:#]?\s*\d", sp):
        return True
    if re.match(r"^po\s*box", sp):
        return True
    if re.match(r"^[(\[]", sp):
        return True
    # invoice/date label variants with OCR letters (lnvoice, INVO1CE)
    if re.search(r"(?:^|\s)[il1]n?vo[il1]ce", sp):
        return True
    if re.match(r"^(?:invoice|receipt|statement)", sp):
        return True
    # address-ish (keep names that carry a legal suffix)
    def has_addr_hint():
        for h in ADDRESS_HINTS:
            if " " in h or h.endswith(" "):
                if h in sp:
                    return True
            elif re.search(r"\b" + re.escape(h) + r"\b", sp):
                return True
        return False
    if has_addr_hint() and not COMPANY_SUFFIX_RE.search(sp) \
            and not re.search(r"\b(for|of|the)\b", sp):
        return True
    if re.search(r"@|www\.|gov\.uk|tel|phone|fax|support|\.co\.uk|https?:", sp):
        return True
    if re.fullmatch(r"[\d.,\-]+", sq):
        return True
    return False


def detect_supplier(tokens, page_h, page_text_lower):
    # --- targeted rules for specific layout conventions -------------------
    for t in tokens:
        m = re.match(r"your\s+([A-Za-z][\w'&.\- ]*?)\s+team\b", t.text, re.I)
        if m:
            return clean_supplier(m.group(1))
    for t in tokens:
        m = re.match(r"sold\s+by\s+(.+)", t.text, re.I)
        if m:
            return clean_supplier(m.group(1))
    if "dart charge" in page_text_lower:
        return "Dart Charge"
    # ----------------------------------------------------------------------
    band = page_h * 0.32
    top_tokens = [t for t in tokens if t.y0 < band]
    cands = []
    for t in sorted(top_tokens, key=lambda t: (t.y0, t.x0)):
        if is_noise_token(t, page_text_lower):
            continue
        cleaned = clean_supplier(t.text)
        if cleaned and len(cleaned) > 2:
            cands.append((t.y0, t.x0, cleaned))
    if not cands:
        # fallback: any line on the page that carries a company suffix
        for t in sorted(tokens, key=lambda t: (t.y0, t.x0)):
            if is_noise_token(t, page_text_lower):
                continue
            cleaned = clean_supplier(t.text)
            if cleaned and len(cleaned) > 2 and COMPANY_SUFFIX_RE.search(cleaned):
                cands.append((t.y0, t.x0, cleaned))
    if not cands:
        # fallback: website domain
        for t in tokens:
            m = re.search(r"www[.\s]*([\w\-\.]+)", t.text, re.I)
            if m:
                parts = m.group(1).lower().split(".")
                base = parts[0] if len(parts) <= 2 else parts[0]
                for suf in ("online", "removals", "group", "services", "solutions", "leads"):
                    if base.endswith(suf) and len(base) > len(suf) + 3:
                        base = base[: -len(suf)]
                if base:
                    return clean_supplier(base)
    if not cands:
        # fallback: email 'From:' name
        for t in tokens:
            m = re.match(r"from\s*:\s*([^<\n]+)", t.text, re.I)
            if m:
                nm = clean_supplier(m.group(1))
                if nm and not re.search(r"\b(?:transaction|citypay)\b", nm, re.I):
                    return nm
                return None
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    # prefer a name with a legal suffix, otherwise the first (topmost) name
    with_suffix = [c for c in cands if COMPANY_SUFFIX_RE.search(c[2])]
    chosen = with_suffix[0] if with_suffix else cands[0]
    name = chosen[2]
    # join a short logo fragment with the following name fragment (MICHELIN + CONNECTED FLEET)
    if not COMPANY_SUFFIX_RE.search(name):
        for c in cands:
            if c is chosen:
                continue
            if 0 <= c[0] - chosen[0] <= 140 and not COMPANY_SUFFIX_RE.search(c[2]) \
                    and not re.search(r"\d", c[2]) and len(c[2]) <= 24 \
                    and abs(c[1] - chosen[1]) < 400:
                name = f"{name} {c[2]}"
                break
    return clean_supplier(name)


# --------------------------------------------------------------------------
# Label-based field extraction
# --------------------------------------------------------------------------

INV_LABELS = [
    (r"^invo[i1l]ce\s*(?:no\.?|number|#|id|ref(?:erence)?)(?!\s*[a-z])", 100),
    (r"^invo[i1l]ce\s+(?=\d)", 95),
    (r"^invo[i1l]ce\s*(?:no\.?|#|num(?:ber)?)?\s*[:#：]?\s*$", 90),
    (r"document\s*number", 80),
    (r"bill\s*(?:number|no\.?|nur?mber)", 80),
    (r"(?:^|\s)statement(?:\s*(?:no\.?|number))?\b(?!\s+(?:summary|period|date))", 45),
    (r"(?:^|\s)transaction(?:\s*(?:no\.?|number|#))?\b(?!\s+(?:details|information|summary|receipt|date|id|identifier))", 45),
    (r"^receipt(?:\s*(?:no\.?|number|#))?", 40),
    (r"^ref(?:erence)?\s*[:#：]?\s*$|^our\s*ref", 35),
]

DATE_LABELS = [
    (r"invoice\s*(?:date|dated|dale)\b|invoice\s*/?\s*taxpoint\s*date", 100),
    (r"date\s*issued|issue\s*date", 95),
    (r"bill\s*(?:date|dute|dated)\b", 90),
    (r"document\s*date", 90),
    (r"tax\s*point\b", 85),
    (r"date\s+and\s+time", 75),
    (r"statement\s*date", 70),
    (r"transfer\s*created", 65),
    (r"billing\s*from\s*date", 60),
    (r"(?:^|\b)(?<!due\s)(?<!delivery\s)(?<!order\s)(?<!payment\s)date\s*[:#]", 50),
    (r"^date\s+(?=\d|[A-Za-z])", 50),
    (r"^date\s*$", 50),
    (r"period\s*from", 40),
]

NET_LABELS = [
    (r"net\s*amount", 100),
    (r"net\s*total|total\s*net", 98),
    (r"total\s*excl(?:uding)?\.?\s*vat", 95),
    (r"total\s*ex\.?\s*vat", 95),
    (r"goods\s*total", 95),
    (r"^sub\s*total$|^subtotal$|^subtotal\s*[:#]", 93),
    (r"electricity\s*charges\s*exc", 90),
    (r"net\s*charges", 90),
    (r"total\s*\(ex\.?\s*vat\)", 90),
    (r"^net\s*$|^net\s*\(?gbp\)?$|^net\s*egbp$|^net\s*e?gbp$", 85),
    (r"^sub\s*total\s*\(?net", 93),
]

VAT_LABELS = [
    (r"vat\s*(?:amount|total)\b|total\s*vat\b|v\.?a\.?t\.?\s*total", 100),
    (r"vat\s*sub(?:l|1)?otal|sub(?:l|1)?otal\s*vat", 95),
    (r"^v\.?a\.?t\.?\s*@?\s*\d", 90),
    (r"^\+?\s*vat\s*\(?\d", 90),
    (r"subscription\s*vat", 90),
    (r"^vat\s*[:#]?$|^vat\s*[:#]", 85),
    (r"vat\s*out\s*of\s*scope", 85),
    (r"tax\s*total|taxes?\s*$", 80),
    (r"^taxes?\s*$", 78),
    (r"vat\s*@", 75),
]

TOTAL_LABELS = [
    (r"grand\s*tota[il1_]*\s*\(?gbp\)?", 100),
    (r"invoice\s*total", 99),
    (r"(?:^|\s)total\s*due\b", 98),
    (r"(?:^|\s)amount\s*due\b(?!\s+(?:will|is|to|on|by|for|of|the|be))", 98),
    (r"(?:^|\s)total\s*payable\b", 98),
    (r"total\s*invoice\s*amount\s*payable", 99),
    (r"total\s*including\s*vat|total\s*incl\.?\s*vat|total\s*inc\s*vat", 97),
    (r"total\s*charges\s*\(?inc", 97),
    (r"gross\s*amount|gross\s*total|^gross\s*$", 96),
    (r"total\s*charges\s*for\s*bill|total\s*charges|total\s*cost", 95),
    (r"balance\s*due", 94),
    (r"tota[il1_]*\s*\(?gbp\)?$|^tota[il1_]*\s*$|^tota[il1_]*\s*[:#：]", 90),
    (r"total\s*in\b|^tata[il1_]*\s*$", 90),
    (r"^charges\s*$", 88),
    (r"^total\s*$", 90),
    (r"^amount\s*$", 60),
]


def sq_variant(pattern: str) -> str:
    """Convert a spaced regex into a no-space variant for tight OCR text."""
    return pattern.replace(r"\s*", "").replace(r"\s+", "")


def find_value_candidates(tokens, label_tok, scale, numeric_only=True, page_h=None):
    """Yield value tokens in priority order: same visual line to the right
    (closest vertical match first), then tokens just below the label,
    and finally tokens above the label (for layouts where the value sits
    above its label, only considered in the lower half of the page)."""
    line_tol = max(10, 22 * scale)
    same_line = []
    for t in tokens:
        if t is label_tok:
            continue
        if numeric_only and not re.search(r"\d", t.text):
            continue
        same_row = abs(t.cy - label_tok.cy) <= line_tol
        to_right = t.x0 >= label_tok.x1 - 8 or (0 <= t.x0 - label_tok.x0 <= 400)
        if same_row and to_right:
            same_line.append((abs(t.cy - label_tok.cy), t.x0 - label_tok.x1, t))
    same_line.sort(key=lambda c: (c[0], c[1]))
    for _, _, t in same_line:
        yield t
    below = []
    max_dy = 280 * scale
    for t in tokens:
        if t is label_tok:
            continue
        if numeric_only and not re.search(r"\d", t.text):
            continue
        dy = t.y0 - label_tok.y1
        if 5 <= dy <= max_dy:
            below.append((dy // (45 * scale), abs(t.cx - label_tok.x0), t))
    below.sort(key=lambda c: (c[0], c[1]))
    for _, _, t in below:
        yield t
    if page_h and label_tok.y0 > page_h * 0.5:
        above = []
        max_dy = 280 * scale
        for t in tokens:
            if t is label_tok:
                continue
            if numeric_only and not re.search(r"\d", t.text):
                continue
            dy = label_tok.y0 - t.y1
            if 5 <= dy <= max_dy:
                above.append((abs(t.cx - label_tok.x0), dy // (45 * scale), t))
        above.sort(key=lambda c: (c[0], c[1]))
        for _, _, t in above:
            yield t


def find_value_token(tokens, label_tok, scale, numeric_only=True, page_h=None):
    """First value candidate (kept for compatibility)."""
    for t in find_value_candidates(tokens, label_tok, scale, numeric_only, page_h):
        return t
    return None


def match_label(tok, specs):
    """Return (priority, matched_pattern) for the first spec that matches."""
    for pat, prio in specs:
        if re.search(pat, tok.sp):
            return prio, pat
    return None


def extract_invoice_no(tokens, page_text_lower, scale, page_h=None):
    cands = []
    for t in tokens:
        if re.search(r"[@<>]|email", t.text, re.I):
            continue  # email headers / addresses
        m = match_label(t, INV_LABELS)
        if not m:
            continue
        prio, pat = m
        # embedded value in the same token: "Invoice No.:ABC123"
        emb = re.search(
            r"(?:no\.?|number|nur?mber|#|ref(?:erence)?|document\s*number|statement\s*number|transaction\s*(?:no\.?|number)?)\s*[:#.:]?\s*([A-Z0-9][A-Z0-9/\-_.]{2,})",
            t.text, re.I)
        val = emb.group(1) if emb else None
        if val:
            val = clean_amount_text(val)
            if val.lower().startswith("no"):
                val = re.sub(r"^no\.?\s*", "", val, flags=re.I)
            if re.search(r"\d", val):
                cands.append((prio, t.y0, val, "embedded"))
                continue
        # bare "INVO1CE 183" style: value inside the same token
        bare = re.search(
            r"^invo[i1l]ce\s*(?:no\.?|#|num(?:ber)?)?\s*[:#：.]?\s*([A-Z0-9][A-Z0-9/\-_.]{2,})$",
            t.text, re.I)
        if bare and re.search(r"\d", bare.group(1)):
            cands.append((prio, t.y0, bare.group(1), "embedded"))
            continue
        added = 0
        for vt in find_value_candidates(tokens, t, scale, page_h=page_h):
            vtext = clean_amount_text(vt.text)
            if re.match(r"^(?:no\.?|number|#)\s*[:#]?\s*", vtext, re.I):
                vtext = re.sub(r"^(?:no\.?|number|#)\s*[:#]?\s*", "", vtext, flags=re.I)
            if not vtext:
                continue
            if parse_date(vtext):
                continue  # the value is a date, not an invoice number
            if re.search(r"(?i)(invoice|reference|december|january|february|march|"
                         r"april|june|july|august|september|october|november|"
                         r"payment|status|amount|total|balance)", vtext):
                continue
            if re.match(r"^[A-Za-z]{3,}\d+[A-Za-z]{3,}", vtext):
                continue  # description-like text (UNIT76LONDONINDUSTRIALPARK)
            if re.match(r"^[A-Za-z]{1,2}\d{1,3}[A-Za-z]{0,2}\s?\d[A-Za-z]{2}$",
                        vtext.replace(" ", "")):
                continue  # postcodes (E66LS, TW93LU)
            if re.match(r"^(?:GB|NL|DE|FR|IE|BE|ES|IT|BG)\d{6,}", vtext, re.I):
                continue  # VAT registration numbers
            if re.match(r"^(?:date|total|net|vat|amount|due|payment|terms|currency|"
                        r"invoice|balance|subtotal|gross|paid)", vtext, re.I):
                continue
            if not re.search(r"\d", vtext):
                continue
            # plausibility: invoice numbers usually contain letters or are 4+ digits
            if re.fullmatch(r"\d{1,3}(?:[.,]\d{1,2})?", vtext) and \
                    "invoice" not in page_text_lower:
                continue
            cands.append((prio, t.y0, vtext, "pair"))
            added += 1
            if added >= 3:
                break
    if not cands:
        # bare "No:0000000001" on a page that mentions invoice
        if "invoice" in page_text_lower:
            for t in tokens:
                m = re.match(r"^(?:no\.?|number)\s*[:#：]\s*([A-Z0-9][A-Z0-9/\-_.]{3,})$", t.text, re.I)
                if m:
                    cands.append((70, t.y0, m.group(1), "bare-no"))
    if not cands:
        return None, "pair"
    # small bonus for values that look like invoice numbers (2+ letters + digits)
    def eff_prio(c):
        bonus = 5 if re.match(r"^[A-Z]{2,}\d{4,}", c[2]) else 0
        return c[0] + bonus
    cands.sort(key=lambda c: (-eff_prio(c), c[1]))
    return cands[0][2], cands[0][3]


def parse_date(text: str):
    """Normalise an OCR date string to ISO (YYYY-MM-DD)."""
    if not text:
        return None
    s = text.strip()
    s = s.replace("\u00a0", " ").replace("。", ".")
    s = re.sub(r"[\u201c\u201d\u2018\u2019\"']", "", s)
    s = s.replace("：", ":")

    def to_iso(day, mon, year):
        try:
            day = int(day)
            year = int(year)
            if year < 100:
                year += 2000
            return datetime(year, int(mon), day).date().isoformat()
        except ValueError:
            return None

    starts_with_year = bool(re.match(r"^\s*\d{4}\s*[-/.]", s))
    patterns = [
        (r"\b(\d{1,2})\s*[-/.]\s*([A-Za-z]{3,9})\s*[-/.]\s*(\d{2,4})\b", "dm"),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})[,.]?\s+(\d{4})\b", "dm"),
        (r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*,?\s*(\d{4})\b", "md"),
        (r"\b([A-Za-z]{3,9})\s*(\d{1,2}),?\s*,?\s*(\d{4})\b", "md"),
        (r"\b(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})\b", "dm"),
        (r"\b(\d{1,2})([A-Za-z]{3,9})[,.]?\s+(\d{4})\b", "dm"),
        (r"\b(\d{1,2})([A-Za-z]{3,9})(\d{4})\b", "dm"),
    ]
    if starts_with_year:
        patterns.insert(
            0, (r"\b(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?=\D|$)", "ymd"))
    for pat, kind in patterns:
        m = re.search(pat, s)
        if not m:
            continue
        if kind == "dm":
            d, mon, y = m.groups()
            if mon.isdigit():
                iso = to_iso(d, mon, y)
            elif mon.lower() in MONTHS_ABBR:
                iso = to_iso(d, MONTHS_ABBR[mon.lower()], y)
            elif mon.lower() in MONTHS_FULL:
                iso = to_iso(d, MONTHS_FULL[mon.lower()], y)
            else:
                continue
        elif kind == "md":
            mon, d, y = m.groups()
            if mon.isdigit():
                iso = to_iso(d, mon, y)
            elif mon.lower() in MONTHS_ABBR:
                iso = to_iso(d, MONTHS_ABBR[mon.lower()], y)
            elif mon.lower() in MONTHS_FULL:
                iso = to_iso(d, MONTHS_FULL[mon.lower()], y)
            else:
                continue
        elif kind == "ymd":
            y, mo, d = m.groups()
            iso = to_iso(d, mo, y)
        else:
            iso = None
        if iso:
            return iso
    return None


def extract_date(tokens, scale, page_h=None):
    cands = []
    for t in tokens:
        m = match_label(t, DATE_LABELS)
        if not m:
            continue
        prio, pat = m
        emb = re.search(
            r"(?:invoice\s*(?:date|dated|dale)|date\s*issued|issue\s*date|bill\s*(?:date|dute)|"
            r"document\s*date|tax\s*point|date\s+and\s+time|statement\s*date|"
            r"transfer\s*created|billing\s*from\s*date|^date)\s*[:#.]?\s*([A-Za-z0-9][A-Za-z0-9\s\-/,.:]{3,30})",
            t.text, re.I)
        if emb:
            iso = parse_date(emb.group(1))
            if iso:
                cands.append((prio, t.y0, iso))
            continue
        for vt in find_value_candidates(tokens, t, scale, page_h=page_h):
            iso = parse_date(vt.text)
            if iso:
                cands.append((prio, t.y0, iso))
                break
    if not cands:
        # fallback: first token on the page containing a parseable date
        for t in sorted(tokens, key=lambda t: t.y0):
            iso = parse_date(t.text)
            if iso:
                cands.append((30, t.y0, iso))
                break
    if not cands:
        return None
    cands.sort(key=lambda c: (-c[0], c[1]))
    return cands[0][2]


def pick_amount(cands, net=None, vat=None, total=None, field="total"):
    """Choose among amount candidates: highest priority, then bottom-most,
    but use Net + VAT == Total as a cross-check to resolve OCR ambiguity."""
    if not cands:
        return None
    target = None
    if field == "total" and net is not None and vat is not None:
        target = round(net + vat, 2)
    elif field == "vat" and net is not None and total is not None:
        target = round(total - net, 2)
    elif field == "net" and vat is not None and total is not None:
        target = round(total - vat, 2)
    if target is not None:
        ordered = sorted(cands, key=lambda c: (-c[0], -c[1]))
        for c in ordered:
            if abs(c[2] - target) <= 0.02:
                return c
        if field == "vat":
            for c in ordered:
                if net and (abs(c[2] - 0.2 * net) <= 0.05 or
                            abs(c[2] - 0.05 * net) <= 0.05):
                    return c
    best = max(cands, key=lambda c: (c[0], c[1]))
    return best


def extract_amount_field(tokens, specs, scale, net=None, vat=None, total=None,
                         field="total", page_h=None, page_text=""):
    cands = []
    for t in tokens:
        m = match_label(t, specs)
        if not m:
            continue
        prio, pat = m
        # embedded value: "Net Amount:40.00", "Total Due:48.00", "VAT:2,300.00"
        emb = re.search(
            r"(?:net\s*amount|net\s*total|vat\s*amount|vat\s*total|total\s*due|amount\s*due|"
            r"invoice\s*total|sub\s*total|subtotal|goods\s*total|gross\s*amount|"
            r"total\s*including\s*vat|balance\s*due|grand\s*total|total\s*payable|"
            r"^vat\s*|^net\s*|^total\s*|^sub\s*total)\s*[:#.]?\s*([-+]?[\d.,:]+)",
            t.text, re.I)
        val = None
        if emb:
            val = extract_amount(emb.group(1))
            if val is not None:
                cands.append((prio, t.y0, val, t.text))
        if val is None:
            found = 0
            for vt in find_value_candidates(tokens, t, scale, numeric_only=False,
                                            page_h=page_h):
                vtext = clean_amount_text(vt.text)
                if re.fullmatch(r"n/?a\.?|zero|nil", vtext, re.I):
                    cands.append((prio, t.y0, 0.0, t.text))
                    found = 1
                    break
                if "'" in vtext or '"' in vtext:
                    continue  # OCR garbage like "55'6"
                letters = re.sub(r"(?i)(?:gbp|eur|pounds?|euros?|inc|excl|vat|net|total|only)",
                                 "", vtext)
                if not re.search(r"[a-zA-Z]", letters) or \
                        re.search(r"\b(pounds|euros?|gbp)\b", vtext, re.I):
                    if "%" not in vtext or re.search(r"\d", vtext.replace("%", "")):
                        v = extract_amount(vtext)
                        if v is not None:
                            cands.append((prio, t.y0, v, t.text))
                            found += 1
                            if found >= 3:
                                break
    if not cands:
        if field == "total" and page_text and (
                "invoice" in page_text or "receipt" in page_text):
            # bare "GBP16.80" / "1,069.48GBP" style tokens
            for t in tokens:
                m = re.search(r"(?:^|\s)(?:GBP|£|€)\s*([\d.,]+)", t.text, re.I) or \
                    re.search(r"([\d.,]{2,})\s*(?:GBP|£|€)\s*$", t.text, re.I)
                if m:
                    v = parse_number(m.group(1))
                    if v is not None:
                        cands.append((40, t.y0, v, t.text))
        if not cands:
            return None
    chosen = pick_amount(cands, net, vat, total, field)
    return round(chosen[2], 2) if chosen else None


# --------------------------------------------------------------------------
# Document classification
# --------------------------------------------------------------------------

def classify_document(page_text_lower):
    if "transfer confirmation" in page_text_lower or (
            "wise" in page_text_lower and "transfer" in page_text_lower):
        return "Bank Transfer"
    if re.search(r"invo[i1l]ce|invoice|tax invoice|nvoice", page_text_lower):
        return "Invoice"
    if "statement" in page_text_lower and "invoice" not in page_text_lower:
        return "Statement"
    if "receipt" in page_text_lower or (
            "transaction" in page_text_lower and "till" in page_text_lower):
        return "Receipt"
    return "Other"


# --------------------------------------------------------------------------
# Record assembly
# --------------------------------------------------------------------------

class Record:
    def __init__(self, pdf, page_no, doc_type):
        self.pdf = pdf
        self.pages = [page_no]
        self.doc_type = doc_type
        self.date = None
        self.supplier = None
        self.inv_no = None
        self.inv_no_src = None
        self.net = None
        self.vat = None
        self.total = None
        self.currency = None
        self.flags = []
        self.has_totals = False

    def merge(self, other, take_amounts=True):
        self.pages.extend(p for p in other.pages if p not in self.pages)
        fields = ("date", "supplier", "inv_no", "currency")
        if take_amounts:
            fields = fields + ("net", "vat", "total")
        for attr in fields:
            if getattr(self, attr) is None and getattr(other, attr) is not None:
                setattr(self, attr, getattr(other, attr))
        if other.doc_type == "Invoice":
            self.doc_type = "Invoice"
        if other.inv_no_src and not self.inv_no_src:
            self.inv_no_src = other.inv_no_src
        other_flags = other.flags
        if not take_amounts:
            other_flags = [f for f in other_flags
                           if not re.search(r"(net|vat|total|amount)", f)]
        self.flags.extend(f for f in other_flags if f not in self.flags)
        if other.has_totals:
            self.has_totals = True

    def sort_pages(self):
        self.pages.sort()


def detect_currency(tokens):
    text = " ".join(t.text for t in tokens)
    if re.search(r"\bEUR\b|€", text):
        return "EUR"
    if re.search(r"\bUSD\b|\$", text):
        return "USD"
    if re.search(r"\bGBP\b|£|pounds", text, re.I):
        return "GBP"
    return None


def extract_transfer_info(tokens):
    """Pull the meaningful fields from a Wise / bank-transfer confirmation:
    transfer reference, amount paid in GBP, converted amount and recipient."""
    text = " ".join(t.text for t in tokens)
    low = text.lower()
    info = {}
    m = re.search(r"#(\d{6,})", text)
    if m:
        info["ref"] = m.group(1)
    m = re.search(r"amount\s*paid\s*by\s*\S*\s*\D*([\d.,]+)\s*gbp", low)
    if m:
        v = parse_number(m.group(1))
        if v is not None:
            info["amount_gbp"] = round(v, 2)
    m = re.search(
        r"total\s*to\s+([A-Za-z][\w\s.&'\-\u00b7]*?)\s*[:：]?\s*([\d.,]+)\s*([A-Za-z]{3})",
        text, re.I)
    if m:
        info["recipient"] = clean_supplier(m.group(1))
        v = parse_number(m.group(2))
        if v is not None:
            info["converted"] = round(v, 2)
        info["currency"] = m.group(3).upper()
    m = re.search(r"1\s*gbp\s*=\s*([\d.]+)\s*([A-Za-z]{3})", low)
    if m:
        info["rate"] = m.group(1)
        info["rate_currency"] = m.group(2).upper()
    return info


LLM_SYSTEM_PROMPT = """You extract invoice fields from OCR text. \
Return ONLY a JSON object with these exact keys:
{
  "date": "YYYY-MM-DD or null",
  "supplier": "company that issued the document, or null",
  "invoice_no": "invoice/receipt/statement/transfer reference, or null",
  "net_amount_gbp": number or null,
  "vat_amount_gbp": number or null,
  "total_amount_gbp": number or null,
  "currency": "GBP", "EUR", ... or null,
  "notes": "one short sentence, or null"
}
Rules:
- When a date is ambiguous (dd/mm/yyyy vs mm/dd/yyyy) use UK day-first format.
- supplier is the company that ISSUED the invoice, never the customer.
- Amounts are plain numbers without currency symbols (e.g. 130.74).
- Use the values exactly as printed; never invent or estimate them.
- If a document is a statement, receipt or bank transfer, still fill total
  amount when one is shown, and use null for fields that do not apply.
- If a field cannot be determined reliably, use null.
- If amounts are in a currency other than GBP, keep the printed numbers and
  set "currency" accordingly.
"""


def llm_extract_document(text, api_key, base_url, model, timeout=90):
    """Call an OpenAI-compatible chat-completions endpoint with strict JSON."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": "OCR text of the document:\n\n" + text[:30000]},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def post(use_format):
        p = dict(payload)
        if not use_format:
            p.pop("response_format", None)
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(p).encode("utf-8"),
            headers=headers,
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = post(True)
    except urllib.error.HTTPError as e:
        if e.code == 400:  # some local servers reject response_format
            data = post(False)
        else:
            raise
    content = data["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content)


def to_amount(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def apply_llm_result(rec, result):
    """Overwrite a record's fields with the LLM extraction result."""
    rec.date = parse_date(str(result.get("date") or "")) or None
    supplier = str(result.get("supplier") or "").strip()
    rec.supplier = clean_supplier(supplier) or None
    rec.inv_no = str(result.get("invoice_no") or "").strip() or None
    rec.net = to_amount(result.get("net_amount_gbp"))
    rec.vat = to_amount(result.get("vat_amount_gbp"))
    rec.total = to_amount(result.get("total_amount_gbp"))
    cur = str(result.get("currency") or "").strip().upper()
    if cur:
        rec.currency = cur
    rec.flags.append("llm-extracted")
    refine_amounts(rec)


def refine_amounts(rec):
    """Cross-check Net + VAT == Total, repair OCR digit drops, derive missing."""
    net, vat, total = rec.net, rec.vat, rec.total
    if None not in (net, vat, total):
        if abs(total - (net + vat)) <= 0.005:
            pass
        elif abs(total / 100 - (net + vat)) <= 0.005:
            rec.total = round(total / 100, 2)
            rec.flags.append("total corrected (/100)")
        elif abs(net / 100 + vat - total) <= 0.005:
            rec.net = round(net / 100, 2)
            rec.flags.append("net corrected (/100)")
        elif abs(net + vat / 100 - total) <= 0.005:
            rec.vat = round(vat / 100, 2)
            rec.flags.append("vat corrected (/100)")
    if rec.net is None and rec.total is not None and rec.vat is not None:
        if rec.total >= rec.vat:
            rec.net = round(rec.total - rec.vat, 2)
            rec.flags.append("net derived (total - vat)")
    if rec.vat is None and rec.total is not None and rec.net is not None:
        if rec.total >= rec.net:
            rec.vat = round(rec.total - rec.net, 2)
            rec.flags.append("vat derived (total - net)")


def group_records(records):
    """Merge continuation pages into the active invoice."""
    groups = []
    current = None
    for rec in records:
        has_identity = rec.inv_no is not None or (
            rec.supplier is not None and rec.date is not None)
        if has_identity and current is None:
            current = rec
            continue
        if has_identity:
            groups.append(current)
            current = rec
            continue
        # continuation page
        if current is not None and not current.has_totals and rec.has_totals:
            current.merge(rec)
            continue
        if current is not None and not rec.has_totals and not rec.inv_no:
            current.merge(rec)
            continue
        if current is not None and rec.has_totals and not current.has_totals:
            current.merge(rec)
            continue
        groups.append(current) if current is not None else None
        current = rec
    if current is not None:
        groups.append(current)
    for g in groups:
        g.sort_pages()
    return groups


def finalize_flags(rec):
    """Drop stale 'not found' flags once a merged record actually has a field."""
    if rec.supplier:
        rec.flags = [f for f in rec.flags if f != "supplier not found"]
    if rec.inv_no:
        rec.flags = [f for f in rec.flags if f != "invoice no not found"]
    if rec.date:
        rec.flags = [f for f in rec.flags if f != "date not found"]


def dedupe_records(records):
    """Drop near-duplicate pages of the same document (e.g. E-summary + E-invoice)."""
    def supplier_same(a, b):
        if not a or not b:
            return False
        x = a.lower().strip()
        y = b.lower().strip()
        if x == y:
            return True
        short, long = (x, y) if len(x) <= len(y) else (y, x)
        return len(short) >= 3 and long.startswith(short)

    keyed = {}
    by_supplier_date = {}
    for r in records:
        if not r.supplier or not r.date or r.total is None:
            keyed.setdefault(id(r), r)
            continue
        key = (r.supplier.lower().strip(), r.date, round(r.total, 2),
               (r.inv_no or "").strip().lower())
        if key not in keyed:
            keyed[key] = r
            continue
        prev = keyed[key]
        score_prev = (prev.inv_no is not None) + (prev.net is not None) + (prev.vat is not None)
        score_cur = (r.inv_no is not None) + (r.net is not None) + (r.vat is not None)
        if score_cur > score_prev:
            r.merge(prev)
            keyed[key] = r
        else:
            prev.merge(r)

    # same supplier + date on adjacent pages where one record lacks totals
    merged_any = True
    while merged_any:
        merged_any = False
        items = list(keyed.values())
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a is b:
                    continue
                if a.supplier and b.supplier and a.date and b.date and \
                        supplier_same(a.supplier, b.supplier) and \
                        a.date == b.date:
                    pages_a = set(a.pages)
                    pages_b = set(b.pages)
                    close = any(abs(pa - pb) <= 3 for pa in pages_a for pb in pages_b)
                    inv_a = (a.inv_no or "").strip().lower()
                    inv_b = (b.inv_no or "").strip().lower()
                    inv_ok = (not inv_a) or (not inv_b) or inv_a == inv_b or \
                        (len(inv_a) >= 8 and inv_b.startswith(inv_a)) or \
                        (len(inv_b) >= 8 and inv_a.startswith(inv_b)) or \
                        (len(inv_a) <= 5 and inv_a.isdigit()) or \
                        (len(inv_b) <= 5 and inv_b.isdigit())
                    totals_match = (a.total is not None and b.total is not None and
                                    abs(a.total - b.total) <= 0.005)
                    one_missing = (a.total is None) != (b.total is None)
                    totals_far = (a.total is not None and b.total is not None and
                                  max(a.total, b.total) > 5 * min(a.total, b.total))
                    if close and inv_ok and (totals_match or one_missing or totals_far):
                        sane_a = a.total is not None and a.total <= 999_999
                        sane_b = b.total is not None and b.total <= 999_999
                        if sane_a and not sane_b:
                            # keep the clean record, just combine page numbers
                            a.pages.extend(p for p in b.pages if p not in a.pages)
                            a.sort_pages()
                            keyed = {k: (a if v is b else v) for k, v in keyed.items()}
                        elif sane_b and not sane_a:
                            b.pages.extend(p for p in a.pages if p not in b.pages)
                            b.sort_pages()
                            keyed = {k: (b if v is a else v) for k, v in keyed.items()}
                        else:
                            def quality(r):
                                q = 0.0
                                if r.inv_no:
                                    q += 1.0
                                    if re.search(r"[A-Za-z]", r.inv_no):
                                        q += 0.5
                                    if len(r.inv_no) >= 6:
                                        q += 0.5
                                return q
                            score_a = quality(a)
                            score_b = quality(b)
                            if score_b > score_a:
                                b.merge(a, take_amounts=False)
                                keyed = {k: (b if v is a else v) for k, v in keyed.items()}
                            else:
                                a.merge(b, take_amounts=False)
                                keyed = {k: (a if v is b else v) for k, v in keyed.items()}
                        merged_any = True
                        break
            if merged_any:
                break
    out = []
    seen = set()
    for v in keyed.values():
        if id(v) not in seen:
            seen.add(id(v))
            out.append(v)
    return out


# --------------------------------------------------------------------------
# Page -> Record extraction
# --------------------------------------------------------------------------

def extract_page(pdf_name, page_no, tokens, page_h, scale, doc_type):
    rec = Record(pdf_name, page_no, doc_type)
    text = " ".join(t.text for t in tokens)
    text_lower = text.lower()
    rec.currency = detect_currency(tokens)
    rec.supplier = detect_supplier(tokens, page_h, text_lower)
    if doc_type == "Bank Transfer":
        tinfo = extract_transfer_info(tokens)
        if tinfo.get("ref") and not rec.inv_no:
            rec.inv_no = tinfo["ref"]
        if tinfo.get("amount_gbp") is not None:
            rec.total = tinfo["amount_gbp"]
            rec.currency = "GBP"
            note = "Wise transfer"
            if tinfo.get("converted") is not None:
                note += f": {tinfo['converted']:.2f} {tinfo.get('currency', '')} to " \
                        f"{tinfo.get('recipient', '?')}"
            if tinfo.get("rate"):
                note += f" @ {tinfo['rate']} {tinfo.get('rate_currency', '')}/GBP"
            rec.flags.append(note)
    rec.date = extract_date(tokens, scale, page_h=page_h)
    if doc_type != "Bank Transfer":
        rec.inv_no, rec.inv_no_src = extract_invoice_no(
            tokens, text_lower, scale, page_h=page_h)
        rec.net = extract_amount_field(tokens, NET_LABELS, scale, field="net",
                                       page_text=text_lower)
        rec.vat = extract_amount_field(tokens, VAT_LABELS, scale,
                                       net=rec.net, total=rec.total, field="vat",
                                       page_text=text_lower)
        rec.total = extract_amount_field(tokens, TOTAL_LABELS, scale,
                                         net=rec.net, vat=rec.vat, field="total",
                                         page_text=text_lower)
        rec.vat = extract_amount_field(tokens, VAT_LABELS, scale,
                                       net=rec.net, total=rec.total, field="vat",
                                       page_text=text_lower)
        rec.net = extract_amount_field(tokens, NET_LABELS, scale,
                                       vat=rec.vat, total=rec.total, field="net",
                                       page_text=text_lower)
        rec.total = extract_amount_field(tokens, TOTAL_LABELS, scale,
                                         net=rec.net, vat=rec.vat, field="total",
                                         page_text=text_lower)
    rec.has_totals = rec.total is not None or rec.net is not None or rec.vat is not None
    if rec.currency and rec.currency != "GBP":
        rec.flags.append(f"amounts in {rec.currency}, not converted")
    if not rec.supplier:
        rec.flags.append("supplier not found")
    if rec.inv_no is None:
        rec.flags.append("invoice no not found")
    if rec.date is None:
        rec.flags.append("date not found")
    return rec


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------

CSV_FIELDS = [
    "Date",
    "Supplier",
    "Invoice No.",
    "Net Amount (GBP)",
    "VAT Amount (GBP)",
    "Total Amount (GBP)",
    "Currency",
    "Document Type",
    "Source File",
    "Page(s)",
    "Flags",
]


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def record_row(rec):
    return {
        "Date": rec.date or "",
        "Supplier": rec.supplier or "",
        "Invoice No.": rec.inv_no or "",
        "Net Amount (GBP)": fmt(rec.net),
        "VAT Amount (GBP)": fmt(rec.vat),
        "Total Amount (GBP)": fmt(rec.total),
        "Currency": rec.currency or "",
        "Document Type": rec.doc_type,
        "Source File": rec.pdf,
        "Page(s)": ", ".join(str(p) for p in rec.pages),
        "Flags": "; ".join(rec.flags),
    }


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_ocr_text(out_dir, pdf_stem, page_no, tokens, source):
    folder = out_dir / "ocr_text" / pdf_stem
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for t in sorted(tokens, key=lambda t: (t.y0, t.x0)):
        lines.append(f"{t.y0:7.1f} {t.x0:7.1f} {t.conf:5.2f}  {t.text}")
    (folder / f"page_{page_no:03d}.txt").write_text(
        "\n".join(lines) + f"\n[source: {source}]\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract invoice fields from PDFs to CSV.")
    ap.add_argument("--input", default=".", help="Folder containing PDF files (default: current folder)")
    ap.add_argument("--output", default="invoices_extracted.csv", help="Output CSV path")
    ap.add_argument("--dpi", type=int, default=300, help="OCR render resolution (150-400, default 300)")
    ap.add_argument("--no-cache", action="store_true", help="Ignore the OCR cache")
    ap.add_argument("--max-pages", type=int, default=0, help="Process only the first N pages (debug)")
    ap.add_argument("--only-invoices", action="store_true",
                    help="Only keep rows classified as Invoice in the main CSV")
    ap.add_argument("--engine", choices=["rules", "llm"], default="rules",
                    help="Field extraction engine: 'rules' (default) or 'llm' "
                         "(handles any invoice layout via an LLM)")
    ap.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
                    help="LLM model (or set OPENAI_MODEL)")
    ap.add_argument("--llm-base-url",
                    default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    help="OpenAI-compatible base URL (or set OPENAI_BASE_URL)")
    ap.add_argument("--llm-api-key", default=os.environ.get("OPENAI_API_KEY", ""),
                    help="API key (or set OPENAI_API_KEY)")
    args = ap.parse_args(argv)

    if not HAVE_FITZ or not HAVE_RAPID:
        print("ERROR: missing Python packages.\n"
              "Install the dependencies from the project folder with:\n"
              "\n"
              "    python -m pip install -r requirements.txt\n"
              "\n"
              "or install them individually with:\n"
              "\n"
              "    python -m pip install pymupdf rapidocr-onnxruntime",
              file=sys.stderr)
        return 1

    in_dir = Path(args.input)
    if not in_dir.exists():
        print(f"Input folder not found: {in_dir}", file=sys.stderr)
        return 1
    pdfs = sorted(in_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDF files found in {in_dir}", file=sys.stderr)
        return 1

    out_csv = Path(args.output)
    out_dir = out_csv.parent if out_csv.parent != Path(".") else in_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = in_dir / "_cache"
    cache_dir.mkdir(exist_ok=True)

    print(f"Found {len(pdfs)} PDF file(s). OCR engine: RapidOCR")
    if args.engine == "llm":
        print(f"Field extraction engine: LLM ({args.llm_model}) via {args.llm_base_url}")
        if not args.llm_api_key and "localhost" not in args.llm_base_url:
            print("WARNING: no API key found (OPENAI_API_KEY). Falling back to rules.",
                  file=sys.stderr)
            args.engine = "rules"
    ocr = RapidOCR()
    all_records = []
    issues = []
    page_count = 0

    for pdf_path in pdfs:
        print(f"  Processing: {pdf_path.name}")
        doc = fitz.open(str(pdf_path))
        n_pages = doc.page_count
        if args.max_pages:
            n_pages = min(n_pages, args.max_pages)
        page_records = []
        page_texts = {}
        for pno in range(n_pages):
            page_count += 1
            tokens, source = get_page_tokens(
                str(pdf_path), doc, pno, ocr, cache_dir, args.dpi)
            if not tokens:
                continue
            page = doc[pno]
            scale = page_height_scale(page, args.dpi)
            page_h = page.rect.height * args.dpi / 72.0
            text_lower = " ".join(t.text for t in tokens).lower()
            page_texts[pno + 1] = " ".join(t.text for t in tokens)
            doc_type = classify_document(text_lower)
            rec = extract_page(pdf_path.name, pno + 1, tokens, page_h, scale, doc_type)
            refine_amounts(rec)
            page_records.append(rec)
            write_ocr_text(out_dir, pdf_path.stem, pno + 1, tokens, source)
            if pno % 5 == 0:
                print(f"    page {pno + 1}/{doc.page_count} "
                      f"({source}) supplier={rec.supplier!r} total={rec.total}")
        doc.close()
        grouped = group_records(page_records)
        grouped = dedupe_records(grouped)
        for rec in grouped:
            finalize_flags(rec)
        if args.engine == "llm":
            for rec in grouped:
                doc_text = "\n\n--- page ---\n\n".join(
                    page_texts[p] for p in sorted(rec.pages) if p in page_texts)
                if not doc_text.strip():
                    continue
                try:
                    result = llm_extract_document(
                        doc_text, args.llm_api_key, args.llm_base_url, args.llm_model)
                    apply_llm_result(rec, result)
                    print(f"    llm: p{rec.pages} supplier={rec.supplier!r} "
                          f"total={rec.total}")
                except Exception as e:
                    rec.flags.append(f"llm failed ({e.__class__.__name__}); used rules")
        all_records.extend(grouped)

    rows = [record_row(r) for r in all_records]
    if args.only_invoices:
        rows = [r for r in rows if r["Document Type"] == "Invoice"]

    write_csv(out_csv, rows)
    issues_csv = out_csv.parent / "extraction_issues.csv"
    issue_rows = [
        r for r in rows
        if not r["Date"] or not r["Supplier"] or not r["Invoice No."] or not r["Total Amount (GBP)"]
    ]
    write_csv(issues_csv, issue_rows)

    print(f"\nDone. {len(pdfs)} PDF(s), {page_count} page(s), {len(rows)} record(s).")
    print(f"  Main CSV      : {out_csv}")
    print(f"  Issues (QA)   : {issues_csv}")
    print(f"  OCR text      : {out_dir / 'ocr_text'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
