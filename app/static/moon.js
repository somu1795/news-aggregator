// The moon page (templates/moon.html): the moon now, its orbit around Earth and its phase as moving diagrams on
// one clock, and the next 30 days. The astronomy is sky.js's, the pixel moon moonart.js's. The helpers that touch
// no page are exported for Node, like sky.js's.
(function (root) {
    "use strict";
    var MoonPage = {};
    if (typeof module !== "undefined" && module.exports) module.exports = MoonPage;
    else root.MoonPage = MoonPage;
})(this);
