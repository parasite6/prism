"""Strip all metadata from finished outputs: EXIF/XMP/IPTC/ICC/comments/dates in
images, document properties and author names in office files, the same for
images embedded anywhere. Pure stdlib: imported by the daemon and the PDF venv.

Every output passes through scrub_tree(); an output type without a scrubber
fails the conversion rather than publishing something unscrubbed."""
import base64
import gzip
import io
import re
import struct
import zlib
import zipfile
from pathlib import Path

# Already clean at the source: ImageMagick -strip / ffmpeg -map_metadata -1 /
# plain text with document metadata removed by the pandoc filter. WMF has no
# metadata fields.
CLEAN_AT_SOURCE = {"avif", "heic", "heif", "ico", "bmp", "tif", "tiff", "svg", "mp4", "webm", "mkv", "mov", "m4v",
                   "avi", "flv", "wmv", "mp3", "flac", "ogg", "opus", "wav", "m4a", "aac", "wma", "md",
                   "markdown", "rtf", "txt", "csv", "wmf", "wmz"}
OFFICE = {"docx", "xlsx", "pptx", "odt", "ods", "odp"}
JPEG = {"jpg", "jpeg", "jpe", "jfif"}


def strip_jpeg(data):
    """Drop every APPn (EXIF, XMP, ICC, IPTC, JFIF thumbnails) and COM segment,
    and anything after the image (Motion Photo video, MPF depth/gain-map JPEGs
    with their own EXIF). Adobe APP14 stays: decoders need it for CMYK colour."""
    if data[:2] != b"\xff\xd8":
        return data
    out, i, n, scanned = [b"\xff\xd8"], 2, len(data), False
    while True:
        if i + 2 > n or data[i] != 0xFF:
            if scanned:  # truncated or junk after the image data: end the image here
                break
            raise ValueError("malformed JPEG")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xD9 or (marker == 0xD8 and scanned):  # EOI, or another image glued on
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # standalone markers
            out.append(data[i:i + 2])
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0] if i + 4 <= n else 0
        if length < 2 or i + 2 + length > n:
            if scanned:
                break
            raise ValueError("malformed JPEG")
        segment = data[i:i + 2 + length]
        if not ((0xE0 <= marker <= 0xEF and not (marker == 0xEE and segment[4:9] == b"Adobe"))
                or marker == 0xFE):
            out.append(segment)
        i += 2 + length
        if marker == 0xDA:  # entropy-coded data runs to the next real marker
            scanned = True
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    j = n
                    break
                if data[j + 1] == 0x00 or 0xD0 <= data[j + 1] <= 0xD7:  # stuffed byte, restart
                    j += 2
                    continue
                break
            out.append(data[i:j])
            i = j
    out.append(b"\xff\xd9")
    return b"".join(out)


# Chunks needed to draw the image (incl. APNG animation and HDR colour); text,
# EXIF, time, ICC go.
_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"pHYs",
             b"bKGD", b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCv", b"cLLi"}


def strip_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return data
    out, i = [data[:8]], 8
    while i + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[i:i + 8])
        chunk = data[i:i + 12 + length]
        if kind in _PNG_KEEP:
            out.append(chunk)
        i += 12 + length
        if kind == b"IEND":
            break
    return b"".join(out)


