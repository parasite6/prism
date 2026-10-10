-- A document from anywhere must not pull other files, or URLs, into its output
-- (pandoc embeds images for HTML and PDF, and copies them for Markdown).
-- Images may come from the document itself (its media bag, data: URIs) or be
-- files inside its own folder: convert.py checks those, resolving symlinks,
-- and lists them in PRISM_IMAGES. Anything else is dropped, keeping its alt
-- text, and so is raw HTML or styling that could load something.
local allowed = {}
for line in (os.getenv("PRISM_IMAGES") or ""):gmatch("[^\n]+") do
  allowed[line] = true
end

local function inside(src)
  return src:sub(1, 5) == "data:" or allowed[src] or pandoc.mediabag.lookup(src) ~= nil
end


local risky = { "src", "href", "url%s*%(", "@import", "data%s*=", "poster", "xlink",
                "<link", "<iframe", "<object", "<embed", "<script", "<style", "<base" }

local function raw(el)
  if el.format:match("html") then
    local text = el.text:lower()
    for _, pattern in ipairs(risky) do
      if text:find(pattern) then
        return {}
      end
    end
  end
end

RawInline = raw
RawBlock = raw

-- style="background: url(...)" and the like, on any element that has attributes.
local function attributes(el)
  local changed = false
  for key, value in pairs(el.attributes) do
    local k, v = key:lower(), value:lower()
    if v:find("url%s*%(") or v:find("@import") or k == "src" or k == "srcset" or k == "poster"
        or k == "data" or k == "background" then
      el.attributes[key] = nil
      changed = true
    end
  end
  if changed then
    return el
  end
end

function Image(img)
  if inside(img.src) then
    return attributes(img)
  end
  return img.caption
end

for _, kind in ipairs({ "Div", "Span", "Header", "Code", "CodeBlock", "Link", "Table", "Figure" }) do
  _ENV[kind] = attributes
end
