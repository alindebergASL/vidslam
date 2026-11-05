# Backups and restore

`scripts/backup.sh` backs up the two things a deploy can't rebuild:

| What | How | Where |
| --- | --- | --- |
| The Postgres database (projects, renders, spend ledger, settings, encrypted YouTube token) | `pg_dump` in custom format, checked with `pg_restore --list` and a SHA-256 | `<target>/db/avs-<UTC time>.dump`, newest in `db/LATEST` |
| Media under `./data` (uploads, Studio output, renders, music) | `rsync` mirror | `<target>/data/` |

The target is a directory, usually an **Amazon S3 Files** mount, so backups land
in your S3 bucket. It can also be an `s3://bucket/prefix` URL, if you'd rather use
the AWS CLI than a mount.

**Not in the backup: `.env`.** It holds your API keys and `SESSION_SECRET`.
The YouTube refresh token in the database is encrypted with a key derived from
`SESSION_SECRET`, so a restore needs the *same* `.env`. Keep a copy somewhere
other than this server and other than the backup bucket, such as a password
manager or AWS SSM Parameter Store (SecureString).

The database itself stays on the server's local disk. Don't move Postgres's
data directory onto S3 Files or any other network file system. Only the dumps
go there.

## 1. Mount S3 Files (one time)

Commands below are from the AWS S3 Files user guide
([mounting](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-mounting.html),
[prerequisites](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-prereq-policies.html)).
Check there for the current versions.

**In the AWS console:**

1. **Bucket:** it needs **S3 Versioning enabled** and SSE-S3 or SSE-KMS encryption.
   Versioning also keeps earlier copies of anything the backup overwrites or deletes.
2. **Mount target:** your S3 file system needs one **in the same Availability Zone
   as the EC2 instance**.
3. **Security groups:** NFS, TCP **2049**.
   - On the instance's group: outbound to the mount target's security group.
   - On the mount target's group: inbound from the instance's security group.
4. **IAM instance profile:** attach one to the EC2 instance with
   `AmazonS3FilesClientFullAccess`, plus `s3:GetObject`, `s3:GetObjectVersion`
   on `arn:aws:s3:::<bucket>/*` and `s3:ListBucket` on `arn:aws:s3:::<bucket>`.

**On the server:**

```bash
# Mount helper (amazon-efs-utils 3.0.0 or later). Ubuntu, per the AWS guide:
curl https://amazon-efs-utils.aws.com/efs-utils-installer.sh | sudo sh -s -- --install

sudo mkdir -p /mnt/s3files
FS=fs-0123456789abcdef0            # your S3 file system ID
sudo mount -t s3files $FS:/ /mnt/s3files
findmnt -T /mnt/s3files             # should show the s3files mount

# Mount at boot. Keep _netdev: without it the instance can hang at boot.
echo "$FS:/ /mnt/s3files s3files _netdev,nofail 0 0" | sudo tee -a /etc/fstab
sudo umount /mnt/s3files && sudo mount -a && findmnt -T /mnt/s3files

sudo mkdir -p /mnt/s3files/avs-backups
sudo chown "$USER" /mnt/s3files/avs-backups
```

The mount always uses TLS and IAM auth, so no keys go on the server.

## 2. Configure and run the first backup

Add to `.env`:

```bash
BACKUP_TARGET=/mnt/s3files/avs-backups
BACKUP_KEEP_DAYS=14        # dumps older than this are pruned
# ALERT_WEBHOOK_URL=...    # already used by the app; backups post failures here too
```

Then, from the repo directory on the server:

```bash
sudo apt-get install -y rsync    # if it isn't installed already
scripts/backup.sh
scripts/restore.sh --list        # you should see avs-<time>.dump and LATEST
```

The script refuses to run if `BACKUP_TARGET` doesn't exist. A mount that
silently dropped would otherwise "succeed" by writing to the empty local folder
underneath it.

## 3. Schedule it

Nightly at 03:17 UTC (`crontab -e` as the user that owns the repo, in the
`docker` group):

```cron
17 3 * * * cd /home/ubuntu/vidslam && scripts/backup.sh >> /var/log/avs-backup.log 2>&1
```

(Create the log file once: `sudo touch /var/log/avs-backup.log && sudo chown $USER /var/log/avs-backup.log`.)

A failed run exits non-zero, logs the reason, and posts to `ALERT_WEBHOOK_URL`.
Runs never overlap.

## 4. Restore

`scripts/restore.sh` shows what it's about to replace and asks you to type
`restore`:

```bash
scripts/restore.sh --list                      # available dumps
scripts/restore.sh --db latest --data          # full restore: database + media
scripts/restore.sh --db avs-20261105T031700Z.dump   # one specific night, database only
scripts/restore.sh --data                      # media only
```

What it does, in order:

1. **Checks the dump** against its checksum and reads it with `pg_restore --list`.
   A damaged dump stops the restore before anything is touched.
2. **Takes a safety dump** of the current database into
   `<target>/db/pre-restore-<time>.dump`, so a restore can itself be undone.
3. **Stops** the backend, worker and frontend. Postgres keeps running.
4. **Restores:** recreates the database and loads the dump; and/or mirrors
   `<target>/data/` back into `./data`. Files added after the backup are removed.
5. **Starts** the services again. Migrations run at boot, so a dump taken on
   an older version is upgraded to the current schema.

### Restoring onto a new server

When the old instance is gone:

1. Follow the README's EC2 runbook up to `docker compose … up -d` (Postgres must
   be running).
2. Put the **saved `.env`** in place.
3. Mount S3 Files (step 1 above).
4. Run `scripts/restore.sh --db latest --data`.
5. Point DNS at the new instance and run certbot again.

## 5. Practice a restore

A backup you've never restored is a guess. Once after setup, and then every few
months:

```bash
scripts/restore.sh --db latest --data --yes   # on a staging copy, or during a quiet hour
curl -sf https://<domain>/readyz
```

Then open a project and play a render.

`scripts/test_backup_restore.sh` runs the same round trip against a scratch
database: back up, damage it, restore, check. It also checks the failure paths.
CI runs it on every push.
