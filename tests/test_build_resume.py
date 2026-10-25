"""Interrupted TeX passes must not poison the last good aux/ToC checkpoint."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_release import compile_latex, inspect_pdf
import build_release
from provenance import sha_file
from tex_logs import read_tex_log


def pass_directory(command):
    return Path(next(arg.split("=", 1)[1] for arg in command
                     if arg.startswith("-output-directory=")))


def fake_pass(directory, aux="good", toc="\\contentsline {section}{First}{2}{}"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.aux").write_text(aux, encoding="utf-8")
    (directory / "main.toc").write_text(toc, encoding="utf-8")
    (directory / "main.out").write_text("bookmarks", encoding="utf-8")
    (directory / "main.lof").write_text("figures", encoding="utf-8")
    (directory / "main.lot").write_text("tables", encoding="utf-8")
    (directory / "main.pdf").write_bytes(b"new candidate PDF")
    (directory / "main.log").write_text("Output written on main.pdf", encoding="utf-8")


@pytest.mark.parametrize("failure", [1, 3221225786, "timeout", "interrupt", "terminated"])
def test_failed_pass_keeps_checkpoint_and_resumes_without_restarting(tmp_path, failure):
    main = tmp_path / "main.tex"
    main.write_text(r"\include{chapters/one}", encoding="utf-8")
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters/one.tex").write_text("chapter", encoding="utf-8")
    output = tmp_path / "candidate"
    calls = []

    def failing(command, **kwargs):
        directory = pass_directory(command)
        calls.append(directory)
        assert directory != output
        assert (directory / "chapters").is_dir()
        if len(calls) == 1:
            fake_pass(directory)
            (directory / "chapters/one.aux").write_text("chapter labels", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        assert (directory / "main.aux").read_text() == "good"
        assert (directory / "chapters/one.aux").read_text() == "chapter labels"
        fake_pass(directory, aux="truncated \\@newl@bel{")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "terminated":
            raise SystemExit(137)  # Hard termination leaves only the pre-spawn state.
        return SimpleNamespace(returncode=failure)

    if failure in ("interrupt", "terminated"):
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else SystemExit):
            compile_latex(main, output, runner=failing)
    else:
        assert compile_latex(main, output, runner=failing)["status"] == "failed"
    assert (output / "main.aux").read_text() == "good"
    assert (output / "chapters/one.aux").read_text() == "chapter labels"
    assert not (output / "main.pdf").exists()
    good_toc = (output / "main.toc").read_bytes()
    first_log = (output / "pass-1.log").read_bytes()

    def recovered(command, **kwargs):
        directory = pass_directory(command)
        assert (directory / "main.aux").read_text() == "good"
        assert (directory / "main.toc").read_bytes() == good_toc
        assert (directory / "chapters/one.aux").read_text() == "chapter labels"
        fake_pass(directory)
        return SimpleNamespace(returncode=0)

    report = compile_latex(main, output, runner=recovered)
    assert report["status"] == "passed", report
    assert report["resumed"] is True
    assert [row["pass"] for row in report["passes"]] == [1, 2, 2, 3]
    assert (output / "pass-1.log").read_bytes() == first_log
    assert calls[1].joinpath("main.aux").read_text() == "truncated \\@newl@bel{"
    assert (output / "main.pdf").read_bytes() == b"new candidate PDF"


def test_resume_rejects_modified_input_and_completed_or_foreign_pdf(tmp_path):
    main, child = tmp_path / "main.tex", tmp_path / "body.tex"
    main.write_text(r"\input{body.tex}", encoding="utf-8")
    child.write_text("before", encoding="utf-8")
    output = tmp_path / "candidate"
    calls = []

    def successful(command, **kwargs):
        calls.append(1)
        fake_pass(pass_directory(command))
        return SimpleNamespace(returncode=0)

    report = compile_latex(main, output, max_passes=1, runner=successful)
    assert report["status"] == "failed"
    receipt = (output / "compile.state.json").read_bytes()
    child.write_text("after", encoding="utf-8")
    report = compile_latex(main, output, runner=successful)
    assert report["status"] == "failed" and "inputs changed" in report["error"]
    assert len(calls) == 1
    assert (output / "compile.state.json").read_bytes() == receipt
    child.write_text("before", encoding="utf-8")
    (output / "main.pdf").write_bytes(b"foreign finished PDF")
    assert compile_latex(main, output, runner=successful)["status"] == "failed"
    assert len(calls) == 1
    assert (output / "main.pdf").read_bytes() == b"foreign finished PDF"

    clean_output = tmp_path / "fresh"
    assert compile_latex(main, clean_output, runner=successful)["status"] == "passed"
    completed_report = (clean_output / "compile.report.json").read_bytes()
    completed_pdf = sha_file(clean_output / "main.pdf")
    count = len(calls)
    assert compile_latex(main, clean_output, runner=successful)["status"] == "failed"
    assert len(calls) == count
    assert sha_file(clean_output / "main.pdf") == completed_pdf
    assert (clean_output / "compile.report.json").read_bytes() == completed_report


def test_recorded_dependency_and_checkpoint_cannot_change_on_resume(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text("synthetic source", encoding="utf-8")
    font = tmp_path / "dynamic-font.otf"
    font.write_bytes(b"font before")
    output = tmp_path / "candidate"

    def runner(command, **kwargs):
        directory = pass_directory(command)
        fake_pass(directory)
        (directory / "main.fls").write_text("INPUT " + str(font), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    compile_latex(main, output, runner=runner, max_passes=1)
    font.write_bytes(b"font after")
    result = compile_latex(main, output, runner=runner)
    assert result["status"] == "failed" and "inputs changed" in result["error"]
    font.write_bytes(b"font before")
    state = json.loads((output / "compile.state.json").read_text())
    (output / state["checkpoint"]["path"] / "main.aux").write_text("corrupt")
    result = compile_latex(main, output, runner=runner)
    assert result["status"] == "failed" and "checkpoint changed" in result["error"]


def test_resume_recovers_interruption_between_pdf_and_receipt(tmp_path, monkeypatch):
    main = tmp_path / "main.tex"
    main.write_text("source", encoding="utf-8")
    output = tmp_path / "candidate"
    real_write = build_release.write_json

    def fail_receipt(path, value):
        if Path(path).name == "compile.report.json" and value["status"] == "passed":
            raise SystemExit(137)
        return real_write(path, value)

    def runner(command, **kwargs):
        fake_pass(pass_directory(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_release, "write_json", fail_receipt)
    with pytest.raises(SystemExit):
        compile_latex(main, output, runner=runner)
    assert (output / "main.pdf").is_file()
    assert not (output / "compile.report.json").exists()
    digest = sha_file(output / "main.pdf")
    monkeypatch.setattr(build_release, "write_json", real_write)

    def must_not_run(*args, **kwargs):
        pytest.fail("stable checkpoint should only need receipt recovery")

    result = compile_latex(main, output, runner=must_not_run)
    assert result["status"] == "passed"
    assert len(result["passes"]) == 3
    assert sha_file(output / "main.pdf") == digest
    assert json.loads((output / "compile.report.json").read_text())["status"] == "passed"


def test_cli_returns_failure_without_publishing(tmp_path, capsys):
    main = tmp_path / "main.tex"
    main.write_text("source", encoding="utf-8")
    output = tmp_path / "candidate"
    code = build_release.main([str(main), "--output", str(output),
                               "--engine", "nonexistent-test-tex-engine", "--max-passes", "3"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "failed"
    assert "error" in result
    assert not (output / "main.pdf").exists()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"])
def test_log_encodings_preserve_fatal_and_overfull_diagnostics(tmp_path, encoding):
    text = "! Emergency stop.\nMissing character: Ж\nOverfull \\hbox (95.20413pt too wide)\n"
    log, pdf = tmp_path / "main.log", tmp_path / "main.pdf"
    log.write_bytes(text.encode(encoding))
    pdf.write_bytes(b"not a PDF")
    assert read_tex_log(log) == text
    result = inspect_pdf(pdf, log)
    assert "TeX error" in result["errors"]
    assert "Missing character" in result["errors"]
    assert {"kind": "overfull", "count": 1, "max_pt": 95.20413,
            "review": "inspect affected pages in main.log"} in result["warnings"]


def test_utf16_rerun_message_prevents_early_acceptance(tmp_path):
    main = tmp_path / "main.tex"
    main.write_text("synthetic", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        directory = pass_directory(command)
        fake_pass(directory)
        calls.append(directory)
        if len(calls) < 4:
            (directory / "main.log").write_bytes("Please rerun LaTeX.\n".encode("utf-16"))
        return SimpleNamespace(returncode=0)

    result = compile_latex(main, tmp_path / "candidate", runner=runner)
    assert result["status"] == "passed", result
    assert len(calls) == 4


@pytest.mark.skipif(not shutil.which("xelatex"), reason="XeLaTeX is not installed")
def test_real_xelatex_toc_pages_pass_two_equal_pass_three(tmp_path):
    fitz = pytest.importorskip("fitz")
    main = tmp_path / "main.tex"
    main.write_text(r"""\documentclass{article}
\usepackage{fontspec}
\usepackage{hyperref}
\begin{document}
\tableofcontents\clearpage
\section{First chapter}Text for the first chapter.
\newpage
\section{Second chapter}Text for the second chapter.
\end{document}
""", encoding="utf-8")
    output = tmp_path / "candidate"
    result = compile_latex(main, output)
    assert result["status"] == "passed", result
    passes = result["passes"]
    assert len(passes) >= 3
    second, third = [output / row["directory"] for row in passes[1:3]]
    assert (second / "main.toc").read_bytes() == (third / "main.toc").read_bytes()
    with fitz.open(second / "main.pdf") as pdf2, fitz.open(third / "main.pdf") as pdf3:
        assert len(pdf2) == len(pdf3) == 3
        assert pdf2.get_toc() == pdf3.get_toc()
        assert [row[2] for row in pdf3.get_toc()] == [2, 3]
        assert pdf2[0].get_text() == pdf3[0].get_text()
    assert result["pdf_sha256"] == sha_file(output / "main.pdf")
