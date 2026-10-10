"""Files attached to a "Need help" request (clinic/help_requests.py): the one place a person's file reaches the
disk, so everything about it is checked here.

What is allowed: images (png, jpg, jpeg, webp, gif, heic) and documents (pdf, txt, csv, docx, xlsx). Nothing else:
no video, svg, html, scripts, archives, executables or macro-enabled office files. At most 5 files per request,
10 MB each, 25 MB in all.

How a file is handled:

  * Its name never reaches the disk. It is kept only as text in the database (cleaned, for display and for the
    download name); on disk it is a random 32-hex name inside a folder per request (`<root>/HELP-0007/`).
  * It is copied from the upload stream in small chunks into a private staging folder, counting bytes as it goes,
    and the copy stops the moment a limit is crossed. A huge body is never held in memory (the web server's
    parser spools big parts to disk, and the route caps the whole body first: see app.py).
  * Its type is decided from its own leading bytes, not from the extension or the type the browser claimed, and
    must agree with the extension. txt/csv must be valid UTF-8 text with no control bytes. docx/xlsx must be a zip
    that holds [Content_Types].xml and word/ (docx) or xl/ (xlsx) and nothing macro-related.
  * Staging is all-or-nothing: the request creates its folder by one rename after every file passed, and any
    failure removes the staging folder, so a refused upload leaves nothing behind.
  * A download is looked up by database id, and the path is rebuilt from two values that are checked against a
    strict pattern and then confirmed to be inside the upload folder (no path traversal).

The upload folder is HELP_UPLOAD_DIR, default `<repo>/data/help_uploads` (git-ignored).
"""

import codecs
import hashlib
import os
import re
import secrets
import shutil
import time
import unicodedata
import zipfile
from pathlib import Path

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
# The whole request body may be a little bigger than the files (form fields, multipart framing).
BODY_OVERHEAD_BYTES = 1024 * 1024
MAX_BODY_BYTES = MAX_TOTAL_BYTES + BODY_OVERHEAD_BYTES

IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "gif", "heic")
DOCUMENT_EXTENSIONS = ("pdf", "txt", "csv", "docx", "xlsx")
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS

MAX_NAME_CHARS = 100
_CHUNK = 64 * 1024
_STAGING = ".staging"
_STAGING_MAX_AGE_S = 24 * 3600
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")
_ZIP_MAX_ENTRIES = 5000
_ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024
_ZIP_TYPES_MAX = 1024 * 1024

_STORED_NAME = re.compile(r"^[0-9a-f]{32}$")
_REQUEST_DIR = re.compile(r"^HELP-\d{4,9}$")
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')

_TEXT_MIME = {"txt": "text/plain", "csv": "text/csv"}
_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "heic": "image/heic", "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_MIME.update(_TEXT_MIME)


class UploadError(ValueError):
    """A file that cannot be accepted. The message is shown to the person."""

    status = 400
    code = "invalid"


class UploadTooLarge(UploadError):
    status = 413
    code = "too_large"


def upload_root():
    """The folder uploads live under (HELP_UPLOAD_DIR, default <repo>/data/help_uploads), as an absolute path."""
    configured = os.environ.get("HELP_UPLOAD_DIR")
    base = Path(configured) if configured else Path(__file__).resolve().parent.parent / "data" / "help_uploads"
    return base.expanduser().resolve()


def allowed_types_text():
    return "images ({}) and documents ({})".format(", ".join(IMAGE_EXTENSIONS), ", ".join(DOCUMENT_EXTENSIONS))


def limits():
    return {
        "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES,
        "image_extensions": list(IMAGE_EXTENSIONS), "document_extensions": list(DOCUMENT_EXTENSIONS),
    }


# -- names ------------------------------------------------------------------------------------------

def split_name(raw_name):
    """(clean display name, lower-case extension) for a name from the browser. The name is only ever text: the
    folder part is dropped, control and path characters are replaced, and it is shortened. Never used on disk."""
    name = unicodedata.normalize("NFC", str(raw_name or ""))
    name = re.split(r"[\\/]", name)[-1]
    name = _BAD_NAME_CHARS.sub("_", name).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext.lower()
    stem = stem.strip(" .") or "file"
    room = MAX_NAME_CHARS - (len(ext) + 1 if ext else 0)
    stem = stem[:max(1, room)]
    return (stem + "." + ext if ext else stem), ext


def display_name(raw_name):
    return split_name(raw_name)[0]


# -- sniffing the content ----------------------------------------------------------------------------

