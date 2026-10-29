"""Handcrafted snippets for the Stage 3 reference extractor."""

import pytest

from phase1.config import phase1_config
from phase1.extract import ExtractConfig, extract_references, parse_tree


@pytest.fixture(scope="module")
def ec() -> ExtractConfig:
    return ExtractConfig.from_config(phase1_config())


def refs_of(content, ec, ref_type=None):
    refs, _ = extract_references(content, ec)
    return {(r.ref_type, r.ref_value) for r in refs
            if ref_type is None or r.ref_type == ref_type}


def values(content, ec, ref_type):
    return {v for _, v in refs_of(content, ec, ref_type)}


# ------------------------------------------------------------------- PATH
def test_inline_code_path(ec):
    assert values("Entry point is `src/server/index.ts`.", ec, "PATH") == {"src/server/index.ts"}


def test_dot_slash_stripped_and_trailing_slash(ec):
    assert values("Run `./scripts/setup.sh` then look in `docs/`.", ec, "PATH") == \
        {"scripts/setup.sh", "docs"}


def test_plain_text_path_and_known_filename(ec):
    v = values("Configuration lives in config/settings.toml and the Makefile.", ec, "PATH")
    assert v == {"config/settings.toml", "Makefile"}


def test_urls_excluded(ec):
    content = ("See https://github.com/org/repo/blob/main/src/a.py and "
               "github.com/org/repo or www.example.com/docs/x.md")
    assert values(content, ec, "PATH") == set()


def test_placeholders_excluded(ec):
    content = ("Create `src/components/<Name>/index.tsx`, edit `{project}/config.yaml`, "
               "copy to `your-app/settings.py`, or `path/to/file.py`, using `$HOME/bin/x.sh`.")
    assert values(content, ec, "PATH") == set()


def test_globs_logged_separately(ec):
    refs, globs = extract_references("Tests match `tests/**/*.test.ts` and `src/*.py`.", ec)
    assert {r.ref_value for r in globs} == {"tests/**/*.test.ts", "src/*.py"}
    assert not [r for r in refs if r.ref_type == "PATH"]


def test_code_fence_paths_and_prompt(ec):
    content = "```bash\n$ python tools/migrate.py --config conf/dev.yaml\ncd packages/api\n```\n"
    assert values(content, ec, "PATH") == {"tools/migrate.py", "conf/dev.yaml", "packages/api"}


def test_markdown_link_target_and_anchor(ec):
    content = "Read [the guide](docs/CONTRIBUTING.md#setup) and [site](https://x.dev/a.md)."
    assert values(content, ec, "PATH") == {"docs/CONTRIBUTING.md"}


def test_product_names_versions_and_system_paths_excluded(ec):
    content = ("Built with Next.js and Node.js 20.1.0; logs go to `/var/log/app.log`, "
               "deps via `@types/node`, and I/O and/or caching.")
    assert values(content, ec, "PATH") == set()


def test_root_slash_and_line_suffix(ec):
    assert values("Bug is at `/src/app/main.py:42`.", ec, "PATH") == {"src/app/main.py"}


def test_import_and_dotfile(ec):
    assert values("@docs/architecture.md\nCopy `.env.example` to `.env`.", ec, "PATH") == \
        {"docs/architecture.md", ".env.example", ".env"}


def test_html_comment_skipped(ec):
    assert values("<!-- old: src/legacy.py -->\nUse src/new.py", ec, "PATH") == {"src/new.py"}


def test_directory_tree_paths(ec):
    content = ("```\n"
               "my-project/\n"
               "├── src/\n"
               "│   ├── api/           # HTTP handlers\n"
               "│   │   └── routes.ts\n"
               "│   └── utils.ts\n"
               "└── package.json\n"
               "```\n")
    assert values(content, ec, "PATH") == {"src", "src/api", "src/api/routes.ts",
                                            "src/utils.ts", "package.json"}


