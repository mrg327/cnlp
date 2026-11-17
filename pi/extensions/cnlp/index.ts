// cnlp for Pi: read-only ESP32 project checks as tools the model can call.
// Installed by symlink: ~/.pi/agent/extensions/cnlp -> <cnlp repo>/pi/extensions/cnlp
//
// - Registers cnlp_status, cnlp_audit, cnlp_evidence and cnlp_ask (all read-only).
// - When the session's directory is covered by a cnlp config, tells the model to use them
//   before answering questions about progress, pins, rules or whether the code is OK.
// - Blocks bash commands that would record approvals/measurements/flashes in the user's name, and
//   bash or write/edit calls that touch cnlp's records, rules or source (see guard.ts).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { homedir } from "node:os";
import { join } from "node:path";
import { realpathSync } from "node:fs";
import { checkBash, checkWrite } from "./guard.ts";

const CNLP = "cnlp";

// The extension is a symlink into the cnlp repo; its real path locates the repo.
function locateRoot(): string {
  const tries = [
    () => new URL(import.meta.url).pathname,
    () => join(homedir(), ".pi", "agent", "extensions", "cnlp", "index.ts"),
  ];
  for (const t of tries) {
    try { return join(realpathSync(t()), "..", "..", "..", ".."); } catch { /* next */ }
  }
  return process.env.CNLP_ROOT ?? "";
}
const CNLP_ROOT = locateRoot();
const PROJECTS_DIR = process.env.CNLP_PROJECTS_DIR || join(CNLP_ROOT, "projects");

type Run = { code: number; text: string };

