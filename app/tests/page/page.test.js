// What a reader sees and can do on the page (static/retro.js): tabs, order, search, theme, failures.
const test = require("node:test");
const assert = require("node:assert/strict");
const page_ = require("./page");
const { item, REGIONS, TOPICS } = page_;

// Every page a test opens is closed when the test ends, passed or failed: its timers would keep node running
const opened = [];
async function open(views, options) {
    const page = await page_.open(views, options);
    opened.push(page);
    return page;
}
test.afterEach(() => { opened.splice(0).forEach((page) => page.window.close()); });

const [ALL, REGION] = REGIONS, [EVERYTHING, TOPIC] = TOPICS;
const MIX = [item("Storm warning issued", 30), item("Markets close higher", 5), item("Old bridge reopens", 90)];
const VIEWS = {
    [ALL + "/" + EVERYTHING]: MIX,
    [REGION + "/" + EVERYTHING]: [item("Regional election called", 10), MIX[0]],
    [REGION + "/" + TOPIC]: [item("A regional topic story", 20, { summary: "Rain delayed the storm cleanup." })],
};

test("the starting tab lists its headlines in the server's order, numbered", async () => {
    const page = await open(VIEWS);
    assert.deepEqual(page.titles(), ["Storm warning issued", "Markets close higher", "Old bridge reopens"]);
    assert.deepEqual([...page.window.document.querySelectorAll("#list .num")].map((n) => n.textContent), ["1.", "2.", "3."]);
    assert.equal(page.tab("regions", null).getAttribute("aria-current"), "page");
});

test("s goes through newest, oldest, random and back to the mix, and the choice is remembered", async () => {
    const page = await open(VIEWS);
    await page.key("s");
    assert.deepEqual(page.titles(), ["Markets close higher", "Storm warning issued", "Old bridge reopens"]);
    assert.equal(page.$("sort-mode").textContent, "newest");
    await page.key("s");
    assert.deepEqual(page.titles(), ["Old bridge reopens", "Storm warning issued", "Markets close higher"]);
    await page.key("s");
    assert.equal(page.$("sort-mode").textContent, "random");
    assert.deepEqual(page.titles().sort(), MIX.map((i) => i.title).sort());
    await page.key("s");
    assert.deepEqual(page.titles(), MIX.map((i) => i.title));

    const again = await open(VIEWS, { stored: { retroSort: "oldest" } });
    assert.deepEqual(again.titles(), ["Old bridge reopens", "Storm warning issued", "Markets close higher"]);
});

test("a region tab and a topic key each open their own headlines, and the tab is remembered", async () => {
    const page = await open(VIEWS);
    await page.click(page.tab("regions", REGION));
    assert.deepEqual(page.titles(), ["Regional election called", "Storm warning issued"]);
    assert.equal(page.requests.at(-1), "/api/headlines?region=" + REGION);
    await page.key("t");
    assert.deepEqual(page.titles(), ["A regional topic story"]);
    assert.equal(page.requests.at(-1), "/api/headlines?region=" + REGION + "&topic=" + TOPIC);
    assert.equal(page.tab("topics", TOPIC).getAttribute("aria-current"), "page");
    assert.equal(page.window.location.search, "");  // The address doesn't name the tab

    const again = await open(VIEWS, { stored: { retroRegion: REGION, retroTopic: TOPIC } });
    assert.deepEqual(again.titles(), ["A regional topic story"]);
});

test("a number key opens that region, and a shared link opens its tab", async () => {
    const page = await open(VIEWS);
    await page.key("2");
    assert.deepEqual(page.titles(), ["Regional election called", "Storm warning issued"]);

    const linked = await open(VIEWS, { query: "?region=" + REGION + "&topic=" + TOPIC, stored: { retroRegion: ALL } });
    assert.deepEqual(linked.titles(), ["A regional topic story"]);
});

test("a visitor who hasn't picked a tab starts on the region the server guessed", async () => {
    const guessed = require("fs").readFileSync(process.env.PAGE_HTML, "utf8").split('<script id="home-data" type="application/json">')[1].split("</script>")[0];
    const page = await open(VIEWS);
    assert.equal(page.requests[0], "/api/headlines?region=" + JSON.parse(guessed));
});

