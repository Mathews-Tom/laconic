import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "bun:test";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@oh-my-pi/pi-coding-agent";

import {
  JsonlRuntimeProcess,
  LACONIC_BREAKER_FAILURES,
  RuntimeProtocolError,
  RuntimeSupervisor,
  attachRuntimeLifecycle,
  type RuntimeProcess,
  type RuntimeStartOptions,
} from "../../../src/laconic/runtime/omp/laconic";

interface CapturedHandlers {
  session_start?: (event: unknown, ctx: ExtensionContext) => Promise<void>;
  session_switch?: (event: unknown, ctx: ExtensionContext) => Promise<void>;
  session_branch?: () => void;
  session_tree?: () => void;
  session_shutdown?: () => Promise<void>;
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
      await expect(supervisor.invoke("expand", { reference: "LACONIC:S1:F1" })).rejects.toThrow(
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
