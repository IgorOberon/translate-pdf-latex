"""Read native TeX logs and PowerShell UTF-16 redirected logs consistently."""
from pathlib import Path


def read_tex_log(path):
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    # Some launchers strip the BOM. TeX diagnostics have enough ASCII for the
    # zero-byte lanes to distinguish UTF-16 from UTF-8, including short logs.
    sample = data[:8192]
    if len(sample) >= 4:
        even, odd = sample[::2], sample[1::2]
        if odd.count(0) / len(odd) > .3 and even.count(0) / len(even) < .1:
            return data.decode("utf-16-le", errors="replace")
        if even.count(0) / len(even) > .3 and odd.count(0) / len(odd) < .1:
            return data.decode("utf-16-be", errors="replace")
    return data.decode("utf-8-sig", errors="replace")