test("d switches the theme and remembers it", async () => {
    const page = await open(VIEWS);
    const before = page.window.document.documentElement.getAttribute("data-theme");
    await page.key("d");
    const after = page.window.document.documentElement.getAttribute("data-theme");
    assert.deepEqual([before, after].sort(), ["dark", "light"]);
    assert.equal(page.window.localStorage.getItem("retroTheme"), after);
});

test("search finds a word in every tab's headlines and summaries, each headline once, saying where", async () => {
    const page = await open(VIEWS);
    await page.key("/");
    await page.type("storm");
    // The headline match first (it is in two tabs, listed once), then the one found in a summary
    assert.deepEqual(page.titles(), ["Storm warning issued", "A regional topic story"]);
    const where = [...page.window.document.querySelectorAll("#list .where")].map((w) => w.textContent);
    assert.equal(where.length, 2);
    assert.ok(where[1].includes(" / "), where[1]);  // A region and its topic
    assert.ok(page.window.document.querySelector("#list .snippet").textContent.includes("storm cleanup"));
    const marked = (selector) => [...page.window.document.querySelectorAll("#list " + selector + " mark")].map((m) => m.textContent);
    assert.deepEqual([marked(".title"), marked(".snippet")], [["Storm"], ["storm"]]);
    assert.equal(page.requests.filter((url) => url.startsWith("/api/search-index")).length, 1);
    await page.type("warn");
    assert.deepEqual(page.titles(), ["Storm warning issued"]);  // The start of a word
    await page.type("arning");
    assert.deepEqual(page.titles(), []);  // Not the middle of one
});

test("a link that isn't a web address is never listed", async () => {
    const page = await open({ [ALL + "/" + EVERYTHING]: [item("Bad link", 1, { link: "javascript:alert(1)" }), item("Good link", 2)] });
    assert.deepEqual(page.titles(), ["Good link"]);
    assert.equal(page.window.document.querySelector("#list a.title").protocol, "https:");
});

test("a headline's text is shown as text, whatever it holds", async () => {
    const page = await open({ [ALL + "/" + EVERYTHING]: [item("<img src=x onerror=alert(1)> & <b>bold</b>", 1, { link: "https://pub.example/x" })] });
    assert.deepEqual(page.titles(), ["<img src=x onerror=alert(1)> & <b>bold</b>"]);
    assert.equal(page.window.document.querySelectorAll("#list img:not(.site-icon), #list b").length, 0);
});

test("a tab that can't be loaded says so, and r loads it once the server answers", async () => {
    const page = await open(VIEWS, { failing: true });
    assert.deepEqual(page.titles(), []);
    assert.equal(page.$("notice").hidden, false);
    assert.match(page.$("notice").textContent, /could not load .*Headlines are not available yet/);
    page.failing = false;
    await page.key("r");
    assert.deepEqual(page.titles(), MIX.map((i) => i.title));
    assert.equal(page.$("notice").hidden, true);
});

test("an empty tab says there are no headlines", async () => {
    const page = await open({});
    assert.equal(page.$("notice").hidden, false);
    assert.match(page.$("notice").textContent, /no headlines for /);
});

// A canvas that records what is written on it (jsdom draws nothing), and a download or share sheet that records the file
function snapStandIns(window, seen) {
    seen.drawn = [];
    window.HTMLCanvasElement.prototype.getContext = () => new Proxy({
        measureText: (text) => ({ width: text.length * 8 }),
        fillText: (text) => seen.drawn.push(text),
    }, { get: (target, name) => name in target ? target[name] : () => {}, set: () => true });
    window.HTMLCanvasElement.prototype.toBlob = (done) => done(new window.Blob(["png"], { type: "image/png" }));
    window.URL.createObjectURL = () => "blob:snap";
    window.URL.revokeObjectURL = () => {};
    window.HTMLAnchorElement.prototype.click = function () { if (this.download) seen.download = this.download; };
}

const SUMMARISED = { [ALL + "/" + EVERYTHING]: [item("Félix Gallardo dies at 80", 30, { source: "Latin Times", summary: "The drug lord died in hospital after a long illness." })] };

