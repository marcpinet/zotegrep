import json

import pymupdf
import pytest
from pyzotero.errors import ResourceNotFoundError

from zotegrep import config, store
from zotegrep.sync import replace_pages


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh ZoteGrep home (config + index) for each test."""
    monkeypatch.delenv("ZOTEGREP_HOME", raising=False)
    # Never read the developer's real Zotero storage; tests create this folder when they need it.
    monkeypatch.setenv("ZOTEGREP_ZOTERO_STORAGE", str(tmp_path / "zotero-storage"))
    config.set_home(tmp_path)
    store.init_db()
    yield tmp_path
    config.set_home(None)


def make_pdf(*pages: list[str]) -> bytes:
    """A PDF whose pages hold the given paragraphs. Lines outside Latin-1
    (curly quotes, dashes) use a built-in font that has those glyphs."""
    doc = pymupdf.open()
    for paragraphs in pages:
        page = doc.new_page()
        y = 72
        for text in paragraphs:
            font = "helv" if all(ord(c) < 256 for c in text) else "china-s"
            page.insert_text((60, y), text, fontname=font, fontsize=10)
            y += 120  # well apart: separate blocks, so separate paragraphs
    data = doc.tobytes()
    doc.close()
    return data


def add_paper(key: str, pages: list[str], library_id: int = 1, **meta) -> None:
    """Insert an indexed paper directly, as a sync would."""
    row = {
        "title": key, "authors": "", "authors_full": "", "date": "", "abstract": "",
        "tags": [], "collections": [], "item_type": "journalArticle",
        "publication": "", "doi": "", "url": "",
    }
    row.update(meta)
    row["tags"] = json.dumps(row["tags"])
    row["collections"] = json.dumps(row["collections"])
    conn = store.get_db()
    conn.execute(
        f"INSERT INTO items (zotero_key, library_id, parent_key, filename, version, indexed, "
        f"{', '.join(store.METADATA_COLUMNS)}) VALUES (?, ?, '', ?, 1, ?, "
        f"{', '.join('?' for _ in store.METADATA_COLUMNS)})",
        (key, library_id, f"{key}.pdf", store.INDEX_VERSION, *(row[c] for c in store.METADATA_COLUMNS)),
    )
    replace_pages(conn, key, pages)
    conn.commit()
    conn.close()


class FakeZotero:
    """Just enough of pyzotero's client for the sync, without network."""

    def __init__(self):
        self.version = 1
        self.parents: list[dict] = []
        self.attachments: list[dict] = []
        self.notes: list[dict] = []
        self.collections_data: list[dict] = []
        self.tag_colors: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.broken: set[str] = set()
        self.calls: list[str] = []

    def last_modified_version(self):
        return self.version

    def everything(self, items):
        return items

    def top(self):
        self.calls.append("top")
        return [{"data": p} for p in self.parents]

    def items(self, itemType=None, **_):
        self.calls.append(f"items:{itemType}")
        if itemType == "attachment":
            return [{"data": a} for a in self.attachments]
        return [{"data": n} for n in self.notes]

    def collections(self):
        return [{"data": c} for c in self.collections_data]

    def settings(self):
        return {"tagColors": {"value": self.tag_colors}}

    def file(self, key):
        self.calls.append(f"file:{key}")
        if key in self.broken:
            raise ConnectionError("connection reset")
        if key in self.files:
            return self.files[key]
        raise ResourceNotFoundError("\nCode: 404\nResponse: Not found")

    def add_paper(self, key: str, pdf: bytes | None, title: str = "", parent: str | None = None,
                  link_mode: str = "imported_file", md5: str | None = None, **parent_fields):
        parent = parent or f"P{key}"
        self.parents.append({"key": parent, "itemType": "journalArticle", "title": title or key,
                             "creators": [], "tags": [], "collections": [], **parent_fields})
        self.attachments.append({
            "key": key, "version": self.version, "parentItem": parent, "contentType": "application/pdf",
            "linkMode": link_mode, "filename": f"{key}.pdf", "md5": md5 or key, "dateAdded": "2026-01-01",
        })
        if pdf is not None:
            self.files[key] = pdf
