import json
import logging
import os
import secrets
import string

from .llm import ModelCallError, complete
from .models import AdminUser, AgentMessage, AgentTeam, AgentTeamMember, FileOperation, Project, ProviderConfig, Sprint, TeamMember, WorkItem, db
from .workspace import WorkspaceError, read_project_context, resolve_workspace_file

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_PASSWORD = "Admin@12345"


def _resolve_admin_password():
    """优先从环境变量读取管理员初始密码；否则使用演示默认密码并给出警告。"""
    env_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if env_password:
        return env_password
    logger.warning(
        "=" * 72 + "\n"
        "[安全警告] 未设置 ADMIN_PASSWORD 环境变量，将使用默认演示密码。\n"
        "默认密码：%s\n"
        "建议：请在 .env 中配置 ADMIN_PASSWORD 后重启应用。\n"
        + "=" * 72,
        DEFAULT_ADMIN_PASSWORD,
    )
    return DEFAULT_ADMIN_PASSWORD


ROLE_LABELS = {
    "manager": "项目经理 Agent",
    "developer": "开发 Agent",
    "tester": "测试 Agent",
    "reviewer": "回顾 Agent",
}


class CollaborationStopped(RuntimeError):
    """Raised when the administrator asks the sequential AI workflow to stop."""


def ensure_collaboration_running(sprint):
    """Reload the persisted stop flag after a completed model step."""
    db.session.expire(sprint, ["stop_requested"])
    if sprint.stop_requested:
        raise CollaborationStopped("管理员已请求停止本次工作")


def active_provider():
    return ProviderConfig.query.filter_by(enabled=True).order_by(ProviderConfig.updated_at.desc()).first()


def ensure_seed_data():
    if not AdminUser.query.filter_by(username="admin").first():
        admin = AdminUser(username="admin", display_name="系统管理员")
        admin.set_password(_resolve_admin_password())
        db.session.add(admin)

    if not Project.query.first():
        project = Project(
            name="学生社团报名系统",
            description="答辩演示项目：为现有 Python 应用增加登录、报名和导出功能。",
            status="active",
        )
        db.session.add(project)
        db.session.flush()
        members = [
            ("林晨", "developer", "Python、Flask、数据库", 1),
            ("周宁", "developer", "Python、测试、数据导出", 2),
            ("陈悦", "developer", "前端页面、接口联调", 0),
        ]
        for name, role, skills, workload in members:
            db.session.add(TeamMember(project_id=project.id, name=name, role=role, skills=skills, workload=workload))
        sprint = Sprint(project_id=project.id, name="迭代 1：报名功能", goal="完成登录、报名和名单导出。", status="draft")
        db.session.add(sprint)
    if not AgentTeam.query.first():
        default_team = AgentTeam(name="默认开发团队", description="适合 Python、Flask 和常规网页开发任务")
        db.session.add(default_team)
        db.session.flush()
        for name, skills, workload in [
            ("林晨", "Python、Flask、数据库", 1),
            ("周宁", "Python、测试、数据导出", 2),
            ("陈悦", "前端页面、接口联调", 0),
        ]:
            db.session.add(AgentTeamMember(team_id=default_team.id, name=name, skills=skills, workload=workload))
    db.session.commit()


def add_message(sprint, sender, role, stage, summary, content, decision="", receiver="团队"):
    message = AgentMessage(
        sprint_id=sprint.id,
        sender=sender,
        sender_role=role,
        receiver=receiver,
        stage=stage,
        summary=summary,
        content=content,
        decision=decision,
    )
    db.session.add(message)
    return message