def test_parse_tree_without_root_line():
    block = [(1, "├── app/"), (2, "│   └── page.tsx"), (3, "└── lib/db.ts")]
    assert parse_tree(block) == [(1, "app"), (2, "app/page.tsx"), (3, "lib/db.ts")]


# ----------------------------------------------------------------- SCRIPT
def test_package_scripts(ec):
    content = ("Use `npm run build:prod` and `pnpm run test:unit`.\n"
               "```\nyarn lint\npnpm install\nnpm test\nbun run dev\n```\n")
    assert values(content, ec, "SCRIPT") == {"build:prod", "test:unit", "lint", "test", "dev"}


def test_make_and_just_only_in_code(ec):
    content = ("Make sure you just run the tests.\n"
               "```sh\nmake -j4 build test VERBOSE=1\njust fmt\nmake -C backend migrate\n```\n")
    refs, _ = extract_references(content, ec)
    scripts = {(r.ref_value, r.extra["kind"], r.extra.get("dir")) for r in refs
               if r.ref_type == "SCRIPT"}
    assert scripts == {("build", "make", None), ("test", "make", None),
                       ("fmt", "just", None), ("migrate", "make", "backend")}


def test_workspace_flag_recorded(ec):
    refs, _ = extract_references("`pnpm --filter web run dev`", ec)
    s = [r for r in refs if r.ref_type == "SCRIPT"]
    assert [r.ref_value for r in s] == ["dev"] and s[0].extra["workspace"] is True


# ------------------------------------------------------------------- TOOL
def test_tools_from_commands(ec):
    content = ("```\npnpm install\nuv sync\nuv pip install -e .\npoetry run pytest\n```\n"
               "We like yarn a lot.")
    assert values(content, ec, "TOOL") == {"pnpm", "uv", "poetry"}


def test_pip_in_plain_text_with_subcommand(ec):
    assert values("Install with pip install -r requirements.txt", ec, "TOOL") == {"pip"}


# ------------------------------------------------------------------ IDENT
def test_identifiers(ec):
    content = ("Call `useAuthSession()` from `UserRepository`, set `MAX_RETRIES`, "
               "import `app.core.settings`, see `parse_config`.")
    assert values(content, ec, "IDENT") == {"useAuthSession", "UserRepository", "MAX_RETRIES",
                                             "app.core.settings", "parse_config"}


def test_ident_exclusions(ec):
    content = ("Use `TypeScript`, the `--dry-run` flag, `npm run build`, `id`, "
               "`config.json`, `foo` and `GitHub`.")
    assert values(content, ec, "IDENT") == set()


def test_reference_types_config_respected():
    cfg = dict(phase1_config())
    cfg["reference_types"] = ["PATH"]
    ec2 = ExtractConfig.from_config(cfg)
    assert {t for t, _ in refs_of("`npm run build` `fooBar` `src/a.py`", ec2)} == {"PATH"}


def test_line_numbers_and_context(ec):
    refs, _ = extract_references("# Title\n\nSee `src/a.py`.\n```\nmake build\n```", ec)
    by_val = {r.ref_value: r for r in refs}
    assert by_val["src/a.py"].line_no == 3 and by_val["src/a.py"].context == "inline"
    assert by_val["build"].line_no == 5 and by_val["build"].context == "fence"
    assert by_val["src/a.py"].line_text == "See `src/a.py`."


def test_indented_fence_in_list_item(ec):
    content = ("1. Build:\n"
               "    ```bash\n"
               "    make build\n"
               "    ```\n"
               "2. Run just that test first.\n")
    refs, _ = extract_references(content, ec)
    assert {(r.ref_value, r.context) for r in refs if r.ref_type == "SCRIPT"} == {("build", "fence")}


def test_project_files_are_paths_not_idents_and_style_words_skipped(ec):
    content = "Edit `go.mod`, `flake.nix`, `Directory.Build.props`; use `snake_case` and `camelCase`."
    assert values(content, ec, "PATH") == {"go.mod", "flake.nix", "Directory.Build.props"}
    assert values(content, ec, "IDENT") == set()
