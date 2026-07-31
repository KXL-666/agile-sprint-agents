import json
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from .file_ops import FileOperationError, execute_file_operation
from .artifacts import ArtifactError, export_excel_tasks, export_markdown_summary, export_word_test_report
from .llm import ModelCallError, check_connection
from .models import AdminUser, AgentTeam, AgentTeamMember, FileOperation, ModelUsage, Project, ProviderConfig, Sprint, TeamMember, TestRun, WorkItem, db
from .security import encrypt_secret
from .services import (
    CollaborationStopped,
    ROLE_LABELS,
    add_message,
    active_provider,
    collaboration_members,
    advance_demo_manual_collaboration,
    continue_collaboration_conversation,
    generate_developer_file_proposals,
    generate_repair_proposals,
    generate_retrospective,
    run_demo_collaboration,
    run_model_collaboration,
    start_demo_manual_collaboration,
)
from .test_runner import PythonRunError, run_python_tests
from .workspace import project_root

web = Blueprint("web", __name__)

SUPPORTED_PROVIDERS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model_name": "deepseek-v4-flash"},
    "通义千问": {"base_url": "", "model_name": ""},
    "智谱": {"base_url": "", "model_name": ""},
    "OpenAI": {"base_url": "", "model_name": ""},
    "豆包": {"base_url": "", "model_name": ""},
}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        return view(*args, **kwargs)
    return wrapped


@web.app_context_processor
def inject_globals():
    return {"role_labels": ROLE_LABELS}


def _friendly_model_failure(error):
    return "本次模型调用未成功，系统没有执行任何待审批文件。你可以重试这一步，或到“模型与安全”检查连接。"


def _save_model_failure(sprint_id, stage, error):
    """Discard unfinished collaboration changes, then persist only a safe retry state."""
    db.session.rollback()
    sprint = db.session.get(Sprint, sprint_id)
    if sprint:
        sprint.last_failed_stage = stage
        sprint.last_error = str(error)[:800]
        provider = active_provider()
        if provider:
            db.session.add(ModelUsage(
                provider=provider.provider,
                model_name=provider.model_name,
                success=False,
                attempts=3,
                error_message=str(error)[:500],
            ))
        db.session.commit()


def _clear_model_failure(sprint):
    sprint.last_failed_stage = ""
    sprint.last_error = ""


def _sprint_destination(sprint, after):
    if after == "collaboration":
        return url_for("web.collaboration_center", project_id=sprint.project_id, sprint_id=sprint.id)
    return url_for("web.project_detail", project_id=sprint.project_id)


def _approval_destination(project_id):
    """Keep approvals inside the active page when it supplies a safe local return path."""
    return_to = request.form.get("return_to", "")
    if return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    return url_for("web.project_detail", project_id=project_id) + "#file-operations"


def _operation_preview(operation):
    """Return a bounded before/after preview for a proposed text-file change."""
    if operation.action != "write_file":
        return {"before": "（新建操作，没有旧文件内容）", "after": operation.content or ""}
    target = Path(operation.target_path)
    try:
        if target.exists() and target.is_file() and target.stat().st_size <= 80_000:
            return {"before": target.read_text(encoding="utf-8", errors="replace"), "after": operation.content or ""}
    except OSError:
        pass
    return {"before": "（无法读取当前文件内容）", "after": operation.content or ""}


def _apply_pending_operations_automatically(sprint):
    """Apply the current AI proposal batch after the user selected automatic mode once."""
    project = sprint.project
    operations = FileOperation.query.filter_by(project_id=project.id, status="pending").order_by(FileOperation.created_at.asc()).all()
    applied = failed = 0
    for operation in operations:
        try:
            operation.result = execute_file_operation(operation, project.workspace_path, project.attached_file_path)
            operation.status = "applied"
            applied += 1
        except FileOperationError as error:
            operation.status = "failed"
            operation.result = str(error)
            failed += 1
    if operations:
        add_message(
            sprint,
            "系统执行中心",
            "manager",
            "automatic_execution",
            f"自动执行本轮 {len(operations)} 个文件操作",
            f"本任务已选择自动执行：成功写入 {applied} 项，失败 {failed} 项。"
            "如需中止后续工作，可点击左侧的“停止本次工作”。",
            "自动运行验证" if not failed else "存在执行失败，请在对话中处理失败原因",
        )
    db.session.commit()
    return applied, failed


def _complete_automatic_workflow(sprint):
    """Run auto mode end-to-end: apply proposals, test Python work, repair, then retest."""
    project = sprint.project
    applied, failed = _apply_pending_operations_automatically(sprint)
    if failed:
        sprint.status = "blocked"
        db.session.commit()
        return {"state": "blocked", "applied": applied, "failed": failed, "rounds": 0}

    if sprint.task_type != "development" or not (project.workspace_path or project.attached_file_path):
        generate_retrospective(sprint)
        return {"state": "done", "applied": applied, "failed": 0, "rounds": 0}

    while True:
        from .services import ensure_collaboration_running
        ensure_collaboration_running(sprint)
        try:
            passed, output, command = run_python_tests(project.workspace_path, project.attached_file_path)
        except PythonRunError as error:
            sprint.status = "blocked"
            add_message(sprint, "测试 Agent", "tester", "testing", "无法运行 Python 测试", str(error), "请在对话中补充测试环境或停止本次工作")
            db.session.commit()
            return {"state": "blocked", "applied": applied, "failed": failed, "rounds": sprint.repair_round}

        db.session.add(TestRun(sprint_id=sprint.id, command=command, status="passed" if passed else "failed", output=output, repair_round=sprint.repair_round))
        if passed:
            add_message(sprint, "测试 Agent", "tester", "testing", "真实 Python 测试通过", output[-900:], "生成迭代复盘")
            db.session.commit()
            generate_retrospective(sprint)
            return {"state": "done", "applied": applied, "failed": failed, "rounds": sprint.repair_round}

        sprint.repair_round += 1
        sprint.status = "rework"
        add_message(
            sprint, "测试 Agent", "tester", "testing",
            f"真实测试失败：自动进入第 {sprint.repair_round} 轮修复",
            output[-900:], "测试结果已交给开发 Agent 自动修复；你可随时停止或在对话中介入。",
        )
        db.session.commit()
        try:
            created = generate_repair_proposals(sprint, output)
        except ModelCallError as error:
            _save_model_failure(sprint.id, "自动修复", error)
            return {"state": "model_error", "applied": applied, "failed": failed, "rounds": sprint.repair_round}
        if not created:
            sprint.status = "blocked"
            add_message(sprint, "系统执行中心", "manager", "automatic_execution", "自动修复未生成可执行文件", "AI 没有生成符合工作区规则的修复文件，因此没有继续写入。", "请在对话中补充要求或人工处理")
            db.session.commit()
            return {"state": "blocked", "applied": applied, "failed": failed, "rounds": sprint.repair_round}
        current_applied, current_failed = _apply_pending_operations_automatically(sprint)
        applied += current_applied
        failed += current_failed
        if current_failed:
            sprint.status = "blocked"
            db.session.commit()
            return {"state": "blocked", "applied": applied, "failed": failed, "rounds": sprint.repair_round}