def _is_text_file(path):
    """True for a file that is valid UTF-8 text with no NUL or other control bytes (tab, newline, form feed and
    carriage return are fine)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                final = not chunk
                text = decoder.decode(chunk, final)
                for char in text:
                    code = ord(char)
                    if code < 32 and char not in "\t\n\r\x0c":
                        return False
                    if code == 0x7F:
                        return False
                if final:
                    return True
    except (UnicodeDecodeError, OSError):
        return False


def _office_problem(path, ext):
    """None when the file is a plain docx/xlsx, else the reason it is refused."""
    prefix = "word/" if ext == "docx" else "xl/"
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _ZIP_MAX_ENTRIES:
                return "it holds too many parts"
            if sum(info.file_size for info in infos) > _ZIP_MAX_UNCOMPRESSED:
                return "it expands to an unreasonable size"
            names = [info.filename for info in infos]
            for name in names:
                parts = name.replace("\\", "/").split("/")
                if name.startswith(("/", "\\")) or ".." in parts:
                    return "it holds an unsafe path"
                lowered = name.lower()
                if "vbaproject" in lowered or lowered.startswith(("macros/", "xl/macrosheets/")):
                    return "it contains macros"
            if "[Content_Types].xml" not in names:
                return "it is not a {} file".format(ext)
            if not any(name.startswith(prefix) for name in names):
                return "it is not a {} file".format(ext)
            with archive.open("[Content_Types].xml") as handle:
                types = handle.read(_ZIP_TYPES_MAX + 1)
            if len(types) > _ZIP_TYPES_MAX:
                return "it is not a {} file".format(ext)
            lowered_types = types.lower()
            if b"macroenabled" in lowered_types or b"vbaproject" in lowered_types:
                return "it contains macros"
    except zipfile.BadZipFile:
        return "it is not a {} file".format(ext)
    except (OSError, RuntimeError, NotImplementedError, ValueError, EOFError):
        return "it could not be read"
    return None


def sniff_mime(path, ext):
    """The canonical type of the file at `path` when its content matches `ext`; raises UploadError otherwise."""
    with open(path, "rb") as handle:
        head = handle.read(64)
    if ext == "png":
        ok = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in ("jpg", "jpeg"):
        ok = head.startswith(b"\xff\xd8\xff")
    elif ext == "gif":
        ok = head[:6] in (b"GIF87a", b"GIF89a")
    elif ext == "webp":
        ok = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    elif ext == "heic":
        ok = head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS
    elif ext == "pdf":
        ok = head.startswith(b"%PDF-")
    elif ext in _TEXT_MIME:
        ok = _is_text_file(path)
    elif ext in ("docx", "xlsx"):
        problem = _office_problem(path, ext)
        if problem:
            raise UploadError("This {} file was refused because {}.".format(ext, problem))
        ok = True
    else:
        ok = False
    if not ok:
        raise UploadError("The content of this file does not look like a real .{} file.".format(ext))
    return _MIME[ext]


# -- staging -------------------------------------------------------------------------------------------

class Staged:
    """Files that passed every check and sit in a private staging folder, waiting for their request.
    `files` are dicts: original_name, stored_name, mime, bytes, sha256."""

    def __init__(self, folder, files):
        self.folder = folder
        self.files = files

    def discard(self):
        if self.folder is not None:
            shutil.rmtree(str(self.folder), ignore_errors=True)
            self.folder = None


def _sweep_staging(root):
    """Remove staging folders a crashed upload left behind (older than a day)."""
    staging = root / _STAGING
    try:
        entries = list(staging.iterdir())
    except OSError:
        return
    cutoff = time.time() - _STAGING_MAX_AGE_S
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(str(entry), ignore_errors=True)
        except OSError:
            pass


def _ensure_root(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / _STAGING).mkdir(exist_ok=True, mode=0o700)


def stage_files(storages, root=None):
    """Check and copy the uploaded files (werkzeug FileStorage, or anything with `filename` and `stream`) into a
    new staging folder. Returns Staged (possibly with no files). Raises UploadError / UploadTooLarge and leaves
    nothing on disk when anything is wrong."""
    root = root or upload_root()
    parts = [s for s in storages if s is not None and (getattr(s, "filename", "") or "").strip()]
    if len(parts) > MAX_FILES:
        raise UploadError("Attach at most {} files.".format(MAX_FILES))
    if not parts:
        return Staged(None, [])
    _ensure_root(root)
    _sweep_staging(root)
    folder = root / _STAGING / secrets.token_hex(12)
    folder.mkdir(mode=0o700)
    files, total = [], 0
    try:
        for storage in parts:
            shown, ext = split_name(storage.filename)
            if ext not in ALLOWED_EXTENSIONS:
                raise UploadError("'{}' cannot be attached. Allowed: {}.".format(shown, allowed_types_text()))
            stored = secrets.token_hex(16)
            path = folder / stored
            size, digest = 0, hashlib.sha256()
            with open(str(path), "xb") as out:
                while True:
                    chunk = storage.stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise UploadTooLarge("'{}' is larger than {} MB.".format(shown, MAX_FILE_BYTES // (1024 * 1024)))
                    if total + size > MAX_TOTAL_BYTES:
                        raise UploadTooLarge("The files together are larger than {} MB.".format(MAX_TOTAL_BYTES // (1024 * 1024)))
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise UploadError("'{}' is empty.".format(shown))
            try:
                mime = sniff_mime(path, ext)
            except UploadError as exc:
                raise UploadError("'{}': {}".format(shown, exc))
            total += size
            files.append({"original_name": shown, "stored_name": stored, "mime": mime, "bytes": size,
                          "sha256": digest.hexdigest()})
    except BaseException:
        shutil.rmtree(str(folder), ignore_errors=True)
        raise
    return Staged(folder, files)


def publish_folder(staged, ticket_no, root=None):
    """Move the staging folder to `<root>/<ticket_no>` in one rename. Returns the final folder. A folder with that
    name can only be an orphan from a crash between this step and the database commit (the ticket number was
    just allocated), so it is removed first."""
    root = root or upload_root()
    if not _REQUEST_DIR.match(ticket_no):
        raise UploadError("Bad ticket number.")
    final = root / ticket_no
    if final.exists():
        shutil.rmtree(str(final), ignore_errors=True)
    os.rename(str(staged.folder), str(final))
    staged.folder = None
    return final


def remove_request_folder(ticket_no, root=None):
    root = root or upload_root()
    if _REQUEST_DIR.match(ticket_no or ""):
        shutil.rmtree(str(root / ticket_no), ignore_errors=True)


def stored_path(ticket_no, stored_name, root=None):
    """The file's path on disk, or None. Both parts must match a strict pattern and the result must be inside the
    upload folder, so no value from a URL or a database row can point anywhere else."""
    root = root or upload_root()
    if not (_REQUEST_DIR.match(str(ticket_no or "")) and _STORED_NAME.match(str(stored_name or ""))):
        return None
    path = (root / ticket_no / stored_name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None
