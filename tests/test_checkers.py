import subprocess
from pathlib import Path

import pytest

from src.checkers import (CheckResult, check_artifact, check_diff_applies,
                          check_go_test, check_pytest, check_repo_changed,
                          run_acceptance)
from src.models import StepSpec


@pytest.fixture
def go_repo(tmp_path):
    (tmp_path / "go.mod").write_text("module t\n\ngo 1.22\n")
    (tmp_path / "a.go").write_text("package t\n\nfunc Add(a, b int) int { return a + b }\n")
    (tmp_path / "a_test.go").write_text(
        'package t\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) { if Add(1,2) != 3 { t.Fatal("no") } }\n')
    return str(tmp_path)


def test_go_test_ok(go_repo):
    r = check_go_test(go_repo)
    assert r.ok, r.details


def test_go_test_fails(go_repo):
    (Path(go_repo) / "a_test.go").write_text(
        'package t\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) { t.Fatal("boom") }\n')
    r = check_go_test(go_repo)
    assert not r.ok


def test_diff_applies(go_repo):
    diff = """--- a/a.go
+++ b/a.go
@@ -1,3 +1,3 @@
 package t

-func Add(a, b int) int { return a + b }
+func Add(a, b int) int { return a + b + 1 }
"""
    # честный diff против копии: git apply --check на временной копии
    r = check_diff_applies(go_repo, diff)
    assert r.ok


def test_diff_broken(go_repo):
    r = check_diff_applies(go_repo, "--- a/nope\n+++ b/nope\n@@\n-не существует\n")
    assert not r.ok


def test_artifact_exists(tmp_path):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"\x00" * 100)
    assert check_artifact(str(f), {}).ok
    assert not check_artifact(str(tmp_path / "no.mp4"), {}).ok


def test_run_acceptance_chain(go_repo):
    spec = StepSpec(id="s", type="mock.ok", acceptance={"checks": ["go_test"]})
    r = run_acceptance(spec, go_repo, {})
    assert r.ok


def test_run_acceptance_unknown_checker(go_repo):
    spec = StepSpec(id="s", type="mock.ok", acceptance={"checks": ["wat"]})
    assert not run_acceptance(spec, go_repo, {}).ok


def test_repo_changed_detects_noop(go_repo):
    subprocess.run(["git", "init", "-q", go_repo])
    subprocess.run(["git", "-C", go_repo, "add", "-A"])
    subprocess.run(["git", "-C", go_repo, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"])
    subprocess.run(["git", "-C", go_repo, "branch", "dev"])  # база для чекера
    r = check_repo_changed(go_repo)
    assert not r.ok          # чистое дерево, совпадает с базой -> шаг ничего не сделал
    (Path(go_repo) / "x.go").write_text("package t\n")
    r2 = check_repo_changed(go_repo)
    assert r2.ok


def test_run_acceptance_requires_changes(go_repo):
    spec = StepSpec(id="s", type="mock.ok",
                    acceptance={"checks": ["go_test", "repo_changed"]})
    assert not run_acceptance(spec, go_repo, {}).ok   # без правок — провал


def test_pytest_checker_on_fixture_repo(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = .\n")
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr("shutil.which",
                        lambda cmd: "/matrix/dirigent/.venv/bin/python"
                        if cmd == "python3" else None)
    r = check_pytest(str(tmp_path))
    assert r.ok, r.details[-300:]
    (tmp_path / "test_x.py").write_text("def test_bad():\n    assert False\n")
    r2 = check_pytest(str(tmp_path))
    assert not r2.ok


def test_files_exist_checker(tmp_path):
    spec = StepSpec(id="s", type="mock.ok",
                    acceptance={"checks": ["files_exist"],
                                "expect_files": ["a/x.html", "a/app.js"]})
    (tmp_path / "a").mkdir()
    assert not run_acceptance(spec, str(tmp_path), {}).ok
    (tmp_path / "a" / "x.html").write_text("<html></html>")
    (tmp_path / "a" / "app.js").write_text("console.log(1)")
    assert run_acceptance(spec, str(tmp_path), {}).ok


def test_repo_changed_counts_agent_commits(go_repo):
    subprocess.run(["git", "init", "-q", go_repo])
    subprocess.run(["git", "-C", go_repo, "add", "-A"])
    subprocess.run(["git", "-C", go_repo, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"])
    subprocess.run(["git", "-C", go_repo, "branch", "dev"])  # базовая ветка как в реале
    assert not check_repo_changed(go_repo).ok       # чисто, без коммитов — провал
    # агент сам закоммитил правку (дерево чистое!) — всё равно засчитываем
    (Path(go_repo) / "b.go").write_text("package t\n")
    subprocess.run(["git", "-C", go_repo, "add", "-A"])
    subprocess.run(["git", "-C", go_repo, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "agent work"])
    r = check_repo_changed(go_repo)
    assert r.ok, r.details
