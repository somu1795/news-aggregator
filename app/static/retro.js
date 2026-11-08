(function () {
    "use strict";

    // Regions (All + regions) and topics (Everything + topics) come from the server's source catalog.
    // A view is a region and a topic; the default topic is the region's main view.
    var VIEWS = JSON.parse(document.getElementById("views-data").textContent);
    var TOPICS = JSON.parse(document.getElementById("topics-data").textContent);
    var DEFAULT_VIEW = VIEWS[0].id;
    var DEFAULT_TOPIC = TOPICS[0].id;
    // Starting region for visitors who haven't picked one: the server's guess from their country
    var HOME_VIEW = JSON.parse(document.getElementById("home-data").textContent);
    if (!VIEWS.some(function (v) { return v.id === HOME_VIEW; })) HOME_VIEW = DEFAULT_VIEW;
    var STALE_MS = 60000;  // Re-fetch a cached view in the background after this long

    // archive.ph/newest/<url> opens the latest snapshot directly (or archive.today's
    // search page if there is none). 12ft.io was removed: the service is shut down.
    var BYPASS_SERVICES = [
        { id: "none", name: "off", prefix: null },
        { id: "archive", name: "archive", prefix: "https://archive.ph/newest/" }
    ];
    var PAYWALLED = JSON.parse(document.getElementById("paywalled-data").textContent);
    var SITE_NAME = document.title;  // site.yaml name, set by the server

    var list = document.getElementById("list");
    var statusEl = document.getElementById("status");
    var statusAgeEl = document.getElementById("status-age");
    var statusBusyEl = document.getElementById("status-busy");
    var notice = document.getElementById("notice");
    var regionsEl = document.getElementById("regions");
    var topicsEl = document.getElementById("topics");
    var refreshBtn = document.getElementById("refresh");
    var themeBtn = document.getElementById("theme");
    var sortBtn = document.getElementById("sort");
    var advancedEl = document.getElementById("advanced");
    var advancedBtn = document.getElementById("advanced-toggle");
    var installBtn = document.getElementById("install");
    var siteIconsBtn = document.getElementById("site-icons");
    var autoRefreshBtn = document.getElementById("auto-refresh");
    var backActionBtn = document.getElementById("back-action");
    var searchBtn = document.getElementById("search-toggle");
    var searchEl = document.getElementById("search");
    var searchInput = document.getElementById("search-input");
    var searchNote = document.getElementById("search-note");
    var advancedNote = document.getElementById("advanced-note");
    var bypassEl = document.getElementById("bypass");

    // Per-view state, by viewKey(): { data, fetchedAt, loadedAt, loading, error }
    var views = {};
    var current = DEFAULT_VIEW;
    var currentTopic = DEFAULT_TOPIC;
    function viewKey(region, topic) { return region + "/" + topic; }
    function stateOf(region, topic) {
        var key = viewKey(region, topic);
        if (!views[key]) views[key] = { data: null, fetchedAt: null, loadedAt: 0, loading: false, error: null };
        return views[key];
    }

    // Paywall bypass for links to paywalled sites (see PAYWALLED), off by default
    var bypass = read("bypassService") || "none";
    if (!BYPASS_SERVICES.some(function (s) { return s.id === bypass; })) bypass = "none";  // e.g. a saved "12ft"

    // --- Storage (may be unavailable in private windows) ---
    function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    function write(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* ignore */ } }
    function forget(key) { try { localStorage.removeItem(key); } catch (e) { /* ignore */ } }

    function labelOf(id) {
        var match = VIEWS.filter(function (v) { return v.id === id; })[0];
        return match ? match.label : id;
    }
    function isView(id) { return VIEWS.some(function (v) { return v.id === id; }); }
    function isTopic(id) { return TOPICS.some(function (t) { return t.id === id; }); }
    function topicLabel(id) {
        var match = TOPICS.filter(function (t) { return t.id === id; })[0];
        return match ? match.label : id;
    }
    // e.g. "asia", "asia / sport"
    function viewName(region, topic) {
        return labelOf(region).toLowerCase() + (topic === DEFAULT_TOPIC ? "" : " / " + topicLabel(topic).toLowerCase());
    }

    // --- Theme: the server's DEFAULT_THEME until toggled ---
    var DEFAULT_THEME = document.documentElement.getAttribute("data-theme");
    function isDark() {
        return document.documentElement.getAttribute("data-theme") === "dark";
    }
    function applyTheme(theme) {
        if (theme) document.documentElement.setAttribute("data-theme", theme);
        themeBtn.classList.toggle("on", isDark());
    }
    function toggleTheme() {
        var next = isDark() ? "light" : "dark";
        write("retroTheme", next);
        applyTheme(next);
    }
    applyTheme(read("retroTheme"));

    // --- Bypass selector ---
    function renderBypass() {
        while (bypassEl.childNodes.length > 1) bypassEl.removeChild(bypassEl.lastChild);
        BYPASS_SERVICES.forEach(function (service) {
            var b = document.createElement("button");
            b.type = "button";
            b.textContent = service.id === bypass ? "[" + service.name + "]" : service.name;
            if (service.id === bypass) b.className = "on";
            b.setAttribute("aria-pressed", service.id === bypass ? "true" : "false");
            b.style.marginLeft = "1ch";
            b.addEventListener("click", function () {
                bypass = service.id;
                write("bypassService", bypass);
                renderBypass();
                render();
            });
            bypassEl.appendChild(b);
        });
    }

    // --- Region and topic tabs: real links (shareable, open in new tab), switched in place on click ---
    function urlFor(region, topic) {
        var params = new URLSearchParams();
        if (region !== DEFAULT_VIEW) params.set("region", region);
        if (topic !== DEFAULT_TOPIC) params.set("topic", topic);
        var query = params.toString();
        return query ? "/?" + query : "/";
    }
    // The address without a tab in it: switching tabs doesn't show the tab there, and a shared link's
    // tab (/?region=asia) goes on the first switch, so the address never names the wrong one
    function addressWithoutTab() {
        var url = new URL(location.href);
        url.searchParams.delete("region");
        url.searchParams.delete("topic");
        return url.pathname + url.search + url.hash;
    }
    function renderNav(el, items, selected, hrefFor, onSelect, hint) {
        el.textContent = "";
        items.forEach(function (item, i) {
            var a = document.createElement("a");
            a.href = hrefFor(item.id);
            a.textContent = item.id === selected ? "[" + item.label.toLowerCase() + "]" : item.label.toLowerCase();
            a.title = item.label + " (" + hint(i) + ")";
            if (item.id === selected) a.setAttribute("aria-current", "page");
            if (hint === numberKey && i < 9) {
                var key = document.createElement("sup");
                key.textContent = String(i + 1);
                key.setAttribute("aria-hidden", "true");
                a.appendChild(key);
            }
            a.addEventListener("click", function (event) {
                if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
                event.preventDefault();
                onSelect(item.id);
            });
            el.appendChild(a);
        });
    }
    function numberKey(i) { return String(i + 1); }
    function topicKey() { return "t: next topic"; }
    function renderTabs() {
        renderNav(regionsEl, VIEWS, current, function (id) { return urlFor(id, currentTopic); },
            function (id) { selectView(id, currentTopic, true); }, numberKey);
        renderNav(topicsEl, TOPICS, currentTopic, function (id) { return urlFor(current, id); },
            function (id) { selectView(current, id, true); }, topicKey);
    }

    // --- Helpers ---
    function safeUrl(url) {
        try {
            var parsed = new URL(url);
            if (parsed.protocol === "https:" || parsed.protocol === "http:") return url;
        } catch (e) { /* invalid */ }
        return null;
    }
    function hostOf(url) {
        try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; }
    }
    function isPaywalled(host) {
        return PAYWALLED.some(function (d) { return host === d || host.endsWith("." + d); });
    }
    function ago(seconds) {
        var s = Math.max(0, Date.now() / 1000 - seconds);
        if (s < 60) return "just now";
        if (s < 3600) return Math.floor(s / 60) + "m ago";
        if (s < 86400) return Math.floor(s / 3600) + "h ago";
        return Math.floor(s / 86400) + "d ago";
    }
    function cleanTitle(title, source) {
        // Google News appends " - Publisher"; the publisher is already shown below the title
        var suffix = " - " + source;
        return source && title.length > suffix.length && title.slice(-suffix.length) === suffix
            ? title.slice(0, -suffix.length)
            : title;
    }
    function link(href, text, className) {
        var a = document.createElement("a");
        a.href = href;
        a.textContent = text;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        if (className) a.className = className;
        return a;
    }
    function showNotice(text, isError) {
        notice.textContent = text;
        notice.className = isError ? "notice error" : "notice";
        notice.hidden = false;
    }

    // --- Rendering (always of the current view) ---
    // Order of the list. "mix" is the server's: each feed's own ranking, interleaved by weight.
    var SORTS = ["mix", "newest", "oldest", "random"];
    var sortMode = SORTS.indexOf(read("retroSort")) >= 0 ? read("retroSort") : "mix";
    var shuffleKeys = Object.create(null);  // Link -> random number; kept while "random" stays selected, so refreshes don't reshuffle

    function sorted(items) {
        var copy = items.slice();
        if (sortMode === "newest") copy.sort(function (a, b) { return b.published - a.published; });
        else if (sortMode === "oldest") copy.sort(function (a, b) { return a.published - b.published; });
        else if (sortMode === "random") {
            copy.forEach(function (item) { if (!(item.link in shuffleKeys)) shuffleKeys[item.link] = Math.random(); });
            copy.sort(function (a, b) { return shuffleKeys[a.link] - shuffleKeys[b.link]; });
        }
        return copy;
    }

    function cycleSort() {
        sortMode = SORTS[(SORTS.indexOf(sortMode) + 1) % SORTS.length];
        if (sortMode === "random") shuffleKeys = Object.create(null);  // A fresh shuffle each time it's chosen
        write("retroSort", sortMode);
        renderSort();
        render();
    }

    function renderSort() {
        document.getElementById("sort-mode").textContent = sortMode;
        sortBtn.classList.toggle("on", sortMode !== "mix");
    }

    // --- Advanced: reset, install, site icons ---
    function toggleAdvanced() {
        advancedEl.hidden = !advancedEl.hidden;
        advancedBtn.classList.toggle("on", !advancedEl.hidden);
        advancedBtn.setAttribute("aria-expanded", String(!advancedEl.hidden));
        showNote("");
    }

    // A short message in the advanced row, cleared after `seconds`
    var noteTimer = null;
    function showNote(text, seconds) {
        clearTimeout(noteTimer);
        advancedNote.textContent = text;
        if (text) noteTimer = setTimeout(function () { advancedNote.textContent = ""; }, seconds * 1000);
    }

    // Each website's icon before its headlines, served from our own domain (/site-icon/<domain>)
    var siteIcons = read("retroSiteIcons") !== "off";  // On unless turned off
    function renderSiteIcons() {
        siteIconsBtn.textContent = "site icons: " + (siteIcons ? "on" : "off");
        siteIconsBtn.classList.toggle("on", siteIcons);
        siteIconsBtn.setAttribute("aria-pressed", String(siteIcons));
    }
    function toggleSiteIcons() {
        siteIcons = !siteIcons;
        write("retroSiteIcons", siteIcons ? "on" : "off");
        renderSiteIcons();
        render();
    }

    // Auto refresh: re-fetch the open view once it's older than the chosen period, only while the page is visible
    var AUTO_REFRESH = [{ id: "off", ms: 0 }, { id: "5m", ms: 300000 }, { id: "15m", ms: 900000 },
        { id: "30m", ms: 1800000 }, { id: "1h", ms: 3600000 }];
    var autoRefresh = AUTO_REFRESH.filter(function (p) { return p.id === read("retroAutoRefresh"); })[0] || AUTO_REFRESH[0];
    function renderAutoRefresh() {
        autoRefreshBtn.textContent = "auto refresh: " + autoRefresh.id.padEnd(3, " ");  // Same width for every period, so the row doesn't reflow
        autoRefreshBtn.classList.toggle("on", autoRefresh.ms > 0);
    }
    function cycleAutoRefresh() {
        autoRefresh = AUTO_REFRESH[(AUTO_REFRESH.indexOf(autoRefresh) + 1) % AUTO_REFRESH.length];
        write("retroAutoRefresh", autoRefresh.id);
        renderAutoRefresh();
        autoRefreshIfDue();
    }
    // Back action: "exit" (the default) updates the address in place when switching tabs, so Back
    // leaves the site (as an installed app should); "last tab" adds a history entry per switch
    var backToLastTab = read("retroBackAction") === "last tab";
    function renderBackAction() {
        backActionBtn.textContent = "back action: " + (backToLastTab ? "last tab" : "exit".padEnd(8, "\u00a0"));  // Same width either way
        backActionBtn.classList.toggle("on", backToLastTab);
    }
    function toggleBackAction() {
        backToLastTab = !backToLastTab;
        write("retroBackAction", backToLastTab ? "last tab" : "exit");
        renderBackAction();
    }

    // --- Search: every tab's headlines and summaries, in the browser ---
    // Opening it loads every tab's headlines in one response (/api/search-index, cached by Cloudflare),
    // then each keystroke filters in memory. Every word typed must start a word in the headline, source
    // or summary (case and accents ignored); headline matches first, newest first.
    var SEARCH_LIMIT = 100;
    var SEARCH_STALE_MS = 5 * 60 * 1000;
    var search = { active: false, query: "", index: null, loadedAt: 0, loading: false, error: null, matches: [], count: 0, olderCount: 0 };

    function searching() { return search.active && search.query.trim() !== ""; }
    function normalize(text) {
        return (text || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    }
    function loadSearchIndex() {
        if (search.loading || (search.index && Date.now() - search.loadedAt < SEARCH_STALE_MS)) return;
        search.loading = true;
        search.error = null;
        updateSearchNote();
        getJson("/api/search-index")
            .then(function (body) {
                search.index = indexOf(Array.isArray(body.items) ? body.items : []);
                search.loadedAt = Date.now();
            })
            .catch(function (error) {
                search.error = error.message;  // Keep any older index; reopening search tries again
            })
            .then(function () {
                search.loading = false;
                updateSearchNote();
                render();
                updateStatus();
            });
    }
    // One entry per story: the server lists each link once, with the views it's in ("asia:tech");
    // the tabs' headlines of one story (the same "story", which a story's headline has in every tab,
    // or the same headline from another outlet) are one entry's members
    function indexOf(items) {
        var seen = Object.create(null), stories = new Map(), index = [];  // No inherited keys: a headline "constructor" is just a headline
        items.forEach(function (item) {
            var title = normalize(cleanTitle(item.title, item.source));
            var key = title.replace(/[^a-z0-9]+/g, " ").trim();
            var entry = (item.story != null && stories.get(item.story)) || seen[key];
            if (!entry) {
                entry = { members: [], titles: [], views: [], text: "" };
                index.push(entry);
            }
            seen[key] = entry;
            if (item.story != null) stories.set(item.story, entry);
            entry.members.push(item);
            entry.titles.push(title);
            entry.text += " " + normalize(cleanTitle(item.title, item.source) + " " + item.source + " " + (item.summary || ""));
            (item.views || []).forEach(function (view) {
                var parts = view.split(":");
                entry.views.push([parts[0], parts[1] || DEFAULT_TOPIC]);
            });
        });
        // A story from several outlets: its tabs in tab order, which sets the order of its "where" label
        index.forEach(function (entry) { entry.views.sort(function (a, b) { return tabPosition(a) - tabPosition(b); }); });
        return index;
    }
    function tabPosition(view) {  // [region, topic]: each region, then its topics
        return VIEWS.findIndex(function (v) { return v.id === view[0]; }) * TOPICS.length +
            TOPICS.findIndex(function (t) { return t.id === view[1]; });
    }
    function whereLabel(inViews) {
        var regions = [], topics = [];
        inViews.forEach(function (view) {
            var region = labelOf(view[0]).toLowerCase(), topic = topicLabel(view[1]).toLowerCase();
            if (view[0] !== DEFAULT_VIEW && regions.indexOf(region) < 0) regions.push(region);
            if (view[1] !== DEFAULT_TOPIC && topics.indexOf(topic) < 0) topics.push(topic);
        });
        var everyRegion = regions.length === VIEWS.length - 1;
        return (everyRegion ? "every region" : regions.length ? regions.join(", ") : labelOf(DEFAULT_VIEW).toLowerCase()) +
            (topics.length ? " / " + topics.join(", ") : "");
    }
    function searchResults() {
        // Each word must start a word in the text ("elect" finds "election", "ai" doesn't find "said")
        var words = normalize(search.query).split(/\s+/).filter(Boolean).map(function (w) {
            return new RegExp("(^|[^a-z0-9])" + escapeRegExp(w));
        });
        function all(text) { return words.every(function (w) { return w.test(text); }); }
        var inTitle = [], inSummary = [];
        search.index.forEach(function (entry) {
            var lead = entry.titles.findIndex(all);  // A story is shown by a headline that matches, if one does
            entry.lead = entry.members[Math.max(lead, 0)];
            if (lead >= 0) inTitle.push(entry);
            else if (all(entry.text)) inSummary.push(entry);
        });
        function newest(a, b) { return b.lead.published - a.lead.published; }
        inSummary.forEach(function (entry) { entry.inSummaryOnly = true; });
        inTitle.forEach(function (entry) { entry.inSummaryOnly = false; });
        var matches = inTitle.sort(newest).concat(inSummary.sort(newest));
        search.matches = matches;
        search.count = matches.length;
        return matches.slice(0, SEARCH_LIMIT).map(function (entry) {
            var item = Object.assign({}, entry.lead, { related: [], where: whereLabel(entry.views), inSummaryOnly: entry.inSummaryOnly });
            entry.members.forEach(function (member) { addRelated(item, [member].concat(member.related || [])); });
            return item;
        });
    }
    // Other headlines of an item's story, under it: each link once, not the item's own
    function addRelated(item, others) {
        var listed = new Set([item.link].concat(item.related.map(function (other) { return other.link; })));
        others.forEach(function (other) {
            if (listed.has(other.link)) return;
            listed.add(other.link);
            item.related.push({ title: other.title, link: other.link, source: other.source, comments: other.comments });
        });
    }
    function escapeRegExp(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

    // Highlighting: the searched words (two letters or more) wherever they start a word,
    // found on the normalized text and mapped back to the original's letters ("cafe" marks "Café")
    function highlightWords() {
        if (!searching()) return [];
        var corrected = older.result ? older.result.corrected.map(function (c) { return normalize(c.to); }) : [];
        return normalize(search.query).split(/\s+/).concat(corrected).filter(function (w) { return w.length >= 2; });
    }
    function matchRanges(text, words) {
        if (!words.length || !text) return [];
        var norm = "", map = [];
        for (var i = 0; i < text.length; i++) {
            var n = normalize(text[i]);
            for (var j = 0; j < n.length; j++) { norm += n[j]; map.push(i); }
        }
        var ranges = [];
        words.forEach(function (w) {
            var re = new RegExp("(^|[^a-z0-9])(" + escapeRegExp(w) + ")", "g"), m;
            while ((m = re.exec(norm))) {
                var start = m.index + m[1].length;
                ranges.push([map[start], map[start + w.length - 1] + 1]);
            }
        });
        ranges.sort(function (a, b) { return a[0] - b[0]; });
        var merged = [];
        ranges.forEach(function (r) {
            var last = merged[merged.length - 1];
            if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
            else merged.push(r.slice());
        });
        return merged;
    }
    function highlighted(text, words) {  // Text nodes and <mark>s, never HTML
        var fragment = document.createDocumentFragment(), at = 0;
        matchRanges(text, words).forEach(function (r) {
            if (r[0] > at) fragment.appendChild(document.createTextNode(text.slice(at, r[0])));
            var mark = document.createElement("mark");
            mark.textContent = text.slice(r[0], r[1]);
            fragment.appendChild(mark);
            at = r[1];
        });
        if (at < text.length) fragment.appendChild(document.createTextNode(text.slice(at)));
        return fragment;
    }
    function snippetOf(text, words) {  // A few words around the first match, cut at spaces
        var first = matchRanges(text, words)[0];
        if (!first) return null;
        var start = Math.max(0, first[0] - 60), end = Math.min(text.length, first[1] + 90);
        if (start > 0) start = text.indexOf(" ", start) + 1 || start;
        if (end < text.length) end = text.lastIndexOf(" ", end) > first[1] ? text.lastIndexOf(" ", end) : end;
        return (start > 0 ? "\u2026" : "") + text.slice(start, end).trim() + (end < text.length ? "\u2026" : "");
    }
    function updateSearchNote() {
        searchNote.textContent = search.loading ? "loading every tab..." :
            search.error ? "couldn't load " + (search.index ? "the latest headlines" : "the headlines") + " (" + search.error +
                "); reopen search to try again" : "";
    }
    function openSearch() {
        search.active = true;
        searchEl.hidden = false;
        searchBtn.classList.add("on");
        searchBtn.setAttribute("aria-expanded", "true");
        searchInput.focus();
        loadSearchIndex();
        render();
        updateStatus();
    }
    function closeSearch(redraw) {
        search.active = false;
        search.query = "";
        searchInput.value = "";
        searchEl.hidden = true;
        searchBtn.classList.remove("on");
        searchBtn.setAttribute("aria-expanded", "false");
        stopOlder();
        if (redraw !== false) {
            render();
            updateStatus();
        }
    }

    // --- Older headlines: the server's archive, listed with the tabs' search results ---
    // Asked for once typing pauses (OLDER_DELAY_MS); a newer keystroke cancels the request in flight, and
    // recent answers are kept, so going back to a query shows its results at once. Headlines the tabs'
    // search already found are left out. 50 at a time: "more headlines" adds the next 50.
    var OLDER_DELAY_MS = 60;
    var OLDER_KEPT = 30;
    var older = { key: "", result: null, loading: false, error: null, timer: 0, controller: null, kept: new Map() };

    function olderKey() { return normalize(search.query).trim().replace(/\s+/g, " "); }
    function scheduleOlder() {
        clearTimeout(older.timer);
        if (older.controller) older.controller.abort();
        older.controller = null;
        older.key = olderKey();
        older.error = null;
        older.result = searching() ? older.kept.get(older.key) || null : null;
        older.loading = searching() && !older.result;
        if (older.loading) older.timer = setTimeout(fetchOlder, OLDER_DELAY_MS);
    }
    function fetchOlder(after) {
        var key = older.key, before = older.result, controller = older.controller = new AbortController();
        getJson("/api/search?q=" + encodeURIComponent(search.query.trim()) + (after ? "&after=" + after : ""), controller.signal)
            .then(function (body) {
                if (after) body = Object.assign({}, body, { items: before.items.concat(body.items) });  // Added to those listed
                older.kept.delete(key);
                older.kept.set(key, body);
                if (older.kept.size > OLDER_KEPT) older.kept.delete(older.kept.keys().next().value);  // The oldest
                if (key === older.key) older.result = body;
            })
            .catch(function (error) {
                if (key === older.key && error.name !== "AbortError") older.error = error.message;
            })
            .then(function () {
                if (key !== older.key) return;  // A newer query took over
                older.loading = false;
                older.controller = null;
                render();
                updateStatus();
            });
    }
    function moreOlder() {
        if (older.loading) return;
        older.loading = true;
        older.error = null;
        this.textContent = "loading more headlines...";
        fetchOlder(older.result.next);
        updateStatus();
    }
    function stopOlder() {
        clearTimeout(older.timer);
        if (older.controller) older.controller.abort();
        older.controller = null;
        older.key = "";
        older.result = null;
        older.loading = false;
    }
    function titleKey(item) { return normalize(cleanTitle(item.title, item.source)).replace(/[^a-z0-9]+/g, " ").trim(); }
    // The older stories to list: one the tabs' search found (searchResults(), `found`), or one listed
    // already (from an earlier page), gets the archive's headlines of it under it instead; else none
    // of the headlines the tabs' search found, each headline once
    function olderItems(found) {
        var shown = new Set(), byStory = new Map();
        found.forEach(function (item) { if (item.story != null) byStory.set(item.story, item); });
        search.matches.forEach(function (entry) {
            entry.members.forEach(function (member) {
                shown.add(member.link);
                shown.add(titleKey(member));
                (member.related || []).forEach(function (other) { shown.add(other.link); });
            });
        });
        var words = older.result.words.map(function (w) { return new RegExp("(^|[^a-z0-9])" + escapeRegExp(normalize(w))); });
        var list = [];
        older.result.items.forEach(function (item) {
            var into = item.story != null && byStory.get(item.story);
            if (into) return addRelated(into, [item].concat(item.related || []));
            var key = titleKey(item);
            if (shown.has(item.link) || shown.has(key)) return;
            shown.add(key);
            var title = normalize(cleanTitle(item.title, item.source));
            var copy = Object.assign({}, item, { related: [], inSummaryOnly: !words.every(function (w) { return w.test(title); }) });
            addRelated(copy, item.related || []);
            if (item.story != null) byStory.set(item.story, copy);
            list.push(copy);
        });
        return list;
    }
    function autoRefreshIfDue() {
        if (!autoRefresh.ms || document.hidden) return;
        if (Date.now() - stateOf(current, currentTopic).loadedAt >= autoRefresh.ms) load(current, currentTopic);
    }

    // The browser offers installing (beforeinstallprompt) only when it can and the app isn't installed yet
    var installPrompt = null;
    function isInstalled() {
        return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
    }
    function renderInstall() {
        installBtn.textContent = isInstalled() ? "installed" : "install";
        installBtn.disabled = isInstalled();
    }
    function install() {
        if (!installPrompt) {
            showNote("This browser isn't offering to install right now (it may already be installed). " +
                "Use its menu instead: Install app, or Share \u2192 Add to Home Screen on iPhone.", 12);
            return;
        }
        installPrompt.prompt();
        installPrompt.userChoice.then(function () { installPrompt = null; renderInstall(); });
    }
    window.addEventListener("beforeinstallprompt", function (event) {
        event.preventDefault();  // Shown from the install button instead
        installPrompt = event;
        renderInstall();
    });
    window.addEventListener("appinstalled", function () {
        installPrompt = null;
        showNote("Installed.", 4);
        renderInstall();
    });

    function resetAll() {
        ["retroRegion", "retroTopic", "retroSort", "retroTheme", "bypassService", "retroSiteIcons", "retroAutoRefresh", "retroBackAction"].forEach(forget);  // bypassService: shared with the old classic page
        openSummaries = Object.create(null);
        openRelated = Object.create(null);
        sortMode = "mix";
        shuffleKeys = Object.create(null);
        siteIcons = true;
        autoRefresh = AUTO_REFRESH[0];
        backToLastTab = false;
        bypass = "none";
        applyTheme(DEFAULT_THEME);
        renderBypass();
        renderSort();
        renderSiteIcons();
        renderAutoRefresh();
        renderBackAction();
        selectView(HOME_VIEW, DEFAULT_TOPIC, true);
        forget("retroRegion");  // Back to following the visitor's location
        forget("retroTopic");
        render();
        showNote("Back to the defaults.", 3);
    }

    // Open summaries, by link: they stay open when the list is re-rendered
    var openSummaries = Object.create(null);  // Keyed by links from feeds: no inherited keys

    // Open lists of other outlets' headlines, by link (as for summaries)
    var openRelated = Object.create(null);

    function setRelatedOpen(related, toggle, itemLink, open) {
        related.hidden = !open;
        toggle.textContent = open ? "hide sources" : "+" + related.children.length + " sources";
        toggle.setAttribute("aria-expanded", String(open));
        if (open) openRelated[itemLink] = true; else delete openRelated[itemLink];
    }

    function setSummaryOpen(summary, toggle, itemLink, open) {
        summary.hidden = !open;
        toggle.textContent = open ? "hide summary" : "summary";
        toggle.setAttribute("aria-expanded", String(open));
        if (open) openSummaries[itemLink] = true; else delete openSummaries[itemLink];
    }

    function render() {
        var refocus = document.activeElement && document.activeElement.classList.contains("more");
        list.textContent = "";
        notice.hidden = true;
        var items = itemsToShow() || [];
        var service = BYPASS_SERVICES.filter(function (s) { return s.id === bypass; })[0];
        var words = highlightWords();
        var mark = true;
        var more = items.length && searching() && older.result && older.result.next ? moreMark() : null;
        var moreFirst = more && older.result.next.charAt(0) === "t";  // Headline matches to come: before the summary matches
        items.forEach(function (item, i) {
            if (mark && item.inSummaryOnly) {
                if (moreFirst) list.appendChild(more);
                list.appendChild(groupMark());
                mark = false;
            }
            var li = renderItem(item, i, service, words);
            if (li) list.appendChild(li);
        });
        if (more && !more.parentNode) list.appendChild(more);
        if (refocus && more) more.firstChild.focus({ preventScroll: true });
    }

    // The open view's items in the chosen order, or the search results in theirs (whatever the sort);
    // null if there's nothing to list (with a notice saying why)
    function itemsToShow() {
        if (searching()) {
            if (!search.index) return null;  // Still loading, or failed; the status line says so
            var found = searchResults(), archived = older.result ? olderItems(found) : [];
            search.olderCount = archived.length;
            // The tabs' and the archive's together: headline matches first, then the rest, each newest first
            var items = found.concat(archived).sort(function (a, b) {
                return a.inSummaryOnly - b.inSummaryOnly || b.published - a.published;
            });
            if (!items.length && older.result) showNotice("nothing matches \u201c" + search.query.trim() + "\u201d", false);
            return items;
        }
        var state = stateOf(current, currentTopic);
        if (state.data === null) {
            if (state.error) showNotice("could not load " + viewName(current, currentTopic) + ": " + state.error, true);
            return null;
        }
        if (state.data.length) return sorted(state.data);
        showNotice("no headlines for " + viewName(current, currentTopic) + " right now.", false);
        return null;
    }

    // Search results come headline matches first: this line starts the rest, found in a summary or source
    function groupMark() {
        var li = document.createElement("li");
        li.className = "group-mark";
        li.textContent = "\u00b7 in summaries \u00b7";
        return li;
    }

    // The archive has more: where its next ones will be listed
    function moreMark() {
        var li = document.createElement("li"), button = document.createElement("button");
        li.className = "group-mark";
        button.type = "button";
        button.className = "more";
        button.textContent = older.loading ? "loading more headlines..." : "more headlines";
        button.addEventListener("click", moreOlder);
        li.appendChild(button);
        return li;
    }

    // One item: its headline, a line of details and buttons, and what the buttons open
    function renderItem(item, i, service, words) {
        var href = safeUrl(item.link);
        if (!href) return null;
        var host = hostOf(href);
        var bypassed = viaBypass(href, service);

        var li = document.createElement("li");
        var num = document.createElement("span");
        num.className = "num";
        // A tab's rank; search results have none: each page of older headlines adds results among them
        num.textContent = searching() ? "" : (i + 1) + ".";
        li.appendChild(num);
        li.appendChild(headline(item, href, host, words));

        var meta = document.createElement("div");
        meta.className = "meta";
        meta.appendChild(highlighted(item.source, words));
        meta.appendChild(document.createTextNode(" · " + ago(item.published)));
        if (item.where) {  // A search result: the tabs it's in
            var where = document.createElement("span");
            where.className = "where";
            where.textContent = item.where;
            appendPart(meta, where);
        }
        var comments = item.comments && safeUrl(item.comments);
        if (comments) appendPart(meta, link(comments, "discuss"));
        var summary = item.summary ? summaryOf(item, i, words) : null;
        if (summary) appendPart(meta, summary.toggle);
        var related = relatedOf(item, i, service, words);
        if (related) appendPart(meta, related.toggle);
        if (bypassed !== href) appendPart(meta, link(bypassed, "via " + service.name, "paywall"));
        li.appendChild(meta);

        // A result found only in its summary: show where, so it's clear why it's here
        var snippetText = item.inSummaryOnly && item.summary && snippetOf(item.summary, words);
        if (snippetText) {
            var snippet = document.createElement("p");
            snippet.className = "snippet";
            snippet.appendChild(highlighted(snippetText, words));
            li.appendChild(snippet);
        }
        if (summary) li.appendChild(summary.block);
        if (related) li.appendChild(related.block);
        return li;
    }

    // The website's icon (with site icons on), the linked title and the domain
    function headline(item, href, host, words) {
        var body = document.createElement("div");
        if (siteIcons && host) {
            var icon = document.createElement("img");
            icon.className = "site-icon";
            icon.src = "/site-icon/" + encodeURIComponent(host);
            icon.alt = "";
            icon.width = 16;
            icon.height = 16;
            icon.loading = "lazy";
            icon.addEventListener("error", function () { this.remove(); });  // No icon cached for that site
            body.appendChild(icon);
        }
        var titleLink = link(href, "", "title");
        titleLink.appendChild(highlighted(cleanTitle(item.title, item.source), words));
        body.appendChild(titleLink);
        if (host) {
            var domain = document.createElement("span");
            domain.className = "domain";
            domain.textContent = " (" + host + ")";
            body.appendChild(domain);
        }
        return body;
    }

    // The item's machine summary and the button that opens it: { block, toggle }
    function summaryOf(item, i, words) {
        var block = document.createElement("p");
        block.className = "summary";
        block.id = "summary-" + i;
        block.appendChild(highlighted(item.summary, words));
        var note = document.createElement("small");
        note.textContent = "machine summary · may contain errors";
        block.appendChild(note);
        var toggle = document.createElement("button");
        toggle.type = "button";
        toggle.setAttribute("aria-controls", block.id);
        setSummaryOpen(block, toggle, item.link, !!openSummaries[item.link]);
        toggle.addEventListener("click", function () { setSummaryOpen(block, toggle, item.link, block.hidden); });
        return { block: block, toggle: toggle };
    }

    // Other outlets' headlines about the same story (folded under this one by the server) and the
    // button that opens them: { block, toggle }, or null if there are none
    function relatedOf(item, i, service, words) {
        if (!item.related || !item.related.length) return null;
        var block = document.createElement("ul");
        block.className = "related";
        block.id = "related-" + i;
        item.related.forEach(function (other) {
            var otherHref = safeUrl(other.link);
            if (!otherHref) return;
            var row = document.createElement("li");
            var otherLink = link(otherHref, "");
            otherLink.appendChild(highlighted(cleanTitle(other.title, other.source), words));
            row.appendChild(otherLink);
            var from = document.createElement("span");
            from.textContent = " · " + other.source;
            row.appendChild(from);
            var otherBypassed = viaBypass(otherHref, service);
            if (otherBypassed !== otherHref) appendPart(row, link(otherBypassed, "via " + service.name, "paywall"));
            var otherComments = other.comments && safeUrl(other.comments);
            if (otherComments) appendPart(row, link(otherComments, "discuss", "discuss"));
            block.appendChild(row);
        });
        if (!block.children.length) return null;
        var toggle = document.createElement("button");
        toggle.type = "button";
        toggle.setAttribute("aria-controls", block.id);
        setRelatedOpen(block, toggle, item.link, !!openRelated[item.link]);
        toggle.addEventListener("click", function () { setRelatedOpen(block, toggle, item.link, block.hidden); });
        return { block: block, toggle: toggle };
    }

    // A paywalled site's link through the bypass service, when one is chosen (shown as "via <service>"
    // beside the headline, which keeps the site's own link); otherwise the link itself
    function viaBypass(href, service) {
        return service.prefix && isPaywalled(hostOf(href)) ? service.prefix + href : href;
    }

    function appendPart(line, node) {  // " · " and the next part of a line
        line.appendChild(document.createTextNode(" · "));
        line.appendChild(node);
    }

    function updateStatus() {
        if (search.active) {
            if (!search.index) setStatus(search.error ? "search \u00b7 could not load the headlines" : "search \u00b7 loading every tab...");
            else if (!searching()) setStatus("search \u00b7 type to search " + search.index.length + " headlines in every tab");
            else {
                var total = search.count + search.olderCount, shown = Math.min(search.count, SEARCH_LIMIT) + search.olderCount;
                setStatus("search \u00b7 " + total + (total === 1 ? " result" : " results") +
                    (total > shown ? " (showing " + shown + ")" : "") +
                    (older.result ? older.result.corrected.map(function (c) {
                        return " \u00b7 showing \u201c" + c.to + "\u201d for \u201c" + c.from + "\u201d";
                    }).join("") : ""), "",
                    older.loading ? " \u00b7 searching older headlines..." :
                        older.error ? " \u00b7 couldn't search older headlines (" + older.error + ")" : "");
            }
            return;
        }
        var state = stateOf(current, currentTopic);
        var label = viewName(current, currentTopic);
        if (state.data === null) {
            setStatus(state.error ? label + " \u00b7 offline" : "loading " + label + "...");
            return;
        }
        setStatus(label + " \u00b7 " + state.data.length + " items", " \u00b7 updated " + ago(state.fetchedAt),
            state.loading ? " \u00b7 refreshing..." : "");
    }

    // The status line's three parts. Screen readers announce changes to the first and last (aria-live),
    // not to its age, which changes every minute
    function setStatus(text, age, busy) {
        setText(statusEl, text);
        setText(statusAgeEl, age || "");
        setText(statusBusyEl, busy || "");
    }
    function setText(el, text) {  // Unchanged text isn't rewritten, so it isn't announced again
        if (el.textContent !== text) el.textContent = text;
    }

    // --- Data ---
    // An API response's body, or an error with the API's message
    function getJson(url, signal) {
        return fetch(url, { headers: { "Accept": "application/json" }, signal: signal }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) throw new Error(body.detail || body.error || "HTTP " + response.status);
                return body;
            });
        });
    }

    function load(region, topic) {
        var state = stateOf(region, topic);
        if (state.loading) return state.promise;
        state.loading = true;
        function isCurrent() { return region === current && topic === currentTopic; }
        if (isCurrent()) { refreshBtn.disabled = true; updateStatus(); }
        var url = "/api/headlines?region=" + encodeURIComponent(region) +
            (topic === DEFAULT_TOPIC ? "" : "&topic=" + encodeURIComponent(topic));
        var redraw = true;
        state.promise = getJson(url)
            .then(function (body) {
                var fetchedAt = body.fetched_at || Date.now() / 1000;
                // The same snapshot again (e.g. a refresh before the next one is out): the list stays as it
                // is, so reading position, keyboard focus and selected text in it aren't lost
                redraw = state.data === null || fetchedAt !== state.fetchedAt;
                if (redraw) {
                    state.data = Array.isArray(body.data) ? body.data : [];
                    state.fetchedAt = fetchedAt;
                }
                state.loadedAt = Date.now();
                state.error = null;
            })
            .catch(function (error) {
                state.error = error.message;  // Keep any data we already have
                redraw = state.data === null;  // Only to say it couldn't load
            })
            .then(function () {
                state.loading = false;
                if (isCurrent()) {  // Ignore responses for a view the user has left
                    refreshBtn.disabled = false;
                    if (redraw) render();
                    updateStatus();
                }
            });
        return state.promise;
    }

    function selectView(region, topic, byUser) {
        if (byUser && search.active) closeSearch(false);  // Picking a tab leaves the search
        if (!isView(region)) region = DEFAULT_VIEW;
        if (!isTopic(topic)) topic = DEFAULT_TOPIC;
        var changed = region !== current || topic !== currentTopic;
        current = region;
        currentTopic = topic;
        if (byUser) {  // Remember the user's own choices only, not the starting region
            write("retroRegion", region);
            write("retroTopic", topic);
        }
        // The address stays as it is (the last tab is remembered, so a refresh reopens it). By default
        // the switch replaces the history entry, so Back leaves the site; with back action "last tab",
        // each switch is a history entry of its own, holding its tab
        if (byUser && changed) {
            var entry = { region: region, topic: topic };
            if (backToLastTab) history.pushState(entry, "", addressWithoutTab());
            else history.replaceState(entry, "", addressWithoutTab());
        }
        document.title = region === DEFAULT_VIEW && topic === DEFAULT_TOPIC ? SITE_NAME : SITE_NAME + " / " + viewName(region, topic);
        renderTabs();
        render();
        updateStatus();
        if (changed) window.scrollTo(0, 0);
        var state = stateOf(region, topic);
        if (state.data === null || Date.now() - state.loadedAt > STALE_MS) load(region, topic);
    }

    function fromUrl(name, isValid) {
        var param = new URLSearchParams(location.search).get(name);
        return param && isValid(param) ? param : null;
    }

    // --- Wiring ---
    refreshBtn.addEventListener("click", function () { load(current, currentTopic); });
    themeBtn.addEventListener("click", toggleTheme);
    sortBtn.addEventListener("click", cycleSort);
    advancedBtn.addEventListener("click", toggleAdvanced);
    document.getElementById("reset").addEventListener("click", resetAll);
    installBtn.addEventListener("click", install);
    siteIconsBtn.addEventListener("click", toggleSiteIcons);
    autoRefreshBtn.addEventListener("click", cycleAutoRefresh);
    backActionBtn.addEventListener("click", toggleBackAction);
    searchBtn.addEventListener("click", function () { if (search.active) closeSearch(); else openSearch(); });
    searchInput.addEventListener("input", function () {
        search.query = searchInput.value;
        scheduleOlder();
        render();
        updateStatus();
    });
    searchInput.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeSearch(); searchBtn.focus(); }
    });
    document.addEventListener("visibilitychange", autoRefreshIfDue);  // Catch up on returning to the tab
    window.addEventListener("popstate", function (event) {
        // Back or Forward to another tab: it's in the history entry, not the address
        var entry = event.state || {};
        selectView(isView(entry.region) ? entry.region : fromUrl("region", isView) || DEFAULT_VIEW,
            isTopic(entry.topic) ? entry.topic : fromUrl("topic", isTopic) || DEFAULT_TOPIC, false);
        write("retroRegion", current);  // A refresh reopens the tab shown, as after a click
        write("retroTopic", currentTopic);
    });
    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.target && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
        if (event.key === "/") { event.preventDefault(); openSearch(); return; }  // Don't type the "/" into the box
        if (event.key === "r") load(current, currentTopic);
        else if (event.key === "d") toggleTheme();
        else if (event.key === "s") cycleSort();
        else if (event.key === "a") toggleAdvanced();
        else if (event.key === "t" || event.key === "T") {
            var index = TOPICS.map(function (t) { return t.id; }).indexOf(currentTopic);
            var step = event.key === "t" ? 1 : TOPICS.length - 1;  // Shift+t goes back
            selectView(current, TOPICS[(index + step) % TOPICS.length].id, true);
        }
        else if (/^[1-9]$/.test(event.key) && VIEWS[Number(event.key) - 1]) selectView(VIEWS[Number(event.key) - 1].id, currentTopic, true);
    });
    setInterval(function () {
        updateStatus();  // Keep "updated Xm ago" honest
        autoRefreshIfDue();
    }, 30000);

    renderBypass();
    renderSort();
    renderSiteIcons();
    renderAutoRefresh();
    renderBackAction();
    renderInstall();
    // A view in the URL wins (shared links); otherwise reopen the last one viewed
    var params = new URLSearchParams(location.search);
    var linked = params.has("region") || params.has("topic");
    var initialRegion = linked ? fromUrl("region", isView) || DEFAULT_VIEW : (isView(read("retroRegion")) ? read("retroRegion") : HOME_VIEW);
    var initialTopic = linked ? fromUrl("topic", isTopic) || DEFAULT_TOPIC : (isTopic(read("retroTopic")) ? read("retroTopic") : DEFAULT_TOPIC);
    history.replaceState({ region: initialRegion, topic: initialTopic }, "", location.href);  // The address stays as typed
    current = null;
    selectView(initialRegion, initialTopic, false);
})();
