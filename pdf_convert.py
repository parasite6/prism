#!/usr/bin/python3
"""Run only in the isolated PDF environment; never import into the GTK process."""
import re
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
        # pdf2docx's default skips a page that fails and still reports success.
        converter.convert(out, multi_processing=False, ignore_page_error=False)
    except Exception as exc:
        Path(out).unlink(missing_ok=True)
        page = re.search(r"Error when (?:parsing|make) page (\d+)", str(exc))
        raise RuntimeError(f"Page {page[1]} could not be converted; nothing was saved" if page
                           else "This PDF could not be converted; nothing was saved") from exc
    finally:
        converter.close()


def _value(pdf, xref, key):
    kind, value = pdf.xref_get_key(xref, key)
    if kind == "xref":  # indirect, e.g. /Filter 12 0 R
        value = pdf.xref_object(int(value.split()[0]), compressed=True)
    return value


def _clean_jpeg(pdf, xref, filters):
    """Strip EXIF/APPn/COM from a JPEG image stream, also one under outer filters
    (/Filter [/FlateDecode /DCTDecode]). Fail closed: never leave one uncleaned."""
    from scrub import strip_jpeg

    failed = RuntimeError("An image in this PDF could not be cleaned; output was not saved")
    if filters[-1] != "DCTDecode" or filters.count("DCTDecode") > 1 or not pdf.is_stream(xref):
        raise failed
    transform = re.search(r"/ColorTransform\s*(\d)", _value(pdf, xref, "DecodeParms"))
    try:
        if len(filters) == 1:
            jpeg = pdf.xref_stream_raw(xref)
        else:  # xref_stream decodes the whole chain to pixels: drop DCT so it stops at the JPEG.
            # DCT is last, so the outer filters keep their DecodeParms by position.
            pdf.xref_set_key(xref, "Filter", "[" + "".join("/" + f for f in filters[:-1]) + "]")
            jpeg = pdf.xref_stream(xref)
        if jpeg[:2] != b"\xff\xd8":
            raise ValueError
        clean = strip_jpeg(jpeg)
    except Exception:
        raise failed from None
    if clean != jpeg or len(filters) > 1:
        pdf.update_stream(xref, clean, compress=0)
        pdf.xref_set_key(xref, "Filter", "/DCTDecode")
        pdf.xref_set_key(xref, "DecodeParms", f"<</ColorTransform {transform[1]}>>" if transform else "null")


def scrub(path):
    """Remove all metadata: info dict, XMP, annotation authors/dates, page
    thumbnails, EXIF inside embedded JPEGs. Full rewrite, so nothing old
    survives in an incremental-update section."""
    import pymupdf

    tmp = Path(path).with_name(Path(path).name + ".scrub")
    with pymupdf.open(path) as pdf:
        pdf.scrub(attached_files=False, clean_pages=False, embedded_files=False, hidden_text=False,
                  javascript=False, metadata=True, redactions=False, remove_links=False,
                  reset_fields=False, reset_responses=True, thumbnails=True, xml_metadata=True)
        pdf.set_metadata({})
        for xref in range(1, pdf.xref_length()):
            filters = re.findall(r"/(\w+)", _value(pdf, xref, "Filter"))
            if "DCTDecode" in filters:  # bare name, [/DCTDecode] or a chain ending in it
                _clean_jpeg(pdf, xref, filters)
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
