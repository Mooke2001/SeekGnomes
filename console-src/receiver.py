"""Private intake for a loopback listener behind the publisher's HTTPS proxy."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import islice
import os
from pathlib import Path
import re
import sqlite3
import stat

if __package__:
    from .sharing import (DIGEST, MAX_SUBMISSION_BYTES, ROUTE,
                          canonical_json, decode_json, validated_submission_id)
else:
    from sharing import (DIGEST, MAX_SUBMISSION_BYTES, ROUTE,
                         canonical_json, decode_json, validated_submission_id)


class PendingStore:
    """Append an accepted raw envelope to a private, content-addressed pool."""

    def __init__(self, database: Path):
        self.database = database.absolute()
        with self._database_file() as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("The intake database must be a private regular file.")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status = 'pending'),
                    received_utc TEXT NOT NULL,
                    raw_payload BLOB NOT NULL
                )
            """)

    def _database_file(self):
        if any(parent.is_symlink() for parent in self.database.parents):
            raise ValueError("The intake database directory may not traverse symlinks.")
        fd = os.open(self.database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        return os.fdopen(fd, "rb")

    def add(self, submission_id: str, raw_payload: bytes) -> bool:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            insertion = connection.execute("""
                INSERT INTO submissions (submission_id, received_utc, raw_payload)
                VALUES (?, ?, ?)
                ON CONFLICT(submission_id) DO NOTHING
            """, (submission_id, datetime.now(timezone.utc).isoformat(), raw_payload))
            return insertion.rowcount == 1


class IntakeServer(HTTPServer):
    def __init__(self, server_address, store):
        self.store = store
        super().__init__(server_address, IntakeHandler)


class IntakeHandler(BaseHTTPRequestHandler):
    server_version = "SeekGnomesIntake/1"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 20

    def log_message(self, *_args):
        # Keep submitted contents and identifying headers out of service logs.
        return

    def _reply(self, status, value):
        body = canonical_json(value)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != ROUTE:
            self._reply(404, {"error": "not_found"})
            return
        media = self.headers.get_all("Content-Type") or []
        encodings = self.headers.get_all("Content-Encoding") or []
        if (media not in (["application/json"], ["application/json; charset=utf-8"])
                or encodings not in ([], ["identity"])):
            self._reply(415, {"error": "expected_utf8_json"})
            return
        if self.headers.get_all("Transfer-Encoding"):
            self._reply(400, {"error": "transfer_encoding_unsupported"})
            return
        lengths = self.headers.get_all("Content-Length") or []
        keys = self.headers.get_all("Idempotency-Key") or []
        if (len(lengths) != 1 or re.fullmatch(r"[0-9]{1,9}", lengths[0]) is None
                or len(keys) != 1 or DIGEST.fullmatch(keys[0]) is None):
            self._reply(400, {"error": "invalid_request_headers"})
            return
        length = int(lengths[0])
        if length > MAX_SUBMISSION_BYTES:
            self._reply(413, {"error": "submission_too_large"})
            return
        try:
            raw = self.rfile.read(length)
            # The only potentially large strings are base64 and do not need JSON
            # escapes. Bound the token count before letting the decoder allocate
            # Python containers from a publicly supplied document.
            separators = re.finditer(rb'[\[\]{},:"]', raw)
            if (len(raw) != length or b"\\" in raw
                    or next(islice(separators, 25000, 25001), None) is not None):
                raise ValueError
            submission_id = validated_submission_id(decode_json(raw))
            if keys[0] != submission_id:
                raise ValueError
        except (OSError, ValueError, RecursionError):
            self._reply(400, {"error": "invalid_submission"})
            return
        try:
            inserted = self.server.store.add(submission_id, raw)
        except sqlite3.Error:
            self._reply(503, {"error": "intake_unavailable"})
            return
        self._reply(201 if inserted else 200, {"status": "pending", "submission_id": submission_id,
                                                "duplicate": not inserted})

    def do_GET(self):
        self._reply(404, {"error": "not_found"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite database in the publisher's private storage")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    store = PendingStore(args.db)
    with IntakeServer(("127.0.0.1", args.port), store) as server:
        host, port = server.server_address
        print(f"Private SeekGnomes intake listening on {host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
