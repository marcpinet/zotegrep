"""Synchronization with the Zotero Web API: metadata, PDFs, collections, tag
colors, notes and annotations."""
import os
import re
import json
import time
import sqlite3
import logging
import threading
from pathlib import Path

from pyzotero import zotero
from pyzotero.errors import ResourceNotFoundError

from zotegrep import store
from zotegrep.config import Library
from zotegrep.text import NO_TEXT_MAX_CHARS, extract_pages, fold, normalize_text, strip_html

logger = logging.getLogger(__name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9]{8}$")

# Failed indexings (key, version): retried once per process, so that a broken
# PDF is not reopened at every sync.
_index_failed: set[tuple[str, int]] = set()


# --------------------------------------------------------------------------
# Local Zotero storage
# --------------------------------------------------------------------------

def local_storage_dir() -> Path | None:
    """Zotero desktop's own storage folder (<dir>/<attachment key>/<file>).

    Files that Zotero syncs through WebDAV never reach zotero.org, so the web
    API answers 404 for them; they are only on this machine. The folder is
    ~/Zotero/storage unless ZOTEGREP_ZOTERO_STORAGE says otherwise."""
    raw = os.environ.get("ZOTEGREP_ZOTERO_STORAGE")
    path = Path(raw).expanduser() if raw else Path.home() / "Zotero" / "storage"
    return path if path.is_dir() else None


def read_local_pdf(storage: Path | None, key: str, filename: str) -> bytes | None:
    """The attachment's PDF from Zotero's local storage, or None if it is not
    there. The key and the file name come from the API: neither may lead
    outside the storage folder."""
    if storage is None or not _ITEM_KEY_RE.match(key):
        return None
    folder = storage / key
    if not folder.is_dir():
        return None
    name = Path(filename).name
    exact = folder / name if name and not name.startswith(".") else None
    try:
        if exact is not None and exact.is_file():
            return exact.read_bytes()
        # Renamed on disk: accept the folder's only visible file if it is a PDF.
        visible = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
        if len(visible) == 1:
            data = visible[0].read_bytes()
            if data.startswith(b"%PDF"):
                return data
    except OSError as e:
        logger.warning("Cannot read local copy of %s: %s", key, e)
    return None


# --------------------------------------------------------------------------
# Zotero metadata
# --------------------------------------------------------------------------

def _format_authors(creators: list[dict]) -> str:
    names = []
    for c in creators:
        if c.get("creatorType") != "author":
            continue
        last = c.get("lastName", "") or c.get("name", "")
        first = c.get("firstName", "")
        if last:
            names.append(f"{last}, {first[0]}." if first else last)
    if not names:
        return ""
    if len(names) <= 3:
        return " ; ".join(names)
    return f"{names[0]} et al."


def _format_authors_full(creators: list[dict]) -> str:
    names = []
    for c in creators:
        if c.get("creatorType") not in ("author", "editor", None):
            continue
        name = c.get("name") or " ".join(p for p in (c.get("firstName", ""), c.get("lastName", "")) if p)
        if name:
            names.append(name.strip())
    return "; ".join(names)


def _resolve_authors(parent: dict | None, att_data: dict) -> str:
    authors = _format_authors(parent.get("creators", [])) if parent else ""
    if authors:
        return authors
    # Standalone PDF named the Zotero way: "Author - 2020 - Title.pdf"
    m = re.match(r'^(.+?)\s*-\s*\d{4}\s*-', att_data.get("filename", ""))
    return m.group(1).strip() if m else ""


def _resolve_date(parent: dict | None, att_data: dict) -> str:
    raw = parent.get("date", "") if parent else ""
    if raw:
        m = re.search(r'\b(1[89]\d{2}|20\d{2})\b', raw)
        return m.group(1) if m else raw.strip()
    m = re.search(r'-\s*(\d{4})\s*-', att_data.get("filename", ""))
    return m.group(1) if m else ""


def _resolve_title(parent: dict | None, att_data: dict) -> str:
    raw = (parent.get("title", "") if parent else "") or att_data.get("title", "") or att_data.get("filename", "")
    if raw.strip().upper() == "PDF":
        raw = att_data.get("filename", raw)
    raw = raw.removesuffix(".pdf").strip()
    raw = re.sub(r'^[^-]+-\s*\d{4}\s*-\s*', '', raw).strip()
    return raw or "Unknown"