def strip_webp(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return data
    end = min(len(data), 8 + struct.unpack("<I", data[4:8])[0])  # data appended after the RIFF goes
    out, i = [], 12
    while i + 8 <= end:
        kind, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if i + 8 + size > end:
            break
        chunk = data[i:i + 8 + size + (size & 1)]
        if kind == b"VP8X":  # clear the ICC/EXIF/XMP present flags
            chunk = chunk[:8] + bytes([chunk[8] & ~0x2C]) + chunk[9:]
        if kind not in (b"EXIF", b"XMP ", b"ICCP"):
            out.append(chunk)
        i += 8 + size + (size & 1)
    body = b"WEBP" + b"".join(out)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def strip_gif(data):
    """Drop comment and application extensions, except the NETSCAPE loop count."""
    if data[:4] != b"GIF8":
        return data
    flags = data[10]
    i = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
    out = [data[:i]]

    def sub_blocks(j):
        while data[j]:
            j += data[j] + 1
        return j + 1

    # Truncated data or an unknown block ends the image there: what was
    # collected is complete blocks, so nothing unparsed is passed through.
    try:
        while i < len(data):
            block = data[i]
            if block == 0x3B:  # trailer
                break
            if block == 0x21:
                label = data[i + 1]
                end = sub_blocks(i + 2)
                app = data[i + 3:i + 3 + 11] if label == 0xFF else b""
                if label not in (0xFE, 0xFF) or app in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                    out.append(data[i:end])
                i = end
            elif block == 0x2C:  # image: descriptor, optional local table, LZW data
                local = data[i + 9]
                j = i + 10 + (3 << ((local & 7) + 1) if local & 0x80 else 0)
                end = sub_blocks(j + 1)
                out.append(data[i:end])
                i = end
            else:
                break
    except IndexError:
        pass
    out.append(b"\x3b")
    return b"".join(out)


def _orientation(exif):
    """EXIF Orientation (1 = upright) from a TIFF-structured EXIF block."""
    if exif.startswith(b"Exif\0\0"):
        exif = exif[6:]
    order = {b"II": "<", b"MM": ">"}.get(exif[:2])
    try:
        offset = struct.unpack(order + "I", exif[4:8])[0]
        for k in range(struct.unpack(order + "H", exif[offset:offset + 2])[0]):
            entry = exif[offset + 2 + 12 * k:offset + 14 + 12 * k]
            if struct.unpack(order + "H", entry[:2])[0] == 0x0112:
                return struct.unpack(order + "H", entry[8:10])[0]
    except (TypeError, struct.error):
        pass
    return 1


def needs_pixels(data):
    """True when metadata changes how the image looks (rotation, a non-sRGB
    colour profile), so stripping it alone would alter the picture."""
    exif, icc = b"", b""
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] not in (0xDA, 0xD9):
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            body = data[i + 4:i + 2 + length]
            if data[i + 1] == 0xE1 and body.startswith(b"Exif\0\0"):
                exif = body
            elif data[i + 1] == 0xE2 and body.startswith(b"ICC_PROFILE\0"):
                icc += body[14:]
            i += 2 + length
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        i = 8
        while i + 12 <= len(data):
            length, kind = struct.unpack(">I4s", data[i:i + 8])
            body = data[i + 8:i + 8 + length]
            if kind == b"eXIf":
                exif = body
            elif kind == b"iCCP":
                try:
                    icc = zlib.decompress(body[body.index(b"\0") + 2:])
                except (ValueError, zlib.error):
                    return True
            i += 12 + length
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        i = 12
        while i + 8 <= len(data):
            kind, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
            if kind == b"EXIF":
                exif = data[i + 8:i + 8 + size]
            elif kind == b"ICCP":
                icc = data[i + 8:i + 8 + size]
            i += 8 + size + (size & 1)
    srgb = not icc or b"sRGB" in icc[:4096]
    return (exif and _orientation(exif) not in (0, 1)) or not srgb


