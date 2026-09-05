-- refresh-button.lua
-- On-screen tappable "Refresh" button for a kiosk RTSP stream, plus
-- keyboard ('r') and volume-button refresh. A refresh reloads the stream
-- in-process (fast, no black flash).

local BTN_W, BTN_H = 260, 96      -- button size in pixels
local MARGIN = 40                  -- gap from the screen edges
local ov = mp.create_osd_overlay("ass-events")
local bx, by = 0, 0                -- computed button top-left corner

local function relayout()
    local d = mp.get_property_native("osd-dimensions")
    if not d or not d.w or d.w == 0 then return false end
    ov.res_x, ov.res_y = d.w, d.h
    bx = d.w - BTN_W - MARGIN
    by = MARGIN
    return true
end

local function redraw()
    if not relayout() then return end
    local box = string.format(
        "{\\pos(%d,%d)\\an7\\bord0\\shad0\\1c&H303030&\\1a&H25&\\p1}m 0 0 l %d 0 l %d %d l 0 %d{\\p0}",
        bx, by, BTN_W, BTN_W, BTN_H, BTN_H)
    -- The \226\159\179 bytes are the U+27F3 refresh glyph. If it shows as a
    -- box on your display, delete them and keep just "REFRESH".
    local label = string.format(
        "{\\an5\\pos(%d,%d)\\fs44\\bord2\\3c&H000000&\\1c&HFFFFFF&}\226\159\179  REFRESH",
        bx + BTN_W / 2, by + BTN_H / 2)
    ov.data = box .. "\n" .. label
    ov:update()
end

local function do_refresh()
    local path = mp.get_property("path")
    if path then
        mp.osd_message("Refreshing...", 1)
        mp.commandv("loadfile", path)   -- reload the stream in-process
    end
end

local function on_tap()
    local m = mp.get_property_native("mouse-pos")
    if m and m.x >= bx and m.x <= bx + BTN_W and m.y >= by and m.y <= by + BTN_H then
        do_refresh()
    end
end

mp.add_forced_key_binding("MBTN_LEFT", "refresh_tap", on_tap)
mp.add_forced_key_binding("r", "refresh_key", do_refresh)          -- keyboard fallback
mp.add_forced_key_binding("VOLUME_UP", "refresh_volup", do_refresh)   -- Surface volume +
mp.add_forced_key_binding("VOLUME_DOWN", "refresh_voldn", do_refresh) -- Surface volume -
mp.observe_property("osd-dimensions", "native", redraw)
mp.register_event("file-loaded", redraw)
