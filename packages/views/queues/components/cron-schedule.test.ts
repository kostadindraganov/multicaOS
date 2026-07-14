import { describe, expect, it } from "vitest";
import { buildCron, parseCron, DEFAULT_TIMEZONE, TIMEZONE_OPTIONS } from "./cron-schedule";

describe("cron-schedule", () => {
  it("builds expressions for each frequency", () => {
    expect(buildCron({ frequency: "hourly", hour: 9, dayOfWeek: 1 })).toBe("0 * * * *");
    expect(buildCron({ frequency: "daily", hour: 14, dayOfWeek: 1 })).toBe("0 14 * * *");
    expect(buildCron({ frequency: "weekly", hour: 8, dayOfWeek: 5 })).toBe("0 8 * * 5");
    expect(buildCron({ frequency: "none", hour: 9, dayOfWeek: 1 })).toBe("");
  });

  it("round-trips built expressions back to the same schedule", () => {
    for (const frequency of ["hourly", "daily", "weekly"] as const) {
      const schedule = { frequency, hour: 17, dayOfWeek: 3 };
      const parsed = parseCron(buildCron(schedule));
      expect(parsed?.frequency).toBe(frequency);
      if (frequency !== "hourly") expect(parsed?.hour).toBe(17);
      if (frequency === "weekly") expect(parsed?.dayOfWeek).toBe(3);
    }
  });

  it("parses empty as no schedule", () => {
    expect(parseCron("")?.frequency).toBe("none");
    expect(parseCron("  ")?.frequency).toBe("none");
  });

  it("returns null (custom) for expressions the dropdowns cannot represent", () => {
    expect(parseCron("0 9 * * 1-5")).toBeNull(); // weekday range
    expect(parseCron("*/15 * * * *")).toBeNull(); // sub-hourly
    expect(parseCron("0 9 1 * *")).toBeNull(); // day-of-month
    expect(parseCron("0 9 * 6 *")).toBeNull(); // month-bound
    expect(parseCron("garbage")).toBeNull();
    expect(parseCron("0 25 * * *")).toBeNull(); // hour out of range
  });

  it("defaults timezone to Europe/Sofia and lists it first", () => {
    expect(DEFAULT_TIMEZONE).toBe("Europe/Sofia");
    expect(TIMEZONE_OPTIONS[0]).toBe("Europe/Sofia");
  });
});
