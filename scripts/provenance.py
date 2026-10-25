"""Content identities, ordered parts and explicit derivation chains.

No mtime trust; no automatic adoption of unversioned translation reports.
All manifest paths must resolve inside the declared book directory.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

SCHEMA = 1
MANIFEST = "parts.manifest.json"


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return sha_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def blocks(text):
    return [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]


def normalized(text):
    return "\n\n".join(blocks(text))


def inside(root, relative):
    root = Path(root).resolve()
    if Path(relative).is_absolute():
        raise ValueError(f"absolute manifest path: {relative}")
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"path escapes run directory: {relative}")
    return path


def discover_parts(root):
    root = Path(root)
    found = sorted((int(m.group(1)), p) for p in root.glob("book.part*.md")
                   if (m := re.fullmatch(r"book\.part(\d+)\.md", p.name)))
    if not found or [n for n, _ in found] != list(range(1, len(found) + 1)):
        raise ValueError("missing/non-contiguous source parts")
    return [p for _, p in found]


def create_manifest(root, config=None, structure=None):
    root = Path(root)
    paths = discover_parts(root)
    source = root / "book.md"
    joined = "\n\n".join(normalized(p.read_text(encoding="utf-8")) for p in paths)
    if joined != normalized(source.read_text(encoding="utf-8")):
        raise ValueError("source parts do not reproduce current book.md")
    offset, parts = 0, []
    for i, path in enumerate(paths, 1):
        count = len(blocks(path.read_text(encoding="utf-8")))
        parts.append({"path": path.name, "sha256": sha_file(path),
                      "blocks": [offset, offset + count], "output": f"book.ru.part{i}.md"})
        offset += count
    structure = Path(structure) if structure else root / "structure.json"
    configs = {}
    if structure.is_file():
        # External profiles are represented by content identity, not machine paths.
        configs["structure_sha256"] = sha_file(structure)
        if structure.resolve().is_relative_to(root.resolve()):
            configs["structure_path"] = structure.resolve().relative_to(root.resolve()).as_posix()
    value = {"schema": SCHEMA, "run_id": uuid.uuid4().hex,
             "source": "book.md", "source_sha256": sha_file(source),
             "profile": configs, "config": config or {}, "parts": parts}
    value["manifest_sha256"] = canonical_hash(value)
    write_json(root / MANIFEST, value)
    return value


def load_manifest(root):
    root = Path(root)
    value = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    identity = {k: v for k, v in value.items() if k != "manifest_sha256"}
    if value.get("schema") != SCHEMA or canonical_hash(identity) != value.get("manifest_sha256"):
        raise ValueError("invalid manifest identity/schema")
    source = inside(root, value["source"])
    if sha_file(source) != value["source_sha256"]:
        raise ValueError("book.md changed after manifest creation")
    profile = value["profile"]
    trace_hash = value.get("config", {}).get("structure_trace_sha256")
    if trace_hash and sha_file(root / "structure.trace.json") != trace_hash:
        raise ValueError("structure trace changed after manifest creation")
    if profile.get("structure_path") and sha_file(inside(root, profile["structure_path"])) != profile["structure_sha256"]:
        raise ValueError("structure profile changed after manifest creation")
    actual = [p.name for p in discover_parts(root)]
    if actual != [r["path"] for r in value["parts"]]:
        raise ValueError("extra, reordered or missing source parts")
    expected_outputs = {r["output"] for r in value["parts"]}
    if {p.name for p in root.glob("book.ru.part*.md")} - expected_outputs:
        raise ValueError("extra translated tail part")
    joined, offset = [], 0
    for row in value["parts"]:
        path = inside(root, row["path"])
        inside(root, row["output"])
        text = path.read_text(encoding="utf-8")
        count = len(blocks(text))
        if sha_file(path) != row["sha256"] or row["blocks"] != [offset, offset + count]:
            raise ValueError(f"part hash/range mismatch: {path.name}")
        joined.extend(blocks(text))
        offset += count
    if joined != blocks(source.read_text(encoding="utf-8")):
        raise ValueError("parts differ from book.md content/order")
    return value


def check_result(src, dst, manifest_hash=None):
    """Raises on foreign/missing reports or untracked post-edits; returns raw MT report."""
    src, dst = Path(src), Path(dst)
    report = json.loads(Path(str(dst) + ".report.json").read_text(encoding="utf-8"))
    if report.get("schema") != SCHEMA or report.get("source_sha256") != sha_file(src):
        raise ValueError(f"source/report identity mismatch: {src.name}")
    if manifest_hash and report.get("manifest_sha256") != manifest_hash:
        raise ValueError(f"report belongs to another manifest: {dst.name}")
    if (not report.get("request_config") or report.get("request_config_sha256") !=
            canonical_hash(report["request_config"]) or report.get("aborted")):
        raise ValueError(f"missing request identity or aborted report: {dst.name}")
    if report.get("status") not in ("passed", "failed"):
        raise ValueError(f"invalid stage status: {dst.name}")
    # The source identity is already proven by source_sha256 above. Counting the
    # source's paragraphs is only valid when every paragraph is a request: with
    # --group-chars several paragraphs form one block, so compare the report
    # with the OUTPUT, which is where the alignment actually matters.
    # A footnote definition is a block of its own in Markdown but not a
    # translation unit, so it must not count towards the reported total.
    def unit_blocks(text):
        return [b for b in blocks(text)
                if not re.match(r"^\[\^[^\]\s]+\]:", b)]

    current = report.get("output_sha256")
    output_text = dst.read_bytes().decode("utf-8")
    raw_output_text = output_text
    if not current:
        raise ValueError("report has no output identity")
    if sha_file(dst) != current:
        chain = json.loads(Path(str(dst) + ".lineage.json").read_text(encoding="utf-8"))
        if chain.get("root_report_sha256") != sha_file(str(dst) + ".report.json"):
            raise ValueError("lineage has a different raw MT parent")
        for stage in chain["stages"]:
            parent = inside(dst.parent, stage["parent_snapshot"])
            result = inside(dst.parent, stage["output_snapshot"])
            if (stage["parent_hash"] != current or sha_file(parent) != current
                    or sha_file(result) != stage["output_hash"] or not stage.get("edits")):
                raise ValueError("broken post-edit lineage")
            if current == report["output_sha256"]:
                raw_output_text = parent.read_bytes().decode("utf-8")
            current = stage["output_hash"]
        if current != sha_file(dst):
            raise ValueError("translation has unrecorded edits")
    from source_preservation import verify_preserved, remap_preserved
    source_text = src.read_bytes().decode("utf-8")
    verify_preserved(source_text, raw_output_text, report)
    current_report = remap_preserved(raw_output_text, output_text, report)
    verify_preserved(source_text, output_text, current_report)
    protocol = report["request_config"].get("paragraph_protocol")
    if protocol:
        from paragraph_units import PROTOCOL, verify_coverage
        from translate import split_blocks, group_blocks
        if protocol != PROTOCOL:
            raise ValueError("unknown paragraph coverage protocol")
        source_blocks = split_blocks(source_text)
        grouped = group_blocks(source_blocks, report["request_config"].get("group_chars", 0))
        if len(grouped) != report.get("blocks_total"):
            raise ValueError("paragraph group count mismatch")
        verify_coverage(source_blocks, grouped, raw_output_text, report.get("paragraph_coverage"))
        # Recorded prose edits may change spelling/length, but may not silently
        # merge or delete paragraph boundaries from the accepted raw MT stage.
        if len(blocks(output_text)) != len(blocks(raw_output_text)):
            raise ValueError("post-edit paragraph alignment mismatch")
        return report
    # A deliberate preserved group keeps all paragraph/note boundaries. Its
    # verified span is one MT unit, even when it contains multiple paragraphs.
    for row in sorted(current_report.get("allowed_source", []), key=lambda r: r["output_start"], reverse=True):
        output_text = output_text[:row["output_start"]] + "preserved unit" + output_text[row["output_end"]:]
    if len(unit_blocks(output_text)) != report.get("blocks_total"):
        raise ValueError(f"block alignment mismatch: {dst.name}")
    return report


def release_inputs(root):
    """Snapshot the exact assembly inputs; detect even valid concurrent rewrites."""
    root = Path(root).resolve()
    manifest = load_manifest(root)
    names = {MANIFEST, manifest["source"], "structure.json", "structure.trace.json", "headings.ru.json", "editorial.findings.json"}
    for row in manifest["parts"]:
        names.update((row["path"], row["output"], row["output"] + ".report.json", row["output"] + ".lineage.json"))
    paths = [inside(root, n) for n in names if inside(root, n).is_file()]
    paths += [p for p in (root / "images").rglob("*") if p.is_file()]
    return canonical_hash({p.relative_to(root).as_posix(): sha_file(p) for p in paths})


def book_settings(args, glossary=None):
    """Shared desired release identity for single-book and folder entrypoints."""
    keys = {"model": "hy-mt2-7b", "host": "http://localhost:1234", "mode": "hymt",
            "target_lang": "Russian", "extract": "auto", "no_clean": False,
            "layout": "screen", "heading_numbers": "auto", "author": "",
            "polish": False, "polish_model": "qwen3.5:9b", "polish_host": "http://localhost:11434",
            "split_chars": 0, "group_chars": 0, "context_per_request": 0}
    value = {key: getattr(args, key, None) or default for key, default in keys.items()}
    value["glossary_sha256"] = sha_file(glossary) if glossary and Path(glossary).is_file() else None
    for key in ("glossary_profile", "book_profile"):
        path = getattr(args, key, None)
        value[key + "_sha256"] = sha_file(path) if path else None
    keep_source = getattr(args, "keep_source", None)
    if keep_source:
        value["source_preservation_sha256"] = sha_file(keep_source)
    return canonical_hash(value)


def record_edit(src, dst, result, edits, config=None):
    """Append a derived stage, keeping raw report and before/after snapshots immutable."""
    dst = Path(dst)
    report = check_result(src, dst)
    before = dst.read_bytes().decode("utf-8")
    if before == result:
        return
    if (report["request_config"].get("paragraph_protocol")
            and len(blocks(before)) != len(blocks(result))):
        raise ValueError("post-edit paragraph alignment mismatch")
    lineage = Path(str(dst) + ".lineage.json")
    root_hash = sha_file(str(dst) + ".report.json")
    chain = {"schema": SCHEMA, "root_report_sha256": root_hash, "stages": []}
    if lineage.exists():
        previous = json.loads(lineage.read_text(encoding="utf-8"))
        if previous.get("root_report_sha256") == root_hash:
            chain = previous
    if report.get("allowed_source"):
        from source_preservation import remap_preserved, verify_preserved
        raw = (inside(dst.parent, chain["stages"][0]["parent_snapshot"]).read_bytes().decode("utf-8")
               if chain["stages"] else before)
        current_report = remap_preserved(raw, before, report)
        proposed = remap_preserved(before, result, current_report)
        verify_preserved(Path(src).read_bytes().decode("utf-8"), result, proposed)
    parent_hash, result_hash = sha_text(before), sha_text(result)
    snapshots = dst.parent / ".lineage" / dst.name
    for digest, text in ((parent_hash, before), (result_hash, result)):
        path = snapshots / (digest + ".txt")
        if path.exists() and sha_file(path) != digest:
            raise ValueError("corrupt lineage snapshot")
        if not path.exists():
            atomic_text(path, text)
    stage = {"stage": "postprocess", "parent_hash": parent_hash, "output_hash": result_hash,
             "parent_snapshot": (snapshots / (parent_hash + ".txt")).relative_to(dst.parent).as_posix(),
             "output_snapshot": (snapshots / (result_hash + ".txt")).relative_to(dst.parent).as_posix(),
             "config_sha256": canonical_hash(config or {}), "edits": edits}
    chain["stages"].append(stage)
    write_json(lineage, chain)
    atomic_text(dst, result)


def archive_stale(path):
    path = Path(path)
    if path.exists():
        target = path.with_name(path.name + ".stale")
        if target.exists():
            target = path.with_name(path.name + "." + uuid.uuid4().hex + ".stale")
        path.rename(target)