def _continue_auto_after_approval(project_id):
    """Legacy/manual approval pages can still hand an automatic task back to the auto loop."""
    sprint = Sprint.query.filter_by(project_id=project_id).order_by(Sprint.created_at.desc()).first()
    if not sprint or sprint.mode != "auto":
        return
    if FileOperation.query.filter_by(project_id=project_id, status="pending").count():
        return
    try:
        outcome = _complete_automatic_workflow(sprint)
        flash(
            f"已继续自动工作：写入 {outcome['applied']} 项，修复 {outcome['rounds']} 轮。"
            if outcome["state"] == "done" else "已继续自动工作；当前状态和下一步建议已写入对话。",
            "success" if outcome["state"] == "done" else "warning",
        )
    except CollaborationStopped:
        sprint.status = "blocked"
        db.session.commit()
        flash("已按你的请求停止后续自动工作。", "info")


@web.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = AdminUser.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and user.check_password(request.form.get("password", "")):
            session["user_id"] = user.id
            session["display_name"] = user.display_name
            return redirect(url_for("web.dashboard"))
        flash("账号或密码不正确。", "danger")
    return render_template("login.html")


@web.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@web.route("/")
@login_required
def dashboard():
    projects = Project.query.order_by(Project.updated_at.desc()).all()
    teams = AgentTeam.query.order_by(AgentTeam.created_at.asc()).all()
    sprints = Sprint.query.order_by(Sprint.updated_at.desc()).all()
    latest = sprints[0] if sprints else None
    provider = active_provider()
    workspace_project = next((project for project in projects if project.workspace_path or project.attached_file_path), None)
    tested = TestRun.query.count() > 0
    exported = FileOperation.query.filter(FileOperation.requested_by.like("%导出%"), FileOperation.status == "applied").count() > 0
    guide_steps = [
        {"label": "新建项目", "detail": "告诉 Agent 这次迭代要完成什么", "done": bool(projects), "url": url_for("web.new_project")},
        {"label": "接入文件夹", "detail": "限定 Agent 可以读取和提案的范围", "done": bool(workspace_project), "url": url_for("web.project_detail", project_id=workspace_project.id) if workspace_project else url_for("web.new_project")},
        {"label": "配置模型", "detail": "保存并测试真实模型连接", "done": provider is not None, "url": url_for("web.settings")},
        {"label": "启动协同", "detail": "让项目经理、开发、测试与回顾 Agent 协商", "done": bool(latest and latest.status != "draft"), "url": url_for("web.project_detail", project_id=latest.project_id) if latest else url_for("web.new_project")},
        {"label": "审批文件", "detail": "查看差异后批准或跳过代码修改", "done": FileOperation.query.filter_by(status="applied").count() > 0, "url": url_for("web.project_detail", project_id=latest.project_id) + "#file-operations" if latest else url_for("web.new_project")},
        {"label": "运行测试与导出", "detail": "验证代码并生成 Word、Excel、Markdown 产物", "done": tested and exported, "url": url_for("web.project_artifacts", project_id=latest.project_id) if latest else url_for("web.new_project")},
    ]
    usage_rows = ModelUsage.query.order_by(ModelUsage.created_at.desc()).all()
    usage_summary = {
        "calls": len(usage_rows),
        "success": sum(1 for row in usage_rows if row.success),
        "tokens": sum(row.total_tokens for row in usage_rows),
        "last": usage_rows[0] if usage_rows else None,
    }
    # Conservative public DeepSeek Flash estimate: uncached input 1元/M + output 2元/M.
    if provider and provider.provider == "DeepSeek":
        usage_summary["estimated_cost"] = sum((row.prompt_tokens * 1 + row.completion_tokens * 2) / 1_000_000 for row in usage_rows)
    else:
        usage_summary["estimated_cost"] = None
    return render_template("dashboard.html", projects=projects, teams=teams, sprints=sprints, latest=latest, active_provider=provider, guide_steps=guide_steps, usage_summary=usage_summary)


@web.route("/projects/new", methods=["GET", "POST"])
@login_required
def new_project():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        goal = request.form.get("goal", "").strip()
        if not name or not goal:
            flash("请填写项目名称和本次迭代目标。", "danger")
            return render_template("project_form.html")
        project = Project(name=name, description=request.form.get("description", "").strip(), workspace_path=request.form.get("workspace_path", "").strip(), attached_file_path=request.form.get("attached_file_path", "").strip(), status="active")
        db.session.add(project)
        db.session.flush()
        sprint = Sprint(project_id=project.id, name="迭代 1", goal=goal, status="draft")
        db.session.add(sprint)
        for name, skills in [("林晨", "Python、Flask、数据库"), ("周宁", "Python、测试、导出"), ("陈悦", "页面、接口联调")]:
            db.session.add(TeamMember(project_id=project.id, name=name, role="developer", skills=skills))
        db.session.commit()
        flash("项目已创建。现在可以启动四角色协同。", "success")
        return redirect(url_for("web.project_detail", project_id=project.id))
    return render_template("project_form.html")


