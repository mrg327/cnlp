// cnlp for Pi: read-only ESP32 project checks as tools the model can call.
// Installed by symlink: ~/.pi/agent/extensions/cnlp -> <cnlp repo>/pi/extensions/cnlp
//
// - Registers cnlp_status, cnlp_audit, cnlp_evidence and cnlp_ask (all read-only).
// - When the session's directory is covered by a cnlp config, tells the model to use them
//   before answering questions about progress, pins, rules or whether the code is OK.
// - Blocks bash commands that would record approvals or measurements in the user's name.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const CNLP = "cnlp";
const RECORDING = /\bcnlp\b[^\n]*\b(flash|gate\s+(answer|review)|evidence\s+add|measure\b[^\n]*--record|sync\b[^\n]*--apply)\b/;

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
    () => ["status", "."], undefined,
    "Show the ESP32 project's phase, audit, real-time and evidence status");

  tool("cnlp_audit", "cnlp audit",
    "Check the project's C code against its pin plan (AUDIT_* rules), chip/module pin rules (C*, W*) and the " +
    "project's real-time rules (RT1-RT6). Works on a temp snapshot; never edits code. Run after code changes.",
    () => ["audit", "."], undefined,
    "Audit ESP32 code against the pin plan and real-time rules");

  tool("cnlp_evidence", "cnlp evidence",
    "Requirement evidence matrix (R*/F* IDs from the project's spec): status, strongest evidence, which " +
    "verification test covers each. Use before saying anything about whether a requirement is met.",
    () => ["evidence", "."]);

  tool("cnlp_try", "cnlp try",
    "What-if check for a proposed pin change, before anyone edits code: give the new pin line in cnlp's " +
    "controlled English and it reports every pin rule the change would break or fix against the project's " +
    "whole pin plan (pin conflicts, input-only pins, strapping pins, pulls). Changes nothing. Pin line format: " +
    "gpio <n> is input|output|open-drain \"<role>\" with internal pull-up|external pull-up|off-board pull-up|no pull. " +
    "Using an existing role moves that pin. Example: gpio 35 is input \"enc sw\" with internal pull-up.",
    (p) => ["try", String(p.statement), "."],
    Type.Object({ statement: Type.String({ description: "Controlled-English pin line(s), each ending with a period" }) }),
    "Check a proposed GPIO/pin change against all pin rules before editing code");

  tool("cnlp_ask", "cnlp ask",
    "Ask cnlp a question about itself, a rule ID, a pin, or which cnlp command to use. Answers come only from " +
    "cnlp's own sources with citations; '?' means it doesn't know.",
    (p) => ["ask", String(p.question)],
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

  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const cmd = String((event.input as any)?.command ?? "");
    if (RECORDING.test(cmd)) {
      return { block: true, reason: "That cnlp command records an approval, a measurement or a flash in the user's name. Show it to the user to run themselves." };
    }
  });
}
