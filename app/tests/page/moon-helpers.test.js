// The moon page's helpers (static/moon.js) that touch no page: the clock, phase times, text lines
const test = require("node:test");
const assert = require("node:assert/strict");
const MoonPage = require("../../static/moon.js");

const DAY = 86400000;

test("the next four phases, in order, within an hour of JPL's", () => {
    const phases = MoonPage.nextPhases(Date.parse("2026-11-19T00:00:00Z"));
    assert.deepEqual(phases.map((p) => p.name), ["full moon", "last quarter", "new moon", "first quarter"]);
    [1795532014, 1796105320, 1796777511, 1797486160].forEach((seconds, i) => {
        const minutes = Math.abs(phases[i].time / 1000 - seconds) / 60;
        assert.ok(minutes < 60, `${phases[i].name} off by ${minutes.toFixed(0)} min`);
    });
    const after = MoonPage.nextPhases(Date.parse("2026-12-24T02:00:00Z"));  // Just after a full moon
    assert.equal(after[0].name, "last quarter");
    assert.ok(Math.abs(after[0].time / 1000 - 1798657170) / 60 < 60);
});

test("the clock plays a day a second, and steps and resets", () => {
    const t0 = Date.parse("2026-11-19T00:00:00Z");
    const clock = MoonPage.createClock(t0, true);
    clock.advance(1000);
    assert.equal(clock.time(), t0 + DAY);
    clock.pause();
    clock.advance(1000);
    assert.equal(clock.time(), t0 + DAY);
    clock.play();
    clock.step(-1);
    assert.equal(clock.time(), t0);
    assert.equal(clock.playing, false);
    clock.toggle();
    clock.reset(t0 + 5);
    assert.equal(clock.time(), t0 + 5);
    assert.equal(clock.playing, true);
});

test("times round to 10 minutes; the moon's age", () => {
    assert.equal(MoonPage.roundTo10(Date.parse("2026-11-24T14:53:34Z")), Date.parse("2026-11-24T14:50:00Z"));
    assert.equal(MoonPage.roundTo10(Date.parse("2026-11-24T14:55:00Z")), Date.parse("2026-11-24T15:00:00Z"));
    const age = MoonPage.age(Date.parse("2026-11-24T14:53:34Z"));  // Full: about half way
    assert.ok(age > 14 && age < 15.6, String(age));
    // Ten days after JPL's new moon of 9 Dec 2026 00:51:51 UTC
    const ten = MoonPage.age(Date.parse("2026-12-19T00:51:51Z"));
    assert.ok(Math.abs(ten - 10) < 0.05, String(ten));
});

test("text lines", () => {
    assert.equal(MoonPage.distanceLine(389922), "389,922 km · farther than average");
    assert.equal(MoonPage.distanceLine(356779), "356,779 km · nearer than average");
    assert.equal(MoonPage.riseSetLine({ rise: null, set: null }, true, () => "x"), "up all day");
    assert.equal(MoonPage.riseSetLine({ rise: null, set: null }, false, () => "x"), "down all day");
    assert.equal(MoonPage.riseSetLine({ rise: null, set: 5 }, true, () => "03:10"), "sets 03:10");
    assert.equal(MoonPage.riseSetLine({ rise: 1, set: 2 }, true, (t) => (t === 1 ? "14:32" : "03:10")), "rises 14:32 · sets 03:10");
    assert.equal(MoonPage.compass(0), "north");
    assert.equal(MoonPage.compass(130), "south-east");
    assert.equal(MoonPage.compass(350), "north");
});

test("the canvas scale fits the column, and is at least 1", () => {
    assert.equal(MoonPage.scaleFor(300, 96), 3);
    assert.equal(MoonPage.scaleFor(50, 96), 1);
});

test("without a position, the place is guessed from the time zone and the hemisphere", () => {
    assert.deepEqual(MoonPage.guessPlace(-330, false), [30, 82.5]);  // India: UTC+5:30
    assert.deepEqual(MoonPage.guessPlace(0, true), [-30, 0]);
    assert.deepEqual(MoonPage.guessPlace(-840, false), [30, 180]);  // UTC+14: no further east than 180
});

test("today's rise and set: only those between today's midnights, on 23- and 25-hour days too", () => {
    const H = 3600000, start = 1000 * H;
    const riseSet = (from) => (from === start ? { rise: start + 2 * H, set: start + 24.5 * H } : { rise: start + 2 * H, set: start + 24.5 * H });
    assert.deepEqual(MoonPage.riseSetBetween(riseSet, start, start + 23 * H), { rise: start + 2 * H, set: null });  // Tomorrow's set is left out
    assert.deepEqual(MoonPage.riseSetBetween(riseSet, start, start + 25 * H), { rise: start + 2 * H, set: start + 24.5 * H });  // The 25th hour is in
    const late = (from) => (from === start ? { rise: null, set: null } : { rise: start + 24.5 * H, set: null });
    assert.deepEqual(MoonPage.riseSetBetween(late, start, start + 25 * H), { rise: start + 24.5 * H, set: null });  // Found from the end
});

test("how high the moon is, in words", () => {
    assert.equal(MoonPage.heightLine(19.4, 135, true), "now 19° up, in the south-east");
    assert.equal(MoonPage.heightLine(0.4, 95, true), "just above the horizon, in the east");
    assert.equal(MoonPage.heightLine(-5, 0, false), "below the horizon now");
});
