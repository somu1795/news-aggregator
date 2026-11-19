// What a visitor sees and can do on the moon page (static/moon.js): the moon now, the diagrams and their clock,
// the next 30 days
const test = require("node:test");
const assert = require("node:assert/strict");
const moonpage = require("./moonpage");

// Every page a test opens is closed when the test ends
const opened = [];
async function open(options) {
    const page = await moonpage.open(options);
    opened.push(page);
    return page;
}
test.afterEach(() => { while (opened.length) opened.pop().close(); });

const LONDON = [51.5, -0.1];

test("the moon now, seen from the visitor's place", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z", moon: { south: false, position: LONDON } });
    const text = page.text("now-text");
    assert.match(text, /waxing gibbous/);
    const percent = Number(text.match(/(\d+)% lit/)[1]);
    assert.ok(percent >= 65 && percent <= 73, text);
    assert.match(text, /farther than average/);
    assert.match(text, /rises|sets|up all day|down all day/);
    assert.ok(page.draws("now-moon") > 0);
    assert.deepEqual(page.errors, []);
});

test("without the visitor's place: no rise or set, the rest as usual", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z", moon: { south: false, position: null } });
    const text = page.text("now-text");
    assert.match(text, /waxing gibbous/);
    assert.match(text, /km/);
    assert.doesNotMatch(text, /rises|sets|all day/);
    assert.deepEqual(page.errors, []);
});

test("if the sky's scripts don't load, the page says so and nothing breaks", async () => {
    const page = await open({ noSky: true });
    assert.equal(page.$("moon-notice").hidden, false);
    assert.match(page.text("moon-notice"), /the sky's script didn't load/);
    assert.deepEqual(page.errors, []);
});

// --- The diagrams and their clock ---
const LIT = "#d9a441";  // --moon in the light theme: the colour lit pixels are painted with

test("the clock plays 8 hours a second; 1, 2 and 3 pick an hour, 8 hours or a day a second", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    assert.match(page.text("clock-label"), /now/);
    page.frame(1000);
    assert.match(page.text("clock-label"), /\+8 h\b/);
    assert.equal(page.$("speed-2").getAttribute("aria-pressed"), "true");
    await page.key("3");
    page.frame(0);
    page.frame(1000);
    assert.match(page.text("clock-label"), /\+1 d 8 h\b/);
    assert.equal(page.$("speed-3").getAttribute("aria-pressed"), "true");
    await page.click(page.$("speed-1"));
    page.frame(0);
    page.frame(1000);
    assert.match(page.text("clock-label"), /\+1 d 9 h\b/);
});

test("space pauses: nothing moves, and no frames are asked for", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    await page.key(" ");
    const label = page.text("clock-label");
    page.frame(1000);
    assert.equal(page.text("clock-label"), label);
    assert.equal(page.frames(), 0);
    assert.match(page.text("play"), /play/);
    await page.key(" ");
    assert.ok(page.frames() > 0);
});

test("arrows step a day and pause; n goes back to now", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    await page.key("ArrowRight");
    assert.match(page.text("clock-label"), /\+1 d\b/);
    assert.equal(page.frames(), 0);
    await page.key("ArrowLeft");
    await page.key("ArrowLeft");
    assert.match(page.text("clock-label"), /-1 d\b/);
    await page.key("n");
    assert.match(page.text("clock-label"), /now/);
});

test("the buttons do what the keys do", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    await page.click(page.$("play"));
    assert.equal(page.frames(), 0);
    await page.click(page.$("step-forward"));
    await page.click(page.$("step-forward"));
    assert.match(page.text("clock-label"), /\+2 d\b/);
    await page.click(page.$("step-back"));
    assert.match(page.text("clock-label"), /\+1 d\b/);
    await page.click(page.$("now-button"));
    assert.match(page.text("clock-label"), /now/);
});

test("with reduced motion the clock starts paused", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z", reducedMotion: true });
    assert.equal(page.frames(), 0);
    assert.match(page.text("play"), /\[space\] play/);
});

test("a hidden tab asks for no frames, and starts again when shown", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    const document = page.window.document;
    let hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    document.dispatchEvent(new page.window.Event("visibilitychange"));
    assert.equal(page.frames(), 0);
    hidden = false;
    document.dispatchEvent(new page.window.Event("visibilitychange"));
    assert.ok(page.frames() > 0);
});

test("the phase diagram names the phase the clock is at, and both diagrams say what they show", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    for (let i = 0; i < 5; i++) await page.key("ArrowRight");
    const w = page.window;
    const expected = w.MoonArt.phaseName(w.Sky.elongation(Date.parse("2026-11-24T00:00:00Z")));
    const current = page.$("phase-names").querySelectorAll(".current");
    assert.equal(current.length, 1);
    assert.equal(current[0].textContent, expected);
    assert.equal(page.$("phase-names").children.length, 8);
    assert.match(page.$("orbit").getAttribute("aria-label"), new RegExp(expected));
    assert.match(page.$("orbit").getAttribute("aria-label"), /\+5 d/);
    assert.equal(page.$("orbit").getAttribute("role"), "img");
    assert.match(page.$("phase").getAttribute("aria-label"), new RegExp(expected));
    assert.ok(page.draws("orbit") > 0 && page.draws("phase") > 0);
});

