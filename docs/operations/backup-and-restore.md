# Backup and restore

The bundled backup captures PostgreSQL schema/data and global role definitions.
It does **not** copy R2 object bytes, Cloudflare configuration, secrets, or the
Google/model cache. Protect and test those recovery domains separately.

## Recovery objectives

The example daily timer provides at best a 24-hour database RPO; operational
delays make the real RPO longer. Set an explicit business RPO/RTO with event
organizers. If 24 hours is unacceptable, use a managed PostgreSQL service or
WAL archiving/PITR and test it. A reasonable initial target for the scripted
path is RPO 24 hours and RTO 4 hours, subject to measured database/R2 size.

Use an R2/object-store recovery policy that prevents database rows from
outliving recoverable objects. Bucket versioning/retention is not a substitute
for the application's privacy deletion requirements; define how a required
biometric deletion propagates into backups and expired object versions.

## Configure encrypted off-host backups

Install `age` and `rclone` on the database host. Store the age private identity
offline and give the host only its public recipient. Configure an off-host
remote with versioning/object lock or equivalent immutability and credentials
that cannot delete prior backup generations.

```bash
install -d -m 0700 ~/.config/raceframe
sudo install -d -m 0700 -o "$USER" -g "$(id -gn)" /var/backups/raceframe
cp deploy/systemd/raceframe-backup.env.example \
  ~/.config/raceframe/backup.env
chmod 0600 ~/.config/raceframe/backup.env
cp deploy/systemd/raceframe-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now raceframe-backup.timer
systemctl --user start raceframe-backup.service
journalctl --user -u raceframe-backup.service --since today
```

The timer's service account needs Docker socket access and lingering
(`sudo loginctl enable-linger "$USER"`) when it must run without an active login.

`scripts/backup-postgres.sh` creates encrypted custom-format database and SQL
globals dumps, hashes the ciphertext, and uploads all three files with
`rclone --immutable`. Alert on every failed/missed timer. Monitor local and
remote capacity, define retention generations, and prune only through a
separately reviewed, recoverable process.

## Monthly isolated restore test

Download one complete generation and verify the ciphertext before decrypting:

```bash
sha256sum --check raceframe-YYYYMMDDTHHMMSSZ.sha256
export BACKUP_FILE="$PWD/raceframe-YYYYMMDDTHHMMSSZ.dump.age"
export AGE_IDENTITY_FILE=/secure/offline-mounted/raceframe-backup.agekey
export RESTORE_DATABASE=raceframe_YYYYMM_restore_test
scripts/restore-postgres.sh
```

The script refuses an existing database and, by default, any target not ending
in `_restore_test`. It leaves a failed new database in place for investigation.
After the restore:

1. Confirm `alembic_version`, schema/object ownership, constraints, table row
   counts, and representative organizer/event/participant/job records.
2. Point an isolated backend at the restored DB and a non-production/test
   bucket boundary. Run `/readyz` and application smoke tests without sending
   customer messages or mutating production R2.
3. Run storage reconciliation against the corresponding object inventory. A DB
   restore alone cannot recreate missing photos.
4. Measure end-to-end restore time and document every manual credential/config
   dependency. Securely drop the test database after evidence is retained.

The encrypted globals file is for disaster reconstruction of roles. Review it
before restore because it can contain password verifiers and role attributes.
Routine isolated restores into the existing cluster normally reuse existing
roles and do not apply the globals dump.

## Disaster recovery

Build a replacement host from reviewed Compose files, create/recover roles,
restore into a new database, apply only rehearsed migrations, restore the
matching R2/configuration state, and validate privately. Change DNS/tunnel
routing only after ready, smoke, authorization, object, and worker checks pass.
Keep the old database read-only until recovery is accepted.

Treat backup material as customer data. Restrict access, audit downloads,
rotate exposed identities, and record deletion/retention obligations.
