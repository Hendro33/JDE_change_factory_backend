#!/usr/bin/env python3
"""
Consistent backup / restore for a single Jade instance.

Run it on the machine (or Render shell) where the API runs, with the SAME
environment variables (JDE_API_DATA_DIR, JDE_BACKLOG_DIR, JDE_CHANGE_DIR,
JDE_EVIDENCE_DIR, JDE_CREDENTIAL_KEY), so it sees the same data and the
same pause flag:

    python3 scripts/jade_backup.py backup  --out /tmp/jade-YYYYMMDD-HHMM.tar.gz
    python3 scripts/jade_backup.py verify  --archive /tmp/jade-....tar.gz
    python3 scripts/jade_backup.py restore --archive /tmp/jade-....tar.gz [--replace-existing]

backup   pauses writes (the API answers 503 to changes; reads keep working;
         no JDE attempt can start), waits a moment for in-flight requests,
         refuses if an agent run or JDE attempt is in progress, copies
         SQLite with its online-backup API and every JSON record, writes
         manifest.json (checksums, state summary, credential key IDs --
         never the key) and resumes writes.
verify   checks every checksum and reports whether the current
         JDE_CREDENTIAL_KEY (or JDE_CREDENTIAL_KEY_PREVIOUS) can read the
         archived Jira tokens.
restore  verifies first, then restores under a write pause. Current data
         is moved aside to <dir>.pre-restore-<timestamp>, never deleted.
         Restart the service afterwards.

See docs/OPERATIONS.md ("Backup and restore", "Credential encryption key").
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api_service"))
sys.path.insert(0, os.path.join(ROOT, "mcp_server"))

from jde_api_service.services import backup_restore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--out", required=True)
    b.add_argument("--by", default=os.environ.get("USER", "operator"))
    b.add_argument("--settle-seconds", type=float, default=2.0)
    v = sub.add_parser("verify")
    v.add_argument("--archive", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--archive", required=True)
    r.add_argument("--by", default=os.environ.get("USER", "operator"))
    r.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()

    try:
        if args.cmd == "backup":
            manifest = backup_restore.create_backup(args.out, by=args.by, settle_seconds=args.settle_seconds)
            s = manifest["summary"]
            print(json.dumps({
                "archive": os.path.abspath(args.out), "created_at": manifest["created_at"],
                "files": len(manifest["files"]), "changes": len(s["changes"]), "memberships": len(s["memberships"]),
                "credential_key_ids_needed": s["credential_key_ids_needed"],
            }, indent=2))
        elif args.cmd == "verify":
            report = backup_restore.verify_archive(args.archive)
            report.pop("manifest")
            print(json.dumps({"checksums": "ok", **report}, indent=2))
        else:
            report = backup_restore.restore_backup(args.archive, by=args.by, replace_existing=args.replace_existing)
            print(json.dumps(report, indent=2))
            if not report["credentials_readable"]:
                print("\nWARNING: set JDE_CREDENTIAL_KEY (or JDE_CREDENTIAL_KEY_PREVIOUS) to the key with id "
                      f"{', '.join(report['missing_key_ids'])} before restarting, or re-enter the Jira token.",
                      file=sys.stderr)
            if report["sqlite_integrity"] != "ok" or not report["matches_backup"]:
                return 1
    except (backup_restore.BackupRefused, backup_restore.RestoreRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
