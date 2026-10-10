# Prism

A radial file converter for Nautilus on Hyprland. Select files, hold the mouse
**Forward** button, and a ring of options opens under the pointer. Release over
one to run it. The result is saved next to the original, and a notification
tells you when it's done.

The main ring has up to five petals. CONVERT, COMPRESS and RESCALE each open
their own ring, which replaces the main one when you rest on the petal. Every
sub ring has a BACK petal.

| Petal | What it does |
|---|---|
| **CONVERT** | Formats common to everything selected (see below) |
| **COMPRESS** | Audio and images: LIGHT · MEDIUM · STRONG · HEAVY · MAX |
| **RESCALE** | Images and video: ½ · ⅓ · 2×, offered according to the pixel size |
| **EDIT** | One image: CROP or ROTATE, each in the edit window |
| **CLEAN** | Same file, every bit of metadata removed |

To hold something else instead of Forward, open **Prism: Set Shortcut** from
the app launcher (or run `prism-bind`), press the keys or mouse button, and
click OK. A binding is modifiers (Ctrl, Shift, Alt, Super) plus one key or
mouse button, e.g. Ctrl + Alt + A or Shift + Back. It is saved in
`~/.config/prism/binding.json` and takes effect immediately.

Other ways to open the ring: **Ctrl+Alt+C** in Nautilus, or right-click >
**Scripts > Prism**. A quick tap leaves the ring open for clicking. Escape,
Forward again, or releasing outside the ring cancels. Outside Nautilus,
Forward stays the ordinary Forward button.

## Conversions

- **Images** (png, jpg, webp, avif, heic, gif, bmp, tiff, ico, svg): png, jpg, webp, avif, heic, gif, ico
- **Video** (mp4, webm, mkv, mov, avi, m4v, flv, wmv): mp4, webm, mkv, mov, gif, mp3
- **Audio** (mp3, flac, ogg, opus, wav, m4a, aac, wma): mp3, flac, ogg, opus, wav, m4a
- **Documents** (md, txt, docx, doc, odt, rtf, html, epub): pdf, docx, odt, md, html, txt, png, jpg
- **Spreadsheets** (xlsx, ods, xls, csv): xlsx, ods, csv, pdf, png, jpg
- **Slides** (pptx, odp, ppt): pdf, pptx, odp, png, jpg
- **PDF**: md, docx, txt, png, jpg

Multi-page documents to images give one image per page, in a folder. PDF to
Markdown gives a folder with the images, linked relatively. A multi-frame
source going to a still format keeps the first frame, and the notification
says so. HEIC output appears only if libheif has a HEVC encoder (Fedora's
build does not).

**Compress** keeps the dimensions. MEDIUM is the "profile picture" level: about
half the size, with no visible change. HEAVY is mono, 16 kHz, 48 kb/s audio,
which is the usual speech-to-text setting. Lossless audio (wav, flac) and wma become
mp3. If a level wouldn't make the file smaller, nothing is saved and the
notification says so.

**Rescale** uses Lanczos for images and the GPU (NVENC) for video when it's
available. 2× is offered only up to 3000 px for images, and up to 1920 px for
video so the result stays within 4K.

## Edit: crop and rotate

CROP and ROTATE open the same window. It is an ordinary window, so Hyprland
tiles it. It has no title bar, and **Esc** closes it.

**Crop:**

- **Drag** to draw the box, drag inside it to move it, and drag the orange
  handles to resize. A click outside the box selects the whole image again.
- **Ctrl +**, **Ctrl −** and **Ctrl 0**, or **Ctrl+scroll**, zoom the view
  (the crop is not affected). **Scroll** or **middle-drag** pans.

**Rotate:** the toolbar buttons, or **[** and **]**, turn the image left and
right a quarter turn; **H** and **V** flip it. The size shows what will be
written.

**Both** end with the same three buttons:

- **Copy** puts the result on the clipboard (`wl-copy`) and leaves the window open.
- **Save** opens the xdg-desktop-portal save dialog. A different extension
  saves in that format.
- **Replace** overwrites the original after an inline confirmation. It keeps
  the file's permissions and refuses if the file changed on disk since it was
  opened.

