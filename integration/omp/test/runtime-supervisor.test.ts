import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import type {
  ExtensionAPI,
  ExtensionContext,
  ToolResultEvent,
  ToolResultEventResult,
} from "@oh-my-pi/pi-coding-agent";

import {
  JsonlRuntimeProcess,
  LACONIC_BREAKER_FAILURES,
  RuntimeProtocolError,
  RuntimeSupervisor,
  attachObservationInterceptor,
  attachRecoverySurface,
  attachRuntimeLifecycle,
  type JsonObject,
  type RuntimeOperation,
  type RuntimeProcess,
  type RuntimeStartOptions,
} from "../../../src/laconic/runtime/omp/laconic";

interface CapturedHandlers {
  session_start?: (event: unknown, ctx: ExtensionContext) => Promise<void>;
  session_switch?: (event: unknown, ctx: ExtensionContext) => Promise<void>;
  session_branch?: () => void;
  session_tree?: () => void;
  session_shutdown?: () => Promise<void>;
  tool_result?: (
    event: ToolResultEvent,
    ctx: ExtensionContext,
  ) => Promise<ToolResultEventResult | void>;
}

class FakeRuntimeProcess implements RuntimeProcess {
  shutdownCalls = 0;
  failRequests = false;

  async request(): Promise<Record<string, never>> {
    if (this.failRequests) {
      throw new RuntimeProtocolError("fixture_failure", "fixture runtime failed");
    }
    return {};
  }

  async shutdown(): Promise<void> {
    this.shutdownCalls += 1;
  }
}

class ScriptedRuntimeProcess implements RuntimeProcess {
  readonly requests: Array<{ operation: RuntimeOperation; fields: JsonObject | undefined }> = [];
  readonly responses: Array<JsonObject | Error> = [];

  async request(operation: RuntimeOperation, fields?: JsonObject): Promise<JsonObject> {
    this.requests.push({ operation, fields });
    const response = this.responses.shift();
    if (response === undefined) {
      throw new Error("missing scripted response");
    }
    if (response instanceof Error) {
      throw response;
    }
    return response;
  }

  async shutdown(): Promise<void> {}
}

function fakeContext(sessionId: string, cwd: string): ExtensionContext {
  return {
    cwd,
    sessionManager: {
      getSessionId: () => sessionId,
    },
  } as unknown as ExtensionContext;
}

function captureApi(handlers: CapturedHandlers): ExtensionAPI {
  return {
    on(event: keyof CapturedHandlers, handler: unknown) {
      handlers[event] = handler as never;
    },
    setLabel() {},
  } as unknown as ExtensionAPI;
}

function pythonInterpreter(): string {
  const probe = Bun.spawnSync(["uv", "run", "python", "-c", "import sys; print(sys.executable)"], {
    stdout: "pipe",
    stderr: "pipe",
  });
  if (probe.exitCode !== 0) {
    throw new Error(`cannot resolve test interpreter: ${probe.stderr.toString()}`);
  }
  return probe.stdout.toString().trim();
}

const INTEGRATION_REQUEST_TIMEOUT_MS = 2_000;

function runtimeOptions(command: readonly string[], dataDirectory: string): RuntimeStartOptions {
  return {
    command,
    sessionId: "session-supervisor-test",
    workingDirectory: process.cwd(),
    dataDirectory,
    policy: { span_budget: 120, keep_head: 40, keep_tail: 40, max_errors: 20 },
    requestTimeoutMs: INTEGRATION_REQUEST_TIMEOUT_MS,
  };
}

function textResult(
  toolName: string,
  text: string,
  overrides: Record<string, unknown> = {},
): ToolResultEvent {
  return {
    type: "tool_result",
    toolCallId: "call-1",
    toolName,
    input: { path: "src/example.py" },
    content: [{ type: "text", text }],
    isError: false,
    details: { source: "fixture" },
    ...overrides,
  } as ToolResultEvent;
}

