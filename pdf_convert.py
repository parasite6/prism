#!/usr/bin/python3
"""Run only in the isolated PDF environment; never import into the GTK process."""
import sys
from pathlib import Path


def main(src, out):
    import pymupdf

    with pymupdf.open(src) as pdf:
        if pdf.needs_pass:
            raise RuntimeError("This PDF is password protected")
        if not any(page.get_text().strip() for page in pdf):
            raise RuntimeError("This PDF has no extractable text. Scanned PDFs need OCR, which is not enabled.")
        if Path(out).suffix == ".md":
            import pymupdf4llm
            markdown = pymupdf4llm.to_markdown(pdf, write_images=True,
                                             image_path="media", use_ocr=False)
            Path(out).write_text(markdown, encoding="utf-8")
            return
    from pdf2docx import Converter
    converter = Converter(src)
    try:
        converter.convert(out, multi_processing=False)
    finally:
        converter.close()


def scrub(path):
    """Remove all metadata: info dict, XMP, annotation authors/dates, page
    thumbnails, EXIF inside embedded JPEGs. Full rewrite, so nothing old
    survives in an incremental-update section."""
    import pymupdf
    from scrub import strip_jpeg

    tmp = Path(path).with_name(Path(path).name + ".scrub")
    with pymupdf.open(path) as pdf:
        pdf.scrub(attached_files=False, clean_pages=False, embedded_files=False, hidden_text=False,
                  javascript=False, metadata=True, redactions=False, remove_links=False,
                  reset_fields=False, reset_responses=True, thumbnails=True, xml_metadata=True)
        pdf.set_metadata({})
        for xref in range(1, pdf.xref_length()):
            if pdf.xref_get_key(xref, "Filter") == ("name", "/DCTDecode"):
                raw = pdf.xref_stream_raw(xref)
                try:
                    clean = strip_jpeg(raw)
                except ValueError:
                    raise RuntimeError("An image in this PDF could not be read; output was not saved") from None
                if clean != raw:
                    pdf.update_stream(xref, clean, compress=0)
                    pdf.xref_set_key(xref, "Filter", "/DCTDecode")
            for key in ("CreationDate", "ModDate", "M", "LastModified"):  # stray dates on any object
                if pdf.xref_get_key(xref, key)[0] == "string":
                    pdf.xref_set_key(xref, key, "null")
            # XMP on images/pages and Illustrator/Photoshop private data (paths, names).
            if xref != pdf.pdf_catalog():
                for key in ("Metadata", "PieceInfo"):
                    if pdf.xref_get_key(xref, key)[0] != "null":
                        pdf.xref_set_key(xref, key, "null")
            # /T is an annotation's author, but a form field's name: keep those.
            # Annotations always have a /Subtype; parent fields in a hierarchy don't.
            subtype = pdf.xref_get_key(xref, "Subtype")
            if pdf.xref_get_key(xref, "T")[0] == "string" and pdf.xref_get_key(xref, "FT")[0] == "null" \
                    and subtype[0] == "name" and subtype != ("name", "/Widget"):
                pdf.xref_set_key(xref, "T", "null")
        pdf.save(tmp, garbage=4, deflate=True)
    tmp.replace(path)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--scrub":
            scrub(sys.argv[2])
            sys.exit(0)
        main(*sys.argv[1:])
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
