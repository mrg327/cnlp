// Run: node --test pi/extensions/cnlp/   (node >= 22.18 strips the types)
import test from "node:test";
import assert from "node:assert/strict";
import { parseSlash } from "./commands.ts";

const O = { cnlpRoot: "/home/x/cnlp", projectsDir: "/home/x/cnlp/projects", cwd: "/home/x/esp/synth" };
const p = (s) => parseSlash(s, O);

// [input, argv]
const OK = [
  ["status", ["status", "."]],
  ["  status  ", ["status", "."]],
  ["status .", ["status", "."]],
  ["cnlp status", ["status", "."]],
  ["audit", ["audit", "."]],
  ["evidence", ["evidence", "."]],
  ["evidence .", ["evidence", "."]],
  ["rules", ["rules"]],
  ["rules --needs-review", ["rules", "--needs-review"]],
  ["rules --family realtime", ["rules", "--family", "realtime"]],
  ["rules --family cross-pin --needs-review", ["rules", "--family", "cross-pin", "--needs-review"]],
  ["verify-plan", ["verify-plan", "."]],
  ["verify-plan R4", ["verify-plan", ".", "R4"]],
  ["verify-plan . F2", ["verify-plan", ".", "F2"]],
  ["doctor", ["doctor", "."]],
  ["config", ["config", "."]],
  ["check-pins", ["check-pins"]],
  ["check-pins --manifest m.json", ["check-pins", "--manifest", "m.json"]],
  ["check-fsm", ["check-fsm"]],
  ["lint", ["lint"]],
  ["render", ["render"]],
  ["try 'gpio 35 is input \"enc sw\" with internal pull-up.'", ["try", 'gpio 35 is input "enc sw" with internal pull-up.', "."]],
  ['try "gpio 4 is output \'led\' with no pull."', ["try", "gpio 4 is output 'led' with no pull.", "."]],
  ["try 'gpio 4 is output \"a\" with no pull.' .", ["try", 'gpio 4 is output "a" with no pull.', "."]],
];
for (const [inp, argv] of OK) {
  test(`allowed: ${inp}`, () => {
    const r = p(inp);
    assert.deepEqual(r.argv, argv, JSON.stringify(r));
    assert.equal(r.share, false);
  });
}

test("--share is stripped and flagged, anywhere", () => {
  assert.deepEqual(p("status --share"), { argv: ["status", "."], share: true });
  assert.deepEqual(p("--share audit"), { argv: ["audit", "."], share: true });
  assert.deepEqual(p("rules --share --needs-review"), { argv: ["rules", "--needs-review"], share: true });
});

test("help forms return text and run nothing", () => {
  for (const s of ["", "   ", "help", "--help", "-h", "help --share"]) {
    const r = p(s);
    assert.ok(typeof r.text === "string" && !r.argv, s);
  }
});

// Refused, with the message containing every needle.
const NO = [
  ["evidence add . R4 measured_scope pass", ["run this in your shell yourself", "cnlp evidence add"]],
  ["evidence add R4 x pass --value 1", ["yourself"]],
  ["measure . --record", ["yourself", "cnlp measure"]],
  ["flash", ["yourself", "cnlp flash"]],
  ["gate answer . q1 yes", ["yourself", "gate answer"]],
  ["gate review", ["yourself"]],
  ["sync --apply", ["yourself", "sync --apply"]],
  ["gen", ["yourself"]],
  ["ask 'what is RT1'", ["yourself", "'what is RT1'"]],
  ["translate", ["yourself"]],
  ["frobnicate", ["unknown"]],
  ["status; rm -rf ~", ["one plain command"]],
  ["status && rm -rf ~", ["one plain command"]],
  ["status | sh", ["one plain command"]],
  ["status > /etc/passwd", ["one plain command"]],
  ["status < /etc/passwd", ["one plain command"]],
  ["status $(id)", ["one plain command"]],
  ["status `id`", ["one plain command"]],
  ["status $HOME", ["one plain command"]],
  ["status &", ["one plain command"]],
  ["status\nrm -rf ~", ["one plain command"]],
  ["status # hi", ["one plain command"]],
  ["status *", ["one plain command"]],
  ["status /tmp/other", ["does not accept"]],
  ["status --json", ["does not accept"]],
  ["audit --save", ["does not accept"]],
  ["rules --sheet", ["does not accept"]],
  ["rules --family bogus", ["does not accept"]],
  ["verify-plan --out /etc/x", ["does not accept"]],
  ["verify-plan R4 R5", ["does not accept"]],
  ["verify-plan '$(id)'", ["does not accept"]],
  ["try", ["usage"]],
  ["try --json", ["usage"]],
  ["try 'gpio 4 is input \"a\" with no pull.' /etc", ["usage"]],
  ['try "gpio $(id)"', ["one plain command"]],
  ["try 'unterminated", ["one plain command"]],
  ["check-pins --manifest --out", ["does not accept"]],
  ["lint extra", ["does not accept"]],
  ["status;evidence add . R1 x pass", ["one plain command"]],
  ["cnlp evidence add . R1 x pass", ["yourself"]],
];
for (const [inp, needles] of NO) {
  test(`refused: ${JSON.stringify(inp)}`, () => {
    const r = p(inp);
    assert.ok(typeof r.refuse === "string", JSON.stringify(r));
    assert.ok(!r.argv);
    for (const n of needles) assert.ok(r.refuse.includes(n), `${n} not in: ${r.refuse}`);
  });
}

test("quoted metacharacters inside single quotes are data, not shell", () => {
  const r = p("try 'gpio 4 is output \"a;b\" with no pull.'");
  assert.deepEqual(r.argv, ["try", 'gpio 4 is output "a;b" with no pull.', "."]);
});

test("refusal hint quotes words so it is safe to paste", () => {
  const r = p("ask 'a b; c'");
  assert.ok(r.refuse.includes("cnlp ask 'a b; c'"), r.refuse);
  assert.ok(!r.refuse.endsWith(" ."), r.refuse);
});
