"""Unpack the bundle zip made by PowerShell Compress-Archive (entry names may use '\\')."""

import sys
import zipfile
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(src) as z:
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/") or ".." in Path(name).parts or name.startswith("/"):
            continue
        out = dst / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(z.read(info))
print("unpacked", src, "->", dst)
