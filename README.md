# Prism

A file converter for Hyprland: convert, compress, rescale, crop, rotate, remove
backgrounds or strip metadata. It works with either file manager, and each has
its own installer:

- **Nautilus** (`install-nautilus.sh`): select files, hold the mouse **Forward**
  button, and a ring of options opens under the pointer. Release over one to run it.
- **Dolphin** (`install-dolphin.sh`): the same actions as right-click menus.

The result is saved next to the original. While it works, a small
card in the centre of the screen shows the progress: the title, each file as it
finishes, a bar, "1 of 3" and the time left. It never takes keyboard focus, and
its × hides it while the work goes on. For REMOVE BG the bar follows the real
stages, weighted by measured time: loading the model, the model's passes (hooked
inside the network), refining edges, saving and releasing the GPU. The other
converters can't report progress inside a file, so there the bar moves as each
file finishes. The result and any failures
arrive as notifications. Without Wayland or gtk4-layer-shell, the progress goes
to the notification instead.

The main ring has up to six petals. CONVERT, COMPRESS and RESCALE each open
their own ring, which replaces the main one when you rest on the petal. Every
sub ring has a BACK petal.

| Petal | What it does |
|---|---|
| **CONVERT** | Formats common to everything selected (see below) |
| **COMPRESS** | Audio and images: LIGHT · MEDIUM · STRONG · HEAVY · MAX |
| **RESCALE** | Images and video: ½ · ⅓ · 2×, offered according to the pixel size |
| **EDIT** | One image: CROP or ROTATE, each in the edit window |
| **REMOVE BG** | Images: a plain backdrop made transparent, saved as `name (no background).png` |
| **CLEAN** | Same file, every bit of metadata removed |

REMOVE BG has two methods and picks one for each image:

- **Flat backdrops** (logos, icons, screenshots: edges digitally one colour):
  `cutout.py`, no model, exact to the pixel. Everything connected to the edges
  in that colour goes, along with holes of exactly that colour (the inside of an
  "O"). The outline is softened and the backdrop tint is unmixed from it.
- **Everything else** (photos): [BiRefNet_HR-matting](https://huggingface.co/ZhengPeng7/BiRefNet_HR-matting)
  (MIT) on an NVIDIA GPU, at its full 2048 × 2048 resolution. It gives a true
  alpha matte, so hair, fur and glass stay partly transparent. Edge colours are
  then cleaned with the model's own refinement step (blur-fusion). The network
  code is in `birefnet/`. The weights aren't included: put `model.safetensors`
  from that page in `~/.local/share/prism-models/ZhengPeng7/`, and Prism checks
  its SHA-256 before using it.

On an RTX 4060 Laptop GPU, the first photo takes ~7.5 s, and each further photo
in the same selection ~2.5 s (a batch loads the model once).

It refuses rather than overload the machine. Before the model loads, and before
each image (sized from the file header, before decoding):
- Free VRAM must cover the step (measured peak 5.0 GB) plus 768 MB left for
  other apps.
- Free RAM must cover the step plus 3 GB left for the desktop.

The worker is also hard-capped (6 GB RAM in a systemd scope, VRAM capped to what
was checked) and runs at background priority. Batches take turns on the GPU.
Edge refinement runs in bands sized to the VRAM free at that moment, so big
photos fit; banded output is identical to a single pass. Images over ~65 MP are
refused. With no GPU or no weights, a photo gets a notification instead of a
worse result.

To hold something else instead of Forward, open **Prism: Set Shortcut** from
the app launcher (or run `prism-bind`), press the keys or mouse button, and
click OK. A binding is modifiers (Ctrl, Shift, Alt, Super) plus one key or
mouse button, e.g. Ctrl + Alt + A or Shift + Back. It is saved in
`~/.config/prism/binding.json` and takes effect immediately.

**Dolphin** has no ring (it doesn't share its selection, so Forward stays Dolphin's
own). The same actions are right-click submenus instead: **Prism Convert**, **Prism
Compress**, **Prism Rescale** and **Prism Edit** (one image), plus the items **Prism
Remove Background** and **Prism Clean**. Dolphin
menus nest one level, and they're picked by file type only, so an action that doesn't
apply to the exact selection (PNG to PNG, ½ of a tiny image) just says so in a
notification. They're generated from `convert.py` by `menu.py`.

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

Fedora with Hyprland 0.56+ (Lua config) and Nautilus or Dolphin. Dependencies:

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell ImageMagick ffmpeg \
    libheif-tools pandoc libreoffice poppler-utils pngquant oxipng gifsicle wl-clipboard \
    libnotify perl-Image-ExifTool bubblewrap python3-numpy python3-scipy python3-pillow
```

For REMOVE BG on photos, also: the NVIDIA driver, plus PyTorch with CUDA,
torchvision, kornia, einops and safetensors (`pip install --user torch
torchvision kornia einops safetensors`), and the weights described above.
Without them, flat graphics still work.

The `ffmpeg` package must be the full build (RPM Fusion, with libx264, libvpx,
libopus and libmp3lame). A FileChooser portal backend such as
`xdg-desktop-portal-gnome` or `-gtk` is needed for the edit window's Save.
`exiftool` is optional; it lets CLEAN strip AVIF and HEIC losslessly.

Then clone the repo and run the installer for your file manager. You can run
both if you use both.

```sh
git clone https://github.com/parasite6/prism && cd prism
./install-nautilus.sh   # Nautilus: the ring
./install-dolphin.sh    # Dolphin: right-click menus
```

Re-run the same installer after pulling an update. Both first:

- copy the code to `~/.local/share/prism`, so nothing running ever sees a
  half-edited checkout (`install-common.sh`);
- the first time, set up the PDF libraries in `.pdf-venv` (about 450 MB from
  PyPI) with `setup-pdf.sh`.

`install-nautilus.sh` then:

- links the Nautilus script and adds the Ctrl+Alt+C accelerator;
- adds **Prism: Set Shortcut** to the app launcher;
- adds a `dofile` of `hypr-binds.lua` to `~/.config/hypr/custom.lua`, which
  starts the ring with the session, blurs the ring's layer, and stops the
  binding from also reaching Nautilus;
- restarts the ring and reloads Hyprland.

Reading the Forward button (or your binding) needs read access to `/dev/input`
(for example the `input` group); without it only Ctrl+Alt+C works. The daemon
asks the kernel for the binding's keys only, so typing and mouse movement never
reach it.

`install-dolphin.sh` then generates Dolphin's right-click menus
(`menu.py --dolphin-menus`, into `~/.local/share/kio/servicemenus`), replacing
any it made before. It changes nothing else: no ring, binding or Hyprland
config. If the Nautilus ring is also installed, it restarts it on the new code.

Both are safe to re-run after an update. Everything runs on `/usr/bin/python3`, so
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