async function openSummary(page) {
    await page.click([...page.window.document.querySelectorAll("#list .meta button")].find((b) => b.textContent === "summary"));
    return page.window.document.querySelector("#list .summary small button");
}

test("snap saves the headline and its summary as an image, without the page's buttons", async () => {
    const seen = {};
    const page = await open(SUMMARISED, { stored: { retroSiteIcons: "off" }, setup: (w) => snapStandIns(w, seen) });
    const button = await openSummary(page);
    assert.equal(button.textContent, "snap");
    await page.click(button);
    assert.equal(seen.download, "news-felix-gallardo-dies-at-80.png");
    const drawn = seen.drawn.join(" ");
    for (const part of ["Félix Gallardo dies at 80", "(pub.example)", "Latin Times · 30m ago", "long illness.",
                        "machine summary · may contain errors", "news.txt | news.freestuffsthat.work"]) {
        assert.ok(drawn.includes(part), part + " in: " + drawn);
    }
    assert.ok(!/snap|hide summary|discuss/.test(drawn), drawn);
});

// A clipboard that records what is copied to it (jsdom has none)
function clipboardStandIn(window, seen) {
    Object.defineProperty(window.navigator, "clipboard", { configurable: true,
        // Answering only after the image is saved, as a real clipboard can
        value: { writeText: (text) => new Promise((done) => setTimeout(() => { seen.copied = text; done(); }, 30)) } });
}

const LINK = SUMMARISED[ALL + "/" + EVERYTHING][0].link;

// Snaps on a desktop (a download) or a phone (the share sheet), in a snap mode; what was saved, shared and copied
async function snapIn(mode, { phone = false, clipboard = true } = {}) {
    const seen = {};
    const page = await open(SUMMARISED, { stored: Object.assign({ retroSiteIcons: "off" }, mode && { retroSnippet: mode }), setup: (w) => {
        snapStandIns(w, seen);
        if (clipboard) clipboardStandIn(w, seen);
        else Object.defineProperty(w.navigator, "clipboard", { configurable: true, value: { writeText: () => Promise.reject(new Error("denied")) } });
        if (phone) {
            w.matchMedia = (query) => ({ matches: query === "(pointer: coarse)", addEventListener() {}, addListener() {} });
            w.navigator.canShare = () => true;
            w.navigator.share = (data) => {
                seen.shared = Array.from(data.files, (f) => f.name + " " + f.type);
                seen.sharedText = data.text;
                return Promise.resolve();
            };
        }
    } });
    const button = await openSummary(page);
    await page.click(button);
    await page.settle(60);
    seen.button = button.textContent;
    return seen;
}

test("by default (image) snap saves the image and copies the headline's link, but shares only the image", async () => {
    const seen = await snapIn(null);
    assert.equal(seen.download, "news-felix-gallardo-dies-at-80.png");
    assert.equal(seen.copied, LINK);
    assert.equal(seen.button, "snap");  // Unchanged: no "link copied"
    const phone = await snapIn(null, { phone: true });
    assert.deepEqual(phone.shared, ["news-felix-gallardo-dies-at-80.png image/png"]);
    assert.equal(phone.sharedText, undefined);
    assert.equal(phone.copied, LINK);
});

test("snap: url shares the headline's link as the caption, and copies nothing", async () => {
    const seen = await snapIn("url");
    assert.equal(seen.download, "news-felix-gallardo-dies-at-80.png");
    assert.equal(seen.copied, undefined);
    assert.equal(seen.button, "snap");
    const phone = await snapIn("url", { phone: true });
    assert.equal(phone.sharedText, LINK);  // WhatsApp's caption
    assert.equal(phone.copied, undefined);
});

test("snap: text copies and shares the headline with its link", async () => {
    const seen = await snapIn("text");
    assert.equal(seen.copied, "Félix Gallardo dies at 80\n" + LINK);
    assert.equal(seen.button, "snap");
    const phone = await snapIn("text", { phone: true });
    assert.equal(phone.sharedText, "Félix Gallardo dies at 80\n" + LINK);
    assert.equal(phone.download, undefined);
});