function emitted(raw: string, content: string, reference = "session-a/F1"): JsonObject {
  return {
    decision: "emitted",
    reason: "smaller_envelope",
    content,
    reference,
    raw_chars: [...raw].length,
    visible_chars: [...content].length,
    latency_ms: 1,
  };
}

function passThrough(raw: string): JsonObject {
  return {
    decision: "pass_through",
    reason: "not_smaller",
    content: null,
    reference: null,
    raw_chars: [...raw].length,
    visible_chars: [...raw].length,
    latency_ms: 1,
  };
}

async function interceptorHarness(): Promise<{
  handlers: CapturedHandlers;
  runtime: ScriptedRuntimeProcess;
  supervisor: RuntimeSupervisor;
  context: ExtensionContext;
}> {
  const handlers: CapturedHandlers = {};
  const runtime = new ScriptedRuntimeProcess();
  const supervisor = new RuntimeSupervisor({ createProcess: async () => runtime });
  const context = fakeContext("session-a", "/project");
  await supervisor.bind(context);
  attachObservationInterceptor(captureApi(handlers), supervisor);
  return { handlers, runtime, supervisor, context };
}

interface ToolExecutionResult {
  content: Array<{ type: string; text: string }>;
  isError?: boolean;
}

interface RegisteredToolFixture {
  name: string;
  loadMode?: string;
  approval?: string;
  execute(toolCallId: string, params: { reference: string }): Promise<ToolExecutionResult>;
}

interface RegisteredCommandFixture {
  handler(args: string, ctx: ExtensionContext): Promise<void>;
}

interface RecoverySurface {
  tool?: RegisteredToolFixture;
  commandName?: string;
  command?: RegisteredCommandFixture;
}

function captureRecoveryApi(surface: RecoverySurface): ExtensionAPI {
  const stringSchema = {
    describe() {
      return stringSchema;
    },
  };
  return {
    zod: {
      string: () => stringSchema,
      object: (shape: unknown) => shape,
    },
    registerTool(tool: RegisteredToolFixture) {
      surface.tool = tool;
    },
    registerCommand(name: string, command: RegisteredCommandFixture) {
      surface.commandName = name;
      surface.command = command;
    },
  } as unknown as ExtensionAPI;
}

function operatorContext(
  sessionId: string,
  notifications: Array<{ message: string; type: string | undefined }>,
  statuses: Array<{ key: string; text: string | undefined }>,
): ExtensionContext {
  return {
    cwd: "/project",
    sessionManager: { getSessionId: () => sessionId },
    ui: {
      notify(message: string, type?: string) {
        notifications.push({ message, type });
      },
      setStatus(key: string, text: string | undefined) {
        statuses.push({ key, text });
      },
    },
  } as unknown as ExtensionContext;
}

describe("OMP runtime lifecycle", () => {
  test("binds host session identity, preserves branch/tree, and rebinds on switch", async () => {
    const handlers: CapturedHandlers = {};
    const processes: FakeRuntimeProcess[] = [];
    const starts: RuntimeStartOptions[] = [];
    const createProcess = async (options: RuntimeStartOptions): Promise<RuntimeProcess> => {
      starts.push(options);
      const runtime = new FakeRuntimeProcess();
      processes.push(runtime);
      return runtime;
    };
    const supervisor = attachRuntimeLifecycle(captureApi(handlers), {
      command: ["python", "-m", "laconic.runtime"],
      dataDirectory: "/private/ledgers",
      createProcess,
    });

    await handlers.session_start?.({}, fakeContext("session-a", "/project/a"));
    expect(supervisor.snapshot()).toEqual({
      state: "ready",
      sessionId: "session-a",
      consecutiveFailures: 0,
      breakerOpen: false,
    });
    expect(starts[0]?.sessionId).toBe("session-a");
    expect(starts[0]?.workingDirectory).toBe("/project/a");

    handlers.session_branch?.();
    handlers.session_tree?.();
    expect(supervisor.snapshot().sessionId).toBe("session-a");
    expect(starts).toHaveLength(1);

    await handlers.session_switch?.({}, fakeContext("session-b", "/project/b"));
    expect(processes[0]?.shutdownCalls).toBe(1);
    expect(starts[1]?.sessionId).toBe("session-b");
    expect(supervisor.snapshot().sessionId).toBe("session-b");

    await handlers.session_shutdown?.();
    expect(processes[1]?.shutdownCalls).toBe(1);
    expect(supervisor.snapshot().state).toBe("stopped");
  });

  test("third consecutive engine failure opens the session breaker", async () => {
    const runtime = new FakeRuntimeProcess();
    const supervisor = new RuntimeSupervisor({
      createProcess: async () => runtime,
    });
    await supervisor.bind(fakeContext("session-a", "/project"));
    runtime.failRequests = true;

    for (let attempt = 0; attempt < LACONIC_BREAKER_FAILURES; attempt += 1) {
      await expect(supervisor.invoke("expand", { reference: "session-a/F1" })).rejects.toThrow(
        "fixture runtime failed",
      );
    }

    expect(supervisor.snapshot()).toEqual({
      state: "open",
      sessionId: "session-a",
      consecutiveFailures: LACONIC_BREAKER_FAILURES,
      breakerOpen: true,
    });
    await expect(supervisor.invoke("expand")).rejects.toThrow("circuit breaker is open");
  });
});

