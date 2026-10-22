"""Restore the most recent GODLIKE paper-only SQLite artifact from prior GitHub runs.

No wallet, secrets, or live trades. Fail closed on corrupt/malformed archives.
This supports intermittent observation, NOT uninterrupted 24/7 forward testing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import zipfile

FILES = {
    "paper_forward.db",
    "paper_forward.settings.json",
    "paper_forward.status.json",
    "paper_forward.log",
    "paper_forward.runs.jsonl",
    "forward_pulse_runs.jsonl",
    "snapshot_manifest.json",
}
# Artifacts also contain published reports. Validate their known names but
# do not restore stale reports into the new run. Only state files are resumed.
REPORT_FILES = {"forward_profit_gate.json", "forward_capacity_gate.json", "forward_funnel.json", "forward_trench_funnel.json", "forward_order_stage_audit.json"}


def recognized_report(name):
    if name in REPORT_FILES:
        return True
    if name.startswith("forward_reports/"):
        relative = name[len("forward_reports/"):]
        if (relative and "/" not in relative and "\\" not in relative and
                ".." not in relative and relative.endswith((".json", ".csv"))):
            return True
    return False


MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_SQLITE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_EXTRACT_BYTES = 768 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64
MAX_ZIP_METADATA_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


def safe_extract_zip(content: bytes, target: Path):
    """Only named flat files, never symlinks, paths, duplicates or oversized input."""
    target = Path(target)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("Oversized paper state archive")
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        seen = set()
        total_size = 0
        total_compressed = 0
        metadata_size = len(z.comment)
        infos = z.infolist()
        if not infos:
            raise ValueError("Empty state archive")
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Too many paper archive members")
        for info in infos:
            metadata_size += len(info.filename.encode("utf-8")) + len(info.extra) + len(info.comment)
            if metadata_size > MAX_ZIP_METADATA_BYTES:
                raise ValueError("Oversized paper ZIP metadata")
            name = info.filename
            if not (name in FILES or recognized_report(name)) or name in seen or info.is_dir():
                raise ValueError("Unexpected, repeated, or unsafe archive member")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("Unsupported paper ZIP compression method")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted paper ZIP member is not supported")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Archive member is symlink")
            if info.file_size > (MAX_SQLITE_BYTES if name == "paper_forward.db" else MAX_FILE_BYTES):
                raise ValueError("Oversized paper file")
            if info.compress_size < 0 or info.compress_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Invalid paper ZIP compressed member size")
            total_compressed += info.compress_size
            if total_compressed > MAX_ARCHIVE_BYTES:
                raise ValueError("Oversized total compressed paper ZIP members")
            total_size += info.file_size
            if total_size > MAX_TOTAL_EXTRACT_BYTES:
                raise ValueError("Oversized total paper extraction")
            seen.add(name)
        if "paper_forward.db" not in seen:
            raise ValueError("No paper_forward.db in previous run")
        # Extract to memory first, so validation occurs before touching state.
        recovered = {}
        actual_total = 0
        for info in infos:
            if info.filename not in FILES:
                continue
            # Bound actual decompressed bytes, not only untrusted ZIP metadata.
            member_limit = MAX_SQLITE_BYTES if info.filename == "paper_forward.db" else MAX_FILE_BYTES
            with z.open(info) as member:
                chunks = []
                actual_bytes = 0
                while True:
                    chunk = member.read(min(1024 * 1024, member_limit - actual_bytes + 1))
                    if not chunk:
                        break
                    actual_bytes += len(chunk)
                    actual_total += len(chunk)
                    if actual_total > MAX_TOTAL_EXTRACT_BYTES:
                        raise ValueError("Oversized total paper extraction during decompression")
                    if actual_bytes > member_limit:
                        raise ValueError("Oversized paper file during decompression")
                    chunks.append(chunk)
                data = b"".join(chunks)
            if len(data) != info.file_size:
                raise ValueError("Paper state member has incorrect size")
            recovered[info.filename] = data
    if "snapshot_manifest.json" in recovered:
        try:
            manifest = json.loads(recovered["snapshot_manifest.json"])
            if (type(manifest) is not dict or set(manifest) != {"format", "database", "bytes", "sha256"}
                    or manifest["format"] != 1 or manifest["database"] != "paper_forward.db"
                    or type(manifest["bytes"]) is not int
                    or manifest["bytes"] != len(recovered["paper_forward.db"])
                    or type(manifest["sha256"]) is not str
                    or len(manifest["sha256"]) != 64
                    or not all(c in "0123456789abcdef" for c in manifest["sha256"])
                    or hashlib.sha256(recovered["paper_forward.db"]).hexdigest() != manifest["sha256"]):
                raise ValueError("PAPER snapshot manifest mismatch")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ValueError("Invalid PAPER snapshot manifest") from exc
        # The manifest is evidence, not a runtime state file.
        del recovered["snapshot_manifest.json"]
    # A SQLite DB without its WAL is not a complete snapshot. Fail closed
    # rather than silently accepting a potentially stale or incomplete state.
    if recovered["paper_forward.db"][:16] != bytes((83, 81, 76, 105, 116, 101, 32, 102, 111, 114, 109, 97, 116, 32, 51, 0)):
        raise ValueError("Invalid paper SQLite header")
    page_size = int.from_bytes(recovered["paper_forward.db"][16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise ValueError("Invalid paper SQLite page size")
    if len(recovered["paper_forward.db"]) % page_size:
        raise ValueError("Truncated paper SQLite page data")
    if 2 in recovered["paper_forward.db"][18:20]:
        raise ValueError("WAL-mode paper database requires a checkpointed snapshot")
    # Validate SQLite in isolation before replacing any existing PAPER state.
    with tempfile.TemporaryDirectory(prefix="paper-restore-") as scratch:
        db = Path(scratch) / "paper_forward.db"
        db.write_bytes(recovered["paper_forward.db"])
        try:
            with sqlite3.connect(db) as c:
                integrity = c.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise ValueError("Restored paper SQLite full integrity check failed")
        except sqlite3.DatabaseError as exc:
            raise ValueError("Invalid paper SQLite database") from exc
    # Never follow a symlink supplied as the restore root.
    if target.is_symlink():
        raise ValueError("Paper restore target must not be a symlink")
    # Refuse a non-directory root before publishing anything.
    if target.exists() and not target.is_dir():
        raise ValueError("Paper restore target must be a directory")
    # Stage all members before replacing existing PAPER state.
    target.mkdir(parents=True, exist_ok=True)
    # Reject both live and dangling links before moving any existing state.
    for name in recovered:
        destination = target / name
        if destination.is_symlink():
            raise ValueError("Paper restore destination file must not be a symlink")
        if destination.exists() and not destination.is_file():
            raise ValueError("Paper restore destination must be a regular file")
    with tempfile.TemporaryDirectory(prefix=".paper-restore-stage-", dir=target) as staging:
        for name, data in recovered.items():
            (Path(staging) / name).write_bytes(data)
        # Roll back on an ordinary publication error. Power-loss atomicity
        # across multiple files still requires a versioned directory design.
        backup = Path(staging) / "previous"
        backup.mkdir()
        published = []
        try:
            for name in recovered:
                destination = target / name
                had_previous = destination.exists()
                if had_previous:
                    os.replace(destination, backup / name)
                try:
                    os.replace(Path(staging) / name, destination)
                except OSError:
                    if had_previous:
                        os.replace(backup / name, destination)
                    raise
                published.append((name, had_previous))
        except OSError:
            for name, had_previous in reversed(published):
                destination = target / name
                if had_previous:
                    os.replace(backup / name, destination)
                else:
                    destination.unlink(missing_ok=True)
            raise
    return set(recovered)


def event(status, artifact_id=None, detail=None):
    result = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "artifact_id": artifact_id,
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "detail": detail,
    }
    with Path("forward_pulse_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, sort_keys=True) + "\n")
    print(f"PAPER_STATE: {status}. {detail or ''}")


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    if "/" not in repo:
        raise ValueError("Invalid repository")
    import requests
    headers = {"Authorization": f"Bearer {token}", "Accept":"application/vnd.github+json",
               "X-GitHub-Api-Version":"2022-11-28"}
    url = f"https://api.github.com/repos/{repo}/actions/artifacts"
    response = requests.get(url, headers=headers, params={"per_page":100, "name":"godlike-forward-state"}, timeout=30)
    response.raise_for_status()
    artifacts = response.json().get("artifacts", [])
    available = [a for a in artifacts if a.get("name") == "godlike-forward-state"
                 and not a.get("expired") and a.get("id")
                 and str(a.get("workflow_run",{}).get("id")) != os.getenv("GITHUB_RUN_ID")]
    if not available:
        if Path("paper_forward.db").exists():
            raise ValueError("Unexpected existing paper DB without restorable artifact")
        event("FIRST_RUN", detail="No prior paper artifact. Starting separate history.")
        return
    # A previous failed run may have uploaded ONLY diagnostics (no SQLite).
    # Skip such an explicitly incomplete archive, but never accept corrupt,
    # unknown, path-traversing or invalid SQLite data. Keep the gap visible.
    for candidate in sorted(available, key=lambda a: a.get("created_at", ""), reverse=True):
        archive = requests.get(candidate["archive_download_url"], headers=headers, timeout=(15, 90))
        archive.raise_for_status()
        try:
            restored = safe_extract_zip(archive.content, Path("."))
        except ValueError as exc:
            if str(exc) == "No paper_forward.db in previous run":
                print(f"PAPER_STATE: SKIP_REPORT_ONLY_ARTIFACT id={candidate['id']} (missing SQLite)")
                continue
            raise
        event("RESTORED", artifact_id=candidate["id"], detail=f"{len(restored)} paper state files")
        print(f"Restored prior paper state artifact ID {candidate['id']}; this is NOT continuous coverage.")
        return
    raise ValueError("Historical artifacts exist but none contains a recoverable paper SQLite database")


if __name__ == "__main__":
    main()
