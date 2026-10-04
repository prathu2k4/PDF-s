"""
Add page numbers (bottom-right) to a PDF.

- Pops up a file dialog to choose the INPUT pdf
- Pops up a "Save as" dialog to choose the OUTPUT location
- Only draws the page number on each page. Original page content is not
  re-rendered or modified (the number is merged on top as an overlay).

Install once:
    pip install pypdf reportlab
"""

import io
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.pdfgen import canvas

# ---------------- settings you can tweak ----------------
FONT_NAME = "Helvetica"
FONT_SIZE = 10
MARGIN = 30                 # points from the right and bottom edges (72 pt = 1 inch)
FORMAT = "{n}"              # e.g. "{n}", "Page {n}", "{n} / {total}"
# --------------------------------------------------------


def make_overlay(page, text):
    """Build a one-page transparent PDF containing only the page number,
    positioned at the VISUAL bottom-right corner (handles /Rotate)."""
    mb = page.mediabox
    mx0, my0 = float(mb.left), float(mb.bottom)
    mw, mh = float(mb.width), float(mb.height)

    cb = page.cropbox  # the area actually shown by viewers
    x0, y0 = float(cb.left), float(cb.bottom)
    x1, y1 = float(cb.right), float(cb.top)

    rot = (page.get("/Rotate", 0) or 0) % 360

    # (anchor x, anchor y, text rotation) in unrotated page coordinates
    if rot == 0:
        ax, ay, ang = x1 - MARGIN, y0 + MARGIN, 0
    elif rot == 90:
        ax, ay, ang = x1 - MARGIN, y1 - MARGIN, 90
    elif rot == 180:
        ax, ay, ang = x0 + MARGIN, y1 - MARGIN, 180
    else:  # 270
        ax, ay, ang = x0 + MARGIN, y0 + MARGIN, 270

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(mw, mh))
    c.setFont(FONT_NAME, FONT_SIZE)
    c.translate(ax - mx0, ay - my0)
    c.rotate(ang)
    c.drawRightString(0, 0, text)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0], mx0, my0


def add_page_numbers(src, dst, root=None):
    reader = PdfReader(src)

    if reader.is_encrypted:
        if reader.decrypt("") == 0:  # not openable with empty password
            pwd = simpledialog.askstring(
                "Password needed", "This PDF is password-protected.\nEnter password:",
                show="*", parent=root,
            )
            if not pwd or reader.decrypt(pwd) == 0:
                raise ValueError("Could not decrypt the PDF (wrong password).")

    # clone_from keeps bookmarks, links, forms, metadata, etc.
    writer = PdfWriter(clone_from=reader)
    total = len(writer.pages)

    for n, page in enumerate(writer.pages, start=1):
        text = FORMAT.format(n=n, total=total)
        overlay, mx0, my0 = make_overlay(page, text)
        page.merge_transformed_page(overlay, Transformation().translate(mx0, my0))

    with open(dst, "wb") as f:
        writer.write(f)
    return total


def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # make sure dialogs appear in front

    src = filedialog.askopenfilename(
        parent=root,
        title="Select the input PDF",
        filetypes=[("PDF files", "*.pdf")],
    )
    if not src:
        print("No input file selected. Exiting.")
        return

    base = os.path.splitext(os.path.basename(src))[0]
    dst = filedialog.asksaveasfilename(
        parent=root,
        title="Choose where to save the numbered PDF",
        initialdir=os.path.dirname(src),
        initialfile=f"{base}_numbered.pdf",
        defaultextension=".pdf",
        filetypes=[("PDF files", "*.pdf")],
    )
    if not dst:
        print("No output location selected. Exiting.")
        return

    if os.path.abspath(src) == os.path.abspath(dst):
        messagebox.showerror(
            "Same file", "Output must be different from the input file.", parent=root
        )
        return

    try:
        total = add_page_numbers(src, dst, root)
    except Exception as e:
        messagebox.showerror("Error", f"Failed:\n{e}", parent=root)
        sys.exit(1)

    messagebox.showinfo("Done", f"Page numbers added to {total} pages.\n\nSaved to:\n{dst}", parent=root)


if __name__ == "__main__":
    main()
