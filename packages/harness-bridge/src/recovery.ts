import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { platform, uptime } from "node:os";

export type BootIdentity = {
  machineId: string;
  bootId: string;
  bootStartedAt: number;
  observedAt: number;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function validateBootIdentity(value: unknown): BootIdentity {
  const v = value as BootIdentity | null;
  if (
    !v ||
    !/^[a-f0-9]{64}$/.test(v.machineId) ||
    !uuid.test(v.bootId) ||
    /^0+$/.test(v.bootId.replaceAll("-", "")) ||
    !Number.isFinite(v.bootStartedAt) ||
    !Number.isFinite(v.observedAt) ||
    v.bootStartedAt <= 0 ||
    v.observedAt < v.bootStartedAt
  )
    throw new Error("invalid recovery boot evidence");
  return v;
}
/** Local OS evidence only; no network/CLI-supplied boot or machine override. */
export function readBootIdentity(): BootIdentity {
  const os = platform();
  let bootId: string, machine: string;
  if (os === "darwin") {
    const options = {
      encoding: "utf8" as const,
      timeout: 2000,
      maxBuffer: 64 * 1024,
    };
    bootId = execFileSync(
      "/usr/sbin/sysctl",
      ["-n", "kern.bootsessionuuid"],
      options,
    ).trim();
    const hardware = execFileSync(
      "/usr/sbin/ioreg",
      ["-rd1", "-c", "IOPlatformExpertDevice"],
      options,
    );
    machine =
      hardware.match(/"IOPlatformUUID"\s*=\s*"([a-f0-9-]+)"/i)?.[1] ?? "";
    if (!uuid.test(machine) || /^0+$/.test(machine.replaceAll("-", "")))
      throw new Error("machine identity unavailable");
  } else if (os === "linux") {
    bootId = readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
    machine = readFileSync("/etc/machine-id", "utf8").trim();
    if (!/^[a-f0-9]{32}$/i.test(machine) || /^0+$/.test(machine))
      throw new Error("machine identity unavailable");
  } else throw new Error("verified reboot recovery requires macOS or Linux");
  const observedAt = Date.now();
  return validateBootIdentity({
    machineId: createHash("sha256")
      .update(os + "\0" + machine.toLowerCase())
      .digest("hex"),
    bootId: bootId.toLowerCase(),
    bootStartedAt: observedAt - uptime() * 1000,
    observedAt,
  });
}
