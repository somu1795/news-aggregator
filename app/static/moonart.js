// The moon in pixels, and its phase names: shared by the header's icon (retro.js) and the moon page (moon.js).
// Angles in degrees: e is the moon's elongation from the sun (0 new, 180 full), limb the direction of its lit
// edge on screen and face the turn of its face, as sky.js's moonView gives them (retro.js's skyScene rounds them).
(function (root) {
    "use strict";
    var DEGREES = 180 / Math.PI;
    var NAMES = ["new moon", "waxing crescent", "first quarter", "waxing gibbous", "full moon", "waning gibbous", "last quarter", "waning crescent"];
    // The craters of a 24-pixel moon, as [x, y]
    var CRATERS = [[7, 6], [8, 6], [9, 6], [6, 7], [7, 7], [8, 7], [9, 7], [7, 8], [8, 8], [14, 9], [15, 9], [15, 10], [16, 10],
        [15, 11], [9, 15], [10, 15], [11, 15], [10, 16], [16, 17], [17, 17]];

    function phaseName(e) {
        var quarter = Math.round(e / 90);
        if (Math.abs(e - quarter * 90) < 6) return NAMES[(quarter % 4) * 2];  // About half a day either side
        return NAMES[Math.floor(e / 90) * 2 + 1];
    }

    function litPercent(e) { return Math.round((1 - Math.cos(e / DEGREES)) / 2 * 100); }

    // The disc, size x size pixels, as rows of what each pixel shows: null outside it, or "lit", "shade", "crater";
    // in an eclipse (retro.js's scene.eclipse) "blood" and "blood-crater" in Earth's shadow, or the sun's
    // "sun-disc", "eclipsed" and "corona" with the moon over it. options: size, e, limb, face, south, eclipse, craters
    function disc(options) {
        var size = options.size, eclipse = options.eclipse, south = options.south;
        var cos = Math.cos(options.e / DEGREES), limb = options.limb / DEGREES, face = (options.face || 0) / DEGREES;
        var limbX = -Math.sin(limb), limbY = -Math.cos(limb);  // Toward the lit edge, on screen (y down)
        var faceCos = Math.cos(face), faceSin = Math.sin(face);
        var craters = {};
        (options.craters || []).forEach(function (p) { craters[p[0] + "," + p[1]] = true; });
        var half = size / 2, centre = half - 0.5;
        var rows = [];
        for (var y = 0; y < size; y++) {
            var row = [];
            for (var x = 0; x < size; x++) {
                // A pixel is part of the disc if half of it is (4x4 points each), and lit if a third of that is
                var inside = 0, litPoints = 0, covered = 0;
                for (var sy = 0; sy < 4; sy++) {
                    for (var sx = 0; sx < 4; sx++) {
                        var px = (x + (sx + 0.5) / 4 - half) / half, py = (y + (sy + 0.5) / 4 - half) / half;  // The disc's radius is 1
                        if (px * px + py * py > 1) continue;
                        inside++;
                        var along = px * limbX + py * limbY, across = px * limbY - py * limbX;  // Turned to the lit edge
                        var width = Math.sqrt(Math.max(0, 1 - across * across));  // Half the disc's width there
                        // The terminator is half an ellipse: lit beyond it, toward the sun
                        if (along > width * cos) litPoints++;
                        // In an eclipse: inside the covering disc (Earth's shadow, or the moon over the sun)
                        if (eclipse && Math.pow(px - eclipse.at[0], 2) + Math.pow(py - eclipse.at[1], 2) < eclipse.size * eclipse.size) covered++;
                    }
                }
                if (inside < 8) { row.push(null); continue; }
                var kind;
                if (eclipse && eclipse.kind === "solar") {
                    kind = covered * 2 >= inside ? "eclipsed" : "sun-disc";
                    // In totality the corona glows around the moon: the disc's outermost ring of pixels
                    if (eclipse.type === "total" && Math.pow(x - centre, 2) + Math.pow(y - centre, 2) > Math.pow(half - 1.5, 2)) kind = "corona";
                }
                else {
                    // The craters turn with the moon's face (the pixel it shows here, turned back), or mirror
                    var faceX = south ? size - 1 - x : Math.round((x - centre) * faceCos + (y - centre) * faceSin + centre);
                    var faceY = south ? y : Math.round((y - centre) * faceCos - (x - centre) * faceSin + centre);
                    var crater = craters[faceX + "," + faceY];
                    kind = litPoints * 3 < inside ? "shade" : crater ? "crater" : "lit";  // A third: thin crescents stay unbroken
                    if (eclipse && covered * 2 >= inside) kind = crater ? "blood-crater" : "blood";  // In Earth's shadow
                }
                row.push(kind);
            }
            rows.push(row);
        }
        return rows;
    }

    var MoonArt = { disc: disc, phaseName: phaseName, litPercent: litPercent, CRATERS: CRATERS, NAMES: NAMES };
    if (typeof module !== "undefined" && module.exports) module.exports = MoonArt;
    else root.MoonArt = MoonArt;
})(this);
