import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ac import files, pdf
from tests.make_pdf import make_pdf

needs_pdfkit = unittest.skipUnless(pdf._has_pdfkit(), "needs macOS PDFKit")


class PdfTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, name, pages):
        path = self.root / name
        path.write_bytes(make_pdf(pages))
        return path

    @needs_pdfkit
    def test_page_texts(self):
        path = self.write("doc.pdf", ["The budget is 640 euros.", None, "Gate closes (07:10)."])
        self.assertEqual([t.strip() for t in pdf.page_texts(path)],
                         ["The budget is 640 euros.", "", "Gate closes (07:10)."])

    @needs_pdfkit
    def test_whole_pdf_becomes_one_text_attachment(self):
        path = self.write("doc.pdf", ["First page text here.", "Second page text here."])
        (got,), notes = files.read_all(path)
        self.assertEqual((got.kind, got.note, notes), ("text", "PDF, 2 pages", []))
        self.assertEqual(got.content, "[page 1]\nFirst page text here.\n\n[page 2]\nSecond page text here.")
        self.assertEqual(files.describe(got).split(" (")[1], "text, 63 B, PDF, 2 pages)")
        self.assertTrue(files.for_model(got).startswith(f'<file path="{path}" note="PDF, 2 pages">'))

    @needs_pdfkit
    def test_long_pdf_stops_at_a_page_boundary(self):
        path = self.write("long.pdf", ["\n".join([f"Page {n}"] + ["lorem ipsum dolor sit amet"] * 9)
                                       for n in range(1, 8)])
        with mock.patch.object(files, "MAX_TEXT_BYTES", 600):
            (got,), notes = files.read_all(path)
        self.assertEqual(got.note, "PDF, pages 1-2 of 7")
        self.assertTrue(got.content.endswith("lorem ipsum dolor sit amet"))  # whole pages only
        self.assertEqual(got.content.count("[page "), 2)
        self.assertEqual(notes, ["long.pdf is long: sent pages 1-2 of 7"])

    @needs_pdfkit
    def test_scanned_pdf_goes_as_page_images(self):
        path = self.write("scan.pdf", [None, None, None])
        with mock.patch.object(files, "MAX_PDF_IMAGE_PAGES", 2):
            got, notes = files.read_all(path)
        self.assertEqual([(a.kind, a.path) for a in got],
                         [("image", f"{path}#1"), ("image", f"{path}#2")])
        self.assertTrue(all(a.data.startswith(b"\x89PNG\r\n\x1a\n") for a in got))
        self.assertEqual(notes, ["scan.pdf has no text layer (a scan?), so pages 1-2 of 3 went as "
                                 "images"])
        self.assertEqual(files.for_model(got[0]), f'<image path="{path}#1"/>')

    @needs_pdfkit
    def test_broken_pdf(self):
        path = self.root / "broken.pdf"
        path.write_bytes(b"this is not a pdf at all")
        with self.assertRaisesRegex(files.FileError, "can't read broken.pdf: it isn't a readable PDF"):
            files.read_all(path)

    def test_pdftotext_is_used_where_there_is_no_pdfkit(self):
        tool = self.root / "bin" / "pdftotext"
        tool.parent.mkdir()
        tool.write_text('#!/bin/sh\n[ "$4" = "bad.pdf" ] && { echo "Syntax Error: broken" >&2; exit 1; }\n'
                        'printf "page one\\fpage two\\f"\n')
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        path = os.pathsep.join([str(tool.parent), os.environ["PATH"]])
        with mock.patch.object(pdf, "_has_pdfkit", lambda: False), \
                mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(pdf.page_texts("any.pdf"), ["page one", "page two"])
            with self.assertRaisesRegex(pdf.PdfError, "pdftotext failed: Syntax Error: broken"):
                pdf.page_texts("bad.pdf")
            self.assertFalse(pdf.can_render())

    def test_no_reader_available(self):
        scan = self.write("scan.pdf", [None])
        with mock.patch.object(pdf, "_has_pdfkit", lambda: False), \
                mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            with self.assertRaisesRegex(pdf.PdfError, "needs `pdftotext`"):
                pdf.page_texts(scan)
        with mock.patch.object(pdf, "page_texts", lambda p: [""]), \
                mock.patch.object(pdf, "can_render", lambda: False):
            with self.assertRaisesRegex(files.FileError, "no text layer .* can't be turned into images"):
                files.read_all(scan)


if __name__ == "__main__":
    unittest.main()