test("snap still saves the image when the link can't be copied", async () => {
    const seen = await snapIn(null, { clipboard: false });
    assert.equal(seen.download, "news-felix-gallardo-dies-at-80.png");
    assert.equal(seen.button, "snap");
});

test("snap in advanced cycles image, url, text and off, is remembered, and goes back to image with reset", async () => {
    const page = await open(SUMMARISED);
    const toggle = page.$("snap-toggle");
    const labels = [toggle.textContent];
    for (let i = 0; i < 4; i++) {
        await page.click(toggle);
        labels.push(toggle.textContent);
    }
    assert.deepEqual(labels.map((l) => l.trim()), ["snap: image", "snap: url", "snap: text", "snap: off", "snap: image"]);
    assert.ok(labels.every((l) => l.length === labels[0].length), "the same width, so the row doesn't reflow");

    const off = await open(SUMMARISED, { stored: { retroSnippet: "off" } });
    assert.equal(await openSummary(off), null);  // No snap button
    await off.click(off.$("reset"));
    assert.equal(off.$("snap-toggle").textContent.trim(), "snap: image");

    const text = await open(SUMMARISED, { stored: { retroSnippet: "text" } });
    assert.equal(text.$("snap-toggle").textContent.trim(), "snap: text");
    const before = await open(SUMMARISED, { stored: { retroSnippet: "on" } });  // Chosen before the modes: the default
    assert.equal(before.$("snap-toggle").textContent.trim(), "snap: image");
});

test("a long headline and summary wrap within the image, a word too long for a line included", async () => {
    const seen = {};
    const long = "word ".repeat(40) + "x".repeat(120);
    const page = await open({ [ALL + "/" + EVERYTHING]: [item("A headline", 1, { summary: long })] },
        { stored: { retroSiteIcons: "off" }, setup: (w) => snapStandIns(w, seen) });
    await page.click(await openSummary(page));
    const pieces = seen.drawn.filter((t) => /^(word|x+)$/.test(t));
    assert.equal(pieces.filter((t) => t === "word").length, 40);
    assert.equal(pieces.filter((t) => /^x+$/.test(t)).join("").length, 120);  // Broken, not lost
    assert.ok(pieces.every((t) => t.length * 8 <= 640 - 2 * 28 - 14), "every piece fits the summary's width");
});


// The header moon at a moment, seen from a place ([latitude, longitude], or none: today's north/south flip)
const MOON_X = 7, MOON_CENTRE = 11.5;  // The disc's left edge in the icon, and its centre
async function moonAt(utc, position, south) {
    const page = await open(VIEWS, { moon: { south: Boolean(south), position: position || null },
        setup: (w) => { w.Date.now = () => Date.parse(utc); } });
    const pixels = [...page.$("moon").querySelectorAll("svg > rect")].filter((r) => /^(lit|shade|crater)$/.test(r.getAttribute("class")))
        .flatMap((r) => Array.from({ length: Number(r.getAttribute("width")) },  // A row of pixels, one by one
            (_, i) => ({ x: Number(r.getAttribute("x")) + i - MOON_X, y: Number(r.getAttribute("y")), kind: r.getAttribute("class") })));
    return pixels;
}

// Which way the lit part of the disc lies from its centre, in degrees: 0 right, 90 up, -90 down, 180 left
function litDirection(pixels) {
    const lit = pixels.filter((p) => p.kind !== "shade");
    const x = lit.reduce((sum, p) => sum + p.x - MOON_CENTRE, 0), y = lit.reduce((sum, p) => sum + MOON_CENTRE - p.y, 0);
    return Math.atan2(y, x) * 180 / Math.PI;
}
const near = (angle, expected, within) => Math.abs(((angle - expected) % 360 + 540) % 360 - 180) <= within;

