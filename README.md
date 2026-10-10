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
| **CROP** | One image: opens the crop window |
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

## Crop

An ordinary window, so Hyprland tiles it. It has no title bar, and **Esc**
closes it.

- **Drag** to draw the box, drag inside it to move it, and drag the orange
  handles to resize. A click outside the box selects the whole image again.
- **Ctrl +**, **Ctrl −** and **Ctrl 0**, or **Ctrl+scroll**, zoom the view
  (the crop is not affected). **Scroll** or **middle-drag** pans.
- **Copy** puts the crop on the clipboard (`wl-copy`) and leaves the window open.
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
else in:

- ImageMagick is limited, by `magick/policy.xml`, to the formats Prism handles.
  A renamed PostScript, MSL or MVG file can't run Ghostscript, read local files
  or fetch URLs, and no external delegate (Inkscape, LibreOffice...) is started.
- Documents may embed only images inside their own folder (symlinks resolved),
  their own media, and `data:` URIs. An image pointing to `~/.ssh/...`, `../..`
  or a URL is dropped, keeping its alt text, and so is raw HTML or CSS that
  could load something (`safe-resources.lua`).

Inputs are never modified, except by the crop tool's Replace. Outputs are
numbered rather than overwriting anything, and a failed conversion leaves
nothing behind.

## Install

Fedora with Hyprland 0.56+ (Lua config) and Nautilus. Dependencies:

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell ImageMagick ffmpeg \
    libheif-tools pandoc libreoffice poppler-utils pngquant oxipng gifsicle wl-clipboard \
    libnotify perl-Image-ExifTool
```

The `ffmpeg` package must be the full build (RPM Fusion, with libx264, libvpx,
libopus and libmp3lame). A FileChooser portal backend such as
`xdg-desktop-portal-gnome` or `-gtk` is needed for the crop tool's Save. Reading
the Forward button (or your binding) needs read access to `/dev/input` (for
example the `input` group); without it only Ctrl+Alt+C works. The daemon asks
the kernel for the binding's keys only, so typing and mouse movement never
reach it. `exiftool` is optional; it lets CLEAN
strip AVIF and HEIC losslessly.

Copy the folder somewhere permanent, then run:

```sh
cp -r prism ~/.local/share/prism
~/.local/share/prism/setup-pdf.sh   # once: PDF libraries in .pdf-venv
~/.local/share/prism/install.sh
```

`install.sh` does the following:

- links the Nautilus script and adds the Ctrl+Alt+C accelerator;
- adds **Prism: Set Shortcut** to the app launcher;
- adds a `dofile` of `hypr-binds.lua` to `~/.config/hypr/custom.lua`, which
  starts the ring with the session, blurs the ring's layer, and stops Forward
  from also navigating in Nautilus;
- restarts the ring and reloads Hyprland.

It is safe to re-run after an update. Everything runs on `/usr/bin/python3`, so
other Pythons on `PATH` are never picked up. Conversions run at background
priority, and ImageMagick is capped at 768 MiB of RAM.

## Tests

```sh
./setup-pdf.sh                                          # once; the PDF tests need it
/usr/bin/python3 -B -m unittest discover -s tests -v   # regressions (~20 s)
TMPDIR=/some/scratch /usr/bin/python3 tests/matrix.py  # every input × every target, outputs decoded for real
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
