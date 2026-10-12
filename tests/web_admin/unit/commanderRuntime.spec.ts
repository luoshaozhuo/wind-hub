import { afterEach, describe, expect, it, vi } from "vitest";
import { runtimeApi } from "../../../src/web_admin/src/api/runtime";

afterEach(() => vi.unstubAllGlobals());

describe("Commander runtime client", () => {
  it("preserves command id and value in a write request", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        command_id: "c-1",
        outcome: "COMMAND_OUTCOME_CONFIRMED_SUCCESS",
        error: "",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await runtimeApi.writePoint(
      {
        command_id: "c-1",
        device_id: "d-1",
        point_id: "p-1",
        value: false,
        timeout: 1,
      },
      "control-key",
    );
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/v1/commander/commands");
    expect(options.headers).toEqual({
      "X-API-Key": "control-key",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(options.body)).toMatchObject({
      command_id: "c-1",
      device_id: "d-1",
      point_id: "p-1",
      value: false,
    });
  });

  it("routes device verification to Commander", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, stages: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await runtimeApi.verifyDevice("plc/1")).toMatchObject({ ok: true });
    expect(fetchMock.mock.calls[0][0]).toContain("/devices/plc%2F1/verify");
  });
});
