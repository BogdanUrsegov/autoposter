import zipfile
from pathlib import Path

root = Path(r"c:\Users\Дима\Desktop\AutoPoster\AutoPoster")
out = Path(r"c:\Users\Дима\Desktop\AutoPoster\AutoPoster.zip")
exclude_dirs = {
    "__pycache__", ".git", ".github", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "venv", ".venv", "env", "node_modules",
    "backups", "logs", "data", "tmp_deploy", "site-packages",
}
exclude_names = {
    ".env", ".env.local", ".env.prod", ".env.production",
    ".DS_Store", "Thumbs.db", "_test_ub.py", "_build_zip.py",
}
exclude_suffixes = {
    ".pyc", ".pyo", ".pyd", ".log", ".db", ".sqlite", ".sqlite3",
    ".zip", ".session", ".session-journal",
}

if out.exists():
    out.unlink()

count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if set(rel.parts) & exclude_dirs:
            continue
        if path.name in exclude_names:
            continue
        if path.suffix.lower() in exclude_suffixes:
            continue
        zf.write(path, (Path("AutoPoster") / rel).as_posix())
        count += 1

print(f"wrote {out} files={count} size={out.stat().st_size}")
