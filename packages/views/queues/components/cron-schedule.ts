// Maps between the dropdown-friendly schedule shape used by QueueDialog and
// the standard 5-field cron expressions the server's scheduler (robfig/cron)
// consumes. Expressions outside these shapes surface as "custom" so an
// existing hand-written cron survives an edit round-trip untouched.

export type ScheduleFrequency = "none" | "hourly" | "daily" | "weekly" | "custom";

export interface CronSchedule {
  frequency: ScheduleFrequency;
  /** Hour of day 0–23; meaningful for daily/weekly. */
  hour: number;
  /** Day of week 0–6 (Sunday = 0, cron convention); meaningful for weekly. */
  dayOfWeek: number;
}

export function buildCron(s: CronSchedule): string {
  switch (s.frequency) {
    case "hourly":
      return "0 * * * *";
    case "daily":
      return `0 ${s.hour} * * *`;
    case "weekly":
      return `0 ${s.hour} * * ${s.dayOfWeek}`;
    default:
      return "";
  }
}

/** Parse a cron expression back into the dropdown shape; null → custom. */
export function parseCron(expr: string): CronSchedule | null {
  const trimmed = expr.trim();
  if (!trimmed) return { frequency: "none", hour: 9, dayOfWeek: 1 };
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, mon, dow] = parts;
  if (min !== "0" || dom !== "*" || mon !== "*") return null;
  if (hour === "*" && dow === "*") return { frequency: "hourly", hour: 9, dayOfWeek: 1 };
  const h = Number(hour);
  if (!Number.isInteger(h) || h < 0 || h > 23) return null;
  if (dow === "*") return { frequency: "daily", hour: h, dayOfWeek: 1 };
  const d = Number(dow);
  if (!Number.isInteger(d) || d < 0 || d > 6) return null;
  return { frequency: "weekly", hour: h, dayOfWeek: d };
}

// Most-common IANA zones for the timezone dropdown. Europe/Sofia first — it is
// the product default when a schedule is enabled.
export const TIMEZONE_OPTIONS = [
  "Europe/Sofia",
  "UTC",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Kyiv",
  "Europe/Istanbul",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
] as const;

export const DEFAULT_TIMEZONE = "Europe/Sofia";
