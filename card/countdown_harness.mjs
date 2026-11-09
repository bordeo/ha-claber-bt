function fmtRemain(iso, now = Date.now()) {
  if (!iso || iso === "unknown" || iso === "unavailable") return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  let s = Math.max(0, Math.floor((t - now) / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function countdownRemain({ openLines, prog, ends, now }) {
  const active =
    (openLines && openLines.length) ||
    (prog && prog !== "none" && prog !== "unknown" && prog !== "unavailable");
  if (!active) return null;
  return fmtRemain(ends, now);
}
const t0 = Date.parse("2026-11-09T21:00:00Z");
const ends = new Date(t0 + 5 * 60 * 1000).toISOString();
const cases = [
  ["start", { openLines: [1], prog: "none", ends, now: t0 }, "05:00"],
  ["tick", { openLines: [1], prog: "none", ends, now: t0 + 30000 }, "04:30"],
  ["stop_valves_closed_stale_ends", { openLines: [], prog: "none", ends, now: t0 + 35000 }, null],
  ["rerender", { openLines: [], prog: "none", ends, now: t0 + 40000 }, null],
  ["stale_advert_after_stop", { openLines: [], prog: "none", ends, now: t0 + 50000 }, null],
  ["ends_cleared", { openLines: [1], prog: "none", ends: "unknown", now: t0 }, null],
];
let failed = 0;
for (const [name, input, want] of cases) {
  const got = countdownRemain(input);
  const ok = got === want;
  console.log(ok ? "PASS" : "FAIL", name, "got", got, "want", want);
  if (!ok) failed++;
}
if (failed) process.exit(1);
console.log("CARD HARNESS PASS");
