"""Independent extraction/build identities. No book/cache rewrites on migration."""
import json
import ast
from pathlib import Path
import tempfile

from provenance import canonical_hash, sha_file, write_json, inside

# The exact pre-maintenance orchestrator; allows verifying old receipts without
# guessing their source/options or replacing their historical hashes.
LEGACY_EXTRACTORS = {"cf1abf87420833d90367cfdcb99c0283ce46081d432d61447c943fcacfaacc5f"}
EXTRACTION_VERSION = "extraction-v2"
LAYOUT_VERSION = "layout-20261115-v1"


def extraction_identity(source, mode, chunk, scripts):
    scripts = Path(scripts)
    names = ["extract_chunked.py", "procutils.py"] if mode == "math" else []
    renderer = scripts / "translate_book.py"
    text = renderer.read_text(encoding="utf-8")
    functions = {"extract_pdf", "find_md", "to_markdown", "pandoc_to_md"}
    code = [ast.get_source_segment(text, node) for node in ast.parse(text).body
            if isinstance(node, ast.FunctionDef) and node.name in functions]
    return canonical_hash({"schema": EXTRACTION_VERSION, "source": source,
                           "mode": mode, "chunk": chunk,
                           "code": code,
                           "engines": {n: sha_file(scripts / n) for n in names}})


def reuse_extraction(state, source, mode, chunk, scripts, markdown):
    if state.get("source_sha256") not in (None, source):
        return False
    if not markdown.is_file() or sha_file(markdown) != state.get("output_sha256"):
        return False
    expected = extraction_identity(source, mode, chunk, scripts)
    if state.get("schema") == EXTRACTION_VERSION:
        return state.get("source_sha256") == source and state.get("config_sha256") == expected
    return any(state.get("config_sha256") == canonical_hash(
        {"source": source, "mode": mode, "chunk": chunk, "script": old})
        for old in LEGACY_EXTRACTORS)


def tree_hashes(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): sha_file(p) for p in sorted(root.rglob("*"))
            if p.is_file()} if root.is_dir() else {}


def build_code_identity(script):
    text = Path(script).read_text(encoding="utf-8")
    # MT/extraction edits do not invalidate a prepared body, but changes to
    # its actual renderer do. Assets and image contents are separate inputs.
    start = text.index("    # 5) markdown -> LaTeX")
    end = text.index("    # 6) compile", start)
    helpers = []
    for node in ast.parse(text).body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"assemble_main_tex", "copy_figures", "figure_inputs"}):
            helpers.append(ast.get_source_segment(text, node))
        elif (isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "LAYOUT_BLOCKS" for t in node.targets)):
            helpers.append(ast.get_source_segment(text, node))
    return canonical_hash([text[start:end], helpers])


def select_build(root, inputs):
    """Reuse only a hash-checked prepared candidate; never repeat a known failure."""
    root = Path(root)
    key = canonical_hash(inputs)
    receipt = root / "stages-v1" / (key + ".json")
    if receipt.is_file():
        state = json.loads(receipt.read_text(encoding="utf-8"))
        candidate = inside(root, state["candidate"])
        prepared = state.get("prepared")
        dependencies_ok = True
        compile_state = candidate / "compile.state.json"
        if compile_state.is_file():
            compiled = json.loads(compile_state.read_text(encoding="utf-8"))
            dependencies = compiled.get("inputs")
            if not isinstance(dependencies, dict) or not dependencies:
                raise ValueError("invalid compile dependency receipt; existing files retained")
            dependencies_ok = all(Path(p).is_file() and sha_file(p) == digest
                                  for p, digest in dependencies.items())
        if (state.get("key") == key and isinstance(prepared, dict) and prepared
                and dependencies_ok
                and all(inside(candidate, name).is_file() and sha_file(inside(candidate, name)) == digest
                        for name, digest in prepared.items())):
            # A deterministic compilation or technical failure cannot be fixed
            # by starting the exact same typesetting again.
            compile_report = candidate / "compile.report.json"
            if compile_report.is_file():
                report = json.loads(compile_report.read_text(encoding="utf-8"))
                error = report.get("error", "")
                last_rc = (report.get("passes") or [{}])[-1].get("rc")
                interrupted = isinstance(last_rc, int) and (last_rc < 0 or last_rc >= 128)
                if (report.get("status") == "failed" and error.startswith("ValueError:")
                        and not interrupted):
                    raise ValueError("unchanged build failure; repair typesetting inputs: " + str(compile_report))
            release_report = candidate / "release.report.json"
            if release_report.is_file():
                report = json.loads(release_report.read_text(encoding="utf-8"))
                if report.get("technical", {}).get("status") == "failed":
                    raise ValueError("unchanged technical PDF failure; repair typesetting inputs: " + str(release_report))
            return candidate, True, receipt
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="candidate-", dir=root))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    write_json(receipt, {"schema": 1, "key": key, "candidate": candidate.relative_to(root).as_posix()})
    return candidate, False, receipt


def seal_build(candidate, receipt):
    state = json.loads(receipt.read_text(encoding="utf-8"))
    state["prepared"] = {name: digest for name, digest in tree_hashes(candidate).items()
                         if name != "release.report.json"}
    write_json(receipt, state)
