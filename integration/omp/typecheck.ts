import type {
  ExtensionAPI,
  ExtensionContext,
  ToolResultEvent,
  ToolResultEventResult,
} from "@oh-my-pi/pi-coding-agent";

// This file is a compile-only compatibility boundary for the exact OMP version
// pinned in package.json. Runtime behavior belongs to the packaged extension.
export function typecheckHostContract(pi: ExtensionAPI): void {
  pi.on("session_start", (_event, ctx: ExtensionContext) => {
    const sessionId: string = ctx.sessionManager.getSessionId();
    void sessionId;
  });
  pi.on(
    "tool_result",
    (event: ToolResultEvent): ToolResultEventResult | undefined => {
      if (event.isError) return undefined;
      return {
        content: event.content,
        details: event.details,
        isError: event.isError,
      };
    },
  );
  pi.registerCommand("laconic-contract-probe", {
    handler: async (_args, ctx) => {
      const sessionId: string = ctx.sessionManager.getSessionId();
      void sessionId;
    },
  });
}
