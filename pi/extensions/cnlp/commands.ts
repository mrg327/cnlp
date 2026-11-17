// Pure argument handling for the /cnlp slash command: tokenize, whitelist read-only subcommands, assemble argv.
// No Pi imports so it can be tested with plain node (type stripping, node >= 22.18).
import { checkBash, parseScript, type Opts } from "./guard.ts";

export type Slash =
  | { argv: string[]; share: boolean }  // run `cnlp <argv>`; share = also give the output to the model
  | { text: string }                    // show this text, run nothing (help)
  | { refuse: string };                 // never run; message tells the user what to do instead

const REQ = /^[A-Za-z][A-Za-z0-9_-]{0,15}$/;
const FAMILIES = ["chip", "module", "cross-pin", "realtime", "audit", "lint", "param", "fsm", "status"];
// Subcommands that change state, spend hardware time or need the model: shown, never run from here.
const SHELL_ONLY = ["gate", "flash", "measure", "sync", "gen", "translate", "spec", "ask", "readback"];

export const HELP = [
  "/cnlp <command> [--share]    read-only cnlp output shown to you directly (the model is not involved)",
  "  status                     phase, audit, real-time and evidence summary",
  "  audit                      code vs pin plan and real-time rules",
  "  evidence                   requirement evidence matrix",
  "  rules [--needs-review] [--family F]",
  "  verify-plan [REQ]          verification steps and the commands you run",
  "  try '<pin line>'           what-if check of a proposed pin change",
  "  doctor | config | check-pins [--manifest P] | check-fsm | lint | render | help",
  "--share also adds the output to the conversation so the model can discuss it.",
  "Anything that records or changes state (evidence add, measure, flash, gate ...) is shown as a command to run in your shell.",
].join("\n");

/** Shell-quote a word for display in a "run this yourself" hint. */
export function shq(w: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(w) ? w : "'" + w.replace(/'/g, `'\\''`) + "'";
}

// Characters that would mean something to a shell, outside quotes (or any expansion inside double quotes).
function metaReason(raw: string): string | null {
  let q: "'" | '"' | null = null;
  for (const ch of raw) {
    if (q === "'") { if (ch === "'") q = null; continue; }
    if (q === '"') {
      if (ch === '"') q = null;
      else if (ch === "$" || ch === "`") return "expansion is not allowed";
      continue;
    }
    if (ch === "'" || ch === '"') q = ch;
    else if (/[;&|<>`$(){}\n\r\\*?~[\]#!]/.test(ch)) return `unquoted ${JSON.stringify(ch)} is not allowed`;
  }
  return q ? "unterminated quote" : null;
}

function yourself(words: string[]): string {
  // No implicit "." — commands that take a project default to the current directory,
  // and others (flash, gate, measure) would reject a stray positional.
  return `run this in your shell yourself: cnlp ${words.map(shq).join(" ")}`;
}

export function parseSlash(args: string, opts: Opts = {}): Slash {
  const raw = String(args ?? "").trim();
  if (!raw) return { text: HELP };
  const bad = metaReason(raw);
  if (bad) return { refuse: `/cnlp takes one plain command (${bad}). Run other things in your shell.` };
  const parsed = parseScript(raw);
  if (parsed.cmds.length !== 1 || parsed.subs.length || parsed.cmds[0].redirs.length ||
      parsed.cmds[0].heredoc !== undefined || parsed.cmds[0].herestring !== undefined || parsed.cmds[0].pipedFrom) {
    return { refuse: "/cnlp takes one plain command; pipes, redirects, substitutions and chaining are not allowed." };
  }
  let words = parsed.cmds[0].words;
  let share = false;
  if (words.includes("--share")) { share = true; words = words.filter((w) => w !== "--share"); }
  if (words[0] === "cnlp") words = words.slice(1);
  const [sub, ...rest] = words;
  if (!sub || sub === "help" || sub === "-h" || sub === "--help") return { text: HELP };

  const argsOf = (): string[] | { refuse: string } => {
    const bounce = { refuse: `/cnlp ${sub} does not accept those arguments (${rest.map(shq).join(" ")}). ${yourself(words)}` };
    switch (sub) {
      case "status": case "audit": case "evidence": case "doctor": case "config": {
        if (sub === "evidence" && rest[0] === "add") return { refuse: yourself(words) };
        if (rest.length && !(rest.length === 1 && rest[0] === ".")) return bounce;
        return [sub, "."];
      }
      case "rules": {
        const out = ["rules"];
        for (let i = 0; i < rest.length; i++) {
          if (rest[i] === "--needs-review") out.push("--needs-review");
          else if (rest[i] === "--family" && FAMILIES.includes(rest[i + 1])) out.push("--family", rest[++i]);
          else return bounce;
        }
        return out;
      }
      case "verify-plan": {
        const r = rest.filter((w) => w !== ".");
        if (r.length > 1 || (r[0] !== undefined && !REQ.test(r[0]))) return bounce;
        return ["verify-plan", ".", ...r];
      }
      case "try": {
        if (rest.length < 1 || rest.length > 2 || (rest[1] !== undefined && rest[1] !== ".") ||
            !rest[0] || rest[0].startsWith("-")) {
          return { refuse: "usage: /cnlp try '<pin line>'   e.g. /cnlp try 'gpio 35 is input \"enc sw\" with internal pull-up.'" };
        }
        return ["try", rest[0], "."];
      }
      case "check-pins": {
        if (!rest.length) return ["check-pins"];
        if (rest.length === 2 && rest[0] === "--manifest" && !rest[1].startsWith("-")) return ["check-pins", ...rest];
        return bounce;
      }
      case "check-fsm": case "lint": case "render":
        return rest.length ? bounce : [sub];
      default:
        return {
          refuse: SHELL_ONLY.includes(sub)
            ? `/cnlp does not run "${sub}" (it records, changes state, uses hardware or the model). ${yourself(words)}`
            : `unknown /cnlp command "${sub}". Try /cnlp help.`,
        };
    }
  };
  const argv = argsOf();
  if (!Array.isArray(argv)) return argv;
  // Second line of defense: the assembled command must also pass the bash guard.
  const why = checkBash(["cnlp", ...argv].map(shq).join(" "), opts);
  if (why) return { refuse: why };
  return { argv, share };
}
