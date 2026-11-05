# Prism

A file converter for Hyprland on Fedora. It can convert, compress, rescale,
crop, rotate, remove backgrounds or strip metadata. Results are saved next to
the original.

- **Nautilus**: select files, hold the mouse **Forward** button, and a ring of
  options opens under the pointer. Release over one to run it.
- **Dolphin**: the same actions as right-click menus.

```sh
curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-nautilus.sh | bash   # Nautilus: the ring
```

```sh
curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-dolphin.sh | bash    # Dolphin: right-click menus
```

## Contents

- [1. Using it](#using-it)
  - [1.1 The ring (Nautilus)](#the-ring-nautilus)
  - [1.2 Right-click menus (Dolphin)](#right-click-menus-dolphin)
  - [1.3 Choosing the shortcut](#choosing-the-shortcut)
  - [1.4 Progress and results](#progress-and-results)
- [2. What it does](#what-it-does)
  - [2.1 Convert](#convert)
  - [2.2 Compress](#compress)
  - [2.3 Rescale](#rescale)
  - [2.4 Edit: crop and rotate](#edit-crop-and-rotate)
  - [2.5 Remove background](#remove-background)
  - [2.6 Clean](#clean)
- [3. Safety](#safety)
  - [3.1 Metadata](#metadata)
  - [3.2 Untrusted files](#untrusted-files)
  - [3.3 Resource limits](#resource-limits)
- [4. Install](#install)
  - [4.1 Requirements](#requirements)
  - [4.2 Running the installer](#running-the-installer)
  - [4.3 What the installers do](#what-the-installers-do)
  - [4.4 The GPU model](#the-gpu-model)
- [5. Project layout](#project-layout)
- [6. Tests](#tests)
- [7. License](#license)

---

## Using it

### The ring (Nautilus)

Select one or more files and hold **Forward**. The ring opens under the pointer
with up to six petals, showing only the ones that fit everything selected.
Release over a petal to run it.

| Petal         | What it does                                                               |
| ------------- | -------------------------------------------------------------------------- |
| **CONVERT**   | Formats common to everything selected ([Convert](#convert))                |
| **COMPRESS**  | Audio and images: LIGHT · MEDIUM · STRONG · HEAVY · MAX                    |
| **RESCALE**   | Images and video: ½ · ⅓ · 2×, offered according to the pixel size          |
| **EDIT**      | One image: CROP or ROTATE, each in the edit window                         |
| **REMOVE BG** | Images: the backdrop made transparent, saved as `name (no background).png` |
| **CLEAN**     | Same file, every bit of metadata removed                                   |

CONVERT, COMPRESS, RESCALE and EDIT each open their own ring, which replaces
the main one when you rest on the petal. Every sub ring has a BACK petal.
REMOVE BG and CLEAN run straight away.

Other ways to open it:

- **Ctrl+Alt+C** in Nautilus, or right-click > **Scripts > Prism**.
- A quick tap leaves the ring open for clicking.

Escape, Forward again, or releasing outside the ring cancels. Outside Nautilus,
Forward stays the ordinary Forward button.

### Right-click menus (Dolphin)

Dolphin doesn't share its selection with other programs, so there is no ring
and Forward stays Dolphin's own. The same actions are right-click submenus:

- **Prism Convert**, **Prism Compress**, **Prism Rescale** and **Prism Edit** (one image)
- **Prism Remove Background** and **Prism Clean**

Dolphin menus nest one level and are picked by file type only. So an action
that doesn't fit the exact selection (PNG to PNG, ½ of a tiny image) says so
in a notification. The menus are generated from `convert.py` by `menu.py`.

### Choosing the shortcut

To hold something other than Forward:

1. Open **Prism: Set Shortcut** from the app launcher, or run `prism-bind`.
2. Press the keys or mouse button.
3. Click OK.

A binding is modifiers (Ctrl, Shift, Alt, Super) plus one key or mouse button,
for example Ctrl + Alt + A or Shift + Back. It is saved in
`~/.config/prism/binding.json` and takes effect immediately.

### Progress and results

While Prism works, a small card in the centre of the screen shows:

- the title and each file as it finishes
- a progress bar, "1 of 3" and the time left

It never takes keyboard focus, and its × hides it while the work goes on. The
result and any failures arrive as notifications. Without Wayland or
gtk4-layer-shell, the progress goes to the notification instead.

For REMOVE BG, the bar follows the real stages, weighted by measured time:
loading the model, the model's passes (hooked inside the network), refining
edges, saving and releasing the GPU. Other converters can't report progress
inside a file, so there the bar moves as each file finishes.

Inputs are never modified, except by the edit window's Replace. Outputs are
numbered rather than overwriting anything, and a failed conversion leaves
nothing behind.

---

## What it does

### Convert

| Input            | Formats in                                           | Converts to                             |
| ---------------- | ---------------------------------------------------- | --------------------------------------- |
| **Images**       | png, jpg, webp, avif, heic, gif, bmp, tiff, ico, svg | png, jpg, webp, avif, heic, gif, ico    |
| **Video**        | mp4, webm, mkv, mov, avi, m4v, flv, wmv              | mp4, webm, mkv, mov, gif, mp3           |
| **Audio**        | mp3, flac, ogg, opus, wav, m4a, aac, wma             | mp3, flac, ogg, opus, wav, m4a          |
| **Documents**    | md, txt, docx, doc, odt, rtf, html, epub             | pdf, docx, odt, md, html, txt, png, jpg |
| **Spreadsheets** | xlsx, ods, xls, csv                                  | xlsx, ods, csv, pdf, png, jpg           |
| **Slides**       | pptx, odp, ppt                                       | pdf, pptx, odp, png, jpg                |
| **PDF**          | pdf                                                  | md, docx, txt, png, jpg                 |

- Multi-page documents converted to images give one image per page, in a folder.
- PDF to Markdown gives a folder with the images, linked relatively.
- A multi-frame source going to a still format keeps the first frame, and the
  notification says so.
- HEIC output appears only if libheif has a HEVC encoder (Fedora's build does not).

### Compress

Compress keeps the dimensions.

- **MEDIUM** is the "profile picture" level: about half the size, with no
  visible change.
- **HEAVY** audio is mono, 16 kHz, 48 kb/s, the usual speech-to-text setting.
- Lossless audio (wav, flac) and wma become mp3.
- If a level wouldn't make the file smaller, nothing is saved and the
  notification says so.

### Rescale

- Images use Lanczos; video uses the GPU (NVENC) when it's available.
- 2× is offered only up to 3000 px for images, and up to 1920 px for video so
  the result stays within 4K.

### Edit: crop and rotate

CROP and ROTATE open the same window. It's an ordinary window, so Hyprland
tiles it. It has no title bar, and **Esc** closes it.

**Crop**

- **Drag** to draw the box, drag inside it to move it, and drag the orange
  handles to resize. A click outside the box selects the whole image again.
- **Ctrl +**, **Ctrl −** and **Ctrl 0**, or **Ctrl+scroll**, zoom the view
  (the crop is not affected). **Scroll** or **middle-drag** pans.

**Rotate**

- The toolbar buttons, or **[** and **]**, turn the image a quarter turn left
  or right.
- **H** and **V** flip it.
- The size shown is what will be written.

**Saving** (both tools)

- **Copy** puts the result on the clipboard (`wl-copy`) and leaves the window open.
- **Save** opens the xdg-desktop-portal save dialog. A different extension
  saves in that format.
- **Replace** overwrites the original after an inline confirmation. It keeps
  the file's permissions and refuses if the file changed on disk since it was
  opened.

Photos are shown and cut upright (EXIF rotation is applied first). Animated GIF
and WebP keep every frame.

### Remove background

REMOVE BG picks one of two methods for each image:

|            | Flat backdrops                                        | Photos                                                                                                             |
| ---------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **For**    | Logos, icons, screenshots: edges digitally one colour | Everything else                                                                                                    |
| **How**    | `cutout.py`, no model                                 | [BiRefNet_HR-matting](https://huggingface.co/ZhengPeng7/BiRefNet_HR-matting) (MIT) at 2048 × 2048 on an NVIDIA GPU |
| **Result** | Exact to the pixel                                    | A true alpha matte: hair, fur and glass stay partly transparent                                                    |
| **Needs**  | Nothing extra                                         | The GPU model ([4.4](#the-gpu-model))                                                                              |

**Flat backdrops:** everything connected to the edges in that colour goes,
along with holes of exactly that colour (the inside of an "O"). The outline is
softened and the backdrop tint is unmixed from it.

**Photos:** edge colours are cleaned with the model's own refinement step
(blur-fusion). The network code is in `birefnet/`; the installers download the
weights. On an RTX 4060 Laptop GPU, the first photo takes ~7.5 s and each
further photo in the same selection ~2.5 s (a batch loads the model once).

When the machine can't run the model, a photo gets a notification saying why
(the installer's verdict, for example too little VRAM) instead of a worse
result. The memory checks are under [Resource limits](#resource-limits).

### Clean

CLEAN saves the same file with every bit of metadata removed. See
[Metadata](#metadata) for what that covers.

---

## Safety

### Metadata

Every output goes through the same scrubber, not just CLEAN. It removes:

- **Images:** EXIF, GPS, XMP, IPTC, comments, embedded thumbnails, encoder tags
- **Office files:** document properties, comment and revision authors
- **PDF:** document info, XMP and annotation authors
- **HTML:** author and generator tags

EXIF rotation and colour profiles are baked into the pixels first, so the
picture doesn't change. If there is no scrubber for a type, or a file can't be
parsed, the conversion fails rather than saving anything unscrubbed.

### Untrusted files

Files to convert often come from the internet. A file can't pull anything else
in, phone home, or take the desktop down with it:

- **Sandboxed tools:** LibreOffice and FFmpeg run in a bubblewrap sandbox with
  no network, no home folder and no input devices. Of your files, they see
  only the one being converted (read-only) and the job's temp folder. A
  document's linked images and a video "manifest" posing as `.mp4` find
  nothing else to read.
- **ImageMagick policy:** `magick/policy.xml` limits it to the formats Prism
  handles. A renamed PostScript, MSL or MVG file can't run Ghostscript, read
  local files or fetch URLs, and no external delegate (Inkscape,
  LibreOffice...) is started.
- **Document resources:** documents may embed only images inside their own
  folder (symlinks resolved), their own media, and `data:` URIs. An image
  pointing to `~/.ssh/...`, `../..` or a URL is dropped, keeping its alt text,
  and so is raw HTML or CSS that could load something (`safe-resources.lua`).
- **Bounded parsing:** compressed parts of documents and images are never
  inflated without a limit. File headers are read off the ring's UI thread,
  with a bounded walk, so a crafted file can't freeze the ring while it holds
  the keyboard.

`bubblewrap` is required: without it, document and video conversions refuse to
run rather than run unsandboxed.

### Resource limits

| What                                            | Limit                                                                                                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Each conversion                                 | Its own worker process, capped at 3 GB of RAM (a systemd scope), background priority                                      |
| ImageMagick                                     | 768 MiB of RAM                                                                                                            |
| REMOVE BG worker                                | 6 GB of RAM, VRAM capped to what was checked; batches take turns on the GPU                                               |
| REMOVE BG, before loading and before each image | Free VRAM covers the step (measured peak 5.0 GB) plus 768 MB for other apps; free RAM covers it plus 3 GB for the desktop |
| REMOVE BG image size                            | Over ~65 MP is refused                                                                                                    |

Image sizes are read from the file header, before decoding. REMOVE BG's edge
refinement runs in bands sized to the VRAM free at that moment, so big photos
fit; banded output is identical to a single pass.

---

## Install

### Requirements

- Fedora with Hyprland 0.56+ (Lua config)
- Nautilus or Dolphin
- The packages below:

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell ImageMagick ffmpeg \
    libheif-tools pandoc libreoffice poppler-utils pngquant oxipng gifsicle wl-clipboard \
    libnotify perl-Image-ExifTool bubblewrap python3-numpy python3-scipy python3-pillow
```

- `ffmpeg` must be the full build (RPM Fusion, with libx264, libvpx, libopus
  and libmp3lame).
- The edit window's Save needs a FileChooser portal backend such as
  `xdg-desktop-portal-gnome` or `-gtk`.
- `exiftool` is optional; it lets CLEAN strip AVIF and HEIC losslessly.
- Forward (or your binding) needs read access to `/dev/input`, for example the
  `input` group. Without it only Ctrl+Alt+C works. The daemon asks the kernel
  for the binding's keys only, so typing and mouse movement never reach it.
- REMOVE BG on photos needs an NVIDIA GPU ([4.4](#the-gpu-model)).

You don't have to check these yourself: the installers do, and print one
`sudo dnf install` command for anything missing.

### Running the installer

Run the installer for your file manager as yourself, not root. Run both if you
use both. Each one fetches Prism on its own:

```sh
curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-nautilus.sh | bash
curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-dolphin.sh | bash
```

To read it first, download it, then run it:

```sh
curl -fsSLO https://github.com/parasite6/prism/releases/latest/download/install-nautilus.sh
bash install-nautilus.sh
```

Or from a clone:

```sh
git clone https://github.com/parasite6/prism && cd prism
./install-nautilus.sh   # Nautilus: the ring
./install-dolphin.sh    # Dolphin: right-click menus
```

Re-run the same installer to update.

- Run on its own, an installer clones Prism into a temporary folder (removed
  afterwards) and runs from there. `PRISM_REF` picks a tag or branch.
- `install.sh` remains for Prism 0.1's one-line installer. It runs the Nautilus
  installer, plus the Dolphin one if Dolphin's menus are already installed.

### What the installers do

**Both installers** (shared code in `install-common.sh`):

1. Check what Prism needs. If anything is missing, stop and print one
   `sudo dnf install` command for all of it.
2. Copy the code to `~/.local/share/prism`, so nothing running ever sees a
   half-edited checkout.
3. The first time, set up the PDF libraries in `.pdf-venv` (about 450 MB from
   PyPI) with `setup-pdf.sh`.
4. Set up REMOVE BG's GPU model with `setup-model.py` ([4.4](#the-gpu-model)).
   What's already in place is checked, not downloaded again.

**`install-nautilus.sh`** then:

1. Links the Nautilus script and adds the Ctrl+Alt+C accelerator.
2. Adds **Prism: Set Shortcut** to the app launcher.
3. Adds a `dofile` of `hypr-binds.lua` to `~/.config/hypr/custom.lua`. It
   starts the ring with the session, blurs the ring's layer, and stops the
   binding from also reaching Nautilus.
4. Restarts the ring and reloads Hyprland.

**`install-dolphin.sh`** then generates Dolphin's right-click menus
(`menu.py --dolphin-menus`, into `~/.local/share/kio/servicemenus`), replacing
any it made before. It changes nothing else: no ring, binding or Hyprland
config. If the Nautilus ring is also installed, it restarts it on the new code.

Everything runs on `/usr/bin/python3`, so other Pythons on `PATH` are never
picked up.

### The GPU model

`setup-model.py` first checks the machine, and stops with the reason if it
can't run the model:

| Needs                                                          | Refused                                |
| -------------------------------------------------------------- | -------------------------------------- |
| NVIDIA GPU, RTX 20 series (Turing) or newer                    | AMD and Intel GPUs, older NVIDIA cards |
| 8 GB of video memory                                           | 4 GB and 6 GB cards                    |
| A driver supporting CUDA 12.6 or later (12.8 for RTX 50 cards) | Older drivers                          |
| 12 GB of RAM                                                   | Less                                   |

If the machine passes, it asks first on a terminal, then sets up:

- **PyTorch:** a working one in `/usr/bin/python3` (torch with CUDA,
  torchvision, kornia, einops, safetensors) is used as it is. Otherwise the
  build matching the driver's CUDA version (cu130, cu128 or cu126) goes into
  `.model-venv` in the install folder, about 3 GB.
- **The weights:** `model.safetensors` (444 MB) from the model's Hugging Face
  page into `~/.local/share/prism-models/ZhengPeng7/`, checked against its
  SHA-256.

The verdict is kept in `~/.local/share/prism-models/status.json`.

- `setup-model.py --check` shows it without installing anything.
- `PRISM_SKIP_MODEL=1` skips the step.

Without the model, flat graphics still work.

---

## Project layout

```
prism/
├── Ring (Nautilus)
│   ├── ring.py ............... the radial menu, resident after first use
│   ├── ring-start ............ starts it with the Hyprland session
│   ├── ring-ctl .............. talks to the ring, starting it if needed
│   ├── prism ................. the Nautilus script (Ctrl+Alt+C)
│   ├── hypr-binds.lua ........ Hyprland rules: autostart, blur, binding
│   ├── bind.py, prism-bind ... Prism: Set Shortcut
│   └── binding.py ............ reads and writes binding.json
├── Menus (Dolphin)
│   └── menu.py ............... runs one action; generates the service menus
├── Converters
│   ├── convert.py ............ targets, conversions and the worker
│   ├── gif_convert.py ........ parallel FFmpeg GIF encoding
│   ├── pdf_convert.py ........ PDF to Markdown, Word and text
│   ├── sizes.py .............. pixel size from file headers
│   ├── crop.py ............... the edit window (crop and rotate)
│   ├── cutout.py ............. flat-backdrop removal
│   ├── matting.py ............ BiRefNet worker for photos
│   └── birefnet/ ............. the network code
├── Safety
│   ├── scrub.py .............. strips metadata from every output
│   ├── strip-meta.lua ........ pandoc: drops document metadata
│   ├── safe-resources.lua .... pandoc: drops outside resources
│   └── magick/policy.xml ..... ImageMagick format and resource policy
├── Progress
│   ├── notices.py ............ batches, notifications
│   └── progress.py ........... the progress card
├── Install
│   ├── install-nautilus.sh
│   ├── install-dolphin.sh
│   ├── install-common.sh ..... shared steps
│   ├── install.sh ............ Prism 0.1 compatibility
│   ├── setup-pdf.sh .......... PDF libraries (.pdf-venv)
│   ├── requirements-pdf.txt
│   └── setup-model.py ........ GPU check, PyTorch and weights
└── tests/
```

---

## Tests

```sh
/usr/bin/python3 -B -m unittest discover -s tests -v   # regressions (~20 s; PDF tests use the installed .pdf-venv)
TMPDIR=/some/scratch /usr/bin/python3 tests/matrix.py  # every input × every target, outputs decoded for real
```

## License

GPL-3.0 See [LICENSE](LICENSE).
