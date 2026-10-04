"""
Merge multiple PDFs into one.

Default order: by file name - numbers compared numerically, text alphabetically
(case-insensitive).   1.pdf, 2.pdf, 10.pdf, A.pdf, b.pdf

Flow:
  1. Pop-up: select the PDFs to merge (Ctrl/Shift-click for many)
  2. Pop-up: shows the automatic order -> Yes to continue, No to arrange manually
  3. (If No) Arrange window: drag files up/down into the order you want
  4. Pop-up: choose where to save the merged PDF

Install once:
    pip install pypdf
"""

import os
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from pypdf import PdfReader, PdfWriter


# ---------------------------------------------------------------- sorting
def natural_key(path):
    """Digits compared as numbers, text compared case-insensitively.
    At the same position, numbers come before text."""
    name = os.path.splitext(os.path.basename(path))[0]
    key = []
    for p in re.split(r"(\d+)", name):
        if p == "":
            continue
        key.append((0, int(p), "") if p.isdigit() else (1, 0, p.casefold()))
    return key + [(2, 0, name)]


# ---------------------------------------------------------------- merging
def merge_pdfs(paths, dst, root=None):
    writer = PdfWriter()
    for p in paths:
        reader = PdfReader(p)
        if reader.is_encrypted and reader.decrypt("") == 0:
            pwd = simpledialog.askstring(
                "Password needed",
                f"'{os.path.basename(p)}' is password-protected.\nEnter password:",
                show="*", parent=root,
            )
            if not pwd or reader.decrypt(pwd) == 0:
                raise ValueError(f"Could not decrypt '{os.path.basename(p)}' (wrong password).")
        writer.append(reader)  # keeps each file's bookmarks and links

    with open(dst, "wb") as f:
        writer.write(f)
    return len(writer.pages)


# ---------------------------------------------------------------- arrange UI
def arrange_window(root, paths):
    """Drag-to-reorder window. Returns the new list of paths, or None if cancelled."""
    items = list(paths)
    result = {"paths": None}

    win = tk.Toplevel(root)
    win.title("Arrange PDFs - drag to reorder")
    win.geometry("560x520")
    win.attributes("-topmost", True)

    ttk.Label(
        win,
        text="Drag a file up or down to change its position.\n"
             "The merged PDF will follow the order shown here (top = first).",
        justify="left",
    ).pack(anchor="w", padx=12, pady=(12, 6))

    body = ttk.Frame(win)
    body.pack(fill="both", expand=True, padx=12, pady=4)

    lb = tk.Listbox(
        body, activestyle="none", selectmode="browse", exportselection=False,
        font=("Segoe UI", 11), height=16,
        selectbackground="#2f6fed", selectforeground="white",
    )
    sb = ttk.Scrollbar(body, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    status = ttk.Label(win, text="", foreground="#555")
    status.pack(anchor="w", padx=12)

    def refresh(select=None):
        lb.delete(0, "end")
        for i, p in enumerate(items, 1):
            lb.insert("end", f"  {i:>2}.   {os.path.basename(p)}")
        if select is not None and items:
            select = max(0, min(select, len(items) - 1))
            lb.selection_clear(0, "end")
            lb.selection_set(select)
            lb.activate(select)
            lb.see(select)
        status.config(text=f"{len(items)} files")

    # --- drag and drop
    drag = {"i": None}

    def on_press(e):
        if not items:
            return
        drag["i"] = lb.nearest(e.y)
        lb.selection_clear(0, "end")
        lb.selection_set(drag["i"])

    def on_motion(e):
        cur = drag["i"]
        if cur is None:
            return
        # auto-scroll when dragging near the top/bottom edge
        if e.y < 8:
            lb.yview_scroll(-1, "units")
        elif e.y > lb.winfo_height() - 8:
            lb.yview_scroll(1, "units")
        new = max(0, min(lb.nearest(e.y), len(items) - 1))
        if new != cur:
            items.insert(new, items.pop(cur))
            drag["i"] = new
            top = lb.yview()[0]
            refresh(new)
            lb.yview_moveto(top)

    def on_release(_e):
        drag["i"] = None

    lb.bind("<ButtonPress-1>", on_press)
    lb.bind("<B1-Motion>", on_motion)
    lb.bind("<ButtonRelease-1>", on_release)

    # --- buttons
    def selected():
        s = lb.curselection()
        return s[0] if s else None

    def move(delta):
        i = selected()
        if i is None:
            return
        j = i + delta
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            refresh(j)

    def remove():
        i = selected()
        if i is None:
            return
        if len(items) <= 2:
            messagebox.showwarning("Too few files", "At least 2 files are needed.", parent=win)
            return
        items.pop(i)
        refresh(i)

    def reset():
        items.sort(key=natural_key)
        refresh(0)

    def reverse():
        items.reverse()
        refresh(0)

    def ok():
        result["paths"] = list(items)
        win.destroy()

    row = ttk.Frame(win)
    row.pack(fill="x", padx=12, pady=(8, 4))
    ttk.Button(row, text="▲ Up", command=lambda: move(-1)).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="▼ Down", command=lambda: move(1)).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="Reverse", command=reverse).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="Reset (auto sort)", command=reset).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="Remove", command=remove).pack(side="left")

    row2 = ttk.Frame(win)
    row2.pack(fill="x", padx=12, pady=(4, 12))
    ttk.Button(row2, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(row2, text="Merge in this order  →", command=ok).pack(side="right", padx=(0, 8))

    lb.bind("<Delete>", lambda e: remove())
    win.bind("<Escape>", lambda e: win.destroy())

    refresh(0)
    win.focus_force()
    win.grab_set()
    root.wait_window(win)
    return result["paths"]


# ---------------------------------------------------------------- main
def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    paths = filedialog.askopenfilenames(
        parent=root,
        title="Select the PDFs to merge",
        filetypes=[("PDF files", "*.pdf")],
    )
    if not paths:
        print("No files selected. Exiting.")
        return
    if len(paths) < 2:
        messagebox.showwarning("Select more files", "Please select at least 2 PDFs.", parent=root)
        return

    paths = sorted(paths, key=natural_key)

    order = "\n".join(f"{i}. {os.path.basename(p)}" for i, p in enumerate(paths, 1))
    happy = messagebox.askyesno(
        "Confirm merge order",
        f"The PDFs will be merged in this order:\n\n{order}\n\n"
        "Yes = continue\nNo = arrange the order manually",
        parent=root,
    )

    if not happy:
        paths = arrange_window(root, paths)
        if not paths:
            print("Cancelled.")
            return

    dst = filedialog.asksaveasfilename(
        parent=root,
        title="Choose where to save the merged PDF",
        initialdir=os.path.dirname(paths[0]),
        initialfile="merged.pdf",
        defaultextension=".pdf",
        filetypes=[("PDF files", "*.pdf")],
    )
    if not dst:
        print("No output location selected. Exiting.")
        return

    if os.path.abspath(dst) in {os.path.abspath(p) for p in paths}:
        messagebox.showerror(
            "Same file", "Output must be different from the input files.", parent=root
        )
        return

    try:
        total = merge_pdfs(paths, dst, root)
    except Exception as e:
        messagebox.showerror("Error", f"Failed:\n{e}", parent=root)
        sys.exit(1)

    messagebox.showinfo(
        "Done", f"Merged {len(paths)} files ({total} pages).\n\nSaved to:\n{dst}", parent=root
    )


if __name__ == "__main__":
    main()