test("an evening crescent is tilted as seen from the visitor's latitude", async () => {
    // 13 Dec 2026, a waxing crescent, about 7 pm local
    const london = litDirection(await moonAt("2026-12-13T19:00:00Z", [51.5, -0.1]));
    const quito = litDirection(await moonAt("2026-12-14T00:15:00Z", [-0.2, -78.5]));
    const sydney = litDirection(await moonAt("2026-12-13T08:55:00Z", [-33.9, 151.2]));
    assert.ok(near(london, -45, 25), "lit lower right in London: " + london);
    assert.ok(near(quito, -90, 25), "lit from below near the equator: " + quito);
    assert.ok(near(sydney, -135, 25), "lit lower left in Sydney: " + sydney);
});

test("the moon turns through the night", async () => {
    // A first quarter seen from Chennai on 18 Nov 2026: lit on the right at dusk, from below by midnight
    const dusk = litDirection(await moonAt("2026-11-18T12:39:00Z", [13.1, 80.3]));
    const midnight = litDirection(await moonAt("2026-11-18T18:39:00Z", [13.1, 80.3]));
    assert.ok(near(dusk, 0, 25), "lit on the right at 6 pm: " + dusk);
    assert.ok(near(midnight, -90, 25), "lit from below at midnight: " + midnight);
});

test("the moon's face turns too: upside down in the south", async () => {
    // The full moon of 24 Nov 2026 near midnight, high in the north of Sydney's sky and the south of London's
    // Which way the craters lie from the disc's centre, in degrees: half a turn apart from London and Sydney
    const craters = (pixels) => litDirection(pixels.filter((p) => p.kind === "crater"));
    const london = craters(await moonAt("2026-11-25T00:00:00Z", [51.5, -0.1]));
    const sydney = craters(await moonAt("2026-11-24T14:00:00Z", [-33.9, 151.2]));
    assert.ok(near(sydney, london + 180, 20), sydney + " vs " + london);
});

test("without the visitor's position the moon is lit on the right while waxing, mirrored in the south", async () => {
    const north = litDirection(await moonAt("2026-12-13T19:00:00Z", null, false));
    const south = litDirection(await moonAt("2026-12-13T19:00:00Z", null, true));
    assert.ok(near(north, 0, 5), "north: " + north);
    assert.ok(near(south, 180, 5), "south: " + south);
});

// The header's sky at a moment, seen from a place: its pixels by kind, its label and its title
async function skyAt(utc, position) {
    const page = await open(VIEWS, { moon: { south: position[0] < 0, position: position }, setup: (w) => { w.Date.now = () => Date.parse(utc); } });
    const kinds = {};
    for (const r of page.$("moon").querySelectorAll("svg rect[class]")) {
        const kind = r.getAttribute("class").split(" ")[0];
        for (let i = 0; i < Number(r.getAttribute("width")); i++) (kinds[kind] = kinds[kind] || []).push({ x: Number(r.getAttribute("x")) + i, y: Number(r.getAttribute("y")) });
    }
    return { kinds, label: page.$("moon").querySelector("span").textContent, title: page.$("moon").title, svg: page.$("moon").querySelector("svg") };
}
const BENGALURU = [13.0, 77.6], LONDON = [51.5, -0.1], QUITO = [-0.2, -78.5], SYDNEY = [-33.9, 151.2], TROMSO = [69.6, 19.0];

test("a moon that has set sinks behind the horizon, and the tooltip says when it rises", async () => {
    const sky = await skyAt("2026-11-16T19:45:00Z", BENGALURU);  // 1:15 am: the moon set before midnight
    // The horizon is on the label's baseline (the icon is centred on the label: its baseline falls 16 px down)
    assert.ok(sky.kinds.horizon.every((p) => p.y === 16), "a horizon line on row 16");
    const moon = [...(sky.kinds.lit || []), ...(sky.kinds.shade || []), ...(sky.kinds.crater || [])];
    assert.equal(Math.min(...moon.map((p) => p.y)), 4, "the moon's top half shows above it");
    assert.equal(Math.max(...moon.map((p) => p.y)), 15);
    assert.ok(sky.kinds.star.every((p) => p.y < 16), "no stars below the horizon");
    assert.match(sky.title, /moonrise \d\d:\d\d/);
    assert.match(sky.title, /sunrise \d\d:\d\d/);
});

