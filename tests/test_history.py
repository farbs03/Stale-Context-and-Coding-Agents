"""Stage 2 history extraction against a small throwaway git repository."""

import subprocess

import pytest

from phase1.gitutil import git
from phase1.history import alias_target, parse_log, process_repo


def test_alias_target_import_only():
    assert alias_target("# Claude\n\n@AGENTS.md\n", False, "CLAUDE.md") == "AGENTS.md"
    assert alias_target("@./AGENTS.md", False, "pkg/CLAUDE.md") == "pkg/AGENTS.md"
    assert alias_target("@AGENTS.md\n\nAlso run `make test`.", False, "CLAUDE.md") is None
    assert alias_target("", False, "CLAUDE.md") is None


def test_alias_target_symlink():
    assert alias_target("AGENTS.md", True, "CLAUDE.md") == "AGENTS.md"
    assert alias_target("../AGENTS.md", True, "web/CLAUDE.md") == "AGENTS.md"


def test_parse_log_rename():
    out = ("\x1eaaa\x1f200\x1fRename to AGENTS.md\n\nR100\tCLAUDE.md\tAGENTS.md\n"
           "\x1ebbb\x1f100\x1fAdd file | with pipe\n\nA\tCLAUDE.md\n")
    c = parse_log(out)
    assert [x["commit"] for x in c] == ["aaa", "bbb"]
    assert c[0]["path_at_commit"] == "AGENTS.md" and c[0]["renamed_from"] == "CLAUDE.md"
    assert c[1]["message"] == "Add file | with pipe" and c[1]["status"] == "A"


@pytest.fixture()
def repo(tmp_path):
    def run(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")

    def commit(files: dict, msg: str, rm=()):
        for name in rm:
            run("rm", "-q", name)
        for name, text in files.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            run("add", name)
        run("commit", "-q", "-m", msg)

    commit({"CLAUDE.md": "v1 see src/a.py\n", "src/a.py": "x=1\n"}, "c1")
    commit({"CLAUDE.md": "v2 see src/a.py\n"}, "c2")
    commit({"README.md": "unrelated\n"}, "c3")
    run("mv", "CLAUDE.md", "AGENTS.md")
    commit({}, "c4 rename")
    commit({"CLAUDE.md": "@AGENTS.md\n"}, "c5 alias")
    commit({"AGENTS.md": "v3 see src/a.py\n"}, "c6")
    return tmp_path


def test_process_repo_rename_alias_and_dedup(repo):
    rows, aliases = process_repo(repo, "o/r", ["AGENTS.md", "CLAUDE.md"])
    agents = [r for r in rows if r["path"] == "AGENTS.md"]
    claude = [r for r in rows if r["path"] == "CLAUDE.md"]
    # AGENTS.md --follow chain includes CLAUDE.md's pre-rename history.
    assert [r["message"] for r in agents] == ["c1", "c2", "c4 rename", "c6"]
    assert agents[0]["path_at_commit"] == "CLAUDE.md" and agents[2]["renamed_from"] == "CLAUDE.md"
    assert agents[0]["content"] == "v1 see src/a.py\n"
    assert not any(r["duplicate_of_other_chain"] for r in agents)
    # CLAUDE.md chain: old versions duplicate AGENTS.md's chain; the new one is an alias.
    c1c2 = [r for r in claude if r["message"] in ("c1", "c2")]
    assert c1c2 and all(r["duplicate_of_other_chain"] for r in c1c2)
    alias = [r for r in claude if r["message"] == "c5 alias"][0]
    assert alias["is_alias"] and alias["alias_target"] == "AGENTS.md"
    assert any(a["kind"] == "import" and a["target"] == "AGENTS.md" for a in aliases)
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    assert all(r["head_commit"] == head for r in rows)