@web.route("/collaborations/new", methods=["GET", "POST"])
@login_required
def new_collaboration():
    projects = Project.query.order_by(Project.updated_at.desc()).all()
    teams = AgentTeam.query.order_by(AgentTeam.created_at.asc()).all()
    if request.method == "POST":
        try:
            project_id = int(request.form.get("project_id", ""))
        except ValueError:
            project_id = 0
        project = db.session.get(Project, project_id)
        try:
            team_id = int(request.form.get("team_id", ""))
        except ValueError:
            team_id = 0
        selected_team = db.session.get(AgentTeam, team_id)
        goal = request.form.get("goal", "").strip()
        wants_json = request.form.get("ajax") == "1"
        if not project or not goal:
            if wants_json:
                return jsonify({"ok": False, "error": "请先选择项目，并输入想让 AI 帮你做什么。"}), 400
            flash("请先选择项目，并用一句话说明想让 AI 团队完成什么。", "danger")
            return render_template("collaboration_form.html", projects=projects, teams=teams)

        workspace_path = request.form.get("workspace_path", "").strip()
        attached_file_path = request.form.get("attached_file_path", "").strip()
        if workspace_path or attached_file_path:
            project.workspace_path = workspace_path
            project.attached_file_path = attached_file_path

        selected_ids = []
        for value in request.form.getlist("member_ids"):
            try:
                selected_ids.append(int(value))
            except ValueError:
                continue
        valid_ids = {member.id for member in project.members}
        selected_ids = [member_id for member_id in selected_ids if member_id in valid_ids]
        if not selected_ids:
            selected_ids = [member.id for member in project.members[:2]]
        specializations = {
            str(member_id): request.form.get(f"specialization_{member_id}", "").strip()
            for member_id in selected_ids
            if request.form.get(f"specialization_{member_id}", "").strip()
        }
        task_type = request.form.get("task_type", "development")
        if task_type not in {"conversation", "development", "document", "spreadsheet", "office"}:
            task_type = "development"
        mode = request.form.get("mode", "auto")
        if mode not in {"auto", "manual"}:
            mode = "auto"
        sprint = Sprint(
            project_id=project.id,
            assigned_team_id=selected_team.id if selected_team else None,
            name=request.form.get("name", "").strip() or "新的协同任务",
            goal=goal,
            mode=mode,
            status="draft",
            task_type=task_type,
            team_config=json.dumps({
                "preset": request.form.get("team_preset", "standard"),
                "member_ids": selected_ids,
                "specializations": specializations,
            }, ensure_ascii=False),
        )
        db.session.add(sprint)
        db.session.flush()
        db.session.commit()
        model_error = None
        if active_provider():
            try:
                continue_collaboration_conversation(sprint, goal)
            except ModelCallError as error:
                db.session.rollback()
                model_error = str(error)
                flash(f"已创建聊天，但项目经理暂时没有回复。请检查模型连接后重试。({str(error)[:120]})", "warning")
        else:
            add_message(sprint, "你", "user", "user_input", "你发起了新对话", goal, receiver="项目经理 Agent")
            db.session.commit()
            flash("请先在“模型与安全”启用模型，才能开始真实对话。", "warning")
        chat_url = url_for("web.collaboration_center", project_id=project.id, sprint_id=sprint.id)
        if wants_json:
            messages = sorted(sprint.messages, key=lambda item: (item.created_at, item.id))[-2:]
            return jsonify({
                "ok": model_error is None,
                "error": "项目经理暂时无法回复，请检查模型配置后重试。" if model_error else "",
                "sprint_id": sprint.id,
                "chat_url": chat_url,
                "message_url": url_for("web.collaboration_message", project_id=project.id, sprint_id=sprint.id),
                "start_url": url_for("web.start_sprint", sprint_id=sprint.id),
                "task_type": sprint.task_type,
                "messages": [{"sender": item.sender, "role": item.sender_role, "summary": item.summary, "content": item.content or ""} for item in messages],
            }), (200 if model_error is None else 502)
        return redirect(chat_url)
    return render_template("collaboration_form.html", projects=projects, teams=teams)


@web.route("/projects/<int:project_id>/sprints/<int:sprint_id>/collaborate")
@login_required
def collaboration_center(project_id, sprint_id):
    project = db.get_or_404(Project, project_id)
    sprint = db.get_or_404(Sprint, sprint_id)
    if sprint.project_id != project.id:
        return "迭代不属于当前项目", 404
    messages = sorted(sprint.messages, key=lambda message: (message.created_at, message.id))
    latest_reply = next((message for message in reversed(messages) if message.sender_role != "user"), None)
    pending_file_operations = FileOperation.query.filter_by(project_id=project.id, status="pending").order_by(FileOperation.created_at.asc()).all()
    pending_operations = len(pending_file_operations)
    test_runs = TestRun.query.filter_by(sprint_id=sprint.id).order_by(TestRun.created_at.desc()).all()
    return render_template(
        "collaboration_center.html",
        project=project,
        sprint=sprint,
        messages=messages,
        latest_reply=latest_reply,
        pending_operations=pending_operations,
        pending_file_operations=pending_file_operations,
        operation_previews={operation.id: _operation_preview(operation) for operation in pending_file_operations},
        test_runs=test_runs,
        real_model_enabled=active_provider() is not None,
    )


@web.route("/projects/<int:project_id>/sprints/<int:sprint_id>/message", methods=["POST"])
@login_required
def collaboration_message(project_id, sprint_id):
    project = db.get_or_404(Project, project_id)
    sprint = db.get_or_404(Sprint, sprint_id)
    if sprint.project_id != project.id:
        return "该迭代不属于当前项目", 404
    message = request.form.get("message", "").strip()
    wants_json = request.form.get("ajax") == "1"
    if not message:
        if wants_json:
            return jsonify({"ok": False, "error": "请输入你想说的内容。"}), 400
        flash("请先输入你想补充的内容。", "warning")
    else:
        try:
            continue_collaboration_conversation(sprint, message)
            if wants_json:
                messages = sorted(sprint.messages, key=lambda item: (item.created_at, item.id))[-2:]
                return jsonify({"ok": True, "messages": [{"sender": item.sender, "role": item.sender_role, "summary": item.summary, "content": item.content or ""} for item in messages]})
            flash("已发送给 AI 团队，项目经理已给出下一步建议。", "success")
        except ModelCallError as error:
            db.session.rollback()
            if wants_json:
                return jsonify({"ok": False, "error": "本次没有得到 AI 回复，原有内容没有被修改。请检查模型配置后重试。"}), 502
            flash(f"本次消息没有生成回复，原有协同记录没有被修改。你可以重试，或检查模型连接。({str(error)[:160]})", "danger")
    return redirect(url_for("web.collaboration_center", project_id=project.id, sprint_id=sprint.id))


