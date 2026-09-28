# Local harness bridge recovery

An interrupt whose app-server exit cannot be confirmed stays blocked in the local
journal. Its workspace remains owned until backend reconciliation commits terminal
evidence. Restarting the bridge alone does not clear this uncertainty.

Use the local recovery command on the **original machine**, with the same bridge
state directory. Stop the bridge before recovery. Do not copy the journal to a
different machine or edit its SQLite records.

```sh
# List the interrupt command IDs that need operator recovery.
pnpm --filter @nous/harness-bridge start recover-interrupt --store /path/to/state

# Recover exactly one interrupt command (not its run ID).
pnpm --filter @nous/harness-bridge start recover-interrupt --store /path/to/state --command UUID
```

Recovery requires a verified reboot. The journal records the OS boot UUID, a hash
of the machine identity, and the observation time when exit becomes uncertain.
The command refuses the same boot, a different machine, unavailable or malformed
OS evidence, and evidence that does not predate the current boot by more than five
seconds. On macOS it reads `kern.bootsessionuuid` and the platform hardware UUID;
on Linux it reads the kernel boot ID and `/etc/machine-id`. Other platforms fail
closed. Raw machine identifiers are not stored.

For a legacy marker without boot evidence, the first command records a baseline
and **refuses to clear the blocker**. Wait at least ten seconds, reboot the original
machine, and run the same command again. This baseline procedure also applies if
OS evidence was unavailable when uncertainty was recorded. If OS identity cannot
be read, restore access to the normal OS identity sources and retry; there is no
force flag or database-edit bypass. A changed machine identity requires restoring
the original identity before recovery can be verified.

After successful recovery, restart the bridge to reconcile and redeliver the exact
interrupt when authorized. Recovery deletes only that interrupt's uncertainty,
execution claim, and recovery evidence. It does not replay a start, mark a run
complete, change command state, or release a workspace lock. Canonical terminal
evidence and its acknowledgement remain necessary to release ownership.

Use `pnpm --filter @nous/harness-bridge start recover-interrupt --help` for local
instructions. Node 24 and pnpm 10.18.2 are required.