def item_metadata(parent: dict | None, att_data: dict) -> dict:
    source = parent or att_data
    return {
        "title": _resolve_title(parent, att_data),
        "authors": _resolve_authors(parent, att_data),
        "authors_full": _format_authors_full(parent.get("creators", [])) if parent else "",
        "date": _resolve_date(parent, att_data),
        "abstract": (parent.get("abstractNote", "") if parent else "").strip(),
        "tags": json.dumps(sorted({t["tag"] for t in source.get("tags", []) if t.get("tag")}), ensure_ascii=False),
        "collections": json.dumps(source.get("collections", [])),
        "item_type": parent.get("itemType", "") if parent else "",
        "publication": (
            parent.get("publicationTitle") or parent.get("proceedingsTitle")
            or parent.get("bookTitle") or parent.get("conferenceName")
            or parent.get("repository") or parent.get("publisher") or ""
        ) if parent else "",
        "doi": parent.get("DOI", "") if parent else "",
        "url": parent.get("url", "") if parent else "",
    }


def dedupe_attachments(pdf_atts: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """One copy per file: same md5 (or, failing that, same name). The choice is
    deterministic (attached to a parent first, then the oldest) so the same
    copy is kept from one sync to the next. Also returns the mapping
    dropped key -> kept key, to attach annotations to the kept copy."""
    best: dict[str, tuple] = {}
    idents: dict[str, str] = {}
    for a in pdf_atts:
        d = a["data"]
        ident = d.get("md5") or d.get("filename") or d["key"]
        idents[d["key"]] = ident
        rank = (not d.get("parentItem"), d.get("dateAdded", ""), d["key"])
        if ident not in best or rank < best[ident][0]:
            best[ident] = (rank, a)
    kept_for = {key: best[ident][1]["data"]["key"] for key, ident in idents.items()}
    return [a for _, a in best.values()], kept_for


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

class ZoteroSync:
    def __init__(self, library: Library, client=None):
        self.library = library
        self.library_id = library.id
        self.zot = client or zotero.Zotero(library.zotero_id, library.type, library.api_key)
        self._library_tag = f"{library.type}:{library.zotero_id}"
        self._cancel = False
        self._progress_cb = None
        self._waiting = 0
        self._local = 0
        self._storage = local_storage_dir()

    def cancel(self):
        self._cancel = True

    def sync(self, progress_cb=None):
        self._cancel = False
        self._progress_cb = progress_cb
        self._waiting = 0
        self._local = 0

        self._report("Checking library version...")
        library_version = f"{self._library_tag}:{self.zot.last_modified_version()}"

        conn = store.get_db()
        try:
            failed = self._reindex_pending(conn)
            if self._cancel:
                self._report("Sync cancelled")
                return

            # Library unchanged since the last complete sync: no need to list
            # every item and attachment again (several paginated requests).
            if self._get_meta(conn, "lib_version") == library_version:
                # PDFs that could not be downloaded last time are retried on
                # their own, without listing the library.
                waiting = json.loads(self._get_meta(conn, "waiting_files") or "[]")
                if waiting:
                    failed += self._download(conn, waiting)
                if not self._cancel:
                    self._report(self._outcome("Everything up to date", failed))
                return

            failed += self._sync_library(conn)
            if not failed and not self._cancel:
                self._set_meta(conn, "lib_version", library_version)
            if not self._cancel:
                self._report(self._outcome("Sync complete", failed))
        finally:
            conn.close()

    def _outcome(self, done: str, failed: int) -> str:
        notes = [f"{failed} failed"] if failed else []
        if self._local:
            notes.append(f"{self._local} PDF{'s' if self._local > 1 else ''} read from local Zotero storage")
        if self._waiting:
            notes.append(f"{self._waiting} PDF{'s' if self._waiting > 1 else ''} not uploaded to Zotero yet")
        return f"{done} ({', '.join(notes)})" if notes else done

    def _sync_library(self, conn: sqlite3.Connection) -> int:
        self._report("Fetching library metadata...")
        parents = {it["data"]["key"]: it["data"] for it in self.zot.everything(self.zot.top())}

        self._report("Fetching attachment list...")
        attachments = self.zot.everything(self.zot.items(itemType="attachment"))
        pdf_atts, kept_for = dedupe_attachments([
            a for a in attachments
            if a["data"].get("contentType") == "application/pdf"
            # Linked files stay on the user's disk: the Zotero API has nothing
            # to download.
            and a["data"].get("linkMode") not in ("linked_file", "linked_url")
        ])
        self._report(f"Found {len(pdf_atts)} PDF attachments in library")

        existing = {
            row["zotero_key"]: row
            for row in conn.execute(
                "SELECT zotero_key, version FROM items WHERE library_id=?", (self.library_id,)
            ).fetchall()
        }

        remote_keys = set()
        to_download = []
        metadata_updates = []
        for att in pdf_atts:
            d = att["data"]
            key, version = d["key"], d["version"]
            remote_keys.add(key)
            parent_key = d.get("parentItem", "")
            meta = item_metadata(parents.get(parent_key), d)
            prev = existing.get(key)
            if prev is None or prev["version"] < version or not store.pdf_path(self.library_id, key).exists():
                to_download.append({
                    "key": key, "parent_key": parent_key, "version": version,
                    "filename": d.get("filename", f"{key}.pdf"), "meta": meta,
                })
            else:
                metadata_updates.append((*(meta[c] for c in store.METADATA_COLUMNS), parent_key, key))

        conn.executemany(
            f"UPDATE items SET {', '.join(c + '=?' for c in store.METADATA_COLUMNS)}, parent_key=? "
            "WHERE zotero_key=?",
            metadata_updates,
        )
        conn.commit()

        removed = set(existing) - remote_keys
        if removed:
            for key in removed:
                store.delete_item(conn, self.library_id, key)
            conn.commit()
            self._report(f"Removed {len(removed)} deleted items")

        failed = self._download(conn, to_download)
        # After the downloads: notes attach to PDFs already in the database.
        self._sync_collections(conn)
        self._sync_tag_colors(conn)
        self._sync_extras(conn, remote_keys, kept_for)
        return failed

    def _download(self, conn: sqlite3.Connection, to_download: list[dict]) -> int:
        """Download and index PDFs. A 404 is not a failure: Zotero syncs an item
        before its file, which arrives later. Whatever could not be downloaded
        is remembered (waiting_files) and retried on its own at the next sync,
        even if the library has not changed in between."""
        if to_download:
            self._report(f"Downloading {len(to_download)} new/updated PDFs...")
        store.library_dir(self.library_id).mkdir(parents=True, exist_ok=True)

        failed = 0
        pending = []
        for i, item in enumerate(to_download, 1):
            if self._cancel:
                pending += to_download[i - 1:]
                self._report("Sync cancelled")
                break

            self._report(f"[{i}/{len(to_download)}] {item['filename']}")
            path = store.pdf_path(self.library_id, item["key"])
            try:
                # Local Zotero storage first: with WebDAV file sync the web
                # API has no file to give (404).
                pdf_bytes = read_local_pdf(self._storage, item["key"], item["filename"])
                if pdf_bytes is not None:
                    self._local += 1
                else:
                    pdf_bytes = self.zot.file(item["key"])
                tmp = path.with_suffix(".part")
                tmp.write_bytes(pdf_bytes)
                tmp.replace(path)

                meta = item["meta"]
                conn.execute("DELETE FROM pages WHERE zotero_key=?", (item["key"],))
                conn.execute(f"""
                    INSERT OR REPLACE INTO items
                        (zotero_key, library_id, parent_key, filename, version, indexed,
                         {', '.join(store.METADATA_COLUMNS)})
                    VALUES (?, ?, ?, ?, ?, 0, {', '.join('?' for _ in store.METADATA_COLUMNS)})
                """, (
                    item["key"], self.library_id, item["parent_key"], item["filename"], item["version"],
                    *(meta[c] for c in store.METADATA_COLUMNS),
                ))
                # Commit before indexing: if indexing fails, the item stays at
                # indexed=0 with its file and _reindex_pending picks it up from
                # disk later, without downloading it again.
                conn.commit()
            except ResourceNotFoundError:
                self._waiting += 1
                pending.append(item)
                self._report(f"Not uploaded to Zotero yet: {item['filename']}")
                continue
            except Exception as e:
                conn.rollback()
                failed += 1
                pending.append(item)
                logger.warning("Download failed for %s: %s", item["key"], e)
                self._report(f"Failed: {item['filename']} ({e})")
                continue
            if not self._index_pdf(conn, item["key"], item["version"]):
                failed += 1
                self._report(f"Indexing failed: {item['filename']}")
        self._set_meta(conn, "waiting_files", json.dumps(pending, ensure_ascii=False))
        return failed

    def _sync_collections(self, conn: sqlite3.Connection):
        collections = self.zot.everything(self.zot.collections())
        conn.execute("DELETE FROM collections WHERE library_id=?", (self.library_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO collections VALUES (?, ?, ?, ?)",
            [
                (self.library_id, c["data"]["key"], c["data"].get("name", ""),
                 c["data"].get("parentCollection") or None)
                for c in collections
            ],
        )
        conn.commit()

    def _sync_tag_colors(self, conn: sqlite3.Connection):
        """Colors given to tags in Zotero (the library's synced `tagColors`
        setting). Optional: a failure does not stop the sync."""
        try:
            value = (self.zot.settings().get("tagColors") or {}).get("value") or []
        except Exception as e:
            logger.warning("Could not fetch tag colors: %s", e)
            return
        colors = {
            t["name"]: t["color"] for t in value
            if isinstance(t, dict) and t.get("name") and _HEX_COLOR_RE.match(t.get("color") or "")
        }
        self._set_meta(conn, "tag_colors", json.dumps(colors))

    def _sync_extras(self, conn: sqlite3.Connection, pdf_keys: set[str], kept_for: dict[str, str]):
        """Notes (attached to the parent item) and annotations (attached to the
        PDF). Small enough to be replaced as a whole at each full sync."""
        self._report("Fetching notes and annotations...")
        raw = self.zot.everything(self.zot.items(itemType="note || annotation"))
        pdfs_of_parent: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT zotero_key, parent_key FROM items WHERE library_id=?", (self.library_id,)
        ).fetchall():
            pdfs_of_parent.setdefault(row["parent_key"] or "", []).append(row["zotero_key"])

        rows = []
        for it in raw:
            d = it["data"]
            parent = d.get("parentItem", "")
            if d["itemType"] == "annotation":
                text = "\n".join(p for p in (d.get("annotationText", ""), d.get("annotationComment", "")) if p)
                try:
                    page = int(json.loads(d.get("annotationPosition") or "{}").get("pageIndex", -1)) + 1
                except (ValueError, TypeError):
                    page = 0
                targets = [kept_for.get(parent, parent)]
            else:
                text = strip_html(d.get("note", ""))
                page = 0
                targets = [kept_for.get(parent, parent)] if parent in kept_for else pdfs_of_parent.get(parent, [])
            text = normalize_text(text).strip()
            if not text:
                continue
            for key in targets:
                if key in pdf_keys:
                    rows.append((self.library_id, key, d["itemType"], page, text))

        conn.execute("DELETE FROM extras WHERE library_id=?", (self.library_id,))
        conn.executemany(
            "INSERT INTO extras (library_id, zotero_key, kind, page_num, text) VALUES (?,?,?,?,?)", rows,
        )
        conn.commit()

    def _reindex_pending(self, conn: sqlite3.Connection) -> int:
        """(Re)index from disk the PDFs never indexed (earlier failure) or
        indexed with an older format (store.INDEX_VERSION)."""
        pending = [
            row for row in conn.execute(
                "SELECT zotero_key, version, filename FROM items WHERE library_id=? AND indexed!=?",
                (self.library_id, store.INDEX_VERSION),
            ).fetchall()
            if (row["zotero_key"], row["version"]) not in _index_failed
            and store.pdf_path(self.library_id, row["zotero_key"]).exists()
        ]
        if not pending:
            return 0
        self._report(f"Indexing {len(pending)} PDFs...")
        failed = 0
        for i, row in enumerate(pending, 1):
            if self._cancel:
                break
            self._report(f"[{i}/{len(pending)}] {row['filename']}")
            if not self._index_pdf(conn, row["zotero_key"], row["version"]):
                failed += 1
                self._report(f"Indexing failed: {row['filename']}")
        return failed

    def _index_pdf(self, conn: sqlite3.Connection, zotero_key: str, version: int) -> bool:
        path = store.pdf_path(self.library_id, zotero_key)
        try:
            pages, ocr_count = extract_pages(str(path))
            if ocr_count:
                self._report(f"OCR: {ocr_count} scanned page(s)")
            replace_pages(conn, zotero_key, pages)
            no_text = sum(len(t) for t in pages) < NO_TEXT_MAX_CHARS
            conn.execute(
                "UPDATE items SET indexed=?, no_text=? WHERE zotero_key=?",
                (store.INDEX_VERSION, int(no_text), zotero_key),
            )
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            _index_failed.add((zotero_key, version))
            logger.warning("Index failed for %s: %s", path, e)
            return False

    def _get_meta(self, conn: sqlite3.Connection, name: str) -> str | None:
        return store.get_meta(conn, f"{name}_L{self.library_id}")

    def _set_meta(self, conn: sqlite3.Connection, name: str, value: str):
        store.set_meta(conn, f"{name}_L{self.library_id}", value)

    def _report(self, msg: str):
        logger.info(msg)
        if self._progress_cb:
            self._progress_cb(msg)


class SyncRunner:
    """Syncs every configured library, one at a time, and keeps a state the
    interface can show. A single sync runs at once."""

    def __init__(self):
        self.state = {"running": False, "message": "Idle", "log": []}
        self.last_attempt: float | None = None   # time.monotonic()
        self._lock = threading.Lock()
        self._syncer: ZoteroSync | None = None
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self.state["running"]

    def cancel(self) -> bool:
        syncer = self._syncer
        if not self.running or not syncer:
            return False
        self._cancelled = True
        syncer.cancel()
        return True

    def run(self, libraries: list[Library], progress_cb=None, client_factory=None) -> bool:
        """Blocking. False if a sync was already running or nothing to sync."""
        with self._lock:
            if self.state["running"]:
                return False
            if not libraries:
                self.state["message"] = "No Zotero library configured"
                return False
            self.state.update(running=True, log=[])
            self._cancelled = False
            self.last_attempt = time.monotonic()

        def report(msg: str):
            self.state["message"] = msg
            self.state["log"].append(msg)
            if progress_cb:
                progress_cb(msg)

        errors = 0
        try:
            for lib in libraries:
                if self._cancelled:
                    break
                prefix = f"[{lib.label}] " if len(libraries) > 1 else ""
                try:
                    client = client_factory(lib) if client_factory else None
                    self._syncer = ZoteroSync(lib, client)
                    self._syncer.sync(progress_cb=lambda msg, prefix=prefix: report(prefix + msg))
                except Exception as e:
                    errors += 1
                    # pyzotero spreads its errors over several lines: keep the gist
                    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
                    brief = " - ".join(ln for ln in lines if ln.startswith(("Code:", "Response:"))) or " ".join(lines)
                    report(f"{prefix}Error: {brief[:300]}")
                    logger.exception("Sync failed for library %s", lib.label)
            # "Last sync" means the last complete one: no error, not cancelled.
            if not errors and not self._cancelled:
                store.stamp_sync()
                if len(libraries) > 1:
                    report("All libraries synced")
        finally:
            self._syncer = None
            self.state["running"] = False
        return True


def replace_pages(conn: sqlite3.Connection, zotero_key: str, pages: list[str]):
    """Store the normalized text of each page (and its folded copy, which the
    search runs on). Does not commit."""
    rows = []
    for idx, text in enumerate(pages):
        folded = fold(text)
        rows.append((zotero_key, idx + 1, text, None if folded == text else folded))
    conn.execute("DELETE FROM pages WHERE zotero_key=?", (zotero_key,))
    conn.executemany("INSERT INTO pages (zotero_key, page_num, text, folded) VALUES (?,?,?,?)", rows)
