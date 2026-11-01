(function () {
    "use strict";

    // Sub-tabs of the stats page; only one so far.
    var TABS = [{ id: "heatmap", label: "heatmap" }];
    var BLUE = [74, 144, 194], ORANGE = [232, 116, 44], RED = "#d9281e";
    var REFRESH_MS = 60000;  // The worker adds a red dot within a minute of a story's arrival
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

    var grid = null, data = null, chosen = null, cellOwner = {};

    function applySavedTheme() {
        try {
            var theme = localStorage.getItem("retroTheme");
            if (theme) document.documentElement.setAttribute("data-theme", theme);
        } catch (e) { /* Private windows: keep the default */ }
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
        var rule = getComputedStyle(document.documentElement).getPropertyValue("--rule").trim();
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
        var paper = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim();
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
        paint(time);
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

    function showCountry(code) {
        chosen = code;
        var country = data && data.countries && data.countries[code];
        countryEl.textContent = "";
        countryEl.hidden = false;
        countryEl.appendChild(element("h2", nameOf(code) + ": " + (country ? band(country) : "little signal")));
        if (country && country.top && country.top.length) countryEl.appendChild(storyList(country.top));
        else countryEl.appendChild(element("p", "No stories here lately.", "source"));
        redraw();
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
        statusEl.textContent = "stats are offline";
        dotsEl.hidden = true;
        redraw();
    }

    function load() {
        fetch("/stats/heatmap.json", { cache: "no-cache" }).then(function (response) {
            if (!response.ok) throw new Error(String(response.status));
            return response.json();
        }).then(function (json) {
            if (typeof json.generated_at !== "number" || typeof json.countries !== "object" || json.countries === null) {
                throw new Error("unexpected file");
            }
            var wasStill = !data || !data.dots || !data.dots.length;
            data = json;
            renderStatus();
            renderDots();
            if (chosen) showCountry(chosen); else redraw();
            if (wasStill) window.requestAnimationFrame(animate);
        }).catch(offline);
    }

    canvas.addEventListener("click", function (event) {
        if (!grid) return;
        var box = canvas.getBoundingClientRect(), cell = box.width / grid.width;
        var x = Math.floor((event.clientX - box.left) / cell), y = Math.floor((event.clientY - box.top) / cell);
        var code = null;
        [[0, 0], [1, 0], [-1, 0], [0, 1], [0, -1]].some(function (step) {
            code = cellOwner[(x + step[0]) + "," + (y + step[1])] || null;
            return code;
        });
        if (code) showCountry(code);
    });
    window.addEventListener("resize", redraw);

    applySavedTheme();
    renderTabs();
    fetch("/stats/map.json").then(function (response) { return response.json(); }).then(function (json) {
        grid = json;
        grid.cells.forEach(function (c) { cellOwner[c[0] + "," + c[1]] = c[2]; });
        load();
        setInterval(load, REFRESH_MS);
        setInterval(renderStatus, 30000);
    }).catch(offline);
}());