def collaboration_members(sprint):
    """Return the developers chosen for this sprint, falling back to the full project team."""
    if sprint.assigned_team and sprint.assigned_team.members:
        return list(sprint.assigned_team.members)
    try:
        config = json.loads(sprint.team_config or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    chosen_ids = {int(member_id) for member_id in config.get("member_ids", []) if str(member_id).isdigit()}
    members = [member for member in sprint.project.members if not chosen_ids or member.id in chosen_ids]
    return members or list(sprint.project.members)


def member_specialization(sprint, member):
    try:
        config = json.loads(sprint.team_config or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    return (config.get("specializations", {}) or {}).get(str(member.id), "")


def _create_tasks(sprint):
    if sprint.tasks:
        return list(sprint.tasks)
    items = [
        ("设计报名数据模型与接口", "定义报名记录、校验规则和接口返回格式。", "high", "林晨", "2 小时"),
        ("实现登录与报名页面", "完成登录表单、报名表单和提交反馈。", "high", "陈悦", "3 小时"),
        ("实现名单导出与自动测试", "导出 CSV，并为核心报名流程编写 pytest。", "medium", "周宁", "2 小时"),
    ]
    tasks = []
    for title, description, priority, assignee, estimate in items:
        task = WorkItem(sprint_id=sprint.id, title=title, description=description, priority=priority, assignee=assignee, estimate=estimate, status="assigned")
        db.session.add(task)
        tasks.append(task)
    return tasks


def run_demo_collaboration(sprint, mode="auto"):
    """Clear, deterministic demo pipeline. A configured model will replace these outputs later."""
    sprint.mode = mode
    tasks = _create_tasks(sprint)
    sprint.status = "planning"
    add_message(
        sprint, "项目经理 Agent", "manager", "planning", "已完成需求澄清与任务拆分",
        f"收到迭代目标：{sprint.goal or '完成本次功能迭代'}。按依赖关系拆为 3 项可验证任务，并结合成员技能和当前负载完成初步分配。",
        "进入开发阶段",
    )
    add_message(
        sprint, "林晨", "developer", "assignment", "接受后端开发任务",
        "我具备 Flask 与数据库经验，当前负载为低，可在本迭代承担数据模型与接口实现。",
        "接受分配", "项目经理 Agent",
    )
    add_message(
        sprint, "陈悦", "developer", "assignment", "接受页面开发任务",
        "页面任务依赖接口字段定义；我将先完成表单结构，在接口确定后联调。",
        "接受分配", "项目经理 Agent",
    )
    add_message(
        sprint, "周宁", "developer", "assignment", "接受导出与测试任务",
        "测试将在接口完成后执行，优先验证重复报名、必填字段和导出结果。",
        "接受分配", "项目经理 Agent",
    )
    sprint.status = "developing"
    for task in tasks:
        task.status = "in_progress"
    add_message(
        sprint, "开发 Agent 团队", "developer", "developing", "开发方案已提交，等待逐文件审批",
        "将生成或修改 Python 路由、数据模型、HTML 表单和 pytest 文件。真实文件修改会在后续版本逐文件提交给管理员确认。",
        "提交文件修改提案",
    )
    sprint.status = "testing"
    for task in tasks:
        task.status = "testing"
    add_message(
        sprint, "测试 Agent", "tester", "testing", "测试发现一个可修复问题",
        "模拟测试结果：报名接口在重复提交时缺少友好提示。退回“设计报名数据模型与接口”任务，要求增加唯一性校验与错误反馈。",
        "退回开发修复", "项目经理 Agent",
    )
    sprint.status = "rework"
    sprint.repair_round = 1
    tasks[0].status = "rework"
    tasks[0].return_reason = "重复报名未给出友好错误提示"
    add_message(
        sprint, "项目经理 Agent", "manager", "arbitration", "仲裁：保持分配，要求优先修复",
        "问题由林晨负责修复，陈悦等待接口错误字段稳定后补充页面提示；未改变其他任务负责人。",
        "进入第 1 轮修复",
    )
    tasks[0].status = "done"
    for task in tasks[1:]:
        task.status = "done"
    sprint.status = "reviewing"
    add_message(
        sprint, "开发 Agent 团队", "developer", "rework", "已完成第 1 轮修复",
        "增加重复报名校验和清晰提示；测试用例覆盖正常报名、重复报名和导出。",
        "重新提交测试", "测试 Agent",
    )
    add_message(
        sprint, "测试 Agent", "tester", "testing", "复测通过",
        "模拟 pytest 结果：3 passed。登录、报名、重复报名提示和名单导出均符合本迭代验收条件。",
        "允许进入复盘",
    )
    sprint.status = "done"
    add_message(
        sprint, "回顾 Agent", "reviewer", "reviewing", "迭代复盘已生成",
        "本迭代按计划完成，发生 1 次测试退回和 1 轮修复。改进项：项目经理在拆分阶段增加“异常提示”验收标准，减少后续返工。",
        "迭代完成",
    )
    db.session.commit()
    return sprint


def start_demo_manual_collaboration(sprint):
    """Starts one visible stage only; the administrator advances each next stage."""
    tasks = _create_tasks(sprint)
    sprint.mode = "manual"
    sprint.status = "planning"
    add_message(
        sprint, "项目经理 Agent", "manager", "planning", "已完成需求澄清与任务拆分",
        f"收到迭代目标：{sprint.goal or '完成本次功能迭代'}。已拆分为 {len(tasks)} 项任务，等待开发成员逐一评估。",
        "等待管理员确认进入开发成员评估",
    )
    db.session.commit()
    return sprint


def advance_demo_manual_collaboration(sprint):
    tasks = list(sprint.tasks)
    if sprint.status == "planning":
        for task in tasks:
            add_message(sprint, task.assignee or "开发 Agent", "developer", "assignment", f"接受：{task.title}", "我已阅读项目经理的拆分结果，将按自身技能与当前负载执行该任务，并在完成后提交文件修改提案。", "接受分配", "项目经理 Agent")
        sprint.status = "developing"
        message = "开发成员评估已完成，等待管理员确认进入测试验证。"
    elif sprint.status == "developing":
        for task in tasks:
            task.status = "testing"
        add_message(sprint, "开发 Agent 团队", "developer", "developing", "开发方案已提交", "已形成待审批的文件修改计划。实际文件不会在未经管理员逐项批准时被创建、覆盖或执行。", "交给测试 Agent 评估")
        add_message(sprint, "测试 Agent", "tester", "testing", "发现一个需要修复的问题", "模拟测试发现：重复报名缺少友好提示。测试 Agent 将任务退回，并要求补充异常场景测试。", "退回开发修复", "项目经理 Agent")
        if tasks:
            tasks[0].status = "rework"
            tasks[0].return_reason = "重复报名未给出友好错误提示"
        sprint.status = "rework"
        sprint.repair_round = 1
        message = "测试退回已记录，等待管理员确认第 1 轮修复。"
    elif sprint.status == "rework":
        for task in tasks:
            task.status = "done"
        add_message(sprint, "项目经理 Agent", "manager", "arbitration", "仲裁：保持分配并优先修复", "项目经理依据退回原因确认原负责人继续修复，其他成员保持待命并协助回归验证。", "进入第 1 轮修复")
        add_message(sprint, "开发 Agent 团队", "developer", "rework", "已完成修复并重新提交", "已补充重复报名校验与对应测试用例，等待测试 Agent 复测。", "重新提交测试", "测试 Agent")
        add_message(sprint, "测试 Agent", "tester", "testing", "复测通过", "模拟 pytest 结果：3 passed。核心验收点已满足。", "允许进入复盘")
        sprint.status = "reviewing"
        message = "复测通过，等待管理员确认生成迭代复盘。"
    elif sprint.status == "reviewing":
        add_message(sprint, "回顾 Agent", "reviewer", "reviewing", "迭代复盘已生成", "本迭代发生 1 次测试退回和 1 轮修复。改进项：在需求拆分阶段补充异常场景验收标准。", "迭代完成")
        sprint.status = "done"
        message = "迭代已完成，可回放全部协作记录。"
    else:
        return None
    db.session.commit()
    return message


def _text(payload, field, fallback):
    value = payload.get(field, fallback)
    return value if isinstance(value, str) and value.strip() else fallback


def continue_collaboration_conversation(sprint, user_message):
    """Allow the administrator to steer an active sprint through the project manager."""
    provider = active_provider()
    if not provider:
        raise ModelCallError("请先在模型与安全中启用一个模型，才能继续和 AI 团队对话")
    text = (user_message or "").strip()
    if not text:
        raise ModelCallError("请输入想补充或调整的内容")

    history = [
        {"sender": item.sender, "summary": item.summary, "content": (item.content or "")[:900]}
        for item in sorted(sprint.messages, key=lambda item: (item.created_at, item.id))[-8:]
    ]
    prompt = {
        "project": sprint.project.name,
        "goal": sprint.goal,
        "current_status": sprint.status,
        "recent_collaboration": history,
        "user_message": text,
        "instruction": "你是项目经理 Agent。用户正在中途补充要求。先理解并确认要求，说明会影响哪一部分，并给出简洁下一步。不要声称已经写入文件、运行测试或完成尚未执行的工作。只输出 JSON：summary, response, next_step。",
    }
    result = complete(
        provider,
        "你负责接收用户的中途指令，并协调敏捷开发 AI 团队。回答清楚、简短、面向普通用户。",
        json.dumps(prompt, ensure_ascii=False),
    )
    add_message(sprint, "你", "user", "user_input", "你补充了要求", text, receiver="项目经理 Agent")
    add_message(
        sprint,
        "项目经理 Agent",
        "manager",
        "user_feedback",
        _text(result, "summary", "已收到你的补充要求"),
        _text(result, "response", "我会把这条要求纳入接下来的协同处理。"),
        _text(result, "next_step", "继续当前协同"),
        receiver="AI 团队",
    )
    db.session.commit()
    return result


def run_model_collaboration(sprint, mode="auto"):
    """Run sequential, role-specific model calls. It never fabricates a result on failure."""
    provider = active_provider()
    if not provider:
        raise ModelCallError("未启用任何模型，无法启动真实模型协作")
    ensure_collaboration_running(sprint)
    project = sprint.project
    members = collaboration_members(sprint)
    team = [{"name": member.name, "skills": member.skills, "specialization": member_specialization(sprint, member), "workload": member.workload} for member in members]
    manager_prompt = {
        "project": project.name,
        "goal": sprint.goal,
        "task_type": sprint.task_type,
        "team": team,
        "instruction": "你是项目经理 Agent。请把需求拆为 2 到 6 个可验证任务并初步分配。每项任务须包含 title、description、assignee、estimate、priority。请只输出 JSON：{summary, reasoning, tasks}。",
    }
    manager_result = complete(provider, "你负责敏捷开发团队的规划与仲裁。不要声称已执行代码或测试。", json.dumps(manager_prompt, ensure_ascii=False))
    raw_tasks = manager_result.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ModelCallError("项目经理 Agent 未返回可用的任务列表，已停止本次协作")
    existing_names = {member.name for member in members}
    db.session.query(WorkItem).filter_by(sprint_id=sprint.id).delete()
    task_context = []
    for index, raw_task in enumerate(raw_tasks[:6], 1):
        if not isinstance(raw_task, dict):
            continue
        assignee = raw_task.get("assignee", "")
        if assignee not in existing_names:
            assignee = members[(index - 1) % len(members)].name if members else "待分配"
        task = WorkItem(
            sprint_id=sprint.id,
            title=_text(raw_task, "title", f"任务 {index}"),
            description=_text(raw_task, "description", "请开发成员进一步分析实现方案。"),
            priority=_text(raw_task, "priority", "medium")[:16],
            assignee=assignee,
            estimate=_text(raw_task, "estimate", "待评估")[:32],
            status="assigned",
        )
        db.session.add(task)
        task_context.append({"title": task.title, "description": task.description, "assignee": task.assignee, "estimate": task.estimate})
    if not task_context:
        raise ModelCallError("任务列表格式不正确，未创建任何任务")
    sprint.mode = mode
    sprint.status = "planning"
    add_message(sprint, "项目经理 Agent", "manager", "planning", _text(manager_result, "summary", "已完成需求拆分"), _text(manager_result, "reasoning", "项目经理已根据技能和负载形成初步计划。"), "等待开发成员评估")
    db.session.commit()
    ensure_collaboration_running(sprint)

    developer_results = []
    for member in members:
        assigned = [task for task in task_context if task["assignee"] == member.name]
        if not assigned:
            continue
        developer_prompt = {
            "member": {"name": member.name, "skills": member.skills, "specialization": member_specialization(sprint, member), "workload": member.workload},
            "sprint_goal": sprint.goal,
            "assigned_tasks": assigned,
            "instruction": "你是独立开发 Agent。评估能否接受任务，说明风险、依赖与实现计划。只输出 JSON：{summary, accept, reasoning, implementation_plan, risks}。不要宣称已修改任何文件。",
        }
        result = complete(provider, "你是软件开发 Agent。你的结论会交给项目经理仲裁。", json.dumps(developer_prompt, ensure_ascii=False))
        developer_results.append({"member": member.name, "result": result})
        add_message(sprint, f"{member.name} · 开发 Agent", "developer", "assignment", _text(result, "summary", "已评估分配任务"), _text(result, "reasoning", _text(result, "implementation_plan", "已提交实现计划。")), "接受分配" if result.get("accept") is not False else "提出重新分配请求", "项目经理 Agent")
        db.session.commit()
        ensure_collaboration_running(sprint)

    arbitration_prompt = {
        "sprint_goal": sprint.goal,
        "initial_tasks": task_context,
        "developer_feedback": developer_results,
        "instruction": "你是项目经理 Agent。依据开发成员反馈给出最终仲裁。若需要重新分配，final_assignments 中列出 title、assignee、reason；assignee 必须来自团队成员。只输出 JSON：{summary, decision, next_step, final_assignments:[{title,assignee,reason}]}。不能声称代码或测试已经完成。",
    }
    arbitration = complete(provider, "你负责冲突仲裁与任务推进。", json.dumps(arbitration_prompt, ensure_ascii=False))
    reassigned = []
    final_assignments = arbitration.get("final_assignments", [])
    if isinstance(final_assignments, list):
        tasks_by_title = {task.title: task for task in WorkItem.query.filter_by(sprint_id=sprint.id).all()}
        for item in final_assignments:
            if not isinstance(item, dict):
                continue
            task = tasks_by_title.get(item.get("title"))
            assignee = item.get("assignee")
            if task and assignee in existing_names and task.assignee != assignee:
                previous = task.assignee
                task.assignee = assignee
                reassigned.append(f"{task.title}：{previous} → {assignee}")
    sprint.status = "developing"
    decision = _text(arbitration, "decision", "保持当前分配并进入开发。")
    if reassigned:
        decision += "\n已重新分配：" + "；".join(reassigned)
    add_message(sprint, "项目经理 Agent", "manager", "arbitration", _text(arbitration, "summary", "项目经理已完成仲裁"), decision, _text(arbitration, "next_step", "开发 Agent 提交逐文件修改提案"))
    db.session.commit()
    ensure_collaboration_running(sprint)
    return sprint


def _persist_model_file_operations(project, sprint, member, result, source="开发提案"):
    try:
        root, files, source_context, locked_file = read_project_context(project.workspace_path, project.attached_file_path)
    except WorkspaceError as error:
        raise ModelCallError(str(error)) from error
    operations = result.get("file_operations")
    if not isinstance(operations, list):
        return 0
    created = 0
    for item in operations[:8]:
        if not isinstance(item, dict):
            continue
        relative_path = item.get("path")
        content = item.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 300_000:
            continue
        try:
            target = resolve_workspace_file(root, relative_path)
        except WorkspaceError:
            continue
        if locked_file and target != locked_file:
            continue
        action = "write_file" if target.exists() else "create_file"
        db.session.add(FileOperation(
            project_id=project.id,
            requested_by=f"{member.name} · 开发 Agent（{source}）",
            action=action,
            target_path=str(target),
            content=content,
            risk_level="high",
            status="pending",
        ))
        created += 1
    return created


def generate_developer_file_proposals(sprint):
    """Have each assigned developer produce reviewable, workspace-bounded code changes."""
    provider = active_provider()
    if not provider:
        raise ModelCallError("未启用任何模型，无法生成真实代码提案")
    ensure_collaboration_running(sprint)
    project = sprint.project
    try:
        root, files, source_context, locked_file = read_project_context(project.workspace_path, project.attached_file_path)
    except WorkspaceError as error:
        raise ModelCallError(str(error)) from error
    if not files:
        source_context = "（当前项目文件夹为空。请根据任务创建最小可运行的 Python 项目文件。）"
    created = 0
    for member in collaboration_members(sprint):
        tasks = [task for task in sprint.tasks if task.assignee == member.name]
        if not tasks:
            continue
        prompt = {
            "workspace_root": str(root),
            "known_files": files,
            "source_context": source_context,
            "member": {"name": member.name, "skills": member.skills, "specialization": member_specialization(sprint, member)},
            "tasks": [{"title": task.title, "description": task.description} for task in tasks],
            "instruction": "你是开发 Agent。根据已接入项目源码，为分配任务提出最小可运行的代码修改。只能修改或新建 workspace_root 内的文本文件。不要执行命令、不要删除文件。严格只输出 JSON：{summary, reasoning, file_operations:[{path:'相对路径', content:'完整文件内容', reason:'修改原因'}]}。file_operations 可为空，但不得声称已经写入文件。",
        }
        result = complete(provider, "你是谨慎的 Python 开发 Agent。你只能生成待管理员审批的完整文件内容。", json.dumps(prompt, ensure_ascii=False))
        count = _persist_model_file_operations(project, sprint, member, result)
        created += count
        add_message(
            sprint,
            f"{member.name} · 开发 Agent",
            "developer",
            "developing",
            _text(result, "summary", "已分析源码并提交修改提案"),
            _text(result, "reasoning", f"已向管理员提交 {count} 个逐文件修改提案。"),
            f"等待管理员审批 {count} 个文件操作",
        )
        db.session.commit()
        ensure_collaboration_running(sprint)
    if not created:
        raise ModelCallError("开发 Agent 未生成符合安全规则的文件修改提案")
    db.session.commit()
    return created


def generate_repair_proposals(sprint, test_output):
    """Tester feedback becomes the developer's input; proposals remain pending for approval."""
    provider = active_provider()
    if not provider:
        add_message(sprint, "测试 Agent", "tester", "testing", "真实测试失败，等待人工修复", test_output[-1_500:], "未配置模型，无法自动生成修复提案")
        db.session.commit()
        return 0
    project = sprint.project
    try:
        root, files, source_context, locked_file = read_project_context(project.workspace_path, project.attached_file_path)
    except WorkspaceError as error:
        raise ModelCallError(str(error)) from error
    members = collaboration_members(sprint)
    if not members:
        raise ModelCallError("项目没有可用开发 Agent")
    tester_prompt = {
        "test_output": test_output[-6_000:],
        "tasks": [{"title": task.title, "description": task.description} for task in sprint.tasks],
        "instruction": "你是测试 Agent。依据真实 pytest 输出定位故障，提出不超过三项可验证的修复建议。只输出 JSON：{summary, failure_analysis, repair_focus}。",
    }
    tester_result = complete(provider, "你是严谨的软件测试 Agent。不能伪造未出现的测试结果。", json.dumps(tester_prompt, ensure_ascii=False))
    add_message(sprint, "测试 Agent", "tester", "testing", _text(tester_result, "summary", "已分析真实测试失败"), _text(tester_result, "failure_analysis", test_output[-1_000:]), "将分析结果交给开发 Agent")
    member = min(members, key=lambda candidate: candidate.workload)
    developer_prompt = {
        "workspace_root": str(root),
        "known_files": files,
        "source_context": source_context,
        "member": {"name": member.name, "skills": member_specialization(sprint, member) or member.skills},
        "tester_feedback": tester_result,
        "repair_round": sprint.repair_round,
        "instruction": "你是开发 Agent。根据测试 Agent 的真实失败分析，提出最小范围修复。只能修改或新建 workspace_root 内的文本文件；不能执行命令或删除文件。严格只输出 JSON：{summary, reasoning, file_operations:[{path:'相对路径', content:'完整文件内容', reason:'修复原因'}]}。",
    }
    developer_result = complete(provider, "你是负责修复 Python 测试失败的开发 Agent。只提交待审批文件提案。", json.dumps(developer_prompt, ensure_ascii=False))
    count = _persist_model_file_operations(project, sprint, member, developer_result, source=f"第 {sprint.repair_round} 轮修复")
    add_message(sprint, f"{member.name} · 开发 Agent", "developer", "rework", _text(developer_result, "summary", "已提交修复提案"), _text(developer_result, "reasoning", f"根据测试反馈提交 {count} 个文件修复提案。"), "等待管理员逐文件审批")
    db.session.commit()
    return count


def generate_retrospective(sprint):
    provider = active_provider()
    if not provider:
        return None
    summary = [{"sender": message.sender, "stage": message.stage, "summary": message.summary, "decision": message.decision} for message in sprint.messages[-20:]]
    prompt = {
        "goal": sprint.goal,
        "repair_round": sprint.repair_round,
        "collaboration_events": summary,
        "instruction": "你是回顾 Agent。依据真实协作记录形成简洁客观的迭代复盘。只输出 JSON：{summary, outcomes, improvements}，不能虚构未发生的事实。",
    }
    result = complete(provider, "你是敏捷迭代回顾 Agent。", json.dumps(prompt, ensure_ascii=False))
    add_message(sprint, "回顾 Agent", "reviewer", "reviewing", _text(result, "summary", "已生成迭代复盘"), _text(result, "outcomes", "请查看协作时间线和测试记录。") + "\n改进建议：" + _text(result, "improvements", "下次迭代在规划阶段提前定义验收标准。"), "迭代完成")
    sprint.status = "done"
    db.session.commit()
    return result