test("by day a small sun shines and the stars are gone; at night the stars are out and the sun isn't", async () => {
    const day = await skyAt("2026-11-16T19:45:00Z", QUITO);  // 2:45 pm
    assert.ok(day.kinds.sun && day.kinds.sun.length >= 9);
    assert.equal(day.kinds.star, undefined);
    assert.match(day.title, /sunset \d\d:\d\d/);
    assert.match(day.title, /moonset \d\d:\d\d/);
    const night = await skyAt("2026-11-16T21:00:00Z", LONDON);
    assert.equal(night.kinds.star.length, 4);
    assert.equal(night.kinds.sun, undefined);
    assert.equal(night.kinds.horizon, undefined);
});

test("the brightest planet up is a steady pixel, named in the tooltip", async () => {
    const sky = await skyAt("2026-11-16T19:45:00Z", BENGALURU);
    assert.equal(sky.kinds.planet.length, 1);
    assert.match(sky.title, /Jupiter in the east/);
});

test("a meteor shower's streaks, while it peaks and its radiant is up", async () => {
    const peak = await skyAt("2026-11-17T22:30:00Z", BENGALURU);
    assert.ok(peak.svg.querySelector(".meteor rect"));
    assert.match(peak.title, /Leonids meteor shower/);
    const after = await skyAt("2026-11-25T22:30:00Z", BENGALURU);
    assert.equal(after.svg.querySelector(".meteor"), null);
});

test("a lunar eclipse turns the moon red where it's in Earth's shadow", async () => {
    const total = await skyAt("2028-12-31T16:50:00Z", BENGALURU);  // Mid-eclipse, the moon high
    assert.equal(total.label, "total lunar eclipse");
    assert.ok(total.kinds.blood.length > 150 && !total.kinds.lit, JSON.stringify(Object.keys(total.kinds)));
    assert.match(total.title, /Total lunar eclipse/);
    const sydney = await skyAt("2028-12-31T16:50:00Z", [-33.9, 151.2]);  // 3:50 am: the moon still up there too
    assert.equal(sydney.label, "total lunar eclipse");
    const quito = await skyAt("2028-12-31T16:50:00Z", QUITO);  // Midday: the moon is down
    assert.notEqual(quito.label, "total lunar eclipse");
});

test("a solar eclipse puts the sun in the moon's place with the moon over it", async () => {
    const sydney = await skyAt("2028-07-22T04:01:00Z", SYDNEY);  // Totality there: 1:59 to 2:03 pm
    assert.equal(sydney.label, "total solar eclipse");
    assert.ok(sydney.kinds.eclipsed.length > 100, String(sydney.kinds.eclipsed.length));
    assert.ok(sydney.kinds.corona.length > 20, "the corona rings the moon in totality");
    assert.equal(sydney.kinds.lit, undefined);
    const partial = await skyAt("2028-07-22T03:20:00Z", SYDNEY);
    assert.match(partial.label, /^partial solar eclipse \d+%$/);
    assert.ok(partial.kinds["sun-disc"].length > 10 && partial.kinds.eclipsed.length > 10);
});

