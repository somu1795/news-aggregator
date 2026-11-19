// Earth's face for the moon page (moon.js): the hemisphere centred on a place, its land and sea, and day and
// night for a moment. Land comes from the stats page's map (caddy/static/stats/map.json, from Natural Earth):
// 180 x 80 cells from 83 degrees north to 58 south, one bit each, packed as hex. Angles in degrees unless named.
(function (root) {
    "use strict";
    var Sky = root.Sky || (typeof require === "function" ? require("./sky.js") : null);
    var DEG = Math.PI / 180, DAY = 86400000;
    var LAND = "000000000001bfe77fe000000000000000000000000000000000000387effffffc00004004b0000e000000000000000000234ffbffffff0001f0000000000e000000000000000030027e1fffffe800020000010000f00000000000000000b8afc007ffff00000000030003ffe001980000000000f85b3b003fffc0000000004043ffef8c0400008000000dfca6fc02fff6000000000c0dffffffe9f8000007fe0b9ff314f01fffc00001f80009dffffffffffc3cc1ffffff0c7d93c1ffc00000bffc9fffffffffffffffff97ffffffffe8761ff0020007fe5fffffffffffffffff117ffffffffd9f80fc01e001fbe7ffffffffffffffffe00fffffffff0810078000207e7ffffffffffffffffffd01fd7fffffe01e0038000007efffffffffffffffff2f0007807ffffe01e4000000007e3ffffffffffffff83300001401fffff00fe000000180c3ffffffffffffff00e00004001fffffe1ff00000008387fffffffffffffc00e000200017fffffbff000000741fffffffffffffffd00c000000003fffff9ffc000004efffffffffffffffffc08000000003ffffffff40000011fffffffffffffffff4000000000007fffffece000000ffffffffffffffffff000000000000ffffffdc50000007fffffe7fffffffffe400000000000fffffff200000007f7f9fcffffffffffc000000000000ffffffe00000003f5bf03cffffffffff8600000000000ffffffe00000003e2df31c7ffffffffe0000000000000ffffff800000007d209ffeffffffffdc08000000000007fffff000000003c0c5ffc7fffffff8400000000000007fffff0000000013e442ffffffffffc630000000000003ffffe000000001fe003ffffffffffc080000000000001ffffc040000047ff801ffffffffffc100000000000000ffff8000000007ffef7ffffffffffe0000000000000003fc8c000000007ffffffdffffffffe0000000000000005f80600000001fffffefeffffffffc0000000000000001f80400000001fffffeff9fffffffc0000000000000000f80000000003ffffff7fe0ffffffa0000000000000000780620000003ffffff3ff07fefff000000000000000007c6186000003ffffffbfe03f8fe0000000000000000003cc57f000003ffffff9fc03f07e8000000400000000000fc00f000043ffffffdf801e05f02000000000000000000f003800003ffffffdf001c01f020000000000000000007013800003ffffffe8001c09f820030000000000000001019000003fffffff1000c0138080000000000000000018ff000001fffffff2000c0120100000000000000000005ff000000fffffffa00020100184000000000000000001ff8000007efffffe00020080880004100000000000001fff00000007ffffc000002c1800000000000000000001fff80000001ffff800200143800000000000000000003fff80000005ffff0000001cfa00000080000000000083fff40000003fffe0000000c7908000400000000000003ffff8000001fffc000000067203000000000000000007ffffe000000fffc040000060007c20000000000000007fffff000000fff8000000000001fd0000000000000003fffff8020007ffc000200006000f00010800080000001fffff0000007ffc000000000480082000000000000001ffffe0000007ffc000000220000000001000000000000ffffe000000fffce00000000100000008800000000000ffffc000000fffc2000000000000000000000000000003fffc000000fffce000000000000000104000000000001fffc000000fff0e000000000000002000000000000001fffc0000007fe0c600000000000000020200000000001fffc0000007ff0c000000000000008000000040000001ffe00000007ff0c000000000000000000000000000001ffc00000007fe08000000000000000000000000000001ffc00000003fc00000000000000000000000000000003ff800000003fc00000000000000004000000000000003ff000000001f800000000000000000000000000000003ff000000001f000000000000000000000000000000003fe0000000018000000000000000000000000000000003f80000000000000000000000000000000000000000007f80000000000000000000000000000400000000000007e00000000000000000000000000000400000000000007800000000000000000000000000000c00000000000003c0000000000000000000000000000180000000000000780000000000000000000000000000300000000000000f00000000000000000000000000000200000000000000f80000000000000000400000000000000000000000000700000000000000000000000000000000000000000000f1800000000000000000000000000000000000000000070001000010000000020000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000";
    var LAND_W = 180, LAND_H = 80, LAND_N = 83, LAND_S = -58;
    var ICE_N = 80, ICE_S = -64;  // Sea ice and Antarctica, off the map's grid
    var BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];

    // 1 for land, 0 for sea, null north or south of the map
    function isLand(lat, lon) {
        if (lat > LAND_N || lat < LAND_S) return null;
        var x = Math.floor(((lon + 180) % 360 + 360) % 360 / 360 * LAND_W);
        var y = Math.min(LAND_H - 1, Math.floor((LAND_N - lat) / (LAND_N - LAND_S) * LAND_H));
        var i = y * LAND_W + x;
        return (parseInt(LAND.charAt(i >> 2), 16) >> (3 - (i & 3))) & 1;
    }

    // Where the sun is overhead: its declination, and its right ascension less Greenwich sidereal time
    function subsolar(time) {
        var sun = Sky.sunAndMoon(time).sun;
        var gmst = 280.46061837 + 360.98564736629 * (time / DAY - 10957.5);
        return [sun.dec / DEG, ((sun.ra / DEG - gmst) % 360 + 540) % 360 - 180];
    }

    // The place a cell of the face shows: [lat, lon], or null outside the disc. options: size (pixels), lat and lon
    // (the centre), rot (radians clockwise from up: which way north points)
    function cellLatLon(options, x, y) {
        var r = options.size / 2, dx = (x + 0.5 - r) / r, dy = (y + 0.5 - r) / r, rho = Math.hypot(dx, dy);
        if (rho > 1) return null;
        var cr = Math.cos(options.rot), sr = Math.sin(options.rot);
        var east = dx * cr + dy * sr, north = dx * sr - dy * cr;  // Turned so north is up
        var lat0 = options.lat * DEG, lon0 = options.lon * DEG;
        if (!rho) return [options.lat, options.lon];
        var c = Math.asin(Math.min(1, rho)), sc = Math.sin(c), cc = Math.cos(c);  // The orthographic projection, inverted
        var lat = Math.asin(cc * Math.sin(lat0) + north * sc * Math.cos(lat0) / rho);
        var lon = lon0 + Math.atan2(east * sc, rho * cc * Math.cos(lat0) - north * sc * Math.sin(lat0));
        return [lat / DEG, ((lon / DEG + 540) % 360) - 180];
    }

    // The face as rows of what each pixel shows: null outside the disc, "land", "ocean", "ice" by day, or
    // "land-night", "ocean-night" (ice at night counts as land); the line between day and night dithered
    function face(options) {
        var sub = subsolar(options.time), slat = sub[0] * DEG, slon = sub[1] * DEG, rows = [];
        for (var y = 0; y < options.size; y++) {
            var row = [];
            for (var x = 0; x < options.size; x++) {
                var at = cellLatLon(options, x, y);
                if (!at) { row.push(null); continue; }
                var lat = at[0] * DEG, lon = at[1] * DEG, ice = at[0] > ICE_N || at[0] < ICE_S, land = isLand(at[0], at[1]) === 1;
                var sunUp = Math.sin(lat) * Math.sin(slat) + Math.cos(lat) * Math.cos(slat) * Math.cos(lon - slon);
                var day = Math.max(0, Math.min(1, sunUp * 4 + 0.5)) > (BAYER[(y & 3) * 4 + (x & 3)] + 0.5) / 16;
                row.push(day ? (ice ? "ice" : land ? "land" : "ocean") : (ice || land ? "land-night" : "ocean-night"));
            }
            rows.push(row);
        }
        return rows;
    }

    var Earth = { isLand: isLand, subsolar: subsolar, cellLatLon: cellLatLon, face: face };
    if (typeof module !== "undefined" && module.exports) module.exports = Earth;
    else root.Earth = Earth;
})(this);
