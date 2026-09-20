"""Build tiny, valid PDFs for tests without a PDF library."""


def make_pdf(pages):
    """One page per item: a string becomes that page's text (one line per \\n; text running
    off the page is clipped by extractors); None gives a page with only a drawing on it, which
    is what a scan looks like to a text extractor."""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, text in enumerate(pages):
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page_id} 0 R")
        if text is None:
            stream = b"0.2 0.4 0.8 rg 100 500 300 200 re f"
        else:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            lines = " T* ".join(f"({line}) Tj" for line in escaped.split("\n"))
            stream = f"BT /F1 12 Tf 14 TL 72 720 Td {lines} ET".encode("latin-1")
        objects[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                            f"/Resources << /Font << /F1 3 0 R >> >> "
                            f"/Contents {content_id} 0 R >>").encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()

    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for number in sorted(objects):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
