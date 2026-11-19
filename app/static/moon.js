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
    // The pixels' colours: the stylesheet's, so they follow the theme (the light theme's if it didn't load)
    var COLOURS = { lit: "--moon", shade: "--moon-shade", crater: "--moon-crater" };
    var EARTH = { lit: "--visited", shade: "--eclipsed", crater: "--visited" };  // Day and night
    var FALLBACK = { "--moon": "#d9a441", "--moon-shade": "#d3cbb6", "--moon-crater": "#b8862f", "--visited": "#7a6a8c",
        "--eclipsed": "#4a4236", "--sun": "#e8892b", "--muted": "#6f685a", "--accent": "#a8431a" };
    var MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
    var ORBIT_SIZE = 96, ORBIT_RADIUS = 36, EARTH_SIZE = 9, ORBIT_MOON_SIZE = 7;  // Diagram A, in its own pixels
    var DIAGRAM_SCALE_MAX = 3;
    var NOW_SCALE_MAX = 4;  // The big moon: 24 pixels, at most 4 screen pixels each

    function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    var savedTheme = read("retroTheme");
    if (savedTheme === "dark" || savedTheme === "light") document.documentElement.setAttribute("data-theme", savedTheme);

    function colour(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || FALLBACK[name]; }
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function clockTime(time) { var d = new Date(time); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
    function startOfDay(time) { var d = new Date(time); d.setHours(0, 0, 0, 0); return d.getTime(); }

    // A canvas `size` pixels square, shown `scale` times larger, cleared: its drawing context (null if none)
    function surface(canvas, size, scale) {
        if (canvas.width !== size) canvas.width = canvas.height = size;
        canvas.style.width = canvas.style.height = size * scale + "px";
        var context = canvas.getContext("2d");
        if (context) context.clearRect(0, 0, size, size);
        return context;
    }

    // A disc's pixels (MoonArt.disc's rows) with its top left at x, y, in the given colours
    function dots(context, rows, x0, y0, colours) {
        rows.forEach(function (row, y) {
            row.forEach(function (kind, x) {
                if (kind === null) return;
                context.fillStyle = colour(colours[kind] || colours.lit);
                context.fillRect(x0 + x, y0 + y, 1, 1);
            });
        });
    }

    function paint(canvas, rows, scale) {
        var context = surface(canvas, rows.length, scale);
        if (context) dots(context, rows, 0, 0, COLOURS);
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

    // --- The diagrams, on one clock ---
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var clock = createClock(Date.now(), !reduced);
    var orbitEl = document.getElementById("orbit"), phaseEl = document.getElementById("phase");
    var namesEl = document.getElementById("phase-names"), labelEl = document.getElementById("clock-label");
    var playBtn = document.getElementById("play");
    var pending = null, lastFrame = null, lastLabel = -Infinity;

    MoonArt.NAMES.forEach(function (name) {
        var li = document.createElement("li");
        li.textContent = name;
        namesEl.appendChild(li);
    });
    orbitEl.setAttribute("role", "img");
    phaseEl.setAttribute("role", "img");

    function dateLabel(time) {
        var d = new Date(time);
        return d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() + " " + clockTime(time);
    }

    // How far the clock is from now: "now" within a minute, else "+5 d 14 h", "-3 h", "+20 min"
    function offsetLabel(ms) {
        var minutes = Math.round(Math.abs(ms) / 60000);
        if (minutes < 1) return "now";
        var days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), sign = ms < 0 ? "-" : "+";
        if (days) return sign + days + " d" + (hours ? " " + hours + " h" : "");
        if (hours) return sign + hours + " h";
        return sign + minutes + " min";
    }

    // Diagram A: Earth in the middle, sunlight from the left, the moon on its orbit at its elongation (anticlockwise,
    // seen from above the north pole), its lit half toward the sun; a mark where the moon is now
    function drawOrbit(time, scale) {
        var context = surface(orbitEl, ORBIT_SIZE, scale);
        if (!context) return;
        var centre = ORBIT_SIZE / 2;
        context.fillStyle = colour("--sun");
        for (var y = 8; y < ORBIT_SIZE; y += 12) context.fillRect(1, y, 4, 1);  // Sunlight, from the left
        context.fillStyle = colour("--muted");
        for (var a = 0; a < 360; a += 10) {
            context.fillRect(Math.round(centre + ORBIT_RADIUS * Math.cos(a / DEGREES)), Math.round(centre + ORBIT_RADIUS * Math.sin(a / DEGREES)), 1, 1);
        }
        dots(context, MoonArt.disc({ size: EARTH_SIZE, e: 90, limb: 90 }), Math.round(centre - EARTH_SIZE / 2), Math.round(centre - EARTH_SIZE / 2), EARTH);
        // Toward the sun (left) is 180 degrees; the moon goes round anticlockwise, which on screen (y down) is -e
        var at = function (time, radius) {
            var angle = (180 + Sky.elongation(time)) / DEGREES;
            return [centre + radius * Math.cos(angle), centre - radius * Math.sin(angle)];
        };
        var mark = at(Date.now(), ORBIT_RADIUS + 5);
        context.fillStyle = colour("--accent");
        context.fillRect(Math.round(mark[0]) - 1, Math.round(mark[1]) - 1, 2, 2);
        var moon = at(time, ORBIT_RADIUS);
        dots(context, MoonArt.disc({ size: ORBIT_MOON_SIZE, e: 90, limb: 90 }), Math.round(moon[0] - ORBIT_MOON_SIZE / 2),
            Math.round(moon[1] - ORBIT_MOON_SIZE / 2), COLOURS);
    }

    // Diagram B: the disc as seen from Earth, the textbook way up (lit on the right while waxing, mirrored in the south)
    function drawPhase(time, e, scale) {
        paint(phaseEl, MoonArt.disc({ size: 24, e: e, limb: e < 180 !== MOON.south ? -90 : 90, south: MOON.south }), scale);
        var name = MoonArt.phaseName(e);
        [].forEach.call(namesEl.children, function (li) {
            var current = li.textContent === name;
            li.classList.toggle("current", current);
            if (current) li.setAttribute("aria-current", "true"); else li.removeAttribute("aria-current");
        });
    }

    function drawDiagrams(realTime) {
        var time = clock.time(), e = Sky.elongation(time);
        var width = (document.querySelector(".moon-diagrams figure") || document.body).clientWidth;
        var scale = width ? Math.min(DIAGRAM_SCALE_MAX, scaleFor(width, ORBIT_SIZE)) : 2;
        drawOrbit(time, scale);
        drawPhase(time, e, Math.min(DIAGRAM_SCALE_MAX * 4, scale * 4));  // The same size on screen as Earth's orbit, about
        var offset = offsetLabel(time - Date.now());
        labelEl.textContent = dateLabel(time) + " · " + offset;
        // What the diagrams show, for screen readers: at most once a second while playing
        if (realTime === undefined || realTime - lastLabel >= 1000) {
            lastLabel = realTime === undefined ? lastLabel : realTime;
            var name = MoonArt.phaseName(e), lit = MoonArt.litPercent(e) + "% lit";
            orbitEl.setAttribute("aria-label", "orbit: moon at " + name + ", " + lit + ", " + offset);
            phaseEl.setAttribute("aria-label", "phase: " + name + ", " + lit + ", " + offset);
        }
    }

    function button(el, key, text) {
        el.textContent = "";
        var k = document.createElement("span");
        k.className = "key";
        k.textContent = key;
        el.appendChild(document.createTextNode("["));
        el.appendChild(k);
        el.appendChild(document.createTextNode("] " + text));
    }

    function frame(timestamp) {
        pending = null;
        if (lastFrame !== null) clock.advance(timestamp - lastFrame);
        lastFrame = timestamp;
        drawDiagrams(timestamp);
        schedule();
    }

    function schedule() {
        if (pending === null && clock.playing && !document.hidden) pending = window.requestAnimationFrame(frame);
    }

    function stop() {
        if (pending !== null) window.cancelAnimationFrame(pending);
        pending = null;
        lastFrame = null;
    }

    // After a control: show the change now, and run or stop the animation to match
    function changed() {
        button(playBtn, "space", clock.playing ? "pause" : "play");
        stop();
        drawDiagrams();
        schedule();
    }

    var actions = {
        " ": function () { clock.toggle(); },
        ArrowLeft: function () { clock.step(-1); },
        ArrowRight: function () { clock.step(1); },
        n: function () { clock.reset(Date.now()); }
    };
    [["play", " "], ["step-back", "ArrowLeft"], ["step-forward", "ArrowRight"], ["now-button", "n"]].forEach(function (pair) {
        document.getElementById(pair[0]).addEventListener("click", function () { actions[pair[1]](); changed(); });
    });
    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey || !actions[event.key]) return;
        if (event.target && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName) && event.key === " ") return;  // A focused button's own space
        event.preventDefault();  // Space and the arrows would scroll the page
        actions[event.key]();
        changed();
    });
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) stop(); else schedule();
    });
    changed();
})(this);