Photos are shown and cut upright (EXIF rotation is applied first). Animated GIF
and WebP keep every frame.

## Metadata

Every output goes through the same scrubber, not just CLEAN: EXIF, GPS, XMP,
IPTC, comments, embedded thumbnails, encoder tags, office document properties,
comment and revision authors, PDF info, XMP and annotation authors, and HTML
author and generator tags. EXIF rotation and colour profiles are baked into the
pixels first, so the picture doesn't change. If there is no scrubber for a type,
or a file can't be parsed, the conversion fails rather than saving anything
unscrubbed.

## Untrusted files

Files to convert often come from the internet, so a file can't pull anything
else in, phone home, or take the desktop down with it:

- LibreOffice and FFmpeg run in a bubblewrap sandbox with no network, no home
  folder and no input devices. Of your files, they see only the one being
  converted (read-only) and the job's temp folder. A document's linked images
  and a video "manifest" posing as `.mp4` find nothing else to read.
- ImageMagick is limited, by `magick/policy.xml`, to the formats Prism handles.
  A renamed PostScript, MSL or MVG file can't run Ghostscript, read local files
  or fetch URLs, and no external delegate (Inkscape, LibreOffice...) is started.
- Documents may embed only images inside their own folder (symlinks resolved),
  their own media, and `data:` URIs. An image pointing to `~/.ssh/...`, `../..`
  or a URL is dropped, keeping its alt text, and so is raw HTML or CSS that
  could load something (`safe-resources.lua`).
- Each conversion runs in its own worker process, capped at 3 GB of memory
  (a systemd scope), so a hostile file can't exhaust memory for the ring or
  the desktop. Compressed parts of documents and images are never inflated
  without a limit either.
- File headers are read off the ring's UI thread, with a bounded walk, so a
  crafted file can't freeze the ring while it holds the keyboard.

Inputs are never modified, except by the edit window's Replace. Outputs are
numbered rather than overwriting anything, and a failed conversion leaves
nothing behind.

## Install

Fedora with Hyprland 0.56+ (Lua config) and Nautilus. Dependencies:

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell ImageMagick ffmpeg \
    libheif-tools pandoc libreoffice poppler-utils pngquant oxipng gifsicle wl-clipboard \
    libnotify perl-Image-ExifTool bubblewrap
```

The `ffmpeg` package must be the full build (RPM Fusion, with libx264, libvpx,
libopus and libmp3lame). A FileChooser portal backend such as
`xdg-desktop-portal-gnome` or `-gtk` is needed for the edit window's Save. Reading
the Forward button (or your binding) needs read access to `/dev/input` (for
example the `input` group); without it only Ctrl+Alt+C works. The daemon asks
the kernel for the binding's keys only, so typing and mouse movement never
reach it. `exiftool` is optional; it lets CLEAN
strip AVIF and HEIC losslessly.

Then, from the repo:

```sh
git clone https://github.com/parasite6/prism && cd prism
./install.sh
```

`install.sh` is the only step, and you re-run it after pulling an update. It:

- copies the code to `~/.local/share/prism`, so the running ring never sees a
  half-edited checkout;
- the first time, sets up the PDF libraries in `.pdf-venv` (about 450 MB from
  PyPI) with `setup-pdf.sh`;
- links the Nautilus script and adds the Ctrl+Alt+C accelerator;
- adds **Prism: Set Shortcut** to the app launcher;
- adds a `dofile` of `hypr-binds.lua` to `~/.config/hypr/custom.lua`, which
  starts the ring with the session, blurs the ring's layer, and stops the
  binding from also reaching Nautilus;
- restarts the ring and reloads Hyprland.

It is safe to re-run after an update. Everything runs on `/usr/bin/python3`, so
other Pythons on `PATH` are never picked up. Conversions run at background
priority, and ImageMagick is capped at 768 MiB of RAM. `bubblewrap` is required:
without it, document and video conversions refuse to run rather than run unsandboxed.

## Tests

```sh
/usr/bin/python3 -B -m unittest discover -s tests -v   # regressions (~20 s; PDF tests use the installed .pdf-venv)
TMPDIR=/some/scratch /usr/bin/python3 tests/matrix.py  # every input × every target, outputs decoded for real
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
