# Backup, restore and incident exercise

Use this runbook on the actual production host. A local copy test does not prove
that production credentials, storage, permissions, monitoring or recovery time
objectives work.

## Preparation

Record the release hash, database path, backup destination, responsible operator,
approved maintenance window, recovery point objective and recovery time
objective. Confirm that the backup destination is separate from the production
volume and that access is restricted.

## Exercise

1. Record table counts and create a SQLite backup with
   `python disaster_recovery.py <new-backup-path>`.
2. Copy the backup to an isolated restore location and open it read-only.
3. Require `PRAGMA integrity_check` to return `ok`; compare session, delivery,
   tenant-document and privacy-request counts and selected tenant-scoped records.
4. Start the same signed FrontDesk build against the isolated restore. Exercise
   health, authentication, one conversation and webhook replay handling without
   contacting real users.
5. Simulate loss of the primary process, execute the documented incident roles
   and communications, and measure detection, decision and recovery times.
6. Return to production only through the approved change process. Preserve the
   exercise evidence and securely dispose of the isolated copy under the
   retention schedule.

## Acceptance

Pass only when integrity and tenant boundaries are preserved, the measured RPO
and RTO meet the approved targets, monitoring detects the event, authorised
communications are completed and every material finding has an owner and due
date. Record host, operators and timestamps; do not label a workstation-only
exercise as production recovery.
