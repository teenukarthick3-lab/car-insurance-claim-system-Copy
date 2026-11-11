"""Upload validation and storage. Original filenames are never used on disk."""
import os
import shutil

from flask import current_app

# file_type -> metadata. Order defines display order everywhere.
EVIDENCE_TYPES = {
    "front_photo":   {"label": "Front of Vehicle", "title": "Front Photo",           "short": "Front photo",           "kind": "image"},
    "back_photo":    {"label": "Back of Vehicle",  "title": "Back Photo",            "short": "Back photo",            "kind": "image"},
    "left_photo":    {"label": "Left Side",        "title": "Left Side Photo",       "short": "Left-side photo",       "kind": "image"},
    "right_photo":   {"label": "Right Side",       "title": "Right Side Photo",      "short": "Right-side photo",      "kind": "image"},
    "damage_video": {"label": "Damage Video", "title": "Damage Video", "short": "Damage video", "kind": "video", "required": False},
    "chassis_photo": {"label": "Chassis Number",   "title": "Chassis Number Photo",  "short": "Chassis-number photo",  "kind": "image"},
}

# Legacy mapping for backwards compatibility with earlier claims
LEGACY_EVIDENCE_TYPES = {
    "rear_photo":    {"label": "Rear View (previous)", "title": "Rear Photo (previous)", "short": "Rear photo (previous)", "kind": "image"},
}

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTS = {"mp4", "mov"}
MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
        "mp4": "video/mp4", "mov": "video/quicktime"}

MAX_IMAGE_BYTES = 15 * 1024 * 1024    # 15 MB per photo
MAX_VIDEO_BYTES = 100 * 1024 * 1024   # 100 MB for the video


class UploadError(ValueError):
    pass


def _ext(filename):
    return filename.rsplit(".", 1)[1].lower() if filename and "." in filename else ""


def _sniff(head):
    """Identify real content from magic bytes; returns canonical ext or None."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "mov" if brand == b"qt  " else "mp4"
    return None


def _size(storage):
    stream = storage.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


def validate(file_type, storage):
    """Validate one uploaded FileStorage. Returns (canonical_ext, size)."""
    all_types = dict(EVIDENCE_TYPES)
    all_types.update(LEGACY_EVIDENCE_TYPES)
    if file_type not in all_types:
        raise UploadError(f"Unknown evidence type '{file_type}'.")
    meta = all_types[file_type]
    if storage is None or not storage.filename:
        raise UploadError(f"{meta['short']} is missing.")
    ext = _ext(storage.filename)
    allowed = IMAGE_EXTS if meta["kind"] == "image" else VIDEO_EXTS
    head = storage.stream.read(16)
    storage.stream.seek(0)
    sniffed = _sniff(head)

    if meta["kind"] == "image":
        if sniffed not in IMAGE_EXTS:
            raise UploadError(f"{meta['short']} must be a JPG, PNG or WEBP image.")
        limit = MAX_IMAGE_BYTES
    else:
        if sniffed not in VIDEO_EXTS:
            raise UploadError(f"{meta['short']} must be an MP4 or MOV video.")
        limit = MAX_VIDEO_BYTES
    if ext and ext not in allowed:
        raise UploadError(f"{meta['short']}: .{ext} files are not allowed.")

    size = _size(storage)
    if size == 0:
        raise UploadError(f"{meta['short']} is empty.")
    if size > limit:
        raise UploadError(f"{meta['short']} is too large (max {limit // (1024 * 1024)} MB).")
    # Prefer the user's extension when it agrees with content (e.g. .jpeg vs .jpg, .mov vs .mp4)
    final_ext = ext if ext in allowed and (MIME.get(ext) == MIME.get(sniffed) or meta["kind"] == "video") else sniffed
    return final_ext, size


def validate_damage_photo(storage):
    """Validate one damage photo FileStorage. Returns (canonical_ext, size)."""
    if storage is None or not storage.filename:
        raise UploadError("Damage photo is missing.")
    ext = _ext(storage.filename)
    head = storage.stream.read(16)
    storage.stream.seek(0)
    sniffed = _sniff(head)
    if sniffed not in IMAGE_EXTS:
        raise UploadError("Damage photo must be a JPG, PNG or WEBP image.")
    if ext and ext not in IMAGE_EXTS:
        raise UploadError(f"Damage photo: .{ext} files are not allowed.")
    size = _size(storage)
    if size == 0:
        raise UploadError("Damage photo is empty.")
    if size > MAX_IMAGE_BYTES:
        raise UploadError(f"Damage photo is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
    final_ext = ext if ext in IMAGE_EXTS and MIME.get(ext) == MIME.get(sniffed) else sniffed
    return final_ext, size


def save_damage_photo(claim_reference, storage, ext, index):
    """Save damage photo under uploads/claims/<ref>/damage_<index>.<ext>; returns relative path."""
    folder = os.path.join(claims_root(), claim_reference)
    os.makedirs(folder, exist_ok=True)
    name = f"damage_{index}.{ext}"
    storage.save(os.path.join(folder, name))
    return f"claims/{claim_reference}/{name}"


def claims_root():
    return os.path.join(current_app.config["UPLOAD_FOLDER"], "claims")


def save(claim_reference, file_type, storage, ext):
    """Save under uploads/claims/<ref>/<file_type>.<ext>; returns path relative to UPLOAD_FOLDER."""
    folder = os.path.join(claims_root(), claim_reference)
    os.makedirs(folder, exist_ok=True)
    name = f"{file_type}.{ext}"
    storage.save(os.path.join(folder, name))
    return f"claims/{claim_reference}/{name}"


def remove_claim_folder(claim_reference):
    shutil.rmtree(os.path.join(claims_root(), claim_reference), ignore_errors=True)


def absolute_path(relative_path):
    """Resolve a stored relative path safely inside UPLOAD_FOLDER."""
    root = os.path.realpath(current_app.config["UPLOAD_FOLDER"])
    full = os.path.realpath(os.path.join(root, relative_path))
    if not full.startswith(root + os.sep):
        raise UploadError("Invalid file path.")
    return full


def mime_for(path):
    return MIME.get(_ext(path), "application/octet-stream")
