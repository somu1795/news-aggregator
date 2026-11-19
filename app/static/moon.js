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

    // When the elongation next reaches `target` degrees after `time` (step 1 h forward), or last reached it before
    // (step -1 h): hourly steps until it crosses, then halving to within a minute
    function crossing(time, target, step) {
        for (var t = time; Math.abs(t - time) < 32 * DAY; t += step) {
            var a = Math.min(t, t + step), b = Math.max(t, t + step);
            if (past(a, target) < 0 && past(b, target) >= 0) {
                while (b - a > 60000) {
                    var mid = (a + b) / 2;
                    if (past(mid, target) < 0) a = mid; else b = mid;
                }
                return b;
            }
        }
        return null;
    }

    // The next new moon, first quarter, full moon and last quarter after `time`, in time order: [{ name, time }]
    function nextPhases(time) {
        return PRINCIPAL.map(function (phase) {
            var at = crossing(time, phase[0], 3600000);
            return at === null ? null : { name: phase[1], time: at };
        }).filter(Boolean).sort(function (a, b) { return a.time - b.time; });
    }

    function roundTo10(time) { return Math.round(time / 600000) * 600000; }

    // Days since the last new moon
    function age(time) { return (time - crossing(time, 0, -3600000)) / DAY; }

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

    // --- The page ---
    if (typeof document === "undefined" || !document.body || !document.body.classList.contains("moon-page")) return;
    var MoonArt = root.MoonArt;
    var DEGREES = 180 / Math.PI;
    var MOON = JSON.parse(document.getElementById("moon-data").textContent);
    // The pixels' colours: the stylesheet's, so they follow the theme
    var COLOURS = { lit: "--moon", shade: "--moon-shade", crater: "--moon-crater" };
    var NOW_SCALE_MAX = 4;  // The big moon: 24 pixels, at most 4 screen pixels each

    function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    var savedTheme = read("retroTheme");
    if (savedTheme === "dark" || savedTheme === "light") document.documentElement.setAttribute("data-theme", savedTheme);

    function colour(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888"; }
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function clockTime(time) { var d = new Date(time); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
    function startOfDay(time) { var d = new Date(time); d.setHours(0, 0, 0, 0); return d.getTime(); }

    // A disc of pixels (MoonArt.disc's rows) painted on a canvas, one canvas pixel each, shown `scale` times larger
    function paint(canvas, rows, scale) {
        canvas.width = rows.length;
        canvas.height = rows.length;
        canvas.style.width = canvas.style.height = rows.length * scale + "px";
        var context = canvas.getContext("2d");
        if (!context) return;
        context.clearRect(0, 0, rows.length, rows.length);
        rows.forEach(function (row, y) {
            row.forEach(function (kind, x) {
                if (kind === null) return;
                context.fillStyle = colour(COLOURS[kind] || COLOURS.lit);
                context.fillRect(x, y, 1, 1);
            });
        });
    }

    // How the moon's lit edge points (degrees): as seen from the visitor's place, or the textbook way
    // (lit on the right while waxing, mirrored in the south) without it
    function orientation(time, e) {
        if (MOON.position) {
            var view = Sky.moonView(time, MOON.position);
            return { limb: view.limb * DEGREES, face: view.face * DEGREES, south: false };
        }
        return { limb: e < 180 !== MOON.south ? -90 : 90, face: 0, south: MOON.south };
    }

    function line(parent, text) {
        var p = document.createElement("p");
        p.textContent = text;
        parent.appendChild(p);
    }

    function renderNow() {
        var now = Date.now(), e = Sky.elongation(now), way = orientation(now, e);
        var width = document.querySelector(".moon-now").clientWidth || 96;
        paint(document.getElementById("now-moon"), MoonArt.disc({ size: 24, e: e, limb: way.limb, face: way.face, south: way.south,
            craters: MoonArt.CRATERS }), Math.min(NOW_SCALE_MAX, scaleFor(width, 24)));
        var text = document.getElementById("now-text");
        text.textContent = "";
        line(text, MoonArt.phaseName(e) + " · " + MoonArt.litPercent(e) + "% lit");
        line(text, age(now).toFixed(1) + " days since the new moon (of " + SYNODIC_DAYS.toFixed(1) + ")");
        line(text, distanceLine(Sky.moonDistance(now)));
        if (MOON.position) {
            var up = Sky.isUp("moon", now, MOON.position);
            line(text, riseSetLine(Sky.riseSet("moon", startOfDay(now), MOON.position), up, clockTime));
            var seen = Sky.horizontal(Sky.sunAndMoon(now).moon, now, MOON.position);
            line(text, up ? "now " + Math.round(seen.alt) + "° up, in the " + compass(seen.az) : "below the horizon now");
        }
    }

    if (!Sky || !MoonArt) {
        var notice = document.getElementById("moon-notice");
        notice.textContent = "the sky's script didn't load: reload the page to try again";
        notice.hidden = false;
        return;
    }
    renderNow();
})(this);