def strip_svg(text):
    """Editor metadata in SVG: RDF/metadata blocks, comments, Inkscape/Sodipodi
    attributes (file names, export paths) and namedview."""
    text = re.sub(r"<metadata\b.*?</metadata>|<metadata\b[^>]*/>", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<sodipodi:namedview\b.*?(?:/>|</sodipodi:namedview>)", "", text, flags=re.S)
    return re.sub(r"""\s(?:inkscape|sodipodi|sketch|serif):[\w-]+=(?:"[^"]*"|'[^']*')""", "", text)


def strip_rtf(text):
    """RTF keeps author, company and dates in {\\info ...}; revision ids too."""
    def drop_group(text, start):
        i = text.find(start)
        while i != -1:
            depth, j = 0, i
            while j < len(text):
                if text[j] == "\\":
                    j += 2
                    continue
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
                if depth == 0:
                    break
            text = text[:i] + text[j:]
            i = text.find(start)
        return text
    # Template path (holds the user name), document variables, comment dates.
    for group in ("{\\info", "{\\*\\rsidtbl", "{\\*\\generator", "{\\*\\xmlnstbl", "{\\*\\userprops",
                  "{\\*\\revtbl", "{\\*\\template", "{\\*\\docvar", "{\\*\\atndate"):
        text = drop_group(text, group)
    # Comment author names and initials: emptied, the groups themselves stay.
    text = re.sub(r"(\{(?:\\\*)?\\atn(?:id|author))(?![a-z])[^{}]*\}", r"\1}", text)
    # Zero the revision-session ids rather than delete them: removing a control
    # word can glue its neighbours into a different one ("\\pard" + "Hello").
    return re.sub(r"(\\(?:rsid|insrsid|charrsid|pararsid|sectrsid|delrsid|tblrsid|styrsid))\d+", r"\g<1>0", text)


def strip_markdown(text):
    """YAML front matter or a pandoc % title block at the top."""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    text = text[len(bom):]
    if re.match(r"---[ \t]*\r?\n", text):
        end = re.search(r"\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", text[3:])
        # Only a YAML mapping: a leading horizontal rule followed by prose is content.
        if end and re.match(r"\s*(?:#.*\n\s*)*[\w-]+[ \t]*:", text[3:3 + end.start()] + "\n"):
            text = text[3 + end.end():].lstrip("\r\n")
    lines = text.splitlines(keepends=True)
    while lines and lines[0].startswith("%"):
        lines.pop(0)
        while lines and lines[0][:1] in (" ", "\t"):  # continuation lines
            lines.pop(0)
    # Comment / tracked-change authors and dates in pandoc's attribute blocks,
    # e.g. {.comment-start id="0" author=...}; other braces are content.
    return bom + re.sub(r'\{[^{}\n]*\.(?:comment-start|comment-end|insertion|deletion|paragraph-insertion)\b[^{}\n]*\}',
                        lambda m: re.sub(r'\s(?:author|date)="[^"]*"', "", m.group()), "".join(lines))


def strip_emf(data):
    """EMF headers carry a description string (application and document title)."""
    if len(data) < 88 or data[:4] != b"\x01\x00\x00\x00" or data[40:44] != b" EMF":
        return data
    count, offset = struct.unpack("<II", data[60:68])
    if count and offset >= 88 and offset + 2 * count <= len(data):
        data = data[:offset] + bytes(2 * count) + data[offset + 2 * count:]
    return data


def strip_image_bytes(data):
    if data[:2] == b"\x1f\x8b":  # .emz: a gzipped EMF
        try:
            inner = gzip.decompress(data)
        except (OSError, EOFError):
            return data
        clean = strip_emf(inner)
        return data if clean == inner else gzip.compress(clean, mtime=0)
    for strip in (strip_jpeg, strip_png, strip_webp, strip_gif, strip_emf):
        data = strip(data)
    return data


# Office documents (OOXML and ODF are both zip packages).
_CORE = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties '
         b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
         b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
         b'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
_APP = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties '
        b'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
_CUSTOM = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties '
           b'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
           b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
_XML_RULES = [
    # OOXML: revision-session ids, comment/tracked-change authors and dates,
    # template and absolute source paths, people/author lists.
    (rb'\s(?:w|w14|w15):rsid\w*="[^"]*"', b""),
    (rb"<w:rsids>.*?</w:rsids>", b""),
    (rb"<w:attachedTemplate\b[^>]*/>", b""),
    (rb'<Relationship\b[^>]*Type="[^"]*/attachedTemplate"[^>]*/>', b""),  # its file:///C:/Users/... path
    (rb"<w:docVars>.*?</w:docVars>", b""),
    (rb"<x15ac:absPath\b[^>]*/>", b""),
    (rb'(\s(?:w:|w15:)?(?:author|initials|userId|providerId)=")[^"]*(")', rb"\1\2"),
    (rb'\s(?:w:date|w16du:dateUtc|dt|dT)="[^"]*"', b""),
    (rb"<author>[^<]*</author>", b"<author></author>"),
    # ODF: annotation / tracked-change creators and dates, printer details.
    (rb"<dc:creator>[^<]*</dc:creator>", b""),
    (rb"<meta:creator-initials>[^<]*</meta:creator-initials>", b""),
    (rb"<dc:date>[^<]*</dc:date>", b"<dc:date>1970-01-01T00:00:00</dc:date>"),
    (rb'(<config:config-item config:name="(?:PrinterName|PrinterSetup)"[^>]*>)[^<]*(</config:config-item>)', rb"\1\2"),
]
# Parts that list people (Word people.xml, Excel persons, PowerPoint authors):
# their display names go too. Elsewhere "name=" is ordinary content.
_PEOPLE = re.compile(r"(people|person|author)[^/]*\.xml$", re.I)


def _scrub_xml(name, data):
    if name == "docProps/core.xml":
        return _CORE
    if name == "docProps/app.xml":
        return _APP
    if name == "docProps/custom.xml":
        return _CUSTOM
    if name.rsplit("/", 1)[-1] == "meta.xml":  # also embedded objects' "Object N/meta.xml"
        version = re.search(rb'office:version="([^"]*)"', data)
        return (b'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta '
                b'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="'
                + (version.group(1) if version else b"1.3") + b'"><office:meta/></office:document-meta>')
    for pattern, repl in _XML_RULES:
        data = re.sub(pattern, repl, data, flags=re.S)
    if _PEOPLE.search(name):
        data = re.sub(rb'(\s(?:\w+:)?(?:name|displayName)=")[^"]*(")', rb"\1\2", data)
    if "comment" in name.lower():  # PowerPoint modern comments and replies
        data = re.sub(rb'\screated="[^"]*"', b"", data)
    return data


def _strip_package_bytes(data):
    try:
        out = io.BytesIO()
        _strip_package(io.BytesIO(data), out)
        return out.getvalue()
    except zipfile.BadZipFile:
        return data  # not actually a zip: leave it


def strip_office(path):
    path = Path(path)
    tmp = path.with_name(path.name + ".scrub")
    try:
        _strip_package(path, tmp)
    except zipfile.BadZipFile:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("This document is password-protected or damaged; output was not saved") from None
    tmp.replace(path)


def _strip_package(source, target):
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.endswith((".xml", ".rels")):
                data = _scrub_xml(item.filename, data)
            elif data[:2] == b"PK":  # embedded package (chart data, OLE spreadsheet)
                data = _strip_package_bytes(data)
            elif re.search(r"printerSettings\d*\.bin$", item.filename) and len(data) >= 64:
                data = bytes(64) + data[64:]  # DEVMODE: the printer's name
            elif not item.is_dir():
                data = strip_image_bytes(data)  # embedded photos and thumbnails
            # Fixed timestamps, no extra fields or comments; keep order and
            # compression (ODF's "mimetype" must stay first and stored).
            clean = zipfile.ZipInfo(item.filename, date_time=(1980, 1, 1, 0, 0, 0))
            clean.compress_type = item.compress_type
            clean.external_attr = 0o644 << 16
            dst.writestr(clean, data)


def strip_html(path):
    path = Path(path)
    text = path.read_bytes()

    def image(m):
        clean = strip_image_bytes(base64.b64decode(m.group(2)))
        return m.group(1) + base64.b64encode(clean)

    text = re.sub(rb"(data:image/(?:jpeg|jpg|png|webp|gif);base64,)([A-Za-z0-9+/=]+)", image, text)
    text = re.sub(rb"<meta\b[^>]*>\s*", lambda m: b"" if _META.search(m.group()) else m.group(), text, flags=re.I)
    # Word's "Save as Web Page" properties (author, company, dates) and the
    # browser's "saved from url" comment.
    text = re.sub(rb"<o:(?:Custom)?DocumentProperties>.*?</o:(?:Custom)?DocumentProperties>\s*", b"", text,
                  flags=re.S | re.I)
    text = re.sub(rb"<!--\s*saved from url=.*?-->\s*", b"", text, flags=re.S | re.I)
    # Comment / tracked-change authors and dates that pandoc keeps as attributes.
    text = re.sub(rb'\sdata-(?:author|date|initials)="[^"]*"', b"", text)
    path.write_bytes(text)


# <meta> tags naming people, tools, dates or descriptive metadata, whatever the
# attribute order or quoting. charset, viewport etc. stay.
_META = re.compile(rb"""\s(?:name|property|itemprop)\s*=\s*["']?(?:generator|author|creator|publisher|copyright|"""
                   rb"""keywords|description|subject|date|created|modified|revised|last-modified|owner|"""
                   rb"""originator|progid|dc\.|dcterms\.|og:|article:|twitter:|citation_)""", re.I)


def scrub_tree(root, scrub_pdf):
    """Scrub every file under root (a file or the payload folder)."""
    root = Path(root)
    for f in ([root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())):
        ext = f.suffix.lower().lstrip(".")
        try:
            if ext in JPEG | {"png", "webp", "gif", "emf", "emz"}:
                data = f.read_bytes()
                clean = strip_image_bytes(data)
                if clean != data:
                    f.write_bytes(clean)
            elif ext in OFFICE:
                strip_office(f)
            elif ext in ("html", "htm"):
                strip_html(f)
            elif ext == "pdf":
                scrub_pdf(f)
            elif ext not in CLEAN_AT_SOURCE:
                raise RuntimeError(f"No metadata scrubber for {ext.upper() or 'this file'}; output was not saved")
        except ValueError:  # an image the scrubber cannot parse: never publish it as-is
            raise RuntimeError(f"Could not read {f.name} to remove its metadata; output was not saved") from None