@web.route("/workspace-picker", methods=["POST"])
@login_required
def workspace_picker():
    """Opens Windows' native folder selector on the local machine running Flask."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择要接入的项目文件夹")
        root.destroy()
        return jsonify({"path": selected})
    except Exception as error:
        return jsonify({"error": f"无法打开 Windows 文件夹选择器：{error}"}), 500


@web.route("/file-picker", methods=["POST"])
@login_required
def file_picker():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(title="选择要接入的单个文本或代码文件")
        root.destroy()
        return jsonify({"path": selected})
    except Exception as error:
        return jsonify({"error": f"无法打开 Windows 文件选择器：{error}"}), 500


@web.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id):
    project = db.get_or_404(Project, project_id)
    sprint = Sprint.query.filter_by(project_id=project.id).order_by(Sprint.created_at.desc()).first()
    messages = list(reversed(sprint.messages)) if sprint else []
    file_operations = FileOperation.query.filter_by(project_id=project.id).order_by(FileOperation.created_at.desc()).all()
    test_runs = TestRun.query.filter_by(sprint_id=sprint.id).order_by(TestRun.created_at.desc()).all() if sprint else []
    real_model_enabled = active_provider() is not None
    pending_file_operations = [operation for operation in file_operations if operation.status == "pending"]
    pending_new_operations = [operation for operation in pending_file_operations if operation.action in {"create_file", "create_directory"}]
    pending_modify_operations = [operation for operation in pending_file_operations if operation.action == "write_file"]
    failed_file_operations = [operation for operation in file_operations if operation.status == "failed"]
    completed_file_operations = [operation for operation in file_operations if operation.status in {"applied", "skipped"}]
    operation_previews = {operation.id: _operation_preview(operation) for operation in pending_file_operations}
    return render_template(
        "project_detail.html",
        project=project,
        sprint=sprint,
        messages=messages,
        pending_file_operations=pending_file_operations,
        pending_new_operations=pending_new_operations,
        pending_modify_operations=pending_modify_operations,
        failed_file_operations=failed_file_operations,
        completed_file_operations=completed_file_operations,
        operation_previews=operation_previews,
        test_runs=test_runs,
        real_model_enabled=real_model_enabled,
        pending_operations=len(pending_file_operations),
        active_members=collaboration_members(sprint) if sprint else [],
    )


@web.route("/projects/<int:project_id>/workspace", methods=["POST"])
@login_required
def update_workspace(project_id):
    project = db.get_or_404(Project, project_id)
    workspace_path = request.form.get("workspace_path", "").strip()
    attached_file_path = request.form.get("attached_file_path", "").strip()
    if not workspace_path and not attached_file_path:
        flash("请输入或选择一个项目文件夹，或选择一个单独文件。", "danger")
    else:
        project.workspace_path = workspace_path
        project.attached_file_path = attached_file_path
        db.session.commit()
        flash("已更新本地项目文件夹。开发 Agent 后续只会在此文件夹内读取和提交代码提案。", "success")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web.route("/projects/<int:project_id>/sprints", methods=["POST"])
@login_required
def create_sprint(project_id):
    project = db.get_or_404(Project, project_id)
    name = request.form.get("name", "").strip()
    goal = request.form.get("goal", "").strip()
    if not name or not goal:
        flash("请填写迭代名称和迭代目标。", "danger")
    else:
        sprint = Sprint(project_id=project.id, name=name, goal=goal, status="draft")
        db.session.add(sprint)
        db.session.commit()
        flash("已创建下一次迭代，可以重新启动四角色协同。", "success")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web.route("/projects/<int:project_id>/members", methods=["POST"])
@login_required
def add_team_member(project_id):
    project = db.get_or_404(Project, project_id)
    name = request.form.get("name", "").strip()
    skills = request.form.get("skills", "").strip()
    try:
        workload = max(0, int(request.form.get("workload", "0")))
    except ValueError:
        workload = 0
    if not name or not skills:
        flash("请填写开发 Agent 的名称和技能。", "danger")
    else:
        db.session.add(TeamMember(project_id=project.id, name=name, role="developer", skills=skills, workload=workload, is_agent=True))
        db.session.commit()
        flash(f"已加入开发 Agent：{name}。", "success")
    return redirect(url_for("web.team"))


@web.route("/projects/<int:project_id>/team/delete", methods=["POST"])
@login_required
def delete_team(project_id):
    """Clear only the configurable developer agents for a project."""
    project = db.get_or_404(Project, project_id)
    member_count = len(project.members)
    db.session.query(TeamMember).filter_by(project_id=project.id).delete(synchronize_session=False)
    db.session.commit()
    flash(f"已清空“{project.name}”的 {member_count} 名开发 Agent。项目、聊天和本地文件均未删除。", "success")
    return redirect(url_for("web.team"))


@web.route("/sprints/<int:sprint_id>/delete", methods=["POST"])
@login_required
def delete_sprint(sprint_id):
    """Delete one chat/task and all records that belong specifically to it."""
    sprint = db.get_or_404(Sprint, sprint_id)
    project_id = sprint.project_id
    goal = sprint.goal[:40]
    TestRun.query.filter_by(sprint_id=sprint.id).delete(synchronize_session=False)
    db.session.delete(sprint)
    db.session.commit()
    flash(f"已删除历史任务“{goal}”。本地文件没有被删除。", "success")
    return redirect(url_for("web.dashboard"))


@web.route("/tasks/<int:task_id>/reassign", methods=["POST"])
@login_required
def reassign_task(task_id):
    task = db.get_or_404(WorkItem, task_id)
    sprint = task.sprint
    assignee = request.form.get("assignee", "")
    valid_members = {member.name for member in collaboration_members(sprint)}
    if assignee not in valid_members:
        flash("请选择该项目中存在的开发 Agent。", "danger")
    elif assignee == task.assignee:
        flash("任务负责人未改变。", "warning")
    else:
        old_assignee = task.assignee
        task.assignee = assignee
        add_message(sprint, "管理员", "manager", "arbitration", "管理员手动重新分配任务", f"管理员将“{task.title}”从 {old_assignee or '未分配'} 调整给 {assignee}。项目经理 Agent 将以该决定作为后续协作输入。", "重新分配已生效", assignee)
        db.session.commit()
        flash("任务负责人已更新，并已写入协作记录。", "success")
    return redirect(url_for("web.project_detail", project_id=sprint.project_id))


@web.route("/sprints/<int:sprint_id>/start", methods=["POST"])
@login_required
def start_sprint(sprint_id):
    sprint = db.get_or_404(Sprint, sprint_id)
    after = request.form.get("after", "")
    if sprint.task_type == "conversation":
        flash("对话模式无需启动团队协作，继续和项目经理 Agent 聊天即可。", "info")
        return redirect(url_for("web.collaboration_center", project_id=sprint.project_id, sprint_id=sprint.id))
    if sprint.status == "done":
        flash("这个迭代已完成。请新建下一次迭代后再协同。", "warning")
    else:
        mode = request.form.get("mode", "auto")
        sprint.stop_requested = False
        db.session.commit()
        provider = active_provider()
        if provider:
            try:
                run_model_collaboration(sprint, mode=mode)
                if mode == "auto":
                    count = generate_developer_file_proposals(sprint)
                    outcome = _complete_automatic_workflow(sprint)
                    _clear_model_failure(sprint)
                    db.session.commit()
                    if outcome["state"] == "done":
                        flash(f"AI 已自动完成规划、写入 {outcome['applied']} 个文件、测试和复盘（修复 {outcome['rounds']} 轮）。", "success")
                    elif outcome["state"] == "model_error":
                        flash("AI 已完成已生成的文件操作，但自动修复时模型调用失败。你可在对话中继续或稍后重试。", "warning")
                    else:
                        flash(f"AI 已自动执行 {outcome['applied']} 个文件操作；当前在“需要处理”状态，原因已写入对话记录。", "warning")
                else:
                    _clear_model_failure(sprint)
                    db.session.commit()
                    flash("真实模型已完成规划与仲裁。请确认后再生成逐文件代码提案。", "success")
            except CollaborationStopped:
                sprint = db.session.get(Sprint, sprint_id)
                sprint.status = "blocked"
                sprint.last_error = "管理员已停止本次工作"
                add_message(sprint, "系统", "manager", "stopped", "本次工作已停止", "已完成的讨论记录和待审批文件会保留；系统不会再继续执行后续 Agent。", "可在对话中补充新需求后重新开始")
                db.session.commit()
                flash("已停止本次工作。已经完成的内容仍会保留。", "info")
            except ModelCallError as error:
                _save_model_failure(sprint.id, "启动协同", error)
                flash(_friendly_model_failure(error), "danger")
        else:
            if mode == "manual":
                start_demo_manual_collaboration(sprint)
                flash("已进入手动协同模式：请每次查看当前记录后再确认下一步。", "success")
            else:
                run_demo_collaboration(sprint, mode=mode)
                flash("演示协同已自动完成：文件写入与 Python 测试在真实模型模式下会自动执行。", "success")
    return redirect(_sprint_destination(sprint, after))


@web.route("/sprints/<int:sprint_id>/stop", methods=["POST"])
@login_required
def stop_sprint(sprint_id):
    """Stop after the currently pending model response, without deleting saved work."""
    sprint = db.get_or_404(Sprint, sprint_id)
    sprint.stop_requested = True
    db.session.commit()
    return jsonify({
        "ok": True,
        "message": "已请求停止。当前正在等待的模型回答结束后，系统不会再继续下一位 Agent。",
    })


@web.route("/sprints/<int:sprint_id>/advance", methods=["POST"])
@login_required
def advance_sprint(sprint_id):
    sprint = db.get_or_404(Sprint, sprint_id)
    after = request.form.get("after", "")
    if sprint.mode != "manual":
        flash("当前不是逐步确认模式。", "warning")
    elif active_provider():
        try:
            count = generate_developer_file_proposals(sprint)
            _clear_model_failure(sprint)
            db.session.commit()
            flash(f"开发 Agent 已生成 {count} 个逐文件代码提案，请逐项审核。", "success")
        except ModelCallError as error:
            _save_model_failure(sprint.id, "生成代码提案", error)
            flash(_friendly_model_failure(error), "danger")
    else:
        message = advance_demo_manual_collaboration(sprint)
        flash(message or "当前状态不能继续推进。", "success" if message else "warning")
    return redirect(_sprint_destination(sprint, after))


@web.route("/sprints/<int:sprint_id>/switch-to-auto", methods=["POST"])
@login_required
def switch_sprint_to_auto(sprint_id):
    """Continue a manual run automatically from its current state without replaying it."""
    sprint = db.get_or_404(Sprint, sprint_id)
    after = request.form.get("after", "")
    if sprint.status == "done":
        flash("这个迭代已经完成，无需切换协同方式。", "warning")
    elif sprint.mode != "manual":
        flash("当前已经是自动协同模式。", "warning")
    elif active_provider():
        try:
            pending = FileOperation.query.filter_by(project_id=sprint.project_id, status="pending").count()
            generated = 0
            if not pending:
                generated = generate_developer_file_proposals(sprint)
            sprint.mode = "auto"
            add_message(
                sprint,
                "系统调度中心",
                "manager",
                "mode_switch",
                "管理员将逐步确认切换为自动协同",
                "系统保留此前已完成的角色记录，不会重新拆分或重复分配任务。"
                + (f"开发 Agent 已新增 {generated} 个待审批文件提案。" if generated else "现有待审批文件提案保持不变。"),
                "后续协同将连续推进；文件写入和测试执行仍遵守安全确认规则。",
            )
            db.session.commit()
            flash("已从当前进度切换为自动协同，不会从头重跑。", "success")
        except ModelCallError as error:
            flash(str(error), "danger")
    else:
        for _ in range(5):
            if sprint.status == "done":
                break
            if not advance_demo_manual_collaboration(sprint):
                break
        if sprint.status == "done":
            sprint.mode = "auto"
            db.session.commit()
            flash("已从当前进度切换为自动协同并完成剩余演示步骤。", "success")
        else:
            flash("当前步骤无法自动推进，请继续逐步确认。", "warning")
    return redirect(_sprint_destination(sprint, after))


@web.route("/sprints/<int:sprint_id>/propose-code", methods=["POST"])
@login_required
def propose_code(sprint_id):
    sprint = db.get_or_404(Sprint, sprint_id)
    try:
        count = generate_developer_file_proposals(sprint)
        _clear_model_failure(sprint)
        db.session.commit()
        flash(f"开发 Agent 已生成 {count} 个待审批代码文件提案。", "success")
    except ModelCallError as error:
        _save_model_failure(sprint.id, "生成代码提案", error)
        flash(_friendly_model_failure(error), "danger")
    return redirect(url_for("web.project_detail", project_id=sprint.project_id))


@web.route("/projects/<int:project_id>/file-proposals", methods=["POST"])
@login_required
def create_file_proposal(project_id):
    project = db.get_or_404(Project, project_id)
    action = request.form.get("action", "")
    target_path = request.form.get("target_path", "").strip()
    if action not in {"create_directory", "create_file"} or not target_path:
        flash("请选择操作类型并填写完整的目标路径。", "danger")
        return redirect(url_for("web.project_detail", project_id=project.id))
    operation = FileOperation(
        project_id=project.id,
        requested_by="开发 Agent（待执行提案）",
        action=action,
        target_path=target_path,
        content=request.form.get("content", ""),
        risk_level="medium" if action == "create_directory" else "high",
    )
    db.session.add(operation)
    db.session.commit()
    flash("文件操作提案已创建。请在下方逐项批准或跳过。", "success")
    return redirect(url_for("web.project_detail", project_id=project.id) + "#file-operations")


@web.route("/file-operations/<int:operation_id>/<decision>", methods=["POST"])
@login_required
def decide_file_operation(operation_id, decision):
    operation = db.get_or_404(FileOperation, operation_id)
    if operation.status != "pending":
        flash("这个操作已经处理过了。", "warning")
    elif decision == "skip":
        operation.status = "skipped"
        operation.result = "管理员选择跳过，协作流程继续。"
        db.session.commit()
        flash("已跳过该文件操作。", "success")
    elif decision == "approve":
        try:
            project = db.get_or_404(Project, operation.project_id)
            operation.result = execute_file_operation(operation, project.workspace_path, project.attached_file_path)
            operation.status = "applied"
            add_message(
                Sprint.query.filter_by(project_id=operation.project_id).order_by(Sprint.created_at.desc()).first(),
                "系统审批中心",
                "developer",
                "file_approval",
                "管理员已批准文件操作",
                f"{operation.requested_by} 的操作已执行：{operation.action} → {operation.target_path}",
                "允许进入下一步测试",
            )
            db.session.commit()
            flash(operation.result, "success")
        except FileOperationError as error:
            operation.status = "failed"
            operation.result = str(error)
            db.session.commit()
            flash(str(error), "danger")
    else:
        flash("未知的审批动作。", "danger")
    _continue_auto_after_approval(operation.project_id)
    return redirect(_approval_destination(operation.project_id))


@web.route("/projects/<int:project_id>/file-operations/approve-all", methods=["POST"])
@login_required
def approve_all_file_operations(project_id):
    """Execute every currently pending proposal in order, recording every result."""
    project = db.get_or_404(Project, project_id)
    operations = FileOperation.query.filter_by(project_id=project.id, status="pending").order_by(FileOperation.created_at.asc()).all()
    if not operations:
        flash("当前没有等待确认的文件操作。", "warning")
        return redirect(_approval_destination(project.id))

    applied = 0
    failed = 0
    for operation in operations:
        try:
            operation.result = execute_file_operation(operation, project.workspace_path, project.attached_file_path)
            operation.status = "applied"
            applied += 1
        except FileOperationError as error:
            operation.status = "failed"
            operation.result = str(error)
            failed += 1

    sprint = Sprint.query.filter_by(project_id=project.id).order_by(Sprint.created_at.desc()).first()
    if sprint:
        add_message(
            sprint,
            "系统审批中心",
            "developer",
            "file_approval",
            "管理员一键确认文件操作",
            f"已按提案顺序处理 {len(operations)} 项文件操作：成功 {applied} 项，失败 {failed} 项。失败项已记录，后续项继续执行。",
            "可进入 Python 测试验证修改结果",
        )
    db.session.commit()
    flash(f"一键确认完成：成功执行 {applied} 项，失败 {failed} 项。", "success" if not failed else "warning")
    if not failed:
        _continue_auto_after_approval(project.id)
    return redirect(_approval_destination(project.id))


@web.route("/projects/<int:project_id>/file-operations/approve-selected", methods=["POST"])
@login_required
def approve_selected_file_operations(project_id):
    project = db.get_or_404(Project, project_id)
    selected_ids = [int(value) for value in request.form.getlist("operation_ids") if value.isdigit()]
    operations = FileOperation.query.filter(
        FileOperation.project_id == project.id,
        FileOperation.status == "pending",
        FileOperation.id.in_(selected_ids),
    ).order_by(FileOperation.created_at.asc()).all() if selected_ids else []
    if not operations:
        flash("请先勾选至少一个等待确认的文件操作。", "warning")
        return redirect(_approval_destination(project.id))
    applied = failed = 0
    for operation in operations:
        try:
            operation.result = execute_file_operation(operation, project.workspace_path, project.attached_file_path)
            operation.status = "applied"
            applied += 1
        except FileOperationError as error:
            operation.status = "failed"
            operation.result = str(error)
            failed += 1
    db.session.commit()
    flash(f"已处理勾选的 {len(operations)} 项：成功 {applied} 项，失败 {failed} 项。", "success" if not failed else "warning")
    if not failed:
        _continue_auto_after_approval(project.id)
    return redirect(_approval_destination(project.id))


@web.route("/sprints/<int:sprint_id>/run-tests", methods=["POST"])
@login_required
def run_tests(sprint_id):
    sprint = db.get_or_404(Sprint, sprint_id)
    project = sprint.project
    try:
        passed, output, command = run_python_tests(project.workspace_path, project.attached_file_path)
    except PythonRunError as error:
        flash(str(error), "danger")
        return redirect(_sprint_destination(sprint, request.form.get("after")))

    test_run = TestRun(sprint_id=sprint.id, command=command, status="passed" if passed else "failed", output=output, repair_round=sprint.repair_round)
    db.session.add(test_run)
    if passed:
        add_message(sprint, "测试 Agent", "tester", "testing", "真实 Python 测试通过", output[-900:], "允许进入下一阶段")
        try:
            generate_retrospective(sprint)
            flash("Python 测试通过，回顾 Agent 已生成真实迭代复盘。", "success")
        except ModelCallError as error:
            sprint.status = "reviewing"
            db.session.commit()
            flash(f"Python 测试通过，但回顾 Agent 调用失败：{error}", "warning")
    else:
        sprint.repair_round += 1
        sprint.status = "rework"
        add_message(
            sprint,
            "测试 Agent",
            "tester",
            "testing",
            f"真实测试失败：进入第 {sprint.repair_round} 轮修复",
            output[-900:],
            "请求开发 Agent 分析并提交修复提案；你可随时在对话中补充要求或停止本次工作。",
        )
        db.session.commit()
        try:
            count = generate_repair_proposals(sprint, output)
            flash(f"测试失败，已进入第 {sprint.repair_round} 轮修复；已生成 {count} 个修复提案，等待你审批。", "warning")
        except ModelCallError as error:
            flash(f"测试失败，修复提案生成失败：{error}", "danger")
        return redirect(_sprint_destination(sprint, request.form.get("after")))
    db.session.commit()
    return redirect(_sprint_destination(sprint, request.form.get("after")))


@web.route("/sprints/<int:sprint_id>/export/<artifact_type>", methods=["POST"])
@login_required
def export_artifact(sprint_id, artifact_type):
    sprint = db.get_or_404(Sprint, sprint_id)
    project = sprint.project
    try:
        if artifact_type == "markdown":
            path = export_markdown_summary(project, sprint)
        elif artifact_type == "word":
            runs = TestRun.query.filter_by(sprint_id=sprint.id).order_by(TestRun.created_at.asc()).all()
            path = export_word_test_report(project, sprint, runs)
        elif artifact_type == "excel":
            path = export_excel_tasks(project, sprint)
        else:
            flash("不支持的导出类型。", "danger")
            return redirect(_sprint_destination(sprint, request.form.get("after")))
        db.session.add(FileOperation(project_id=project.id, requested_by="管理员（导出项目资料）", action="create_file", target_path=str(path), risk_level="medium", status="applied", result="管理员确认后已生成项目资料。"))
        db.session.commit()
        flash(f"已生成：{path}", "success")
    except ArtifactError as error:
        flash(str(error), "danger")
    return redirect(_sprint_destination(sprint, request.form.get("after")))


def _demo_workspace_root():
    return Path(__file__).resolve().parent.parent / "demo_workspace" / "club_registration"


def _reset_demo_workspace():
    root = _demo_workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "registration.py").write_text(
        '''"""答辩演示用的社团报名业务模块。"""

registrations = []


def register(name, student_id):
    """登记学生；初始版本故意缺少重复学号校验，供 Agent 修复演示。"""
    if not name or not student_id:
        raise ValueError("姓名和学号不能为空")
    record = {"name": name.strip(), "student_id": student_id.strip()}
    registrations.append(record)
    return record


def list_registrations():
    return list(registrations)
''', encoding="utf-8")
    (root / "test_registration.py").write_text(
        '''import pytest

from registration import list_registrations, register, registrations


def setup_function():
    registrations.clear()


def test_register_student():
    result = register("张同学", "2026001")
    assert result["name"] == "张同学"
    assert list_registrations() == [{"name": "张同学", "student_id": "2026001"}]


def test_duplicate_student_id_is_rejected():
    register("张同学", "2026001")
    with pytest.raises(ValueError, match="学号已报名"):
        register("李同学", "2026001")
''', encoding="utf-8")
    return root


@web.route("/demo/launch", methods=["POST"])
@login_required
def launch_demo():
    root = _reset_demo_workspace()
    project = Project.query.filter_by(name="社团报名系统智能迭代演示").first()
    if not project:
        project = Project(
            name="社团报名系统智能迭代演示",
            description="固定答辩案例：修复社团报名模块的重复学号校验，并完整展示任务拆分、文件审批、测试退回和复盘。",
            workspace_path=str(root),
            status="active",
        )
        db.session.add(project)
        db.session.flush()
        for name, skills in [("林晨", "Python、Flask、数据库"), ("周宁", "Python、测试、数据导出"), ("陈悦", "页面、接口联调")]:
            db.session.add(TeamMember(project_id=project.id, name=name, role="developer", skills=skills, is_agent=True))
    else:
        project.workspace_path = str(root)
        project.attached_file_path = ""
    sprint = Sprint(
        project_id=project.id,
        name="演示迭代：修复重复报名校验",
        goal="为社团报名模块补充重复学号校验、友好错误提示与 pytest 用例验证。",
        status="draft",
    )
    db.session.add(sprint)
    db.session.commit()
    flash("演示案例已恢复到初始状态。现在可选择自动协同或逐步确认，真实文件修改仍需你批准。", "success")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web.route("/projects/<int:project_id>/demo/reset", methods=["POST"])
@login_required
def reset_demo(project_id):
    project = db.get_or_404(Project, project_id)
    if Path(project.workspace_path).resolve() != _demo_workspace_root().resolve():
        flash("只有内置社团报名演示案例可以一键恢复，其他项目文件不会被系统重置。", "danger")
    else:
        _reset_demo_workspace()
        flash("已恢复演示案例的初始代码。现有协作记录仍保留，方便答辩时对照讲解。", "success")
    return redirect(url_for("web.project_detail", project_id=project.id))


@web.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
def delete_project(project_id):
    """Remove only application records; the user's local workspace is deliberately untouched."""
    project = db.get_or_404(Project, project_id)
    project_name = project.name
    sprint_ids = [sprint.id for sprint in project.sprints]
    if sprint_ids:
        TestRun.query.filter(TestRun.sprint_id.in_(sprint_ids)).delete(synchronize_session=False)
    FileOperation.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()
    flash(f"已删除系统中的项目“{project_name}”及其协作记录。本地文件夹和代码没有被删除。", "success")
    return redirect(url_for("web.dashboard"))


