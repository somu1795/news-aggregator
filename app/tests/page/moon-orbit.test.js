// The moon page's orbit in 3D (static/moon.js, static/earth.js): Earth's face from the visitor's place, and
// turning it by hand, with momentum, back to the start when left alone
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

const paused = async (options) => { const page = await open(Object.assign({ now: "2026-11-19T00:00:00Z" }, options)); await page.key(" "); return page; };
const runFrames = (page, n, ms) => { for (let i = 0; i < n && page.frames(); i++) page.frame(ms); };

test("dragging the orbit turns it", async () => {
    const page = await paused();
    const before = page.image("orbit");
    assert.ok(before);
    await page.drag("orbit", 120, 0, { holdMs: 150 });
    runFrames(page, 3, 16);
    assert.notEqual(page.image("orbit"), before);
});

test("let go mid-swipe, it coasts, slows and stops", async () => {
    const page = await paused();
    await page.drag("orbit", 150, 0);
    assert.ok(page.frames() > 0, "it keeps moving with the clock paused");
    page.frame(16);
    const a = page.image("orbit");
    page.frame(32);
    assert.notEqual(page.image("orbit"), a);
    runFrames(page, 60, 100);
    assert.equal(page.frames(), 0);
});

test("held still before letting go, it stays put", async () => {
    const page = await paused();
    await page.drag("orbit", 150, 0, { holdMs: 150 });
    runFrames(page, 3, 16);
    assert.equal(page.frames(), 0);
});

test("with reduced motion it doesn't coast", async () => {
    const page = await open({ now: "2026-11-19T00:00:00Z", reducedMotion: true });
    await page.drag("orbit", 150, 0);
    runFrames(page, 3, 16);
    assert.equal(page.frames(), 0);
});

test("left alone after a turn, it glides back to where it started", async () => {
    const page = await paused();
    const start = page.image("orbit");
    await page.drag("orbit", 150, 40, { holdMs: 150 });
    runFrames(page, 3, 16);
    assert.notEqual(page.image("orbit"), start);
    assert.ok(page.timeouts.some((t) => t.ms === 10000));
    page.fireTimeouts();
    runFrames(page, 40, 100);
    assert.equal(page.frames(), 0);
    assert.equal(page.image("orbit"), start);
});

test("Earth shows the visitor's side: London and Sydney look different", async () => {
    const london = await paused({ moon: { south: false, position: [51.5, -0.1] } });
    const sydney = await paused({ moon: { south: true, position: [-33.9, 151.2] } });
    assert.notEqual(london.image("orbit"), sydney.image("orbit"));
});
