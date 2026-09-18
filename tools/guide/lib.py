"""Helpers: pull files and top-level defs out of git at a commit, verbatim."""
import ast, subprocess, textwrap
REPO = __import__("os").path.abspath(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", ".."))

def show(path, rev="HEAD"):
    return subprocess.check_output(["git", "show", f"{rev}:{path}"], cwd=REPO, text=True)

def _lang(path):
    return {"py": "python", "sql": "sql", "yml": "yaml", "yaml": "yaml", "md": "markdown",
            "html": "html", "txt": "", "example": ""}.get(path.rsplit(".", 1)[-1], "")

def file(path, rev="HEAD", title=None):
    body = show(path, rev).rstrip("\n")
    t = title or f"`{path}`" + (f" at `{rev[:7]}`" if rev != "HEAD" else "")
    return f"**{t}**\n\n```{_lang(path)}\n{body}\n```\n"

def defs(path, names, rev="HEAD", title=None):
    """Top-level functions/classes/assignments by name, in the order given."""
    src = show(path, rev)
    lines = src.splitlines()
    tree = ast.parse(src)
    found = {}
    for node in tree.body:
        key = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            key = node.name
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
        if key in names:
            start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno]) - 1
            end = node.end_lineno
            found[key] = "\n".join(lines[start:end])
    missing = [n for n in names if n not in found]
    if missing:
        raise SystemExit(f"{path}@{rev}: not found {missing}")
    body = "\n\n\n".join(found[n] for n in names)
    t = title or f"`{path}` " + ", ".join(f"`{n}`" for n in names) + (f" at `{rev[:7]}`" if rev != "HEAD" else "")
    return f"**{t}**\n\n```python\n{body}\n```\n"

def sh(cmds, title=None):
    t = f"**{title}**\n\n" if title else ""
    return f"{t}```bash\n{textwrap.dedent(cmds).strip()}\n```\n"

def sql(text, title=None):
    t = f"**{title}**\n\n" if title else ""
    return f"{t}```sql\n{textwrap.dedent(text).strip()}\n```\n"

def stat(rev):
    out = subprocess.check_output(["git", "show", "--stat=100", "--format=", rev], cwd=REPO, text=True)
    files = [l.split("|")[0].strip() for l in out.splitlines() if "|" in l]
    return "Files: " + ", ".join(f"`{f}`" for f in files) + "\n"

def commit(rev):
    return subprocess.check_output(["git", "log", "-1", "--format=%h  %s", rev], cwd=REPO, text=True).strip()
