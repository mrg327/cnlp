// Run: node --test pi/extensions/cnlp/   (node >= 22.18 strips the types in guard.ts)
import test from "node:test";
import assert from "node:assert/strict";
import { checkBash, checkWrite } from "./guard.ts";

const ROOT = "/home/x/cnlp";
const PROJ = "/home/x/cnlp/projects";
const O = { cnlpRoot: ROOT, projectsDir: PROJ, cwd: "/home/x/esp/synth" };

// [command, blocked?]
const BASH = [
  // --- must pass: read-only cnlp and ordinary work
  ["cnlp status .", false],
  ["cnlp audit", false],
  ["cnlp --for-model status .", false],
  ["cnlp try 'gpio 35 is input \"enc sw\" with internal pull-up.' .", false],
  ['cnlp ask "how do I evidence add"', false],
  ['cnlp ask "what does gate answer do? and flash? measure --record?"', false],
  ["cnlp evidence .", false],
  ["cnlp evidence . --json", false],
  ["cnlp gate next .", false],
  ["cnlp measure . --scope-addr x", false],
  ["cnlp sync .", false],
  ["cnlp verify-plan . R4", false],
  ["cnlp rules --needs-review", false],
  ["uv run cnlp status .", false],
  ["echo 'run cnlp evidence add later' | cat", false],
  ["git status && git diff", false],
  ["ls -la components/", false],
  ["cat evidence.jsonl", false],
  ["grep -n R4 projects/synth/evidence.jsonl", false],
  ["idf.py build", false],
  ["idf.py -p /dev/ttyUSB0 monitor", false],
  ["esptool.py chip_id", false],
  ["espefuse.py summary", false],
  ["sed -i 's/a/b/' main/audio.c", false],
  ["sed -n '1,5p' evidence.jsonl", false],
  ["cp evidence.jsonl /tmp/backup.jsonl", false],
  ["echo hi > /tmp/out.txt", false],
  ["python3 -c 'print(1)'", false],
  ["uv run pytest -k cnlp", false],
  ["cd main && cnlp status .", false],
  // --- must block: recording commands, every launcher shape
  ["cnlp evidence add . R4 measured_scope pass", true],
  ["cnlp   evidence    add . R4", true],
  ["cnlp 'evidence' \"add\" . R4", true],
  ["c'n'lp ev\\idence add .", true],
  ["cnlp --project . evidence add R4", true],
  ["cnlp --for-model --json evidence . add", true],
  ["cnlp ev add . R4", true],
  ["cnlp flash .", true],
  ["cnlp gate answer . G1 yes", true],
  ["cnlp gate review .", true],
  ["cnlp measure . R4 --record . R4", true],
  ["cnlp measure . --rec . R4", true],
  ["cnlp measure . --record=x", true],
  ["cnlp sync . --apply", true],
  ["cnlp sync --app .", true],
  ["cd x; cnlp flash .", true],
  ["true && cnlp flash .", true],
  ["false || cnlp flash .", true],
  ["ls | cnlp flash .", true],
  ["cnlp status . ; cnlp flash .", true],
  ["cnlp status . &\ncnlp flash .", true],
  ["cnlp \\\n  evidence \\\n  add . R4", true],
  ["env FOO=1 cnlp flash .", true],
  ["env -i FOO=1 cnlp gate answer . G1 y", true],
  ["env -S 'cnlp flash .'", true],
  ["FOO=1 cnlp flash .", true],
  ["uv run cnlp evidence add . R4", true],
  ["uv run --project /home/x/cnlp cnlp flash .", true],
  ["uv run --project=/home/x/cnlp python -m cnlp flash .", true],
  ["uvx cnlp flash .", true],
  ["python -m cnlp evidence add . R4", true],
  ["python3.12 -m cnlp.cli flash .", true],
  ["/home/x/cnlp/.venv/bin/cnlp flash .", true],
  ["./cnlp gate answer . G1 y", true],
  ["bash -c 'cnlp flash .'", true],
  ["sh -c \"cnlp evidence add . R4\"", true],
  ["bash -lc 'cd x && cnlp flash .'", true],
  ["bash -c \"bash -c 'cnlp flash .'\"", true],
  ["sudo cnlp flash .", true],
  ["sudo -u root cnlp flash .", true],
  ["timeout 5 cnlp flash .", true],
  ["nohup cnlp flash . &", true],
  ["xargs -n1 cnlp flash <<< x", true],
  ["find . -name x -exec cnlp flash . \;", true],
  ["echo $(cnlp flash .)", true],
  ["echo `cnlp flash .`", true],
  ["echo \"$(cnlp flash .)\"", true],
  ["x=$(cnlp evidence add . R4)", true],
  ["$(which cnlp) flash .", true],
  ["$CNLP gate answer . G1 y", true],
  ["eval 'cnlp flash .'", true],
  ["eval cnlp flash .", true],
  ["echo 'cnlp flash .' | bash", true],
  ["bash <<EOF\ncnlp flash .\nEOF", true],
  ["bash <<< 'cnlp flash .'", true],
  ["cat <<EOF\n$(cnlp flash .)\nEOF", true],
  ["if true; then cnlp flash .; fi", true],
  ["for i in 1 2; do cnlp evidence add . R$i; done", true],
  ["python3 -c \"import subprocess; subprocess.run(['cnlp','evidence','add','.','R4'])\"", true],
  ["python3 -c \"from cnlp.cli import main; main(['flash','.'])\"", true],
  // --- must block: hardware
  ["esptool.py --port /dev/ttyUSB0 write_flash 0x0 app.bin", true],
  ["python -m esptool write-flash 0x0 app.bin", true],
  ["esptool erase_flash", true],
  ["espefuse.py burn_efuse FOO", true],
  ["idf.py flash", true],
  ["idf.py -p /dev/ttyUSB0 flash monitor", true],
  ["idf.py app-flash", true],
  ["idf.py erase-flash", true],
  ["make flash", true],
  ["ninja -C build flash", true],
  ["bash -c 'idf.py flash'", true],
  // --- must block: protected files via bash
  ["echo x >> projects/synth/evidence.jsonl", true],
  ["echo x > /home/x/cnlp/projects/synth/evidence.jsonl", true],
  ["echo x>evidence.jsonl", true],
  ["echo x &> evidence.jsonl", true],
  ["cat <<EOF > evidence.jsonl\nx\nEOF", true],
  ["> evidence.jsonl", true],
  ["echo x | tee -a evidence.jsonl", true],
  ["tee decisions.jsonl < x", true],
  ["echo x >> docs/decisions.md", true],
  ["echo x >> decisions.jsonl", true],
  ["sed -i 's/pass/fail/' projects/synth/evidence.jsonl", true],
  ["sed -i.bak 's/a/b/' evidence.jsonl", true],
  ["sed -ni 's/a/b/p' evidence.jsonl", true],
  ["perl -pi -e 's/a/b/' evidence.jsonl", true],
  ["cp /tmp/x evidence.jsonl", true],
  ["cp -t /home/x/cnlp/src/cnlp /tmp/x.py", true],
  ["mv /tmp/x /home/x/cnlp/src/cnlp/rules/review.yaml", true],
  ["mv evidence.jsonl /tmp/", true],
  ["rm evidence.jsonl", true],
  ["rm -rf /home/x/cnlp/projects", true],
  ["rm -rf /home/x/cnlp/projects/synth", true],
  ["rm -rf /home/x/cnlp", true],
  ["truncate -s 0 evidence.jsonl", true],
  ["dd if=/dev/null of=evidence.jsonl", true],
  ["echo x > src/cnlp/verify.py", true],
  ["echo x > /home/x/cnlp/src/cnlp/rules/review.yaml", true],
  ["echo x > rules/review.yaml", true],
  ["echo x > ../../esp/components/board/pins.h", true],
  ["echo x > components/board/board.h", true],
  ["echo x > /home/x/cnlp/pi/extensions/cnlp/guard.ts", true],
  ["sed -i 's/x/y/' /home/x/cnlp/src/cnlp/audit.py", true],
  ["sed -i 's/x/y/' ../../../../home/x/cnlp/src/cnlp/audit.py", true],
  ["bash -c 'echo x >> evidence.jsonl'", true],
  ["echo $(echo x >> evidence.jsonl)", true],
  ["echo x | sudo tee evidence.jsonl", true],
  ["echo x > evid*.jsonl", true],
  ["echo x > $HOME/p/evidence.jsonl", true],
  ["echo x > ~/p/decisions.md", true],
  ["git checkout -- src/cnlp/audit.py", true],
  ["find . -name evidence.jsonl -delete", true],
  ["python3 -c \"open('projects/synth/evidence.jsonl','a').write('x')\"", true],
  ["python3 -c \"open('evid'+'ence.jsonl','w').write('x')\"", true],
  ["python3 - <<'EOF'\nfrom pathlib import Path\nPath('evidence.jsonl').write_text('x')\nEOF", true],
  ["node -e \"require('fs').appendFileSync('decisions.jsonl','x')\"", true],
];

