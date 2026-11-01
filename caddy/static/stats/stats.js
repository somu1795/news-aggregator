(function () {
    "use strict";

    // Sub-tabs of the stats page; only one so far.
    var TABS = [{ id: "heatmap", label: "heatmap" }];
    var BLUE = [74, 144, 194], ORANGE = [232, 116, 44], RED = "#d9281e";
    var REFRESH_MS = 60000;  // The worker adds a red dot within a minute of a story's arrival
    var FRAME_MS = 50;  // A pulse needs no more than 20 frames a second
    var TAP_CELLS = 4;  // A tap this near a country counts: cells are about 2 px wide on a phone
    var STALE_SECONDS = 1800;
    var PULSE_MS = 1800;
    var FADE = 0.3;  // Opacity of a country with next to no evidence

    var canvas = document.getElementById("map");
    var context = canvas.getContext("2d");
    var cells = document.createElement("canvas");
    var statusEl = document.getElementById("status");
    var dotsEl = document.getElementById("dots");
    var countryEl = document.getElementById("country");
    var calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var jumpEl = document.getElementById("jump");
    var grid = null, data = null, chosen = null, stories = null, lastLoad = 0, lastFrame = 0;

    function applySavedTheme() {
        try {
            var theme = localStorage.getItem("retroTheme");
            if (theme) document.documentElement.setAttribute("data-theme", theme);
        } catch (e) { /* Private windows: keep the default */ }
    }

    function toggleTheme() {
        var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        try { localStorage.setItem("retroTheme", next); } catch (e) { /* Private windows: just this page */ }
        redraw();
    }

    function renderTabs() {
        var nav = document.getElementById("tabs");
        TABS.forEach(function (tab, i) {
            var span = document.createElement("span");
            span.textContent = tab.label;
            if (i === 0) span.className = "on";
            nav.appendChild(span);
        });
    }

    function element(tag, text, className) {
        var el = document.createElement(tag);
        if (text !== undefined) el.textContent = text;
        if (className) el.className = className;
        return el;
    }

    function safeLink(url, text) {
        // Feed text, shown as text; only web links become links
        if (!/^https?:\/\//i.test(url || "")) return element("span", text);
        var a = element("a", text);
        a.href = url;
        a.rel = "noopener noreferrer";
        a.target = "_blank";
        return a;
    }

    function nameOf(code) { return grid && grid.names[code] || code; }

    function band(country) {
        if (country.colour === null || country.colour === undefined) return "little signal";
        return country.colour < 0.34 ? "calm" : country.colour < 0.67 ? "mixed" : "stressful";
    }

    function mix(position) {
        return BLUE.map(function (c, i) { return Math.round(c + (ORANGE[i] - c) * position); });
    }

    // The cells only change with new data, so they're drawn once to a layer of their own:
    // their glow is too slow to redraw on every frame of a dot's pulse.
    function drawCells() {
        var width = canvas.clientWidth, cell = width / grid.width, scale = window.devicePixelRatio || 1;
        canvas.width = cells.width = Math.round(width * scale);
        canvas.height = cells.height = Math.round(width * grid.height / grid.width * scale);
        // The fallbacks are for when the app, which serves the site's stylesheet, is down
        var rule = getComputedStyle(document.documentElement).getPropertyValue("--rule").trim() || "#cfc8b6";
        var countries = data && data.countries || {}, layer = cells.getContext("2d");
        layer.setTransform(scale, 0, 0, scale, 0, 0);
        grid.cells.forEach(function (c) {
            var country = countries[c[2]], coloured = country && country.colour !== null && country.colour !== undefined;
            if (coloured) {
                var rgb = mix(country.colour).join(",");
                layer.fillStyle = "rgba(" + rgb + "," + (FADE + (1 - FADE) * country.brightness) + ")";
                layer.shadowColor = "rgba(" + rgb + ",.8)";
                layer.shadowBlur = cell * 1.2 * country.brightness * scale;
            } else {
                layer.fillStyle = rule;
                layer.shadowBlur = 0;
            }
            layer.beginPath();
            layer.arc((c[0] + 0.5) * cell, (c[1] + 0.5) * cell, cell * (c[2] === chosen ? 0.5 : 0.4), 0, 2 * Math.PI);
            layer.fill();
        });
    }

    function paint(time) {
        if (!grid) return;
        var width = canvas.clientWidth, cell = width / grid.width, scale = window.devicePixelRatio || 1;
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.drawImage(cells, 0, 0);
        context.setTransform(scale, 0, 0, scale, 0, 0);
        var paper = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim() || "#f3efe4";
        ((data && data.dots) || []).forEach(function (dot) {
            if (typeof dot.lat !== "number" || typeof dot.lon !== "number") return;
            var x = (dot.lon + 180) / 360 * width;
            var y = (grid.north - dot.lat) / (grid.north - grid.south) * (width * grid.height / grid.width);
            var phase = calm ? 0 : (time % PULSE_MS) / PULSE_MS;
            context.strokeStyle = RED;
            context.lineWidth = 1.25;
            context.globalAlpha = calm ? 0.7 : 1 - phase;
            context.beginPath();
            context.arc(x, y, cell * (0.9 + 2 * phase), 0, 2 * Math.PI);
            context.stroke();
            context.globalAlpha = 1;
            context.fillStyle = paper;  // A gap, so the dot stands clear of the cells around it
            context.beginPath();
            context.arc(x, y, cell * 0.8, 0, 2 * Math.PI);
            context.fill();
            context.fillStyle = RED;
            context.shadowColor = RED;
            context.shadowBlur = cell * 0.6 * scale;
            context.beginPath();
            context.arc(x, y, cell * (0.5 + 0.1 * Math.sin(phase * 2 * Math.PI)), 0, 2 * Math.PI);
            context.fill();
            context.shadowBlur = 0;
        });
    }

    function redraw() {
        if (!grid) return;
        drawCells();
        paint(0);
    }

    function animate(time) {
        if (time - lastFrame >= FRAME_MS) {
            lastFrame = time;
            paint(time);
        }
        if (!calm && data && data.dots && data.dots.length) window.requestAnimationFrame(animate);
    }

    function storyList(stories) {
        var ul = element("ul");
        stories.forEach(function (story) {
            var li = element("li");
            li.appendChild(safeLink(story.link, story.title));
            if (story.source) li.appendChild(element("span", " - " + story.source, "source"));
            ul.appendChild(li);
        });
        return ul;
    }

    // The story lists are most of the data, so they only load once a country is chosen
    function loadStories() {
        if (!stories) {
            stories = fetch("/stats/stories.json", { cache: "no-cache" }).then(function (response) {
                if (!response.ok) throw new Error(String(response.status));
                return response.json();
            }).catch(function () {
                stories = null;
                return null;
            });
        }
        return stories;
    }

    function showCountry(code) {
        chosen = code;
        var country = data && data.countries && data.countries[code];
        countryEl.textContent = "";
        countryEl.hidden = false;
        countryEl.appendChild(element("h2", nameOf(code) + ": " + (country ? band(country) : "little signal")));
        var body = countryEl.appendChild(element("p", "loading...", "source"));
        redraw();
        loadStories().then(function (all) {
            if (chosen !== code) return;
            var list = all && all[code];
            if (list && list.length) countryEl.replaceChild(storyList(list), body);
            else body.textContent = all ? "No stories here lately." : "Stories are unavailable right now.";
        });
    }

    function renderDots() {
        var dots = data.dots || [];
        dotsEl.textContent = "";
        dotsEl.hidden = !dots.length;
        if (!dots.length) return;
        dotsEl.appendChild(element("h2", "Unusual right now"));
        var ul = element("ul");
        dots.forEach(function (dot) {
            var li = element("li");
            li.appendChild(element("span", nameOf(dot.country) + ": ", "source"));
            li.appendChild(safeLink(dot.link, dot.title));
            ul.appendChild(li);
        });
        dotsEl.appendChild(ul);
    }

    function renderJump() {
        var codes = Object.keys(data.countries || {}).sort(function (a, b) {
            return nameOf(a).localeCompare(nameOf(b));
        });
        jumpEl.textContent = "";
        jumpEl.appendChild(element("option", "Choose a country"));
        jumpEl.firstChild.value = "";
        codes.forEach(function (code) {
            var option = element("option", nameOf(code));
            option.value = code;
            option.selected = code === chosen;
            jumpEl.appendChild(option);
        });
    }

    function ageWords(seconds) {
        var minutes = Math.max(0, Math.round(seconds / 60));
        if (minutes < 1) return "just now";
        if (minutes < 120) return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
        return Math.round(minutes / 60) + " hours ago";
    }

    function renderStatus() {
        if (!data) return;
        var age = Date.now() / 1000 - data.generated_at;
        var words = "updated " + ageWords(age);
        if (age > STALE_SECONDS) words += ": this map is out of date";
        else if (!Object.keys(data.countries || {}).length) words += ": no stories yet";
        statusEl.textContent = words;
    }

    function offline() {
        data = null;
        chosen = null;
        statusEl.textContent = "stats are offline";
        dotsEl.hidden = true;
        countryEl.hidden = true;
        redraw();
    }

    function load() {
        lastLoad = Date.now();
        fetch("/stats/heatmap.json", { cache: "no-cache" }).then(function (response) {
            if (!response.ok) throw new Error(String(response.status));
            return response.json();
        }).then(function (json) {
            if (typeof json.generated_at !== "number" || typeof json.countries !== "object" || json.countries === null) {
                throw new Error("unexpected file");
            }
            var wasStill = !data || !data.dots || !data.dots.length;
            if (!data || json.generated_at !== data.generated_at) stories = null;
            data = json;
            renderStatus();
            renderDots();
            renderJump();
            if (chosen) showCountry(chosen); else redraw();
            if (wasStill) window.requestAnimationFrame(animate);
        }).catch(offline);
    }

    function nearestCountry(x, y) {
        var best = null, bestDistance = TAP_CELLS * TAP_CELLS;
        grid.cells.forEach(function (c) {
            var distance = Math.pow(c[0] + 0.5 - x, 2) + Math.pow(c[1] + 0.5 - y, 2);
            if (distance <= bestDistance) {
                best = c[2];
                bestDistance = distance;
            }
        });
        return best;
    }

    canvas.addEventListener("click", function (event) {
        if (!grid) return;
        var box = canvas.getBoundingClientRect(), cell = box.width / grid.width;
        var code = nearestCountry((event.clientX - box.left) / cell, (event.clientY - box.top) / cell);
        if (code) showCountry(code);
    });
    jumpEl.addEventListener("change", function () {
        if (jumpEl.value) showCountry(jumpEl.value);
    });
    document.getElementById("theme").addEventListener("click", toggleTheme);
    document.addEventListener("keydown", function (event) {
        if (event.key === "d" && !event.ctrlKey && !event.metaKey && !event.altKey && event.target === document.body) toggleTheme();
    });
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && grid && Date.now() - lastLoad >= REFRESH_MS) load();
    });
    window.addEventListener("resize", redraw);

    applySavedTheme();
    renderTabs();
    fetch("/stats/map.json").then(function (response) { return response.json(); }).then(function (json) {
        grid = json;
        load();
        setInterval(function () { if (!document.hidden) load(); }, REFRESH_MS);
        setInterval(renderStatus, 30000);
    }).catch(offline);
}());
