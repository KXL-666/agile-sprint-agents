from pathlib import Path

from .workspace import WorkspaceError, resolve_workspace_file, workspace_root


class FileOperationError(RuntimeError):
    pass


def execute_file_operation(operation, project_workspace="", attached_file_path=""):
    target = Path(operation.target_path).expanduser()
    if not target.is_absolute():
        raise FileOperationError("请使用绝对路径，例如 D:\\毕业设计\\demo.py")
    if operation.action == "create_directory":
        if target.exists():
            raise FileOperationError("目标文件夹已存在，系统不会覆盖它")
        if not target.parent.exists():
            raise FileOperationError("父文件夹不存在，请先创建父文件夹")
        target.mkdir()
        return f"已新建文件夹：{target}"
    if operation.action == "create_file":
        if target.exists():
            raise FileOperationError("目标文件已存在，系统不会覆盖它")
        if not target.parent.exists():
            raise FileOperationError("父文件夹不存在，请先创建父文件夹")
        target.write_text(operation.content or "", encoding="utf-8")
        return f"已新建文件：{target}"
    if operation.action == "write_file":
        try:
            if attached_file_path:
                from .workspace import attached_file
                root = attached_file(attached_file_path).parent
            else:
                root = workspace_root(project_workspace)
            expected_target = resolve_workspace_file(root, str(target.relative_to(root)))
        except (WorkspaceError, ValueError) as error:
            raise FileOperationError("覆盖写入只能发生在已接入的项目文件夹内") from error
        if expected_target != target.resolve() or not target.exists() or not target.is_file():
            raise FileOperationError("要覆盖的目标文件不存在或不在项目文件夹内")
        if attached_file_path:
            try:
                from .workspace import attached_file
                if target.resolve() != attached_file(attached_file_path):
                    raise FileOperationError("单文件接入模式下，Agent 只能修改你选定的那个文件")
            except WorkspaceError as error:
                raise FileOperationError(str(error)) from error
        target.write_text(operation.content or "", encoding="utf-8")
        return f"已更新文件：{target}"
    raise FileOperationError("当前版本只允许审批新建文件夹、新建文件或项目内文件覆盖")