describe("tool-result interception", () => {
  test("changes only the text field after an emitted runtime decision", async () => {
    const { handlers, runtime, context } = await interceptorHarness();
    const raw = "raw observation that is much longer than its encoded form";
    const encoded = "[laconic session-a/F1]";
    runtime.responses.push(emitted(raw, encoded));
    const event = textResult("read", raw);

    const result = await handlers.tool_result?.(event, context);

    expect(result).toEqual({ content: [{ type: "text", text: encoded }] });
    expect(event.details).toEqual({ source: "fixture" });
    expect(event.isError).toBe(false);
    expect(runtime.requests).toEqual([
      {
        operation: "encode_observation",
        fields: {
          tool_name: "Read",
          tool_input: { path: "src/example.py" },
          raw_text: raw,
          success: true,
          sequence: 1,
        },
      },
    ]);
  });

  test("passes through runtime pass-through decisions without an override", async () => {
    const { handlers, runtime, context, supervisor } = await interceptorHarness();
    const raw = "short";
    runtime.responses.push(passThrough(raw));

    expect(await handlers.tool_result?.(textResult("bash", raw), context)).toBeUndefined();
    expect(runtime.requests[0]?.fields?.tool_name).toBe("Bash");
    expect(supervisor.snapshot().consecutiveFailures).toBe(0);
  });

  test("never sends errors, unsupported tools, or non-single-text results", async () => {
    const { handlers, runtime, context } = await interceptorHarness();
    const cases = [
      textResult("edit", "unsupported"),
      textResult("Read", "wrong case"),
      textResult("grep", "error", { isError: true }),
      textResult("glob", "mixed", {
        content: [
          { type: "text", text: "mixed" },
          { type: "image", data: "AA==", mimeType: "image/png" },
        ],
      }),
      textResult("read", "image", {
        content: [{ type: "image", data: "AA==", mimeType: "image/png" }],
      }),
    ];

    for (const event of cases) {
      expect(await handlers.tool_result?.(event, context)).toBeUndefined();
    }
    expect(runtime.requests).toEqual([]);
  });

  test("fails open and opens the breaker after three engine errors", async () => {
    const { handlers, runtime, context, supervisor } = await interceptorHarness();
    runtime.responses.push(
      new RuntimeProtocolError("timeout", "deadline"),
      new RuntimeProtocolError("runtime_exited", "crash"),
      new RuntimeProtocolError("invalid_frame", "malformed"),
    );

    for (let attempt = 0; attempt < LACONIC_BREAKER_FAILURES; attempt += 1) {
      const event = textResult("grep", `raw-${attempt}`);
      expect(await handlers.tool_result?.(event, context)).toBeUndefined();
      expect(event.content[0]).toEqual({ type: "text", text: `raw-${attempt}` });
    }
    expect(supervisor.snapshot().breakerOpen).toBe(true);
    expect(runtime.requests).toHaveLength(LACONIC_BREAKER_FAILURES);
  });

  test("real engine emits a namespaced, exactly recoverable envelope", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-encode-"));
    const supervisor = new RuntimeSupervisor({
      command: [pythonInterpreter(), "-m", "laconic.runtime"],
      dataDirectory: join(root, "ledgers"),
      requestTimeoutMs: INTEGRATION_REQUEST_TIMEOUT_MS,
    });
    const raw = Array.from({ length: 200 }, (_, index) => `output line ${index}`).join("\n");
    try {
      await supervisor.bind(fakeContext("session-real", process.cwd()));
      const outcome = await supervisor.encodeObservation("Bash", { command: "fixture" }, raw);
      expect(outcome.decision).toBe("emitted");
      expect(outcome.reference).toMatch(/^session-real\/B1$/);
      expect(outcome.content).toContain("laconic_expand");
      expect(outcome.visibleChars).toBeLessThan(outcome.rawChars);
    } finally {
      await supervisor.shutdown();
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("recovery and operator surfaces", () => {
  test("registers an essential read tool that expands full and spanned cross-session text", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-expand-"));
    const supervisor = new RuntimeSupervisor({
      command: [pythonInterpreter(), "-m", "laconic.runtime"],
      dataDirectory: join(root, "ledgers"),
      requestTimeoutMs: INTEGRATION_REQUEST_TIMEOUT_MS,
    });
    const surface: RecoverySurface = {};
    attachRecoverySurface(captureRecoveryApi(surface), supervisor);
    if (surface.tool === undefined) {
      throw new Error("recovery tool was not registered");
    }
    const raw = Array.from({ length: 200 }, (_, index) => `source line ${index + 1}`).join("\n");
    try {
      await supervisor.bind(fakeContext("source-session", process.cwd()));
      const encoded = await supervisor.encodeObservation("Bash", { command: "fixture" }, raw);
      if (encoded.reference === null) {
        throw new Error("fixture did not emit a runtime reference");
      }
      await supervisor.bind(fakeContext("active-session", process.cwd()));

      const full = await surface.tool.execute("expand-full", {
        reference: encoded.reference,
      });
      const span = await surface.tool.execute("expand-span", {
        reference: `${encoded.reference}:2-3`,
      });

      expect(surface.tool.name).toBe("laconic_expand");
      expect(surface.tool.loadMode).toBe("essential");
      expect(surface.tool.approval).toBe("read");
      expect(full).toEqual({ content: [{ type: "text", text: raw }] });
      expect(span).toEqual({
        content: [{ type: "text", text: "source line 2\nsource line 3" }],
      });
    } finally {
      await supervisor.shutdown();
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("invalid expansion fails loudly without leaking the supplied reference or opening breaker", async () => {
    const runtime = new ScriptedRuntimeProcess();
    const supervisor = new RuntimeSupervisor({ createProcess: async () => runtime });
    await supervisor.bind(fakeContext("session-a", "/project"));
    const surface: RecoverySurface = {};
    attachRecoverySurface(captureRecoveryApi(surface), supervisor);
    if (surface.tool === undefined) {
      throw new Error("recovery tool was not registered");
    }
    const secretReference = "secret-invalid-reference";
    runtime.responses.push(
      new RuntimeProtocolError("invalid_reference", `invalid reference ${secretReference}`),
    );

    const result = await surface.tool.execute("expand-invalid", {
      reference: secretReference,
    });

    expect(result).toEqual({
      content: [{ type: "text", text: "laconic expansion failed: invalid_reference" }],
      isError: true,
    });
    expect(JSON.stringify(result)).not.toContain(secretReference);
    expect(supervisor.snapshot().consecutiveFailures).toBe(0);
    expect(supervisor.snapshot().breakerOpen).toBe(false);
  });

  test("status, pause, and resume stay local to the extension runtime", async () => {
    const processes: FakeRuntimeProcess[] = [];
    const supervisor = new RuntimeSupervisor({
      createProcess: async () => {
        const runtime = new FakeRuntimeProcess();
        processes.push(runtime);
        return runtime;
      },
    });
    const notifications: Array<{ message: string; type: string | undefined }> = [];
    const statuses: Array<{ key: string; text: string | undefined }> = [];
    const context = operatorContext("session-a", notifications, statuses);
    await supervisor.bind(context);
    const surface: RecoverySurface = {};
    attachRecoverySurface(captureRecoveryApi(surface), supervisor);
    if (surface.command === undefined) {
      throw new Error("operator command was not registered");
    }

    await surface.command.handler("", context);
    await surface.command.handler("pause", context);
    expect(supervisor.snapshot().state).toBe("paused");
    expect(processes[0]?.shutdownCalls).toBe(1);
    await surface.command.handler("resume", operatorContext("session-b", notifications, statuses));
    expect(supervisor.snapshot().state).toBe("ready");
    expect(supervisor.snapshot().sessionId).toBe("session-b");
    await surface.command.handler("unknown", context);

    expect(surface.commandName).toBe("laconic");
    expect(statuses).toContainEqual({ key: "laconic", text: "Laconic: paused" });
    expect(statuses).toContainEqual({ key: "laconic", text: "Laconic: ready" });
    expect(notifications.some(({ message }) => message.includes("state=ready"))).toBe(true);
    expect(notifications.at(-1)).toEqual({
      message: "Usage: /laconic [status|pause|resume]",
      type: "error",
    });
  });
});

describe("JSONL runtime transport", () => {
  test("initializes and cleanly shuts down the real Python engine", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-runtime-"));
    try {
      const runtime = await JsonlRuntimeProcess.start(
        runtimeOptions([pythonInterpreter(), "-m", "laconic.runtime"], join(root, "ledgers")),
      );
      await runtime.shutdown();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("reopens a persisted session without sequence or request collisions", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-resume-"));
    const context = fakeContext("persisted-session", process.cwd());
    const options = {
      command: [pythonInterpreter(), "-I", "-m", "laconic.runtime"],
      dataDirectory: join(root, "ledgers"),
      requestTimeoutMs: INTEGRATION_REQUEST_TIMEOUT_MS,
    };
    try {
      const first = new RuntimeSupervisor(options);
      await first.bind(context);
      expect((await first.encodeObservation("Read", { path: "first.py" }, "x")).decision).toBe(
        "pass_through",
      );
      await first.shutdown();

      const reopened = new RuntimeSupervisor(options);
      await reopened.bind(context);
      expect((await reopened.encodeObservation("Read", { path: "second.py" }, "y")).decision).toBe(
        "pass_through",
      );
      expect(reopened.snapshot()).toMatchObject({ state: "ready", breakerOpen: false });
      await reopened.shutdown();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("bounds an unresponsive engine and terminates it", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-timeout-"));
    const options = runtimeOptions(
      [pythonInterpreter(), "-c", "import time; time.sleep(5)"],
      join(root, "ledgers"),
    );
    options.requestTimeoutMs = 25;
    const started = performance.now();
    try {
      await expect(JsonlRuntimeProcess.start(options)).rejects.toThrow(
        "runtime request exceeded its deadline",
      );
      expect(performance.now() - started).toBeLessThan(500);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("rejects malformed engine output without echoing it", async () => {
    const root = mkdtempSync(join(tmpdir(), "laconic-omp-malformed-"));
    const secret = "raw-observation-must-not-escape";
    const options = runtimeOptions(
      [pythonInterpreter(), "-c", `print('not-json ${secret}', flush=True)`],
      join(root, "ledgers"),
    );
    try {
      let message = "";
      try {
        await JsonlRuntimeProcess.start(options);
      } catch (error) {
        message = error instanceof Error ? error.message : String(error);
      }
      expect(message).toContain("invalid JSON");
      expect(message).not.toContain(secret);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
