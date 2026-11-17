// Pure guard logic for the cnlp Pi extension: decides whether a bash command or a file write
// would record something in the user's name, touch hardware, or tamper with cnlp's records and
// checker. No Pi imports; runs under plain node (type stripping) so it can be unit-tested.
//
// Approach: parse the shell command into simple commands (quotes, `;` `&&` `||` `|`, `$(..)`,
// backticks, heredocs, redirections), recurse into `bash -c` / `eval` / substitutions, unwrap
// launchers (env, uv run, sudo, xargs, ...), then judge each command by its parsed words, never by
// a regex over the raw line. Quoted prose (`cnlp ask "how do I evidence add"`) is just an argument.
//
// Known limits (stated, not hidden): variables (`$X`) and globs in directory parts are not
// expanded; a script file the model wrote and then runs is not read (writing it is the gate).
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { posix } from "node:path";

export type Opts = { cnlpRoot?: string; projectsDir?: string; cwd?: string };

const MSG_CNLP =
  "That cnlp command records an approval, a measurement, a flash or a spec change in the user's name. " +
  "Show it to the user to run themselves.";
const MSG_HW = "Flashing, erasing and eFuse changes are the user's to do. Show the command for the user to run.";
const msgFile = (what: string) =>
  `Writing to ${what} is blocked: it is cnlp's record or checker, owned by the user. ` +
  "Describe the change in text and let the user apply it.";

// ---- shell parsing --------------------------------------------------------------------------

type Redir = { op: string; target: string };
type Cmd = { words: string[]; redirs: Redir[]; heredoc?: string; herestring?: string; pipedFrom?: Cmd };

function extractParen(t: string, i: number): { inner: string; end: number } {
  // t[i] is the first char after "(" ; returns inner text and index just past the closing ")".
  let depth = 1;
  let j = i;
  while (j < t.length) {
    const c = t[j];
    if (c === "\\") { j += 2; continue; }
    if (c === "'") { const k = t.indexOf("'", j + 1); j = k < 0 ? t.length : k + 1; continue; }
    if (c === '"') {
      j++;
      while (j < t.length && t[j] !== '"') j += t[j] === "\\" ? 2 : 1;
      j++;
      continue;
    }
    if (c === "(") depth++;
    else if (c === ")" && --depth === 0) return { inner: t.slice(i, j), end: j + 1 };
    j++;
  }
  return { inner: t.slice(i), end: t.length };
}