@web.route("/projects/<int:project_id>/artifacts")
@login_required
def project_artifacts(project_id):
    project = db.get_or_404(Project, project_id)
    files = []
    try:
        reports = project_root(project.workspace_path, project.attached_file_path) / "agilesprint_reports"
        if reports.exists():
            files = sorted((item for item in reports.iterdir() if item.is_file()), key=lambda item: item.stat().st_mtime, reverse=True)
    except Exception:
        reports = None
    sprint = Sprint.query.filter_by(project_id=project.id).order_by(Sprint.created_at.desc()).first()
    return render_template("artifacts.html", project=project, sprint=sprint, files=files, reports=reports)


@web.route("/projects/<int:project_id>/artifacts/<path:filename>/download")
@login_required
def download_artifact(project_id, filename):
    project = db.get_or_404(Project, project_id)
    if Path(filename).name != filename:
        return "无效文件名", 400
    try:
        reports = project_root(project.workspace_path, project.attached_file_path) / "agilesprint_reports"
    except Exception:
        return "未接入有效项目目录", 400
    return send_from_directory(reports, filename, as_attachment=True)


@web.route("/team")
@login_required
def team():
    teams = AgentTeam.query.order_by(AgentTeam.created_at.asc()).all()
    return render_template("team.html", teams=teams)


