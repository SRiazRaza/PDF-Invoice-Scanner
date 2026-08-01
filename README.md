# PDF Invoice Scanner

Automatically reads invoices from scanned or digital PDF files and exports them
to CSV. It extracts:

| Field | Example |
|---|---|
| Date | `2026-01-10` |
| Supplier | `Adobe Systems Software Ireland Ltd` |
| Invoice No. | `IEE2026000613745` |
| Net Amount (GBP) | `130.74` |
| VAT Amount (GBP) | `0.00` |
| Total Amount (GBP) | `130.74` |

The tool is designed for **messy, real-world invoice bundles**: many different
suppliers and layouts, scanned pages, invoices that span several pages, receipts,
statements and bank-transfer confirmations mixed together, and OCR typos in
amounts.

## How it works

1. Each page is rendered to an image (300 DPI by default).
2. Scanned pages are OCR'd locally with **RapidOCR** (no internet or cloud
   service needed). Digital PDFs with a text layer are read directly.
3. Fields are found with label + position matching (`Invoice No.`, `Date`,
   `Subtotal`, `VAT`, `Grand Total`, ...) that works across layouts.
4. Amounts are cross-checked so **Net + VAT = Total**, which also repairs OCR
   mistakes like `13074` instead of `130.74`.
5. Pages that belong to one invoice (e.g. an invoice printed over 3 pages) are
   merged, and duplicated pages of the same document are de-duplicated.

## Requirements

```powershell
python -m pip install -r requirements.txt
```

(`pymupdf` renders pages, `rapidocr-onnxruntime` does the OCR.)

## Usage

Point the script at the folder containing your PDFs and give it an output CSV
path:

```powershell
python invoice_extractor.py --input "D:\Project\PDF Invoice Scanner" --output invoices_extracted.csv
```

Options:

| Option | Purpose |
|---|---|
| `--input` | Folder with the PDF files (default: current folder) |
| `--output` | Output CSV path (default: `invoices_extracted.csv`) |
| `--dpi` | OCR resolution, `150`–`400` (default `300`; use `200` to run ~2× faster) |
| `--only-invoices` | Keep only rows classified as invoices in the main CSV |
| `--max-pages N` | Process only the first N pages (useful for testing) |

Example for a large batch run:

```powershell
python invoice_extractor.py --input "D:\Project\PDF Invoice Scanner" --output invoices_extracted.csv --dpi 200
```

## Clone and run on another computer

The repository contains only the code (no invoice PDFs, no extracted data).
To use it on a second machine (e.g. the one that processes the invoices):

```powershell
# 1. Install Python 3.10+ from https://www.python.org (tick "Add to PATH")

# 2. Get the code
git clone https://github.com/<your-user>/pdf-invoice-scanner.git
cd pdf-invoice-scanner

# 3. Install the dependencies (once)
python -m pip install -r requirements.txt

# 4. Drop your PDFs into the folder and run
python invoice_extractor.py --input . --output invoices_extracted.csv
```

The results (`invoices_extracted.csv`, `extraction_issues.csv`) are written to
the folder you run it from, and OCR text per page is saved under `ocr_text/`.
Everything is cached in `_cache/`, so re-running on the same files is instant.

## First-time setup on a brand-new Windows machine

The other machine needs **Git** and **Python** installed before anything else.

### Option A – automatic (recommended)

Download `setup_other_computer.ps1` from this repository (via the GitHub
web page), open PowerShell in the download folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1
```

It installs Git and Python with winget, clones the project, and installs the
dependencies. Optionally choose a destination folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1 -Dest D:\InvoiceScanner
```

### Option B – manual

1. Install Git: download from https://git-scm.com/download/win and accept the
   defaults.
2. Install Python 3.12: download from https://www.python.org/downloads/ and
   **tick "Add python.exe to PATH"** before clicking Install.
   > Important: the OCR engine only supports Python 3.10 – 3.12. If you
   > installed Python 3.13 or 3.14, install 3.12 alongside it and use
   > `py -3.12` in the commands below instead of `python`.
3. Open PowerShell and run:

```powershell
git clone https://github.com/SRiazRaza/PDF-Invoice-Scanner.git
cd PDF-Invoice-Scanner
python -m pip install -r requirements.txt
```

Then drop the PDFs into the folder and run the extractor as shown above.

## Output files

* `invoices_extracted.csv` – one row per invoice/document with all six fields,
  plus Currency, Document Type, source file, page numbers and any flags.
* `extraction_issues.csv` – rows missing an important field (e.g. a supplier
  that isn't printed on the document, or a bank transfer that has no invoice
  number). Review these by hand.
* `ocr_text/<pdf>/page_XXX.txt` – the OCR text of every page, so you can
  double-check anything.
* `_cache/` – cached OCR results; re-running the script on the same files is
  nearly instant.

## Notes and limitations

* Amounts are kept as printed. Invoices in EUR (or other currencies) are
  flagged in the `Currency` column and not converted – add an FX rate yourself
  if needed.
* Statements (TfL, Dart Charge) and bank-transfer confirmations (Wise) are
  included but marked as `Statement` / `Bank Transfer`, and they don't have
  invoice-style fields.
* For Wise transfer confirmations the tool fills in the transfer reference,
  the amount paid in GBP (as Total) and a flag with the converted amount,
  exchange rate and recipient, e.g.
  `Wise transfer: 1230.00 EUR to My Sunshine EooD @ 1.1545 EUR/GBP`.
* OCR is never perfect: occasionally a supplier name or reference is read
  slightly wrong. The per-page OCR text files and the issues CSV make those
  easy to spot.
