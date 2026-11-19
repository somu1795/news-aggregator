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

test("the clock plays a day a second", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z" });
    assert.match(page.text("clock-label"), /now/);
    page.frame(1000);
    assert.match(page.text("clock-label"), /\+1 d\b/);
    assert.match(page.text("clock-label"), /20 nov 2026/);
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

// Where the lit pixels of the phase diagram lie, from its centre: positive on the right
function litSide(page) {
    const ops = page.$("phase").ops, last = ops.map((op) => op[0]).lastIndexOf("clearRect");  // The latest drawing
    const lit = ops.slice(last).filter((op) => op[0] === "fillRect" && op[1] === LIT);
    return lit.reduce((sum, op) => sum + op[2] + 0.5 - 12, 0);
}

test("the phase diagram is lit on the right while waxing, mirrored in the south", async () => {
    const north = await open({ now: "2026-11-19T00:00:00Z", moon: { south: false, position: null }, reducedMotion: true });
    assert.ok(litSide(north) > 0);
    const south = await open({ now: "2026-11-19T00:00:00Z", moon: { south: true, position: [-33.9, 151.2] }, reducedMotion: true });
    assert.ok(litSide(south) < 0);
});
