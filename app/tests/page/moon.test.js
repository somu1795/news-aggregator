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