test("in polar night the sun doesn't rise, and the tooltip says so", async () => {
    const sky = await skyAt("2026-12-10T11:00:00Z", TROMSO);
    assert.match(sky.title, /the sun doesn't rise today/);
    assert.equal(sky.kinds.sun, undefined);
});

test("without the visitor's position the header shows the moon alone, as before", async () => {
    const page = await open(VIEWS, { moon: { south: false, position: null }, setup: (w) => { w.Date.now = () => Date.parse("2026-11-16T19:45:00Z"); } });
    const svg = page.$("moon").querySelector("svg");
    assert.equal(svg.querySelectorAll(".star").length, 4);
    assert.equal(svg.querySelector(".sun, .horizon, .planet, .meteor"), null);
    assert.doesNotMatch(page.$("moon").title, /rise|set /);
});

test("if sky.js doesn't load, the page works without the moon", async () => {
    const page = await open(VIEWS, { setup: (w) => { Object.defineProperty(w, "Sky", { get: () => undefined, set: () => {} }); } });
    assert.deepEqual(page.titles(), ["Storm warning issued", "Markets close higher", "Old bridge reopens"]);
    assert.equal(page.$("moon").querySelector("svg"), null);
});

test("the icon is redrawn only when what it shows changes, not on every update", async () => {
    let now = Date.parse("2026-11-16T19:45:00Z");  // Bengaluru, Jupiter up
    const ticks = [];  // The page's 30-second timer, run by hand
    const page = await open(VIEWS, { moon: { south: false, position: [13.0, 77.6] },
        setup: (w) => { w.Date.now = () => now; w.setInterval = (fn) => { ticks.push(fn); return 0; }; } });
    const drawn = new Set([page.$("moon").querySelector("svg")]);
    for (let minute = 0; minute < 10; minute++) {  // The planet moves a quarter of a degree a minute: not a pixel
        now += 60000;
        ticks.forEach((tick) => tick());
        drawn.add(page.$("moon").querySelector("svg"));
    }
    assert.ok(drawn.size <= 3, drawn.size + " drawings in 10 minutes");  // A 3-degree step of the tilt now and then
    const last = page.$("moon").querySelector("svg");
    now += 3 * 3600000;  // Hours on, the sky has changed
    ticks.forEach((tick) => tick());
    assert.notEqual(page.$("moon").querySelector("svg"), last);
});

// The header icon must come out byte-identical after the pixel moon moves to moonart.js
const SNAPSHOT = require("path").join(__dirname, "header-moon-snapshot.json");
const SCENES = [["2026-11-19T00:00:00Z", [51.5, -0.1]], ["2026-11-24T14:00:00Z", [-33.9, 151.2]], ["2026-12-09T01:00:00Z", null],
    ["2026-12-17T05:00:00Z", null, true], ["2027-08-02T10:07:00Z", [25.7, 32.6]], ["2028-01-12T04:13:00Z", [51.5, -0.1]]];
test("the header moon is unchanged", async () => {
    const seen = {};
    for (const [utc, position, south] of SCENES) {
        const page = await open(VIEWS, { moon: { south: Boolean(south), position: position }, setup: (w) => { w.Date.now = () => Date.parse(utc); } });
        seen[utc] = page.$("moon").innerHTML;
    }
    if (process.env.WRITE_MOON_SNAPSHOT) require("fs").writeFileSync(SNAPSHOT, JSON.stringify(seen, null, 1));
    assert.deepEqual(seen, JSON.parse(require("fs").readFileSync(SNAPSHOT, "utf8")));
});

// The first tab's headlines are asked for by the page itself, before its scripts have even arrived (they download
// alongside them), and the script takes that answer: one request, for the tab the script opens
const headlineRequests = (page) => page.requests.filter((url) => url.startsWith("/api/headlines"));

test("the first tab's headlines are asked for before the page's scripts run", async () => {
    let skyLoaded = null;
    const page = await open(VIEWS, { setup: (w) => {
        const fetch = w.fetch;
        w.fetch = (url, options) => { if (skyLoaded === null && String(url).startsWith("/api/headlines")) skyLoaded = typeof w.Sky !== "undefined"; return fetch(url, options); };
    } });
    assert.equal(skyLoaded, false);
    assert.deepEqual(headlineRequests(page), ["/api/headlines?region=" + REGIONS[0]]);
    assert.ok(page.titles().length > 0);
});

test("one request for the first tab, whichever it is", async () => {
    const cases = [
        [{}, "", "/api/headlines?region=" + REGIONS[0]],
        [{ retroRegion: REGIONS[2], retroTopic: TOPICS[1] }, "", `/api/headlines?region=${REGIONS[2]}&topic=${TOPICS[1]}`],
        [{ retroRegion: REGIONS[2] }, `?region=${REGIONS[1]}&topic=${TOPICS[2]}`, `/api/headlines?region=${REGIONS[1]}&topic=${TOPICS[2]}`],
        [{ retroRegion: "atlantis", retroTopic: "astrology" }, "", "/api/headlines?region=" + REGIONS[0]],
    ];
    for (const [stored, query, expected] of cases) {
        const page = await open(VIEWS, { stored: stored, query: query });
        assert.deepEqual(headlineRequests(page), [expected], JSON.stringify(stored) + query);
    }
});
