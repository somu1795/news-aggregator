// The moon page (templates/moon.html): the moon now, its orbit around Earth and its phase as moving diagrams on
// one clock, and the next 30 days. The astronomy is sky.js's, the pixel moon moonart.js's. The helpers that touch
// no page are exported for Node, like sky.js's.
(function (root) {
    "use strict";
    var Sky = root.Sky || (typeof require === "function" ? require("./sky.js") : null);
    var DAY = 86400000, SYNODIC_DAYS = 29.530589, AVERAGE_KM = 384400;
    var PRINCIPAL = [[0, "new moon"], [90, "first quarter"], [180, "full moon"], [270, "last quarter"]];
    var COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

    // The diagrams' clock: starts at `start` (ms), and while playing runs `speed` ms for each real one (a day a
    // second unless changed)
    function createClock(start, playing) {
        var time = start;
        var clock = {
            playing: Boolean(playing),
            speed: 86400,
            time: function () { return time; },
            play: function () { clock.playing = true; },
            pause: function () { clock.playing = false; },
            toggle: function () { clock.playing = !clock.playing; },
            step: function (days) { time += days * DAY; clock.playing = false; },
            reset: function (now) { time = now; },
            advance: function (realMs) { if (clock.playing) time += realMs * clock.speed; }
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

    // Without the visitor's position: a place guessed from the time zone (Date's offset, minutes west of UTC) and
    // the hemisphere, as [latitude, longitude]
    function guessPlace(offsetMinutes, south) {
        return [south ? -30 : 30, Math.max(-180, Math.min(180, -offsetMinutes / 4)) + 0];  // + 0: never -0
    }

    var MoonPage = { createClock: createClock, nextPhases: nextPhases, roundTo10: roundTo10, age: age, distanceLine: distanceLine,
        riseSetLine: riseSetLine, compass: compass, scaleFor: scaleFor, guessPlace: guessPlace };
    if (typeof module !== "undefined" && module.exports) module.exports = MoonPage;
    else root.MoonPage = MoonPage;

    // --- The page ---
    if (typeof document === "undefined" || !document.body || !document.body.classList.contains("moon-page")) return;
    var MoonArt = root.MoonArt, Earth = root.Earth;
    var DEGREES = 180 / Math.PI;
    var MOON = JSON.parse(document.getElementById("moon-data").textContent);
    // The pixels' colours: the stylesheet's, so they follow the theme (the light theme's if it didn't load)
    var COLOURS = { lit: "--moon", shade: "--moon-shade", crater: "--moon-crater" };
    var EARTH = { land: "--earth-land", ocean: "--earth-ocean", ice: "--earth-ice", "land-night": "--earth-land-night",
        "ocean-night": "--earth-night" };
    var FALLBACK = { "--moon": "#d9a441", "--moon-shade": "#d3cbb6", "--moon-crater": "#b8862f", "--sun": "#e8892b",
        "--muted": "#6f685a", "--accent": "#a8431a", "--earth-land": "#8c8a4c", "--earth-ocean": "#5d7088", "--earth-ice": "#ece7da",
        "--earth-land-night": "#55513a", "--earth-night": "#3b3a40" };
    var WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
    var DAY_SIZE = 12, DAY_SCALE = 2, DAYS = 30;  // The strip's small moons: 12 pixels, each 2 screen pixels
    var MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
    // Diagram A, the orbit in 3D: its pixels, pixels per Earth radius, the orbit's and the moon's radius (Earth's is 1),
    // Earth's tilt, the orbit's, and how strong the perspective is
    var ORBIT_W = 480, ORBIT_H = 300, ORBIT_K = 52, ORBIT_R = 3.4, MOON_R = 0.42, PERSPECTIVE = 14;
    var TILT = 23.44 / (180 / Math.PI), INCL = 5.14 / (180 / Math.PI);
    var ORBIT_SCALE_MAX = 2, PHASE_SCALE_MAX = 6;
    var SPEEDS = [3600, 8 * 3600, 86400];  // An hour, 8 hours or a day for each second (keys 1, 2, 3)
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

    function dayLabel(time) { var d = new Date(time); return WEEKDAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()]; }

    // How the moon's lit edge points (degrees): as seen from the visitor's place, or the textbook way
    // (lit on the right while waxing, mirrored in the south) without it
    function orientation(time, e) {
        if (MOON.position) {
            var seen = Sky.moonView(time, MOON.position);
            return { limb: seen.limb * DEGREES, face: seen.face * DEGREES, south: false };
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

    if (!Sky || !MoonArt || !Earth) {
        var notice = document.getElementById("moon-notice");
        notice.textContent = "the sky's script didn't load: reload the page to try again";
        notice.hidden = false;
        return;
    }
    renderNow();

    // The next 30 days from today, each moon as it is at local noon, the textbook way up
    function renderDays() {
        var list = document.getElementById("days"), noon = startOfDay(Date.now()) + 12 * 3600000;
        list.textContent = "";
        for (var i = 0; i < DAYS; i++) {
            var d = new Date(noon);
            d.setDate(d.getDate() + i);  // Calendar days: clock changes make some 23 or 25 hours long
            var time = d.getTime(), e = Sky.elongation(time);
            var li = document.createElement("li"), canvas = document.createElement("canvas"), label = document.createElement("span");
            li.title = MoonArt.phaseName(e) + ", " + MoonArt.litPercent(e) + "% lit";
            label.className = "moon-day-date";
            label.textContent = dayLabel(time);
            paint(canvas, MoonArt.disc({ size: DAY_SIZE, e: e, limb: e < 180 !== MOON.south ? -90 : 90, south: MOON.south }), DAY_SCALE);
            li.appendChild(canvas);
            li.appendChild(label);
            list.appendChild(li);
        }
    }

    // The next new moon, quarters and full moon, to about 10 minutes (sky.js is good to an hour or so)
    function renderPhases() {
        var list = document.getElementById("phases");
        list.textContent = "";
        nextPhases(Date.now()).forEach(function (phase) {
            var li = document.createElement("li"), at = roundTo10(phase.time);
            li.textContent = phase.name + " · about " + dayLabel(at) + " " + clockTime(at);
            list.appendChild(li);
        });
    }

    renderDays();
    renderPhases();

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

    // --- Diagram A: the orbit in 3D. World: x right, y up (north of Earth's orbit), z toward us; the sun far off
    // to the left. Earth and the moon are flat: the faces seen from the visitor's place ---
    var PLACE = MOON.position || guessPlace(new Date().getTimezoneOffset(), MOON.south);
    var AXIS = unit([-Math.sin(TILT) * 0.75, Math.cos(TILT), Math.sin(TILT) * 0.66]);  // Leaning partly sunward, partly toward us
    var ORBIT_U = [1, 0, 0], ORBIT_V = [0, Math.sin(INCL), -Math.cos(INCL)];  // Anticlockwise seen from the north
    var START_VIEW = { az: 26 / DEGREES, el: 21 / DEGREES }, EL_MIN = 0, EL_MAX = 80 / DEGREES;
    var view = { az: START_VIEW.az, el: START_VIEW.el };
    var orbitImage = null, craterCache = {};

    function unit(v) { var l = Math.hypot(v[0], v[1], v[2]); return [v[0] / l, v[1] / l, v[2] / l]; }
    function along(a, b, s) { return [a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s]; }
    function project(p) {
        var c = Math.cos(view.az), s = Math.sin(view.az), ce = Math.cos(view.el), se = Math.sin(view.el);
        var x = p[0] * c + p[2] * s, z = -p[0] * s + p[2] * c, y = p[1];
        var vy = y * ce - z * se, vz = y * se + z * ce, f = PERSPECTIVE / (PERSPECTIVE - vz);
        return { x: ORBIT_W / 2 + x * ORBIT_K * f, y: ORBIT_H / 2 - vy * ORBIT_K * f, z: vz, f: f };
    }
    function onOrbit(degrees) {
        var a = degrees / DEGREES;
        return along(along([0, 0, 0], ORBIT_U, ORBIT_R * Math.cos(a)), ORBIT_V, ORBIT_R * Math.sin(a));
    }
    function rgb(name) {
        var hex = colour(name).replace("#", "");
        return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
    }
    // The header's craters, scaled to a disc of `size` pixels
    function craters(size) {
        if (!craterCache[size]) {
            var list = [], k = size / 24;
            MoonArt.CRATERS.forEach(function (p) {
                for (var y = Math.floor(p[1] * k); y < Math.floor((p[1] + 1) * k); y++)
                    for (var x = Math.floor(p[0] * k); x < Math.floor((p[0] + 1) * k); x++) list.push([x, y]);
            });
            craterCache[size] = list;
        }
        return craterCache[size];
    }

    function drawOrbit(time) {
        var context = orbitEl.getContext("2d");
        if (!context) return;
        if (!orbitImage) orbitImage = context.createImageData(ORBIT_W, ORBIT_H);
        var data = orbitImage.data, palette = {};
        data.fill(0);
        function put(x, y, name, alpha) {
            x = Math.round(x); y = Math.round(y);
            if (x < 0 || y < 0 || x >= ORBIT_W || y >= ORBIT_H) return;
            var c = palette[name] || (palette[name] = rgb(name)), i = (y * ORBIT_W + x) * 4;
            data[i] = c[0]; data[i + 1] = c[1]; data[i + 2] = c[2]; data[i + 3] = Math.round((alpha === undefined ? 1 : alpha) * 255);
        }
        function disc(rows, cx, cy, colours) {
            var x0 = Math.round(cx - rows.length / 2), y0 = Math.round(cy - rows.length / 2);
            rows.forEach(function (row, y) { row.forEach(function (kind, x) { if (kind !== null) put(x0 + x, y0 + y, colours[kind] || colours.lit); }); });
        }
        function orbitDots(front) {
            for (var a = 0; a < 360; a += 2.5) {
                var p = project(onOrbit(a));
                if ((p.z >= 0) === front) put(p.x, p.y, "--muted", front ? 1 : 0.55);
            }
        }
        function line(direction, from, to, step, name, every) {
            for (var t = from, i = 0; t < to; t += step, i++) {
                if (every && i % every) continue;
                [1, -1].forEach(function (sign) { var p = project(along([0, 0, 0], direction, sign * t)); put(p.x, p.y, name); });
            }
        }
        var earth = project([0, 0, 0]), e = Sky.elongation(time), moon = project(onOrbit(180 + e));
        var size = Math.max(8, Math.round(MOON_R * ORBIT_K * moon.f) * 2), way = orientation(time, e, PLACE);
        var moonRows = MoonArt.disc({ size: size, e: e, limb: way.limb, face: way.face, south: way.south, craters: craters(size) });
        var tip = project(along([0, 0, 0], AXIS, 1)), north = Math.atan2(tip.x - earth.x, earth.y - tip.y);
        for (var y = 30; y < ORBIT_H - 20; y += 24) for (var x = 2; x < 12; x++) put(x, y, "--sun");  // Sunlight, from the left
        // Back to front: Earth hides what's behind it
        orbitDots(false);
        if (moon.z < earth.z) disc(moonRows, moon.x, moon.y, COLOURS);
        line([0, 1, 0], 1.12, 1.9, 0.035, "--muted", 2);  // Straight up from Earth's orbit
        disc(Earth.face({ size: Math.round(ORBIT_K * earth.f) * 2, lat: PLACE[0], lon: PLACE[1], rot: north, time: time }), earth.x, earth.y, EARTH);
        put(earth.x, earth.y, "--accent");  // The place, in the middle
        [-2, -1, 1, 2].forEach(function (d) { put(earth.x + d, earth.y, "--accent"); put(earth.x, earth.y + d, "--accent"); });
        line(AXIS, 1.03, 1.75, 0.01, "--accent");
        orbitDots(true);
        if (moon.z >= earth.z) disc(moonRows, moon.x, moon.y, COLOURS);
        context.putImageData(orbitImage, 0, 0);
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
        // Whole-number scales keep the pixels crisp; a column narrower than the canvas shrinks it to fit
        var shown = ORBIT_W * (width ? Math.min(ORBIT_SCALE_MAX, scaleFor(width, ORBIT_W)) : 1);
        orbitEl.style.width = (width ? Math.min(shown, width) : shown) + "px";
        drawOrbit(time);
        drawPhase(time, e, width ? Math.min(PHASE_SCALE_MAX, scaleFor(width, 24)) : 4);
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

    // Turning the orbit by hand: it follows the pointer, coasts on after a flick and slows to a stop, and glides
    // back to the starting view on a double-click, v, or after 10 s left alone
    var SENSITIVITY = 0.011, FRICTION = 0.0032, GLIDE_MS = 140, STILL_MS = 90, IDLE_MS = 10000;  // rad per px; per ms; ms
    var hand = { dragging: false, touch: false, vAz: 0, vEl: 0, x: 0, y: 0, t: 0, gliding: false }, idleTimer = null;
    function clampEl(el) { return Math.max(EL_MIN, Math.min(EL_MAX, el)); }
    function moving() { return hand.dragging || hand.gliding || hand.vAz !== 0 || hand.vEl !== 0; }
    function coast(dt) {
        if (hand.gliding) {
            var k = 1 - Math.exp(-dt / GLIDE_MS);
            view.az += (START_VIEW.az - view.az) * k;
            view.el += (START_VIEW.el - view.el) * k;
            if (Math.abs(view.az - START_VIEW.az) < 0.001 && Math.abs(view.el - START_VIEW.el) < 0.001) {
                view.az = START_VIEW.az; view.el = START_VIEW.el; hand.gliding = false;
            }
        } else if (!hand.dragging) {
            view.az += hand.vAz * dt;
            view.el = clampEl(view.el + hand.vEl * dt);
            var decay = Math.exp(-dt * FRICTION);
            hand.vAz *= decay; hand.vEl *= decay;
            if (Math.abs(hand.vAz) < 2e-5) hand.vAz = 0;
            if (Math.abs(hand.vEl) < 2e-5 || view.el === EL_MIN || view.el === EL_MAX) hand.vEl = 0;
        }
    }
    function startView() {
        hand.vAz = hand.vEl = 0;
        if (reduced) { view.az = START_VIEW.az; view.el = START_VIEW.el; drawDiagrams(); }
        else hand.gliding = view.az !== START_VIEW.az || view.el !== START_VIEW.el;
        schedule();
    }

    function frame(timestamp) {
        pending = null;
        // At most a second's worth at once: after a stall (a laptop asleep with the page open) the clock goes on,
        // it doesn't jump years
        var dt = lastFrame === null ? 0 : Math.min(timestamp - lastFrame, 1000);
        lastFrame = timestamp;
        clock.advance(dt);
        coast(Math.min(dt, 100));
        drawDiagrams(timestamp);
        schedule();
    }

    function schedule() {
        if (pending === null && (clock.playing || moving()) && !document.hidden) pending = window.requestAnimationFrame(frame);
    }

    function stop() {
        if (pending !== null) window.cancelAnimationFrame(pending);
        pending = null;
        lastFrame = null;
    }

    // After a control: show the change now, and run or stop the animation to match
    function changed() {
        button(playBtn, "space", clock.playing ? "pause" : "play");
        SPEEDS.forEach(function (speed, i) { document.getElementById("speed-" + (i + 1)).setAttribute("aria-pressed", String(clock.speed === speed)); });
        stop();
        drawDiagrams();
        schedule();
    }

    var actions = {
        " ": function () { clock.toggle(); },
        ArrowLeft: function () { clock.step(-1); },
        ArrowRight: function () { clock.step(1); },
        n: function () { clock.reset(Date.now()); },
        1: function () { clock.speed = SPEEDS[0]; },
        2: function () { clock.speed = SPEEDS[1]; },
        3: function () { clock.speed = SPEEDS[2]; }
    };
    clock.speed = SPEEDS[1];
    [["play", " "], ["step-back", "ArrowLeft"], ["step-forward", "ArrowRight"], ["now-button", "n"],
        ["speed-1", "1"], ["speed-2", "2"], ["speed-3", "3"]].forEach(function (pair) {
        document.getElementById(pair[0]).addEventListener("click", function () { actions[pair[1]](); changed(); });
    });
    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === "b") { document.getElementById("back").click(); return; }
        if (event.key === "v") { startView(); return; }
        if (!actions[event.key]) return;
        if (event.target && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName) && event.key === " ") return;  // A focused button's own space
        event.preventDefault();  // Space and the arrows would scroll the page
        actions[event.key]();
        changed();
    });
    // Mouse: sideways turns, up and down tilts. Touch: sideways only, so up and down still scrolls the page
    orbitEl.addEventListener("pointerdown", function (event) {
        if (event.button > 0) return;
        clearTimeout(idleTimer);
        hand.dragging = true; hand.gliding = false; hand.vAz = hand.vEl = 0;
        hand.touch = event.pointerType === "touch";
        hand.x = event.clientX; hand.y = event.clientY; hand.t = event.timeStamp;
        if (orbitEl.setPointerCapture && event.pointerId !== undefined) orbitEl.setPointerCapture(event.pointerId);
        schedule();
    });
    orbitEl.addEventListener("pointermove", function (event) {
        if (!hand.dragging) return;
        var dt = Math.max(1, event.timeStamp - hand.t), dAz = (event.clientX - hand.x) * SENSITIVITY;
        var dEl = hand.touch ? 0 : (event.clientY - hand.y) * SENSITIVITY;
        view.az += dAz;
        view.el = clampEl(view.el + dEl);
        hand.vAz = 0.7 * dAz / dt + 0.3 * hand.vAz;  // Smoothed: the speed it's let go at
        hand.vEl = 0.7 * dEl / dt + 0.3 * hand.vEl;
        hand.x = event.clientX; hand.y = event.clientY; hand.t = event.timeStamp;
    });
    ["pointerup", "pointercancel"].forEach(function (type) {
        orbitEl.addEventListener(type, function (event) {
            if (!hand.dragging) return;
            hand.dragging = false;
            if (reduced || event.timeStamp - hand.t > STILL_MS) hand.vAz = hand.vEl = 0;  // Held still, then let go: it stays
            clearTimeout(idleTimer);
            idleTimer = setTimeout(startView, IDLE_MS);
            lastFrame = null;
            schedule();
        });
    });
    orbitEl.addEventListener("dblclick", startView);

    // A narrower or wider window: the pixels again, at the scale that fits
    window.addEventListener("resize", function () {
        renderNow();
        drawDiagrams();
    });
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) stop(); else schedule();
    });
    changed();

    // Another theme: the pixels again, in its colours
    if (window.MutationObserver) {
        new MutationObserver(function () {
            renderNow();
            renderDays();
            drawDiagrams();
        }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    // Left open, the page catches up: today's moon, the strip from today, the next phases, the mark for now
    window.setInterval(function () {
        renderNow();
        renderDays();
        renderPhases();
        if (!clock.playing) drawDiagrams();
    }, 60000);
})(this);
