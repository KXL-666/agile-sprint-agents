from pathlib import Path


SAFE_TEXT_SUFFIXES = {".py", ".html", ".css", ".js", ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini"}
MAX_FILE_BYTES = 80_000
MAX_TREE_ITEMS = 80


class WorkspaceError(RuntimeError):
    pass


def workspace_root(path):
    root = Path(path).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise WorkspaceError("请先在项目中填写一个存在的本地项目文件夹绝对路径")
    return root.resolve()


def resolve_workspace_file(root, relative_path):
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise WorkspaceError("文件路径不能为空")
    candidate = Path(relative_path.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkspaceError("智能体只能修改已接入项目文件夹内的文件")
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise WorkspaceError("目标文件不在已接入的项目文件夹内") from error
    return target


def describe_workspace(path):
    root = workspace_root(path)
    files = []
    for item in root.rglob("*"):
        if len(files) >= MAX_TREE_ITEMS:
            break
        if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in item.parts):
            continue
        if item.is_file() and item.suffix.lower() in SAFE_TEXT_SUFFIXES:
            try:
                if item.stat().st_size <= MAX_FILE_BYTES:
                    files.append(str(item.relative_to(root)).replace("\\", "/"))
            except OSError:
                continue
    return root, files


def read_workspace_context(path, limit=12_000):
    root, files = describe_workspace(path)
    chunks = []
    remaining = limit
    for relative in files:
        if remaining <= 0:
            break
        target = root / relative
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = text[: min(len(text), remaining, 2_500)]
        chunks.append(f"--- {relative} ---\n{excerpt}")
        remaining -= len(excerpt)
    return root, files, "\n\n".join(chunks)


def attached_file(path):
    target = Path(path).expanduser()
    if not target.is_absolute() or not target.is_file():
        raise WorkspaceError("请先选择一个存在的本地文件")
    if target.suffix.lower() not in SAFE_TEXT_SUFFIXES:
        raise WorkspaceError("当前仅支持接入文本、代码、配置或 Markdown 文件")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError("单文件超过 80KB，暂不适合直接交给模型分析")
    return target.resolve()


def read_project_context(workspace_path, attached_file_path="", limit=12_000):
    if attached_file_path:
        target = attached_file(attached_file_path)
        text = target.read_text(encoding="utf-8", errors="replace")[:limit]
        return target.parent, [target.name], f"--- {target.name} ---\n{text}", target
    root, files, context = read_workspace_context(workspace_path, limit)
    return root, files, context, None


def project_root(workspace_path, attached_file_path=""):
    return attached_file(attached_file_path).parent if attached_file_path else workspace_root(workspace_path)