@web.route("/teams", methods=["POST"])
@login_required
def create_agent_team():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("请为团队填写一个名称。", "danger")
    elif AgentTeam.query.filter_by(name=name).first():
        flash("已有同名团队，请换一个名称。", "warning")
    else:
        db.session.add(AgentTeam(name=name, description=description))
        db.session.commit()
        flash(f"已创建团队“{name}”。现在可以为它加入开发 Agent。", "success")
    return redirect(url_for("web.team"))


@web.route("/teams/<int:team_id>/members", methods=["POST"])
@login_required
def add_agent_team_member(team_id):
    team = db.get_or_404(AgentTeam, team_id)
    name = request.form.get("name", "").strip()
    skills = request.form.get("skills", "").strip()
    try:
        workload = max(0, int(request.form.get("workload", "0")))
    except ValueError:
        workload = 0
    if not name or not skills:
        flash("请填写 Agent 名称和技能。", "danger")
    else:
        db.session.add(AgentTeamMember(team_id=team.id, name=name, skills=skills, workload=workload))
        db.session.commit()
        flash(f"{name} 已加入“{team.name}”。", "success")
    return redirect(url_for("web.team"))


@web.route("/teams/<int:team_id>/delete", methods=["POST"])
@login_required
def delete_agent_team(team_id):
    team = db.get_or_404(AgentTeam, team_id)
    if any(sprint.assigned_team_id == team.id for sprint in team.assigned_sprints):
        for sprint in team.assigned_sprints:
            sprint.assigned_team_id = None
    name = team.name
    db.session.delete(team)
    db.session.commit()
    flash(f"已删除团队“{name}”。已有任务仍会保留，只是不再绑定该团队。", "success")
    return redirect(url_for("web.team"))


