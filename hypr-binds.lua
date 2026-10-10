-- Prism: start the resident ring with the session. It watches the mouse
-- Forward button itself (read-only evdev), so it needs no Hyprland binding to
-- open. Safe on every config reload: ring-start does nothing if it's running.
local here = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$")
-- Frosted petals: blur what's behind the ring. ignore_alpha keeps the screen
-- dim and the ring's shadow (both fainter than 0.4) unblurred.
hl.layer_rule({ match = { namespace = "prism" }, blur = true, ignore_alpha = 0.4 })

-- In Nautilus, the binding (Forward unless changed with bind.py) opens the
-- ring, so Nautilus must not also get it (Forward would navigate, a key would
-- type). A no-op bind swallows it, enabled only while Nautilus is focused;
-- every other app keeps it as normal.
local function bound()
    local home = os.getenv("XDG_CONFIG_HOME") or ((os.getenv("HOME") or "") .. "/.config")
    local file = io.open(home .. "/prism/binding.json", "r")
    if not file then return "mouse:276" end
    local key = file:read("a"):match('"hypr"%s*:%s*"([^"]+)"')
    file:close()
    return key or "mouse:276"
end
local key = bound()
local swallow = hl.bind(key, function() end, { description = "Prism: " .. key .. " opens the ring in Nautilus" })
    or hl.bind("mouse:276", function() end, { description = "Prism: Forward opens the ring in Nautilus" })
local function track()
    local w = hl.get_active_window()
    swallow:set_enabled(w ~= nil and w.class == "org.gnome.Nautilus")
end
track()
hl.on("window.active", track)

os.execute("'" .. here .. "/ring-start' >/dev/null 2>&1 &")