export default function (pi: ExtensionAPI) {
  let config: string | null = null; // cnlp config covering the session cwd, if any

  async function run(args: string[], cwd: string, signal?: AbortSignal, timeout = 120_000): Promise<Run> {
    const r = await pi.exec(CNLP, args, { cwd, signal, timeout });
    const text = (r.stdout || "") + (r.stderr ? `\n${r.stderr}` : "");
    return { code: r.code ?? 1, text: text.trim() || `(no output, exit ${r.code})` };
  }

  function tool(name: string, label: string, description: string, args: (p: any) => string[],
                parameters = Type.Object({}), snippet?: string) {
    pi.registerTool({
      name, label, description, parameters,
      promptSnippet: snippet,
      async execute(_id, params, signal, _onUpdate, ctx) {
        const r = await run(args(params), ctx.cwd, signal);
        // Exit 1 means findings or "I don't know"; that's a result, not a tool failure.
        if (r.code >= 2) throw new Error(r.text);
        return { content: [{ type: "text", text: r.text }], details: { exitCode: r.code } };
      },
    });
  }

  tool("cnlp_status", "cnlp status",
    "Project dashboard for the ESP32 project in the current directory: build phase, pin-plan audit verdict, " +
    "real-time rule verdict (RT1-RT6), requirement evidence counts, pins not configured yet and the current " +
    "phase's check plus a 'Watch for' list of the current phase's common problems from the build procedure. " +
    "Read-only. Authoritative for phase and progress; prefer it over reading docs by hand.",
    () => ["--for-model", "status", "."], undefined,
    "Show the ESP32 project's phase, audit, real-time and evidence status");

  tool("cnlp_audit", "cnlp audit",
    "Check the project's C code against its pin plan (AUDIT_* rules), chip/module pin rules (C*, W*) and the " +
    "project's real-time rules (RT1-RT6). Works on a temp snapshot; never edits code. Run after code changes.",
    () => ["--for-model", "audit", "."], undefined,
    "Audit ESP32 code against the pin plan and real-time rules");

  tool("cnlp_evidence", "cnlp evidence",
    "Requirement evidence matrix (R*/F* IDs from the project's spec): status, strongest evidence, which " +
    "verification test covers each. Use before saying anything about whether a requirement is met.",
    () => ["--for-model", "evidence", "."]);

  tool("cnlp_try", "cnlp try",
    "What-if check for a proposed pin change, before anyone edits code: give the new pin line in cnlp's " +
    "controlled English and it reports every pin rule the change would break or fix against the project's " +
    "whole pin plan (pin conflicts, input-only pins, strapping pins, pulls). Changes nothing. Pin line format: " +
    "gpio <n> is input|output|open-drain \"<role>\" with internal pull-up|external pull-up|off-board pull-up|no pull. " +
    "Using an existing role moves that pin. Example: gpio 35 is input \"enc sw\" with internal pull-up.",
    (p) => ["--for-model", "try", String(p.statement), "."],
    Type.Object({ statement: Type.String({ description: "Controlled-English pin line(s), each ending with a period" }) }),
    "Check a proposed GPIO/pin change against all pin rules before editing code");

  tool("cnlp_verify_plan", "cnlp verify-plan",
    "Concrete verification steps for the project's requirements: per requirement the status, the test from the " +
    "spec's verification plan and the exact commands the USER runs to measure and record evidence. Optionally " +
    "limit to one requirement (e.g. R4). Read-only; never run the commands it lists.",
    (p) => ["--for-model", "verify-plan", ".", ...(p.req ? [String(p.req)] : [])],
    Type.Object({ req: Type.Optional(Type.String({ description: "Requirement ID such as R4; omit for all" })) }),
    "Show how to verify a requirement (commands for the user to run)");

  tool("cnlp_rules", "cnlp rules",
    "List every rule cnlp checks (IDs, family, whether the user has reviewed it). Optionally filter by family " +
    "or only rules still needing the user's review. Read-only.",
    (p) => ["--for-model", "rules", ...(p.family ? ["--family", String(p.family)] : []),
            ...(p.needs_review ? ["--needs-review"] : [])],
    Type.Object({
      family: Type.Optional(Type.String({ description: "Rule family filter" })),
      needs_review: Type.Optional(Type.Boolean({ description: "Only rules the user has not reviewed" })),
    }));

  tool("cnlp_ask", "cnlp ask",
    "Ask cnlp a question about itself, a rule ID, a pin, or which cnlp command to use. Answers come only from " +
    "cnlp's own sources with citations; '?' means it doesn't know.",
    (p) => ["--for-model", "ask", String(p.question)],
    Type.Object({ question: Type.String({ description: "The question, in plain English" }) }));

  pi.on("session_start", async (_event, ctx) => {
    const r = await run(["config", "."], ctx.cwd, undefined, 10_000).catch(() => null);
    config = r && r.code === 0 ? r.text.split("\n")[0] : null;
  });

  pi.on("before_agent_start", async (event) => {
    if (!config) return;
    return {
      systemPrompt: event.systemPrompt + "\n\n" + [
        "This directory is an ESP32 project checked by cnlp (config: " + config + ").",
        "- cnlp tool output is a REPORT / FACTS / NEXT block. Relay the REPORT lines to the user including their rule IDs exactly as written (e.g. PARAM_TASK, RT7_PERIOD_DRIFT); never rename or paraphrase a rule ID. Use FACTS (phase, check, watch-for, requirement counts) instead of reading the docs or source files to re-derive them. Offer the NEXT steps as suggestions for the user to review; do not carry them out unasked.",
        "- Before answering about progress, the current phase, what to do or watch out for next, pins, rules, or whether the code is OK, call cnlp_status first (its 'Watch for' list is the build procedure's common problems for this phase — include them); after any C code change call cnlp_audit. Base the answer on their output and quote rule IDs; don't re-derive phase or pin facts from the docs.",
        "- Before recommending or making any pin/GPIO change (moving a signal, changing a pull), call cnlp_try with the proposed pin line and base the answer on its result; it knows the whole pin plan, including pins already in use.",
        "- When cnlp reports a finding, any fix you propose must satisfy the rule exactly as cnlp's message states it. Never say a change resolves a finding unless cnlp_audit, run after the change, no longer reports it.",
        "- Use cnlp_evidence before saying whether any requirement (R*/F*) is met. Never claim a measurement or pass that it doesn't show.",
        "- cnlp only reports. Follow the project's own rules (CLAUDE.md / AGENTS.md) about who writes code: explain problems and propose small changes for the user to review.",
        "- Never run cnlp flash, gate answer/review, evidence add, measure --record or sync --apply; show the command for the user to run.",
        "- In commands you show, the project argument is `.` (the user runs them from this directory), never `projects/<name>`. Recording a measurement looks like: cnlp evidence add . R4 measured_scope pass --value 1.001 --unit ms --criterion \"1 ms ±2%\" --source \"scope, GPIO 2 probe\". A measurement needs --criterion: take it from the requirement's verification-plan pass criterion (cnlp_evidence shows it) or ask the user.",
      ].join("\n"),
    };
  });

  pi.on("tool_call", async (event, ctx) => {
    const input = (event.input ?? {}) as Record<string, unknown>;
    const opts = { cnlpRoot: CNLP_ROOT, projectsDir: PROJECTS_DIR, cwd: ctx.cwd };
    let reason: string | null = null;
    if (event.toolName === "bash") {
      reason = checkBash(String(input.command ?? ""), opts);
    } else if (/write|edit|patch|replace|create|delete|move|rename/i.test(event.toolName)) {
      // Pi's built-in write and edit tools take { path } (edit adds edits[]); also cover other names.
      for (const k of ["path", "file_path", "filePath", "file"]) {
        if (typeof input[k] === "string") {
          reason = checkWrite(input[k] as string, CNLP_ROOT, PROJECTS_DIR, ctx.cwd);
          if (reason) break;
        }
      }
    }
    if (reason) return { block: true, reason };
  });
}
