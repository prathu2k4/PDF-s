#!/usr/bin/env python3
"""
Make scanned PDFs searchable by adding a selectable text layer (word-level OCR).
- Preserves visual appearance by drawing page images at original PDF page size.
- Uses pytesseract.image_to_data() to place words at exact positions.
- Tries to use ReportLab's setFillAlpha(0) (if available) to make overlay text invisible.
- Fallback: tiny white text (still selectable) if alpha not supported.
- Shows a live progress bar while processing each PDF's pages.

Usage:
    python searchable_pdf_tool_v08.py
A file picker opens to choose the input PDF, then a second one to choose the
destination folder. After that, choose from the interactive menu.
"""

import os

import re
import sys
import shutil
import tkinter as tk
from tkinter import filedialog
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
from pdf2image import convert_from_path
import pytesseract
from pytesseract import Output
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ============================================================================
# SETTINGS -- everything you might want to tweak lives in this block
# ============================================================================

# ---- Tool locations (Windows paths) ---------------------------------------
# Full path to tesseract.exe (the OCR engine).
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Folder containing Poppler's pdfinfo.exe / pdftoppm.exe (turns PDF pages into
# images). Set to None to let the script search common locations / your PATH.
POPPLER_PATH = r"C:\poppler-25.11.0\Library\bin"

# ---- Quality vs. speed ------------------------------------------------------
# Resolution used to rasterize each page before OCR.
#   150-200 = fastest, lowest RAM, fine for clean/large print
#   250     = good balance (default)
#   300     = sharper, better for small/faint text, but slower and heavier
# Note: the output PDF also embeds these page images, so higher DPI = bigger file.
DPI = 250

# OCR language(s) passed to Tesseract. "eng" = English. Combine with "+",
# e.g. "eng+hin" -- the matching .traineddata files must be installed.
OCR_LANG = "eng"

# Words Tesseract is less sure about than this (0-100) are left out of the
# searchable text layer. 0 keeps everything; try 30-60 to drop OCR garbage.
MIN_CONFIDENCE = 0

# ---- CPU usage --------------------------------------------------------------
# How many pages are OCR'd in parallel. Leave as None for automatic:
# (physical cores estimate) - 1, i.e. one core stays free for Windows/UI.
# Or set a number yourself, e.g. 4 for a lighter load or 8 to use more CPU.
OCR_WORKERS_OVERRIDE = None

# Threads each single Tesseract process may use internally. Keep at 1: we
# already run several pages in parallel (OCR_WORKERS), and stacking both
# layers just causes CPU contention. (A value already set in your shell wins.)
TESSERACT_THREADS_PER_PAGE = 1

# Max threads Poppler uses when converting a batch of pages to images.
# Effective value is min(this, OCR workers). Lower it if the PC feels sluggish.
RASTER_THREADS = 4

# ---- Memory usage -------------------------------------------------------------
# How many pages are rasterized and held in RAM at once. This is the main RAM
# lever for big scans (e.g. 500 pages at 250 DPI would need 5GB+ if loaded all
# at once). Memory use is roughly PAGE_BATCH_SIZE pages' worth of images.
#   Low on RAM -> try 10-20.   Plenty of RAM (32GB+) -> try 60-100.
PAGE_BATCH_SIZE = 40

# ---- Output naming ------------------------------------------------------------
# Subfolders created inside the destination folder you pick at startup.
OCR_SUBFOLDER_NAME = "OCR"          # searchable PDFs go here
SPLIT_SUBFOLDER_NAME = "SPLIT"      # split PDFs go here
# Searchable files are saved as <prefix><original name>.pdf
SEARCHABLE_PREFIX = "SEARCHABLE_"
# File name used when merging PDFs.
MERGED_FILENAME = "merged_output.pdf"

# ============================================================================
# END OF SETTINGS (no need to edit below this line)
# ============================================================================