function extractBacktick(t: string, i: number): { inner: string; end: number } {
  let j = i;
  while (j < t.length && t[j] !== "`") j += t[j] === "\\" ? 2 : 1;
  return { inner: t.slice(i, j).replace(/\\`/g, "`"), end: j + 1 };
}

function harvestSubs(body: string): string[] {
  const subs: string[] = [];
  for (let i = 0; i < body.length; i++) {
    if (body[i] === "\\") { i++; continue; }
    if (body[i] === "$" && body[i + 1] === "(") {
      const r = extractParen(body, i + 2); subs.push(r.inner); i = r.end - 1;
    } else if (body[i] === "`") {
      const r = extractBacktick(body, i + 1); subs.push(r.inner); i = r.end - 1;
    }
  }
  return subs;
}

function ansiC(s: string): string {
  return s.replace(/\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|n|t|r|\\|'|")/g, (_m, e: string) => {
    if (e[0] === "x") return String.fromCharCode(parseInt(e.slice(1), 16));
    if (/^[0-7]/.test(e)) return String.fromCharCode(parseInt(e, 8));
    return ({ n: "\n", t: "\t", r: "\r" } as Record<string, string>)[e] ?? e;
  });
}

export function parseScript(src: string): { cmds: Cmd[]; subs: string[] } {
  const t = src.replace(/\\\r?\n/g, "");
  const cmds: Cmd[] = [];
  const subs: string[] = [];
  const queue: { delim: string; strip: boolean; cmd: Cmd; quoted: boolean }[] = [];
  let cmd: Cmd = { words: [], redirs: [] };
  let last: Cmd | undefined;
  let word: string | null = null;
  let quoted = false;
  let pending: string | null = null;
  let i = 0;

  const endWord = () => {
    if (word === null) return;
    if (pending !== null) {
      const dup = pending.endsWith("&") && /^(\d+|-)$/.test(word);
      if (!dup) cmd.redirs.push({ op: pending, target: word });
      if (pending === "<<<") cmd.herestring = word;
      else if (pending === "<<" || pending === "<<-") queue.push({ delim: word, strip: pending === "<<-", cmd, quoted });
      pending = null;
    } else cmd.words.push(word);
    word = null;
    quoted = false;
  };
  const endCmd = (pipe = false) => {
    endWord();
    if (cmd.words.length || cmd.redirs.length) { cmds.push(cmd); last = cmd; }
    const next: Cmd = { words: [], redirs: [] };
    if (pipe && last) next.pipedFrom = last;
    cmd = next;
  };
  const sub = (inner: string) => { subs.push(inner); word = (word ?? "") + "$SUB"; };

  while (i < t.length) {
    const c = t[i];
    if (c === " " || c === "\t") { endWord(); i++; continue; }
    if (c === "\n") {
      endCmd();
      i++;
      for (const h of queue.splice(0)) {
        const lines: string[] = [];
        while (i < t.length) {
          let e = t.indexOf("\n", i);
          if (e < 0) e = t.length;
          const line = t.slice(i, e);
          i = Math.min(e + 1, t.length);
          if ((h.strip ? line.replace(/^\t+/, "") : line) === h.delim) break;
          lines.push(line);
        }
        h.cmd.heredoc = lines.join("\n");
        if (!h.quoted) subs.push(...harvestSubs(h.cmd.heredoc));
      }
      continue;
    }
    if (c === ";") { endCmd(); i++; continue; }
    if (c === "|") {
      if (t[i + 1] === "|") { endCmd(); i += 2; } else { endCmd(true); i += t[i + 1] === "&" ? 2 : 1; }
      continue;
    }
    if (c === "&") {
      if (t[i + 1] === ">") {
        endWord();
        pending = ">";
        i += t[i + 2] === ">" ? 3 : 2;
      } else { endCmd(); i += t[i + 1] === "&" ? 2 : 1; }
      continue;
    }
    if (c === "(" || c === ")") { endCmd(); i++; continue; }
    if (c === "#" && word === null) {
      while (i < t.length && t[i] !== "\n") i++;
      continue;
    }
    if (c === "<" || c === ">") {
      if (t[i + 1] === "(") {
        const r = extractParen(t, i + 2);
        subs.push(r.inner);
        word = (word ?? "") + "/dev/fd/63";
        i = r.end;
        continue;
      }
      if (word !== null && /^\d+$/.test(word)) { word = null; quoted = false; } else endWord();
      const ops = ["<<<", "<<-", "<<", "<&", "<>", ">>", ">|", ">&", ">", "<"];
      const op = ops.find((o) => t.startsWith(o, i)) as string;
      pending = op === ">|" ? ">" : op;
      i += op.length;
      continue;
    }
    if (c === "$") {
      if (t[i + 1] === "(") {
        const r = extractParen(t, i + 2); sub(r.inner); i = r.end; continue;
      }
      if (t[i + 1] === "'") {
        let j = i + 2;
        while (j < t.length && t[j] !== "'") j += t[j] === "\\" ? 2 : 1;
        word = (word ?? "") + ansiC(t.slice(i + 2, j));
        quoted = true;
        i = j + 1;
        continue;
      }
      if (t[i + 1] === '"') { i++; continue; }
      word = (word ?? "") + "$"; i++; continue;
    }
    if (c === "`") { const r = extractBacktick(t, i + 1); sub(r.inner); i = r.end; continue; }
    if (c === "'") {
      const k = t.indexOf("'", i + 1);
      word = (word ?? "") + t.slice(i + 1, k < 0 ? t.length : k);
      quoted = true;
      i = k < 0 ? t.length : k + 1;
      continue;
    }
    if (c === '"') {
      word = word ?? "";
      quoted = true;
      i++;
      while (i < t.length && t[i] !== '"') {
        const d = t[i];
        if (d === "\\" && i + 1 < t.length) {
          const e = t[i + 1];
          word += "$`\"\\".includes(e) ? e : "\\" + e;
          i += 2;
        } else if (d === "$" && t[i + 1] === "(") {
          const r = extractParen(t, i + 2); sub(r.inner); i = r.end;
        } else if (d === "`") {
          const r = extractBacktick(t, i + 1); sub(r.inner); i = r.end;
        } else { word += d; i++; }
      }
      i++;
      continue;
    }
    if (c === "\\") {
      word = (word ?? "") + (t[i + 1] ?? "");
      quoted = true;
      i += 2;
      continue;
    }
    word = (word ?? "") + c;
    i++;
  }
  endCmd();
  return { cmds, subs };
}

// ---- protected paths ------------------------------------------------------------------------

const PROTECTED_NAMES = ["evidence.jsonl", "decisions.jsonl", "decisions.md", "review.yaml"];

function realish(p: string): string {
  const parts: string[] = [];
  let cur = p;
  for (;;) {
    try {
      const r = realpathSync(cur);
      return parts.length ? posix.join(r, ...parts.reverse()) : r;
    } catch {
      const parent = posix.dirname(cur);
      if (parent === cur) return p;
      parts.push(posix.basename(cur));
      cur = parent;
    }
  }
}

const under = (p: string, root: string) => p === root || p.startsWith(root.replace(/\/$/, "") + "/");
const globRe = (g: string) =>
  new RegExp("^" + g.replace(/[.+^${}()|\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".").replace(/\[!/g, "[^") + "$");

/** Why `word` (a path as typed) is protected, or null. `removal` also protects ancestors (rm -rf projects). */
function pathReason(word: string, o: Opts, removal = false): string | null {
  if (!word) return null;
  let w = word;
  if (w === "~" || w.startsWith("~/")) w = homedir() + w.slice(1);
  const hasVar = w.includes("$");
  const base = posix.basename(w.replace(/\/+$/, ""));
  if (PROTECTED_NAMES.includes(base)) return base;
  if (/[*?[]/.test(base)) {
    const re = globRe(base);
    const hit = PROTECTED_NAMES.find((n) => re.test(n));
    if (hit) return hit;
  }
  if (/(^|\/)components\/board(\/|$)/.test(w)) return "components/board/ (generated by cnlp)";
  if (hasVar) return null;
  const root = o.cnlpRoot ? realish(o.cnlpRoot) : undefined;
  const projects = o.projectsDir ? realish(o.projectsDir) : root ? posix.join(root, "projects") : undefined;
  const abs = posix.isAbsolute(w) ? posix.normalize(w) : o.cwd ? posix.resolve(o.cwd, w) : null;
  if (abs) {
    const p = realish(abs);
    if (/(^|\/)components\/board(\/|$)/.test(p)) return "components/board/ (generated by cnlp)";
    if (PROTECTED_NAMES.includes(posix.basename(p))) return posix.basename(p);
    const roots: [string | undefined, string][] = [
      [root && posix.join(root, "src"), "cnlp's source (src/)"],
      [root && posix.join(root, "pi"), "cnlp's Pi extension (pi/)"],
      [root && posix.join(root, "projects"), "cnlp's projects/ directory"],
      [projects, "the cnlp projects directory"],
    ];
    for (const [r, label] of roots) {
      if (!r) continue;
      if (under(p, r)) return label;
      if (removal && under(r, p)) return label;
    }
  }
  if (!posix.isAbsolute(w)) {
    const n = posix.normalize(w);
    if (/^(\.\/)?src\/cnlp(\/|$)/.test(n)) return "cnlp's source (src/cnlp)";
    if (/(^|\/)src\/cnlp\/rules(\/|$)/.test(n)) return "cnlp's rules";
    if (/^(\.\/)?projects\//.test(n)) return "cnlp's projects/ directory";
  }
  return null;
}

/** Block a write by the Pi write/edit tools. `cwd` resolves relative paths (default: process.cwd()). */
export function checkWrite(path: string, cnlpRoot: string, projectsDir: string, cwd?: string): string | null {
  const r = pathReason(String(path ?? ""), { cnlpRoot, projectsDir, cwd: cwd ?? process.cwd() });
  return r ? msgFile(r) : null;
}

// ---- command analysis -----------------------------------------------------------------------

const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "ash", "fish"]);
const WRAPPERS = new Set([
  "command", "exec", "nohup", "time", "sudo", "doas", "setsid", "nice", "ionice", "stdbuf", "timeout",
  "watch", "builtin", "unbuffer", "xargs", "chroot", "uv", "uvx", "pipx", "poetry", "pdm", "hatch",
  "pipenv", "npx", "conda", "script", "flock", "su", "runuser", "taskset", "chrt", "parallel", "busybox",
]);
const RESERVED = new Set(["if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done", "time", "coproc"]);
const INTERP = /^(python[\d.]*|node|nodejs|perl|ruby|php|lua|deno|bun|awk|gawk|mawk|pwsh|osascript)$/;
const WRITE_OPS = new Set([">", ">>", ">&", "<>"]);

const isAssign = (w: string) => /^[A-Za-z_][A-Za-z0-9_]*\+?=/.test(w);
const prefixOf = (tok: string, full: string, min: number) => tok.length >= min && full.startsWith(tok);

function cnlpArgsReason(args: string[]): string | null {
  const rest: string[] = [];
  let sub: string | null = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (sub === null) {
      if (a === "--project") { i++; continue; }
      if (a.startsWith("-")) continue;
      sub = a;
    } else rest.push(a);
  }
  if (sub === null) return null;
  const flagIs = (full: string) => rest.some((a) => a.startsWith("--") && prefixOf(a.split("=")[0], full, 3));
  const sh = (name: string) => sub === name || prefixOf(sub as string, name, 2);
  if (sh("flash")) return MSG_CNLP;
  if (sh("gate") && rest.some((a) => !a.startsWith("-") && (prefixOf(a, "answer", 2) || prefixOf(a, "review", 2)))) return MSG_CNLP;
  if (sh("evidence") && rest.some((a) => !a.startsWith("-") && prefixOf(a, "add", 2))) return MSG_CNLP;
  if (sh("measure") && flagIs("--record")) return MSG_CNLP;
  if (sh("sync") && flagIs("--apply")) return MSG_CNLP;
  return null;
}

function hardwareReason(base: string, args: string[]): string | null {
  const toks = args.filter((a) => !a.startsWith("-"));
  if (/^esptool(\.py)?$/.test(base) && toks.some((a) => /^(write[_-]flash|erase[_-](flash|region)|burn\w*|write[_-]mem|load[_-]ram)$/.test(a))) return MSG_HW;
  if (/^espefuse(\.py)?$/.test(base) && toks.some((a) => /^(burn|write|set_|execute)/.test(a))) return MSG_HW;
  if (/^idf\.py$/.test(base) && toks.some((a) => /(^|-|_)flash$|^efuse[-_]burn|^erase[-_]/.test(a))) return MSG_HW;
  if (/^(make|ninja|cmake)$/.test(base) && toks.some((a) => /(^|-|_)flash$/.test(a))) return MSG_HW;
  return null;
}

function interpreterReason(text: string, o: Opts): string | null {
  const t = text.replace(/['"]\s*\+\s*['"]/g, "").replace(/\\\r?\n/g, "");
  if (/cnlp/.test(t) &&
      /(evidence\W{1,6}add\b|\bgate\W{1,6}(answer|review)\b|\bflash\b|--record|--apply)/.test(t)) return MSG_CNLP;
  const names = /(evidence\.jsonl|decisions\.(jsonl|md)|review\.yaml|components\/board|src\/cnlp\b|\/rules\/)/.test(t) ||
    [o.cnlpRoot, o.projectsDir].some((p) => p && t.includes(p));
  const writes = /(open\s*\([^)]*,\s*(mode\s*=\s*)?['"][^'"]*[wax+]|\bwrite|append|unlink|rmSync|rmtree|\brename|\bremove|\bcopy|shutil|truncate|>>?\s*\S|\btee\b|\bsed\s+-\w*i|\bmv\b|\bcp\b|\brm\b)/.test(t);
  return names && writes ? msgFile("a protected cnlp file") : null;
}

function mutationReason(words: string[], o: Opts): string | null {
  const base = posix.basename(words[0]);
  const args = words.slice(1);
  const operands = args.filter((a) => !a.startsWith("-"));
  const hit = (list: string[], removal = false) => {
    for (const a of list) { const r = pathReason(a, o, removal); if (r) return msgFile(r); }
    return null;
  };
  switch (base) {
    case "tee": case "touch": case "chmod": case "chattr": case "patch": case "ed": case "ex": case "vi":
    case "vim": case "nano": case "emacs":  case "shred": case "unlink": case "truncate":
      return hit(operands, base === "unlink" || base === "shred");
    case "rm": case "rmdir": return hit(operands, true);
    case "mv": return hit(operands, true);
    case "cp": case "install": case "ln": case "rsync": {
      const t = args.findIndex((a) => a === "-t" || a === "--target-directory");
      return hit(t >= 0 && args[t + 1] ? [args[t + 1]] : operands.slice(-1));
    }
    case "dd": return hit(args.filter((a) => a.startsWith("of=")).map((a) => a.slice(3)));
    case "sed": case "perl": case "ruby": case "gsed":
      return args.some((a) => /^-[A-Za-z]*i/.test(a) || a === "--in-place" || a.startsWith("--in-place=")) ? hit(operands) : null;
    case "awk": case "gawk":
      return args.some((a) => a === "-i" || a === "inplace") ? hit(operands) : null;
    case "find":
      return args.includes("-delete") || args.includes("-exec") ? hit(operands.filter((a) => !a.startsWith("-")), true) : null;
    case "git":
      return /^(checkout|restore|rm|mv|clean|stash|reset|apply|am|revert)$/.test(operands[0] ?? "") ? hit(operands.slice(1), true) : null;
    default: return null;
  }
}

function checkWords(words0: string[], cmd: Cmd, raw: string, o: Opts, depth: number): string | null {
  if (depth > 8) return "Command is nested too deeply to check; show it to the user instead.";
  let words = words0.slice();
  while (words.length && (isAssign(words[0]) || RESERVED.has(words[0]))) words.shift();
  if (!words.length) return null;
  const prog = words[0];
  const base = posix.basename(prog);
  const args = words.slice(1);
  const self = (ws: string[]) => (ws.length ? checkWords(ws, cmd, raw, o, depth + 1) : null);

  if (prog.includes("$")) { const r = cnlpArgsReason(args); if (r) return r; }
  if (base === "cnlp" || base === "cnlp.py") { const r = cnlpArgsReason(args); if (r) return r; }
  const hw = hardwareReason(base, args);
  if (hw) return hw;

  if (SHELLS.has(base)) {
    const ci = args.findIndex((a) => /^-[A-Za-z]*c[A-Za-z]*$/.test(a));
    if (ci >= 0) {
      const payload = args.slice(ci + 1).find((a) => !a.startsWith("-"));
      if (payload !== undefined) { const r = analyze(payload, o, depth + 1); if (r) return r; }
    } else {
      for (const body of [cmd.heredoc, cmd.herestring]) if (body) { const r = analyze(body, o, depth + 1); if (r) return r; }
      const p = cmd.pipedFrom;
      if (p && /^(echo|printf|cat)$/.test(posix.basename(p.words[0] ?? ""))) {
        const r = analyze(p.words.slice(1).join(" "), o, depth + 1); if (r) return r;
        if (p.heredoc) { const r2 = analyze(p.heredoc, o, depth + 1); if (r2) return r2; }
      }
    }
  } else if (base === "eval") {
    const r = analyze(args.join(" "), o, depth + 1); if (r) return r;
  } else if (base === "env") {
    for (let i = 0; i < args.length; i++) {
      const a = args[i];
      if (a === "-S" || a === "--split-string") { const r = analyze(args[i + 1] ?? "", o, depth + 1); if (r) return r; }
      else if (a.startsWith("-S") && a.length > 2) { const r = analyze(a.slice(2), o, depth + 1); if (r) return r; }
    }
    const r = firstSuffix(args, self); if (r) return r;
  } else if (WRAPPERS.has(base)) {
    const r = firstSuffix(args, self); if (r) return r;
  } else if (/^python[\d.]*$/.test(base)) {
    const mi = args.indexOf("-m");
    if (mi >= 0 && /^cnlp(\.|$)/.test(args[mi + 1] ?? "")) { const r = cnlpArgsReason(args.slice(mi + 2)); if (r) return r; }
    if (mi >= 0 && /^(esptool|espefuse)$/.test(args[mi + 1] ?? "")) { const r = hardwareReason(args[mi + 1], args.slice(mi + 2)); if (r) return r; }
    const si = args.findIndex((a) => /(^|\/)cnlp\/(cli|__main__)\.py$/.test(a));
    if (si >= 0) { const r = cnlpArgsReason(args.slice(si + 1)); if (r) return r; }
  }
  if (INTERP.test(base)) { const r = interpreterReason(raw, o); if (r) return r; }
  if (base === "find") {
    const ei = args.findIndex((a) => /^-(exec|execdir|ok|okdir)$/.test(a));
    if (ei >= 0) {
      const end = args.findIndex((a, k) => k > ei && (a === ";" || a === "+"));
      const r = self(args.slice(ei + 1, end < 0 ? undefined : end)); if (r) return r;
    }
  }
  return mutationReason(words, o);
}

function firstSuffix(args: string[], self: (ws: string[]) => string | null): string | null {
  for (let j = 0; j < args.length; j++) {
    if (args[j].startsWith("-") || isAssign(args[j])) continue;
    const r = self(args.slice(j));
    if (r) return r;
  }
  return null;
}

function analyze(src: string, o: Opts, depth: number): string | null {
  if (depth > 8) return "Command is nested too deeply to check; show it to the user instead.";
  const { cmds, subs } = parseScript(src);
  for (const s of subs) { const r = analyze(s, o, depth + 1); if (r) return r; }
  for (const c of cmds) {
    for (const r of c.redirs) {
      if (WRITE_OPS.has(r.op)) { const x = pathReason(r.target, o); if (x) return msgFile(x); }
    }
    const r = checkWords(c.words, c, src, o, depth);
    if (r) return r;
  }
  return null;
}

/** Reason the bash command must not run, or null. Roots/cwd sharpen path checks; names are checked regardless. */
export function checkBash(command: string, opts: Opts = {}): string | null {
  return analyze(String(command ?? ""), opts, 0);
}

/** Directory of the cnlp repo, given the extension's own file URL (a symlink into the repo). */
export function cnlpRootFrom(fileUrl: string): string {
  const p = realpathSync(new URL(fileUrl).pathname);
  return posix.resolve(posix.dirname(p), "../../..");
}
