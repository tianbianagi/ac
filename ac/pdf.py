"""Reading PDFs without a PDF library.

On macOS the system's own PDF engine (PDFKit) does the work, reached through `osascript`,
which ships with the OS. Elsewhere `pdftotext` (poppler) is used if it is installed. Pages
can also be rendered to images, for scanned documents that have no text to extract.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 120
RENDER_LONG_SIDE = 1400  # pixels; enough for a vision model to read body text


class PdfError(Exception):
    pass


_TEXT_JS = """
ObjC.import('PDFKit');
function run(argv) {
    const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
    if (doc.isNil()) return JSON.stringify({error: "it isn't a readable PDF"});
    if (doc.isLocked) return JSON.stringify({error: "it is password-protected"});
    const pages = [];
    for (let i = 0; i < doc.pageCount; i++) {
        const text = doc.pageAtIndex(i).string;
        pages.push(text.isNil() ? "" : ObjC.unwrap(text));
    }
    return JSON.stringify({pages: pages});
}
"""

# argv: pdf path, output directory, long side in pixels, then the 1-based page numbers.
_RENDER_JS = """
ObjC.import('PDFKit');
ObjC.import('AppKit');
function run(argv) {
    const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
    if (doc.isNil()) return JSON.stringify({error: "it isn't a readable PDF"});
    if (doc.isLocked) return JSON.stringify({error: "it is password-protected"});
    const longSide = +argv[2], written = [];
    for (const n of argv.slice(3).map(Number)) {
        const page = doc.pageAtIndex(n - 1);
        const box = page.boundsForBox($.kPDFDisplayBoxMediaBox).size;
        const scale = longSide / Math.max(box.width, box.height);
        const w = Math.round(box.width * scale), h = Math.round(box.height * scale);
        // An explicit bitmap, so the pixel size doesn't depend on the display's scale factor.
        const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
            null, w, h, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0);
        const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
        $.NSGraphicsContext.saveGraphicsState;
        $.NSGraphicsContext.setCurrentContext(ctx);
        $.NSColor.whiteColor.setFill;
        $.NSRectFill($.NSMakeRect(0, 0, w, h));
        $.CGContextScaleCTM(ctx.CGContext, scale, scale);
        page.drawWithBoxToContext($.kPDFDisplayBoxMediaBox, ctx.CGContext);
        $.NSGraphicsContext.restoreGraphicsState;
        const out = argv[1] + "/" + n + ".png";
        rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $())
           .writeToFileAtomically(out, true);
        written.push(n);
    }
    return JSON.stringify({written: written});
}
"""


def _has_pdfkit():
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PdfError(f"reading it took longer than {TIMEOUT} seconds") from None
    except OSError as e:
        raise PdfError(f"{command[0]} failed to start: {e.strerror or e}") from None


def _pdfkit(script, *args):
    done = _run(["osascript", "-l", "JavaScript", "-e", script, *map(str, args)])
    try:
        result = json.loads(done.stdout)
    except ValueError:
        detail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise PdfError(f"the system PDF reader failed: {detail}") from None
    if "error" in result:
        raise PdfError(result["error"])
    return result


def page_texts(path):
    """The text of each page, in order. Pages without text come back as empty strings."""
    if _has_pdfkit():
        return _pdfkit(_TEXT_JS, path)["pages"]
    if shutil.which("pdftotext"):
        done = _run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"])
        if done.returncode != 0:
            detail = (done.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise PdfError(f"pdftotext failed: {detail}")
        pages = done.stdout.split("\f")
        return pages[:-1] if pages and not pages[-1].strip() else pages
    raise PdfError("reading PDFs on this system needs `pdftotext` (part of poppler)")


def can_render():
    return _has_pdfkit()


def render_pages(path, numbers):
    """PNG bytes for the given 1-based page numbers, as [(number, data), ...]."""
    if not can_render():
        raise PdfError("pages can only be turned into images on macOS")
    with tempfile.TemporaryDirectory() as out:
        written = _pdfkit(_RENDER_JS, path, out, RENDER_LONG_SIDE, *numbers)["written"]
        return [(int(n), (Path(out) / f"{int(n)}.png").read_bytes()) for n in written]