os.environ.setdefault("OMP_THREAD_LIMIT", str(TESSERACT_THREADS_PER_PAGE))
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# Worker count (automatic): sized off *physical* cores, not logical/SMT threads.
# OCR is CPU-bound, so hyperthreads add little. //2 approximates physical cores
# on a 2-way-SMT chip (e.g. Ryzen 5 4600H: 12 logical -> ~6 physical).
_logical_cpus = os.cpu_count() or 4
_physical_cpus_estimate = max(1, _logical_cpus // 2)
OCR_WORKERS = (
    max(1, int(OCR_WORKERS_OVERRIDE)) if OCR_WORKERS_OVERRIDE
    else max(1, _physical_cpus_estimate - 1)
)

# Runtime paths -- filled in by choose_paths() when the program starts (file dialogs).
PDF_DIR = os.getcwd()     # replaced by the folder of the PDF you pick
INPUT_PDF = None          # the PDF picked in the first dialog
OUTPUT_DIR = None         # the folder picked in the second dialog
OCR_DIR = os.path.join(PDF_DIR, OCR_SUBFOLDER_NAME)
SPLIT_DIR = os.path.join(PDF_DIR, SPLIT_SUBFOLDER_NAME)
MERGED_OUTPUT_PATH = os.path.join(PDF_DIR, MERGED_FILENAME)


def choose_paths():
    """
    Dialog 1: pick the input PDF.
    Dialog 2 (opens right after): pick the destination folder.
    Sets the global paths used by the rest of the program. Returns False if
    either dialog is cancelled.
    """
    global INPUT_PDF, OUTPUT_DIR, PDF_DIR, OCR_DIR, SPLIT_DIR, MERGED_OUTPUT_PATH

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)   # make sure the dialogs appear in front
    try:
        input_pdf = filedialog.askopenfilename(
            parent=root, title="Select the input PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not input_pdf:
            return False

        output_dir = filedialog.askdirectory(
            parent=root, title="Select the destination folder",
            initialdir=os.path.dirname(input_pdf),
        )
        if not output_dir:
            return False
    finally:
        root.destroy()

    INPUT_PDF = os.path.normpath(input_pdf)
    OUTPUT_DIR = os.path.normpath(output_dir)
    PDF_DIR = os.path.dirname(INPUT_PDF)          # merge options use this folder
    OCR_DIR = os.path.join(OUTPUT_DIR, OCR_SUBFOLDER_NAME)
    SPLIT_DIR = os.path.join(OUTPUT_DIR, SPLIT_SUBFOLDER_NAME)
    MERGED_OUTPUT_PATH = os.path.join(OUTPUT_DIR, MERGED_FILENAME)
    os.makedirs(OCR_DIR, exist_ok=True)
    os.makedirs(SPLIT_DIR, exist_ok=True)
    return True


# --------------------------
# Utilities
# --------------------------
def natural_sort_key(s: str):
    parts = re.split(r'(\d+)', s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            key.append(p.lower())
    return key


def find_poppler(candidate_path=None):
    if candidate_path:
        if os.path.exists(os.path.join(candidate_path, "pdfinfo.exe")):
            return candidate_path
    candidates = [
        r"C:\poppler\Library\bin",
        r"C:\Program Files\poppler\Library\bin",
        r"C:\Program Files (x86)\poppler\Library\bin",
        os.path.join(os.path.expanduser("~"), "Downloads", "poppler-25.11.0", "Library", "bin"),
        os.path.join(os.path.expanduser("~"), "Downloads", "poppler-24.08.0", "Library", "bin"),
    ]
    for c in candidates:
        if os.path.exists(os.path.join(c, "pdfinfo.exe")):
            return c
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        return os.path.dirname(pdfinfo)
    return None


# --------------------------
# Progress bar rendering
# --------------------------
def render_progress(filename, count, total, bar_width=20, first_call=False, done=False):
    """
    Renders a 3-line progress block:
        Processing: <filename>
        Page X/Y  [████░░░░░░░░░░░░░░░░] NN%
        OCR in progress...
    Redraws in place on subsequent calls using ANSI cursor movement.
    `done=True` is used only for the final redraw after the file has actually
    been saved -- it is NOT inferred from count == total, since the last page
    is still being processed (not yet complete) when its progress line is
    first drawn.
    """
    total = max(total, 1)
    fraction = min(max(count / total, 0.0), 1.0)
    filled = int(bar_width * fraction)
    bar = "█" * filled + "░" * (bar_width - filled)
    percent = int(fraction * 100)

    lines = [
        f"Processing: {filename}",
        f"Page {count}/{total}  [{bar}] {percent}%",
        "OCR complete for this file." if done else "OCR in progress...",
    ]

    if not first_call:
        # Move cursor up 3 lines and clear each before redrawing
        sys.stdout.write("\033[F\033[K" * 3)

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# --------------------------
# Merge PDFs
# --------------------------
def merge_pdfs():
    merger = PdfMerger()
    pdf_files = [
        f for f in os.listdir(PDF_DIR)
        if f.lower().endswith(".pdf")
        and os.path.join(PDF_DIR, f) != MERGED_OUTPUT_PATH
    ]
    pdf_files = sorted(pdf_files, key=natural_sort_key)
    if not pdf_files:
        print("No PDFs found to merge in:", PDF_DIR)
        return []
    print("Merging files in numeric order:")
    for pdf in pdf_files:
        print("  -", pdf)
        merger.append(os.path.join(PDF_DIR, pdf))
    merger.write(MERGED_OUTPUT_PATH)
    merger.close()
    print("✔ Merged PDF created at:", MERGED_OUTPUT_PATH)
    return pdf_files


# --------------------------
# Create searchable PDF for one input PDF
# --------------------------
def make_searchable_pdf(input_pdf_path, output_pdf_path, poppler_path=None, dpi=DPI,
                         ocr_workers=OCR_WORKERS, batch_size=PAGE_BATCH_SIZE):
    """
    Processes pages in bounded batches so memory use stays flat regardless of
    total document length (important for e.g. 500-page scans):
      For each batch of `batch_size` pages:
        1. Rasterize just that batch in ONE Poppler call (fewer subprocess
           launches than one-per-page, bounded memory vs. one-call-for-everything).
        2. OCR the batch's pages concurrently (thread pool; each pytesseract
           call shells out to the tesseract binary, so this uses real cores).
        3. Draw the batch's pages onto the (single, long-lived) canvas.
      The canvas itself is opened once and saved once at the very end, so the
      output is one continuous PDF -- only the raw page images are batched.
    """
    reader = PdfReader(input_pdf_path)
    num_pages = len(reader.pages)
    if num_pages == 0:
        raise RuntimeError("PDF has no pages: " + input_pdf_path)

    filename = os.path.basename(input_pdf_path)

    # precompute each page's point-size from the original PDF (unchanged logic)
    page_sizes = []
    for page_index in range(num_pages):
        page = reader.pages[page_index]
        try:
            ml = page.mediabox
            llx = float(ml.left)
            lly = float(ml.bottom)
            urx = float(ml.right)
            ury = float(ml.top)
            page_w = abs(urx - llx)
            page_h = abs(ury - lly)
        except Exception:
            # fallback to letter
            page_w, page_h = 612.0, 792.0
        page_sizes.append((page_w, page_h))

    c = None  # canvas is opened once, on the first page of the first batch

    batch_starts = list(range(0, num_pages, batch_size))
    for batch_start in batch_starts:
        batch_end = min(batch_start + batch_size, num_pages)  # exclusive
        first_page_num = batch_start + 1   # convert_from_path is 1-indexed
        last_page_num = batch_end
        batch_len = batch_end - batch_start

        # --- Stage 1: rasterize this batch in ONE Poppler call (silent -- no terminal chatter) ---
        images = convert_from_path(
            input_pdf_path, dpi=dpi, poppler_path=poppler_path,
            first_page=first_page_num, last_page=last_page_num,
            thread_count=max(1, min(RASTER_THREADS, ocr_workers)),
        )
        if len(images) < batch_len:
            batch_len = len(images)  # be defensive if fewer pages came back
        if batch_len == 0:
            raise RuntimeError(
                f"Failed to rasterize pages {first_page_num}-{last_page_num} of {input_pdf_path}"
            )

        # --- Stage 2: OCR this batch's pages concurrently (silent -- no terminal chatter) ---
        ocr_results = [None] * batch_len

        def run_ocr(idx):
            return idx, pytesseract.image_to_data(images[idx], lang=OCR_LANG, output_type=Output.DICT)

        with ThreadPoolExecutor(max_workers=ocr_workers) as executor:
            futures = [executor.submit(run_ocr, i) for i in range(batch_len)]
            for future in as_completed(futures):
                idx, data = future.result()
                ocr_results[idx] = data

        # --- Stage 3: draw this batch's pages onto the canvas IN ORDER ---
        for page_in_batch in range(batch_len):
            global_page_index = batch_start + page_in_batch
            render_progress(
                filename, global_page_index + 1, num_pages,
                first_call=(global_page_index == 0)
            )

            img = images[page_in_batch]
            page_w, page_h = page_sizes[global_page_index]
            ocr = ocr_results[page_in_batch]

            # create canvas on the very first page with exact page size (points)
            if c is None:
                c = canvas.Canvas(output_pdf_path, pagesize=(page_w, page_h))
            else:
                c.setPageSize((page_w, page_h))

            # draw the raster image to cover the page
            img_reader = ImageReader(img)
            c.drawImage(img_reader, 0, 0, width=page_w, height=page_h)

            n_boxes = len(ocr.get('text', []))
            # image pixel dimensions
            img_w, img_h = img.size  # PIL: (width, height)

            # compute scale from image pixels to PDF points (points = 1/72 inch)
            sx = page_w / img_w
            sy = page_h / img_h

            # Attempt to use alpha transparency for invisible text
            alpha_supported = True
            try:
                # some ReportLab versions provide setFillAlpha
                c.setFillAlpha(0.0)
            except Exception:
                alpha_supported = False

            # If alpha not supported we'll fallback to tiny white text
            if alpha_supported:
                c.setFillColorRGB(0, 0, 0)  # color doesn't matter since alpha 0
            else:
                c.setFillColorRGB(1, 1, 1)  # white (on top of image)
            # We'll use Helvetica as overlay font
            c.setFont("Helvetica", 8)

            for w in range(n_boxes):
                # Get word text
                word = str(ocr.get('text', [''])[w]).strip()
                if not word:
                    continue

                # Robust conf parsing: conf might be str, int, float, '-1', etc.
                raw_conf = ocr.get('conf', [None])[w]
                conf = -1
                try:
                    # try numeric conversion (handles int, float, numeric strings)
                    if raw_conf is None:
                        conf = -1
                    else:
                        conf = int(float(raw_conf))
                except Exception:
                    conf = -1

                # Skip boxes below the MIN_CONFIDENCE setting
                if conf < MIN_CONFIDENCE:
                    continue

                # tesseract coords: left, top, width, height in pixels
                try:
                    left = int(ocr.get('left', [0])[w])
                    top = int(ocr.get('top', [0])[w])
                    width = int(ocr.get('width', [0])[w])
                    height = int(ocr.get('height', [0])[w])
                except Exception:
                    continue

                # map to PDF coordinates:
                x_pt = left * sx
                # PDF origin is bottom-left; Tesseract top is from image top-left
                y_pt = page_h - (top + height) * sy

                # choose font size from bbox height in points (a heuristic)
                font_size = max(1.0, height * sy * 0.9)
                # small upper bound to avoid massive fonts if bad boxes
                if font_size > 200:
                    font_size = 200

                try:
                    c.setFont("Helvetica", font_size)
                    c.drawString(x_pt, y_pt, word)
                except Exception:
                    # fallback: extremely small font so it doesn't disturb layout and still selectable
                    c.setFont("Helvetica", 0.1)
                    c.drawString(x_pt, y_pt, word)

            # reset alpha if set
            if alpha_supported:
                try:
                    c.setFillAlpha(1.0)
                except Exception:
                    pass

            c.showPage()

    if c:
        c.save()
    else:
        raise RuntimeError("Failed to create canvas for searchable PDF.")

    # final redraw to show 100% / complete state (only after the PDF is actually saved)
    render_progress(filename, num_pages, num_pages, first_call=False, done=True)
    print()  # blank line after the block for readability


# --------------------------
# Interactive menu & helpers
# --------------------------
def parse_page_spec(spec: str, num_pages: int):
    """
    Parses a user page specification into a sorted list of unique 1-based page numbers.

    Accepts comma-separated items, each either:
      - a single page:  "7"
      - a range:        "3-9"   (inclusive on both ends)
    e.g. "1,3,5-8,12" -> [1, 3, 5, 6, 7, 8, 12]

    Raises ValueError with a clear message on malformed or out-of-bounds input.
    """
    pages = set()
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("Empty page specification.")

    for raw_item in spec.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "-" in item:
            bits = item.split("-")
            if len(bits) != 2:
                raise ValueError(f"Bad range: '{item}' (expected something like 3-9)")
            start_s, end_s = bits[0].strip(), bits[1].strip()
            if not start_s.isdigit() or not end_s.isdigit():
                raise ValueError(f"Bad range: '{item}' (page numbers must be digits)")
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start   # be forgiving: accept "9-3" as 3-9
            if start < 1 or end > num_pages:
                raise ValueError(
                    f"Range '{item}' is outside the document (it has {num_pages} pages)."
                )
            pages.update(range(start, end + 1))
        else:
            if not item.isdigit():
                raise ValueError(f"Bad page number: '{item}'")
            page = int(item)
            if page < 1 or page > num_pages:
                raise ValueError(
                    f"Page {page} is outside the document (it has {num_pages} pages)."
                )
            pages.add(page)

    if not pages:
        raise ValueError("No valid pages found in specification.")
    return sorted(pages)


def write_pdf_subset(reader, pages_1based, output_path):
    """Writes the given 1-based page numbers from `reader` into a new PDF."""
    writer = PdfWriter()
    for page_num in pages_1based:
        writer.add_page(reader.pages[page_num - 1])
    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path


def split_pdf(input_pdf_path, mode, spec=None):
    """
    Splits a PDF into the SPLIT folder.

    mode:
      "pages"  -> one output PDF containing exactly the pages in `spec`
                  (spec supports ranges and individual pages, e.g. "1,3,5-8")
      "each"   -> one output PDF per page listed in `spec`
      "half"   -> two output PDFs, first half and second half
                  (odd page counts put the extra page in the first half)
    """
    reader = PdfReader(input_pdf_path)
    num_pages = len(reader.pages)
    if num_pages == 0:
        raise RuntimeError("PDF has no pages: " + input_pdf_path)

    base = os.path.splitext(os.path.basename(input_pdf_path))[0]
    outputs = []

    if mode == "half":
        midpoint = (num_pages + 1) // 2   # odd counts -> extra page goes to part 1
        first_half = list(range(1, midpoint + 1))
        second_half = list(range(midpoint + 1, num_pages + 1))
        if not second_half:
            raise RuntimeError(
                f"'{base}' has only {num_pages} page(s) -- nothing to split in half."
            )
        outputs.append(write_pdf_subset(
            reader, first_half, os.path.join(SPLIT_DIR, f"{base}_part1_p1-{midpoint}.pdf")
        ))
        outputs.append(write_pdf_subset(
            reader, second_half,
            os.path.join(SPLIT_DIR, f"{base}_part2_p{midpoint + 1}-{num_pages}.pdf")
        ))

    elif mode == "pages":
        pages = parse_page_spec(spec, num_pages)
        label = spec.replace(",", "_").replace(" ", "")
        outputs.append(write_pdf_subset(
            reader, pages, os.path.join(SPLIT_DIR, f"{base}_pages_{label}.pdf")
        ))

    elif mode == "each":
        pages = parse_page_spec(spec, num_pages)
        for page_num in pages:
            outputs.append(write_pdf_subset(
                reader, [page_num], os.path.join(SPLIT_DIR, f"{base}_p{page_num}.pdf")
            ))

    else:
        raise ValueError(f"Unknown split mode: {mode}")

    return outputs


def split_menu():
    print("\nSPLIT — Choose how to split:")
    print("  a) Page range / specific pages -> ONE new PDF")
    print("     (e.g. 5-20   or   1,3,7   or   1,3,5-8,12)")
    print("  b) Specific pages -> a SEPARATE PDF for each page")
    print("  c) Split in half (two PDFs)")
    print("  d) Back to main menu")
    return input("Enter a / b / c / d: ").strip().lower()


def do_split_flow():
    chosen = os.path.basename(INPUT_PDF)
    input_path = INPUT_PDF

    try:
        page_count = len(PdfReader(input_path).pages)
    except Exception as e:
        print(f"Could not read '{chosen}': {e}")
        return
    print(f"'{chosen}' has {page_count} pages.")

    choice = split_menu()
    try:
        if choice == "a":
            spec = input("Pages to keep (e.g. 5-20 or 1,3,5-8): ").strip()
            outputs = split_pdf(input_path, "pages", spec)
        elif choice == "b":
            spec = input("Pages to extract individually (e.g. 1,4,9-11): ").strip()
            outputs = split_pdf(input_path, "each", spec)
        elif choice == "c":
            outputs = split_pdf(input_path, "half")
        elif choice == "d":
            return
        else:
            print("Invalid choice.")
            return
    except ValueError as e:
        print(f"Input error: {e}")
        return
    except Exception as e:
        print(f"ERROR splitting {chosen}: {e}")
        return

    print(f"✔ Created {len(outputs)} file(s) in: {SPLIT_DIR}")
    for out in outputs:
        print("  -", os.path.basename(out))


def interactive_menu():
    print("\nPDF TOOL — Choose an option:")
    print("1) Merge PDFs (numeric order)")
    print("2) Make PDFs searchable (OCR layer inside PDF)")
    print("3) Merge then make searchable")
    print("4) Split a PDF (range / page numbers / in half)")
    print("5) Exit")
    return input("Enter 1 / 2 / 3 / 4 / 5: ").strip()


def get_pdf_list():
    return sorted(
        [
            f for f in os.listdir(PDF_DIR)
            if f.lower().endswith(".pdf")
            and os.path.join(PDF_DIR, f) != MERGED_OUTPUT_PATH
        ],
        key=natural_sort_key
    )


def main():
    if not choose_paths():
        print("No input file / destination folder selected. Exiting.")
        return
    print("Input PDF:         ", INPUT_PDF)
    print("Destination folder:", OUTPUT_DIR)
    poppler = find_poppler(POPPLER_PATH)
    if poppler:
        print("Poppler found at:", poppler)
    else:
        print("Warning: Poppler not found automatically. You may be prompted to enter the poppler bin folder path.")

    while True:
        choice = interactive_menu()
        if choice == "1":
            merge_pdfs()
        elif choice == "2":
            poppler_to_use = poppler
            if not poppler_to_use:
                p = input("Enter Poppler bin folder (full path) or press Enter to try without it: ").strip()
                poppler_to_use = p if p else None
            pdf = os.path.basename(INPUT_PDF)
            out_pdf = os.path.join(OCR_DIR, f"{SEARCHABLE_PREFIX}{pdf}")
            try:
                make_searchable_pdf(INPUT_PDF, out_pdf, poppler_path=poppler_to_use, dpi=DPI)
            except Exception as e:
                print(f"ERROR processing {pdf}: {e}")
        elif choice == "3":
            pdfs = merge_pdfs()
            if not pdfs:
                continue
            poppler_to_use = poppler
            if not poppler_to_use:
                p = input("Enter Poppler bin folder (full path) or press Enter to try without it: ").strip()
                poppler_to_use = p if p else None
            # OCR each source pdf in the merge order
            for pdf in pdfs:
                inp = os.path.join(PDF_DIR, pdf)
                out_pdf = os.path.join(OCR_DIR, f"{SEARCHABLE_PREFIX}{pdf}")
                try:
                    make_searchable_pdf(inp, out_pdf, poppler_path=poppler_to_use, dpi=DPI)
                except Exception as e:
                    print(f"ERROR processing {pdf}: {e}")
        elif choice == "4":
            do_split_flow()
        elif choice == "5":
            print("Exiting.")
            break
        else:
            print("Invalid choice. Enter 1,2,3,4 or 5.")


if __name__ == "__main__":
    main()