@web.route("/settings")
@login_required
def settings():
    selected = request.args.get("provider") or (active_provider().provider if active_provider() else "DeepSeek")
    if selected not in SUPPORTED_PROVIDERS:
        selected = "DeepSeek"
    provider = ProviderConfig.query.filter_by(provider=selected).first()
    return render_template("settings.html", provider=provider, selected_provider=selected, providers=SUPPORTED_PROVIDERS, active=active_provider())


@web.route("/settings/provider", methods=["POST"])
@login_required
def save_provider_settings():
    provider_name = request.form.get("provider", "")
    if provider_name not in SUPPORTED_PROVIDERS:
        flash("不支持的模型提供商。", "danger")
        return redirect(url_for("web.settings"))
    key = request.form.get("api_key", "").strip()
    provider = ProviderConfig.query.filter_by(provider=provider_name).first()
    if not provider:
        provider = ProviderConfig(provider=provider_name)
        db.session.add(provider)
    defaults = SUPPORTED_PROVIDERS[provider_name]
    provider.base_url = request.form.get("base_url", "").strip() or defaults["base_url"]
    provider.model_name = request.form.get("model_name", "").strip() or defaults["model_name"]
    if key:
        provider.encrypted_key = encrypt_secret(key)
        ProviderConfig.query.update({ProviderConfig.enabled: False})
        provider.enabled = True
        flash(f"{provider_name} 配置已加密保存并启用。", "success")
    elif not provider.encrypted_key:
        flash("请输入 API Key 后才能启用真实模型。", "danger")
        return redirect(url_for("web.settings", provider=provider_name))
    else:
        ProviderConfig.query.update({ProviderConfig.enabled: False})
        provider.enabled = True
    db.session.commit()
    return redirect(url_for("web.settings", provider=provider_name))


@web.route("/settings/provider/delete", methods=["POST"])
@login_required
def delete_provider_settings():
    provider_name = request.form.get("provider", "")
    provider = ProviderConfig.query.filter_by(provider=provider_name).first()
    if provider:
        provider.encrypted_key = ""
        provider.enabled = False
        db.session.commit()
    flash(f"已删除本地保存的 {provider_name} API Key，系统将回到演示模式。", "success")
    return redirect(url_for("web.settings"))


@web.route("/settings/provider/test", methods=["POST"])
@login_required
def test_provider_settings():
    provider_name = request.form.get("provider", "")
    provider = ProviderConfig.query.filter_by(provider=provider_name, enabled=True).first()
    if not provider:
        flash("请先保存并启用该模型，再测试连接。", "danger")
    else:
        try:
            returned_model = check_connection(provider)
            flash(f"模型连接成功：{provider_name} / {returned_model}。", "success")
        except ModelCallError as error:
            flash(str(error), "danger")
    return redirect(url_for("web.settings", provider=provider_name or "DeepSeek"))