// Where the lit pixels of a 24-pixel moon (the phase diagram's, or the big one's) lie, from its centre: positive on the right
function litSide(page, id) {
    const ops = page.$(id || "phase").ops, last = ops.map((op) => op[0]).lastIndexOf("clearRect");  // The latest drawing
    const lit = ops.slice(last).filter((op) => op[0] === "fillRect" && op[1] === LIT);
    return lit.reduce((sum, op) => sum + op[2] + 0.5 - 12, 0);
}

test("without the visitor's place, both moons are lit on the right while waxing, mirrored in the south", async () => {
    const north = await open({ now: "2026-11-19T00:00:00Z", moon: { south: false, position: null }, reducedMotion: true });
    assert.ok(litSide(north, "phase") > 0 && litSide(north, "now-moon") > 0);
    const south = await open({ now: "2026-11-19T00:00:00Z", moon: { south: true, position: null }, reducedMotion: true });
    assert.ok(litSide(south, "phase") < 0 && litSide(south, "now-moon") < 0);
});

test("a long gap between frames (a laptop asleep with the page open) moves the clock a second's worth at most", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    page.frame(8 * 3600 * 1000);
    assert.match(page.text("clock-label"), /\+8 h\b/);  // A second's worth
    assert.match(page.text("clock-label"), /2026/);
});

test("the diagrams fit the column, and shrink with it when the window narrows", async () => {
    let width = 1000;
    const page = await open({ reducedMotion: true, setup: (w) => {
        Object.defineProperty(w.HTMLElement.prototype, "clientWidth", { configurable: true,
            get() { return this.matches(".moon-diagrams figure, .moon-now") ? width : 0; } });
    } });
    assert.equal(parseFloat(page.$("orbit").style.width), 480 * 2);
    width = 250;
    page.window.dispatchEvent(new page.window.Event("resize"));
    await page.settle();
    assert.ok(parseFloat(page.$("orbit").style.width) <= 250, page.$("orbit").style.width);
    assert.ok(parseFloat(page.$("phase").style.width) <= 250);
});

// --- The next 30 days ---
const WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
const dayLabel = (time) => { const d = new Date(time); return WEEKDAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()]; };

test("the next 30 days, a small moon each, from today", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    const days = page.$("days").children;
    assert.equal(days.length, 30);
    assert.equal(days[0].querySelector(".moon-day-date").textContent, dayLabel(Date.parse("2026-11-19T00:00:00Z")));
    assert.equal(days[29].querySelector(".moon-day-date").textContent, dayLabel(Date.parse("2026-11-19T00:00:00Z") + 29 * 86400000));
    assert.ok([...days].every((day) => day.querySelector("canvas") && day.querySelector("canvas").ops.length > 0));
});

test("the next four phases, in order, to about 10 minutes", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    const lines = [...page.$("phases").children].map((li) => li.textContent);
    assert.equal(lines.length, 4);
    assert.match(lines[0], /^full moon/);
    assert.deepEqual(lines.map((l) => l.split(" · ")[0]), ["full moon", "last quarter", "new moon", "first quarter"]);
    for (const l of lines) {
        assert.match(l, /about/);
        assert.equal(Number(l.match(/(\d\d):(\d\d)$/)[2]) % 10, 0, l);
    }
});

test("left open, the page catches up every minute", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z", moon: { south: false, position: LONDON } });
    const firstDay = page.$("days").children[0].textContent, now = page.text("now-text"), phases = page.text("phases");
    page.setNow("2026-11-25T00:00:00Z");
    page.tick();
    assert.notEqual(page.$("days").children[0].textContent, firstDay);
    assert.notEqual(page.text("now-text"), now);
    assert.notEqual(page.text("phases"), phases);
    assert.ok(page.intervals.some((interval) => interval.ms === 60000));
});

// --- Theme and keys ---
test("the page follows the theme chosen on the news page", async () => {
    const page = await open({ stored: { retroTheme: "dark" } });
    assert.equal(page.window.document.documentElement.getAttribute("data-theme"), "dark");
});

test("a theme change redraws the pixels in its colours", async () => {
    const page = await open({ reducedMotion: true });
    const before = page.draws("now-moon") + page.draws("orbit") + page.draws("phase");
    page.window.document.documentElement.setAttribute("data-theme", "dark");
    await page.settle();
    assert.ok(page.draws("now-moon") + page.draws("orbit") + page.draws("phase") > before);
});

test("b goes back to the news", async () => {
    const page = await open();
    let followed = false;
    page.$("back").addEventListener("click", (event) => { followed = true; event.preventDefault(); });
    await page.key("b");
    assert.ok(followed);
});

test("keys with ctrl, alt or cmd are the browser's", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    const document = page.window.document;
    document.dispatchEvent(new page.window.KeyboardEvent("keydown", { key: "ArrowRight", ctrlKey: true, bubbles: true }));
    document.dispatchEvent(new page.window.KeyboardEvent("keydown", { key: "b", metaKey: true, bubbles: true }));
    await page.settle();
    assert.match(page.text("clock-label"), /now/);
});

test("the phase disc is at most half as wide as the orbit diagram", async () => {
    const page = await open({ reducedMotion: true });
    const width = (id) => parseFloat(page.$(id).style.width);
    assert.ok(width("orbit") > 0);
    assert.ok(width("phase") <= width("orbit") / 2, `${width("phase")} vs ${width("orbit")}`);
});
