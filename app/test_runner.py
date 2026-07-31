import subprocess
import sys
from pathlib import Path


class PythonRunError(RuntimeError):
    pass


def run_python_tests(workspace_path, attached_file_path=""):
    workspace = Path(workspace_path or attached_file_path).expanduser()
    if workspace.is_file():
        workspace = workspace.parent
    if not workspace.is_absolute() or not workspace.is_dir():
        raise PythonRunError("请选择一个存在的 Python 项目绝对路径后再运行测试")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise PythonRunError("测试超过 60 秒未结束，已安全停止") from error
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return completed.returncode == 0, output or "测试命令未产生输出", " ".join([sys.executable, "-m", "pytest", "-q"])
