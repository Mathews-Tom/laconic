import { expect, test } from "bun:test";

test("pinned OMP host starts without a provider", () => {
  const result = Bun.spawnSync(["./node_modules/.bin/omp", "--version"], {
    cwd: `${import.meta.dir}/../../..`,
    stderr: "pipe",
    stdout: "pipe",
  });

  expect(result.exitCode).toBe(0);
  expect(result.stderr.toString()).toBe("");
  expect(result.stdout.toString().trim()).toBe("omp/18.1.10");
});
