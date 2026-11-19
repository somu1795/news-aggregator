// The moon page (templates/moon.html): the moon now, its orbit around Earth and its phase as moving diagrams on
// one clock, and the next 30 days. The astronomy is sky.js's, the pixel moon moonart.js's. The helpers that touch
// no page are exported for Node, like sky.js's.
(function (root) {
    "use strict";
    var Sky = root.Sky || (typeof require === "function" ? require("./sky.js") : null);
    var DAY = 86400000, SYNODIC_DAYS = 29.530589, AVERAGE_KM = 384400;
    var PRINCIPAL = [[0, "new moon"], [90, "first quarter"], [180, "full moon"], [270, "last quarter"]];
    var COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

    // The diagrams' clock: starts at `start` (ms), and while playing runs a day for each real second
    function createClock(start, playing) {
        var time = start;
        var clock = {
            playing: Boolean(playing),
            time: function () { return time; },
            play: function () { clock.playing = true; },
            pause: function () { clock.playing = false; },
            toggle: function () { clock.playing = !clock.playing; },
            step: function (days) { time += days * DAY; clock.playing = false; },
            reset: function (now) { time = now; },
            advance: function (realMs) { if (clock.playing) time += realMs * 86400; }
        };
        return clock;
    }

    // How far the elongation is past `target` degrees, from -180 to 180
    function past(time, target) { return ((Sky.elongation(time) - target) % 360 + 540) % 360 - 180; }

    // The next new moon, first quarter, full moon and last quarter after `time`, in time order: [{ name, time }].
    // Hourly steps until the elongation crosses each, then halving to within a minute
    function nextPhases(time) {
        return PRINCIPAL.map(function (phase) {
            for (var t = time; t < time + 32 * DAY; t += 3600000) {
                if (past(t, phase[0]) < 0 && past(t + 3600000, phase[0]) >= 0) {
                    var low = t, high = t + 3600000;
                    while (high - low > 60000) {
                        var mid = (low + high) / 2;
                        if (past(mid, phase[0]) < 0) low = mid; else high = mid;
                    }
                    return { name: phase[1], time: high };
                }
            }
            return null;
        }).filter(Boolean).sort(function (a, b) { return a.time - b.time; });
    }

    function roundTo10(time) { return Math.round(time / 600000) * 600000; }

    // Days since the new moon, from how far round the moon is (within half a day or so)
    function age(time) { return Sky.elongation(time) / 360 * SYNODIC_DAYS; }

    function distanceLine(km) {
        var whole = String(Math.round(km)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        return whole + " km · " + (km < AVERAGE_KM ? "nearer" : "farther") + " than average";
    }

    // Today's rise and set (either may be missing), or, with neither, whether it stays up or down
    function riseSetLine(times, upNow, formatTime) {
        var parts = [];
        if (times.rise) parts.push("rises " + formatTime(times.rise));
        if (times.set) parts.push("sets " + formatTime(times.set));
        return parts.length ? parts.join(" · ") : upNow ? "up all day" : "down all day";
    }

    function compass(az) { return COMPASS[Math.round(az / 45) % 8]; }

    // The largest whole-number scale at which `pixels` fit in `containerPx`, never below 1
    function scaleFor(containerPx, pixels) { return Math.max(1, Math.floor(containerPx / pixels)); }

    var MoonPage = { createClock: createClock, nextPhases: nextPhases, roundTo10: roundTo10, age: age, distanceLine: distanceLine,
        riseSetLine: riseSetLine, compass: compass, scaleFor: scaleFor };
    if (typeof module !== "undefined" && module.exports) module.exports = MoonPage;
    else root.MoonPage = MoonPage;
})(this);