test("checkBash table", () => {
  const bad = [];
  for (const [cmd, blocked] of BASH) {
    const r = checkBash(cmd, O);
    if ((r !== null) !== blocked) bad.push(`${blocked ? "SHOULD BLOCK" : "SHOULD PASS"}: ${JSON.stringify(cmd)} -> ${r}`);
  }
  assert.deepEqual(bad, []);
  assert.ok(BASH.length >= 40);
});

test("checkBash works without roots or cwd (suffix heuristics)", () => {
  assert.equal(checkBash("cnlp status ."), null);
  assert.ok(checkBash("cnlp flash ."));
  assert.ok(checkBash("echo x > projects/synth/evidence.jsonl"));
  assert.ok(checkBash("echo x > src/cnlp/rules/review.yaml"));
});

test("relative paths resolve against cwd inside the cnlp repo", () => {
  const o = { ...O, cwd: "/home/x/cnlp" };
  assert.ok(checkBash("echo x > src/cnlp/new.py", o));
  assert.ok(checkBash("echo x > ./projects/a/spec.cnl", o));
  assert.equal(checkBash("echo x > /tmp/ok.txt", o), null);
  const o2 = { ...O, cwd: "/home/x/cnlp/src/cnlp/rules" };
  assert.ok(checkBash("echo x > other.yaml", o2));
});

