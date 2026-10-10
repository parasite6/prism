-- Prism: start the resident ring with the session. It watches the mouse
-- Forward button itself (read-only evdev), so it needs no Hyprland binding to
-- open. Safe on every config reload: ring-start does nothing if it's running.
local here = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$")
-- Frosted petals: blur what's behind the ring. ignore_alpha keeps the screen
-- dim and the ring's shadow (both fainter than 0.4) unblurred.
hl.layer_rule({ match = { namespace = "prism" }, blur = true, ignore_alpha = 0.4 })

-- In Nautilus, Forward opens the ring, so Nautilus must not also get it (it
-- would navigate forward). A no-op bind swallows the button, enabled only while
-- Nautilus is focused; every other app keeps its normal Forward.
local swallow = hl.bind("mouse:276", function() end,
    { description = "Prism: Forward opens the ring in Nautilus" })
local function track()
    local w = hl.get_active_window()
    swallow:set_enabled(w ~= nil and w.class == "org.gnome.Nautilus")
end
track()
hl.on("window.active", track)

os.execute("'" .. here .. "/ring-start' >/dev/null 2>&1 &")