const W = [
  ["/home/x/cnlp/projects/synth/evidence.jsonl", true],
  ["/home/x/cnlp/projects/synth/requirements.yaml", true],
  ["/home/x/cnlp/src/cnlp/rules/review.yaml", true],
  ["/home/x/cnlp/src/cnlp/audit.py", true],
  ["/home/x/cnlp/src/cnlp/../cnlp/audit.py", true],
  ["/home/x/cnlp/pi/extensions/cnlp/index.ts", true],
  ["/home/x/esp/synth/decisions.jsonl", true],
  ["/home/x/esp/synth/docs/decisions.md", true],
  ["/home/x/esp/synth/evidence.jsonl", true],
  ["/home/x/esp/synth/components/board/pins.h", true],
  ["/home/x/esp/synth/../synth/components/board/pins.h", true],
  ["evidence.jsonl", true],
  ["../../cnlp/src/cnlp/x.py", true],
  ["/home/x/esp/synth/main/audio.c", false],
  ["/home/x/esp/synth/CMakeLists.txt", false],
  ["main/audio.c", false],
  ["/home/x/cnlp/tests/test_x.py", false],
  ["/tmp/scratch.txt", false],
];

test("checkWrite table", () => {
  const bad = [];
  for (const [p, blocked] of W) {
    const r = checkWrite(p, ROOT, PROJ, "/home/x/esp/synth");
    if ((r !== null) !== blocked) bad.push(`${blocked ? "SHOULD BLOCK" : "SHOULD PASS"}: ${p} -> ${r}`);
  }
  assert.deepEqual(bad, []);
});

test("checkWrite follows symlinks", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "guard-"));
  const root = path.join(d, "cnlp");
  fs.mkdirSync(path.join(root, "src", "cnlp"), { recursive: true });
  fs.mkdirSync(path.join(d, "esp"));
  fs.symlinkSync(path.join(root, "src", "cnlp"), path.join(d, "esp", "lnk"));
  assert.ok(checkWrite(path.join(d, "esp", "lnk", "a.py"), root, path.join(root, "projects"), d));
  assert.equal(checkWrite(path.join(d, "esp", "main.c"), root, path.join(root, "projects"), d), null);
  fs.rmSync(d, { recursive: true });
});
