from pathlib import Path

from app import create_app
from app.models import FileOperation, ModelUsage, Project, ProviderConfig, Sprint, TeamMember, TestRun as TestRunModel, WorkItem, db
from app.security import encrypt_secret
from app.services import generate_developer_file_proposals, run_model_collaboration
from app.artifacts import export_excel_tasks, export_markdown_summary, export_word_test_report
from app.llm import complete


def app_client(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "SECRET_KEY": "test",
    })
    return app.test_client(), app


def login(client):
    return client.post("/login", data={"username": "admin", "password": "Admin@12345"}, follow_redirects=True)


def test_login_and_dashboard(tmp_path):
    client, _ = app_client(tmp_path)
    response = login(client)
    assert response.status_code == 200
    assert "学生社团报名系统" in response.get_data(as_text=True)


def test_create_project_and_run_collaboration(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    response = client.post("/projects/new", data={
        "name": "图书借阅系统",
        "description": "学生项目",
        "goal": "增加借阅登记与归还提醒",
        "workspace_path": "",
    }, follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        project = Project.query.filter_by(name="图书借阅系统").first()
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        sprint_id = sprint.id
    response = client.post(f"/sprints/{sprint_id}/start", data={"mode": "auto"}, follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "迭代复盘已生成" in page
    assert "测试发现一个可修复问题" in page
    with app.app_context():
        sprint = db.session.get(Sprint, sprint_id)
        assert sprint.status == "done"
        assert len(sprint.messages) == 10


def test_deepseek_key_is_encrypted(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    response = client.post("/settings/provider", data={
        "provider": "DeepSeek",
        "api_key": "sk-test-secret-value",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
    }, follow_redirects=True)
    assert "DeepSeek 配置已加密保存并启用" in response.get_data(as_text=True)
    with app.app_context():
        provider = ProviderConfig.query.filter_by(provider="DeepSeek").first()
        assert provider.enabled is True
        assert provider.encrypted_key != "sk-test-secret-value"


def test_file_operation_waits_for_and_requires_approval(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        project_id = project.id
    target = tmp_path / "created-by-approval.txt"
    response = client.post(f"/projects/{project_id}/file-proposals", data={
        "action": "create_file", "target_path": str(target), "content": "approved content",
    }, follow_redirects=True)
    assert "文件操作提案已创建" in response.get_data(as_text=True)
    assert not target.exists()
    with app.app_context():
        operation = FileOperation.query.filter_by(project_id=project_id).first()
        operation_id = operation.id
        assert operation.status == "pending"
    client.post(f"/file-operations/{operation_id}/approve", follow_redirects=True)
    assert target.read_text(encoding="utf-8") == "approved content"


def test_failed_python_test_moves_sprint_to_rework(tmp_path):
    (tmp_path / "test_failure.py").write_text("def test_expected_failure():\n    assert False\n", encoding="utf-8")
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        project.workspace_path = str(tmp_path)
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        project_id, sprint_id = project.id, sprint.id
        db.session.commit()
    response = client.post(f"/sprints/{sprint_id}/run-tests", follow_redirects=True)
    assert "进入第 1 轮修复" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Sprint, sprint_id).status == "rework"


def test_manual_collaboration_advances_one_stage_at_a_time(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        sprint = Sprint.query.first()
        sprint_id = sprint.id
        project_id = sprint.project_id
    client.post(f"/sprints/{sprint_id}/start", data={"mode": "manual"}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(Sprint, sprint_id).status == "planning"
    response = client.post(f"/sprints/{sprint_id}/advance", follow_redirects=True)
    assert "开发成员评估已完成" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Sprint, sprint_id).status == "developing"
        assert db.session.get(Project, project_id) is not None


def test_manual_collaboration_can_switch_to_auto_without_replaying(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        sprint = Sprint.query.first()
        sprint_id = sprint.id
    client.post(f"/sprints/{sprint_id}/start", data={"mode": "manual"}, follow_redirects=True)
    response = client.post(f"/sprints/{sprint_id}/switch-to-auto", follow_redirects=True)
    assert "已从当前进度切换为自动协同" in response.get_data(as_text=True)
    with app.app_context():
        sprint = db.session.get(Sprint, sprint_id)
        assert sprint.mode == "auto"
        assert sprint.status == "done"


def test_model_developer_proposals_are_limited_to_workspace(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("print('old')\n", encoding="utf-8")
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        project.workspace_path = str(tmp_path)
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        db.session.add(ProviderConfig(
            provider="DeepSeek", base_url="https://example.invalid/v1", model_name="mock",
            encrypted_key=encrypt_secret("not-a-real-key"), enabled=True,
        ))
        db.session.add(WorkItem(sprint_id=sprint.id, title="更新主程序", description="修改输出", assignee="林晨", status="assigned"))
        db.session.commit()

        def mocked_complete(*_args, **_kwargs):
            return {"summary": "提交 main.py 修改", "reasoning": "更新输出文本", "file_operations": [
                {"path": "main.py", "content": "print('new')\n", "reason": "验证修改"},
                {"path": "../outside.py", "content": "bad", "reason": "不应被接受"},
            ]}

        monkeypatch.setattr("app.services.complete", mocked_complete)
        count = generate_developer_file_proposals(sprint)
        proposals = FileOperation.query.filter_by(project_id=project.id).all()
        assert count == 1
        assert proposals[0].action == "write_file"
        assert proposals[0].target_path == str(tmp_path / "main.py")


def test_export_project_artifacts(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        project.workspace_path = str(tmp_path)
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        db.session.add(WorkItem(sprint_id=sprint.id, title="导出任务", description="用于验证表格", assignee="林晨"))
        db.session.commit()
        markdown = export_markdown_summary(project, sprint)
        word = export_word_test_report(project, sprint, [])
        excel = export_excel_tasks(project, sprint)
        assert markdown.exists() and markdown.suffix == ".md"
        assert word.exists() and word.suffix == ".docx"
        assert excel.exists() and excel.suffix == ".xlsx"


def test_enabling_another_provider_disables_previous_provider(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    client.post("/settings/provider", data={
        "provider": "DeepSeek", "api_key": "sk-first", "base_url": "", "model_name": "",
    })
    response = client.post("/settings/provider", data={
        "provider": "OpenAI", "api_key": "sk-second", "base_url": "https://example.invalid/v1", "model_name": "mock-model",
    }, follow_redirects=True)
    assert "OpenAI 配置已加密保存并启用" in response.get_data(as_text=True)
    with app.app_context():
        deepseek = ProviderConfig.query.filter_by(provider="DeepSeek").first()
        openai = ProviderConfig.query.filter_by(provider="OpenAI").first()
        assert deepseek.enabled is False
        assert openai.enabled is True


def test_admin_can_manage_team_reassign_and_create_next_sprint(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        task = WorkItem(sprint_id=sprint.id, title="待分配任务", description="验证手动管理", assignee="林晨")
        db.session.add(task)
        db.session.commit()
        project_id, task_id = project.id, task.id
    client.post(f"/projects/{project_id}/members", data={"name": "王浩", "skills": "Python、接口", "workload": "0"})
    response = client.post(f"/tasks/{task_id}/reassign", data={"assignee": "王浩"}, follow_redirects=True)
    assert "任务负责人已更新" in response.get_data(as_text=True)
    response = client.post(f"/projects/{project_id}/sprints", data={"name": "迭代 2", "goal": "增加管理员报表"}, follow_redirects=True)
    assert "已创建下一次迭代" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(WorkItem, task_id).assignee == "王浩"
        assert TeamMember.query.filter_by(project_id=project_id, name="王浩").first() is not None
        assert Sprint.query.filter_by(project_id=project_id, name="迭代 2").first() is not None


def test_manager_agent_arbitration_can_reassign_tasks(tmp_path, monkeypatch):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        db.session.add(ProviderConfig(provider="DeepSeek", encrypted_key=encrypt_secret("mock"), enabled=True))
        db.session.commit()
        results = iter([
            {"summary": "已拆分任务", "reasoning": "按技能分配", "tasks": [
                {"title": "接口任务", "description": "实现接口", "assignee": "林晨", "estimate": "2 小时", "priority": "high"},
                {"title": "页面任务", "description": "实现页面", "assignee": "陈悦", "estimate": "2 小时", "priority": "medium"},
            ]},
            {"summary": "林晨任务冲突", "accept": False, "reasoning": "当前负载较高"},
            {"summary": "陈悦接受", "accept": True, "reasoning": "可以承担"},
            {"summary": "已重新分配", "decision": "将接口任务交给周宁", "next_step": "进入开发", "final_assignments": [
                {"title": "接口任务", "assignee": "周宁", "reason": "负载更低"}
            ]},
        ])
        monkeypatch.setattr("app.services.complete", lambda *_args, **_kwargs: next(results))
        run_model_collaboration(sprint)
        task = WorkItem.query.filter_by(sprint_id=sprint.id, title="接口任务").first()
        assert task.assignee == "周宁"
        assert "已重新分配" in sprint.messages[-1].content


def test_single_file_attachment_limits_model_proposals_to_that_file(tmp_path, monkeypatch):
    selected = tmp_path / "selected.py"
    sibling = tmp_path / "sibling.py"
    selected.write_text("print('old')\n", encoding="utf-8")
    sibling.write_text("print('keep')\n", encoding="utf-8")
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        project = Project.query.first()
        project.workspace_path = ""
        project.attached_file_path = str(selected)
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        db.session.add(ProviderConfig(provider="DeepSeek", encrypted_key=encrypt_secret("mock"), enabled=True))
        db.session.add(WorkItem(sprint_id=sprint.id, title="仅改一个文件", description="验证单文件限制", assignee="林晨"))
        db.session.commit()
        monkeypatch.setattr("app.services.complete", lambda *_args, **_kwargs: {
            "summary": "提交两个提案", "reasoning": "仅第一个应被接受", "file_operations": [
                {"path": "selected.py", "content": "print('new')\n"},
                {"path": "sibling.py", "content": "print('should not change')\n"},
            ]
        })
        assert generate_developer_file_proposals(sprint) == 1
        proposal = FileOperation.query.filter_by(project_id=project.id).first()
        assert proposal.target_path == str(selected)
        assert proposal.action == "write_file"


def test_model_connection_button_reports_success_without_exposing_key(tmp_path, monkeypatch):
    client, app = app_client(tmp_path)
    login(client)
    client.post("/settings/provider", data={
        "provider": "DeepSeek", "api_key": "sk-test", "base_url": "", "model_name": "",
    })
    monkeypatch.setattr("app.routes.check_connection", lambda provider: "deepseek-v4-flash")
    response = client.post("/settings/provider/test", data={"provider": "DeepSeek"}, follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "模型连接成功：DeepSeek / deepseek-v4-flash" in page
    assert "sk-test" not in page


def test_approve_all_file_operations_continues_after_a_failed_item(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    existing = tmp_path / "already-exists.txt"
    existing.write_text("keep", encoding="utf-8")
    created = tmp_path / "approved-in-batch.txt"
    with app.app_context():
        project = Project.query.first()
        project_id = project.id
        db.session.add_all([
            FileOperation(
                project_id=project_id,
                requested_by="测试开发 Agent",
                action="create_file",
                target_path=str(existing),
                content="must not overwrite",
                risk_level="high",
            ),
            FileOperation(
                project_id=project_id,
                requested_by="测试开发 Agent",
                action="create_file",
                target_path=str(created),
                content="created by one-click approval",
                risk_level="high",
            ),
        ])
        db.session.commit()

    response = client.post(f"/projects/{project_id}/file-operations/approve-all", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("#file-operations")
    assert created.read_text(encoding="utf-8") == "created by one-click approval"
    assert existing.read_text(encoding="utf-8") == "keep"
    with app.app_context():
        operations = FileOperation.query.filter_by(project_id=project_id).order_by(FileOperation.id.asc()).all()
        assert {operation.status for operation in operations} == {"failed", "applied"}


def test_model_json_length_failure_is_retried_with_more_output_space(tmp_path, monkeypatch):
    client, app = app_client(tmp_path)
    del client
    requests_seen = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    responses = iter([
        FakeResponse({"choices": [{"finish_reason": "length", "message": {"content": '{"summary":"truncated'}}]}),
        FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "```json\n{\"summary\": \"complete\"}\n```"}}]}),
    ])

    def mocked_post(*_args, **kwargs):
        requests_seen.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr("app.llm.requests.post", mocked_post)
    monkeypatch.setattr("app.llm.time.sleep", lambda *_args: None)
    with app.app_context():
        provider = ProviderConfig(
            provider="DeepSeek",
            encrypted_key=encrypt_secret("sk-test"),
            enabled=True,
        )
        result = complete(provider, "Return a json object.", "Return file_operations as json.")

    assert result == {"summary": "complete"}
    assert len(requests_seen) == 2
    assert requests_seen[0]["max_tokens"] == 7000
    assert requests_seen[1]["max_tokens"] == 12000


def test_dashboard_has_onboarding_and_model_usage_summary(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    with app.app_context():
        db.session.add(ModelUsage(provider="DeepSeek", model_name="mock", success=True, attempts=1, duration_ms=123, prompt_tokens=100, completion_tokens=30, total_tokens=130))
        db.session.commit()
    response = client.get("/")
    page = response.get_data(as_text=True)
    assert "新手演示流程" in page
    assert "模型调用统计" in page
    assert "130 Token" in page


def test_launch_demo_creates_a_safe_reusable_demo_project(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    response = client.post("/demo/launch", follow_redirects=True)
    assert "演示案例已恢复到初始状态" in response.get_data(as_text=True)
    with app.app_context():
        project = Project.query.filter_by(name="社团报名系统智能迭代演示").first()
        assert project is not None
        assert (Path(project.workspace_path) / "registration.py").exists()
        assert Sprint.query.filter_by(project_id=project.id).count() == 1


def test_selected_file_operations_can_be_approved_in_a_batch(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    with app.app_context():
        project = Project.query.first()
        project_id = project.id
        operations = [
            FileOperation(project_id=project_id, action="create_file", target_path=str(first), content="one", status="pending"),
            FileOperation(project_id=project_id, action="create_file", target_path=str(second), content="two", status="pending"),
        ]
        db.session.add_all(operations)
        db.session.commit()
        ids = [operation.id for operation in operations]
    response = client.post(f"/projects/{project_id}/file-operations/approve-selected", data={"operation_ids": [str(value) for value in ids]}, follow_redirects=True)
    assert "已处理勾选的 2 项" in response.get_data(as_text=True)
    assert first.read_text(encoding="utf-8") == "one"
    assert second.read_text(encoding="utf-8") == "two"


def test_delete_project_removes_only_system_records_not_local_workspace(tmp_path):
    client, app = app_client(tmp_path)
    login(client)
    local_file = tmp_path / "keep-this-code.py"
    local_file.write_text("print('local file stays')\n", encoding="utf-8")
    with app.app_context():
        project = Project.query.first()
        project_id = project.id
        project.workspace_path = str(tmp_path)
        sprint = Sprint.query.filter_by(project_id=project.id).first()
        db.session.add(FileOperation(project_id=project.id, action="create_file", target_path=str(tmp_path / "record.txt")))
        db.session.commit()
        sprint_id = sprint.id
    response = client.post(f"/projects/{project_id}/delete", follow_redirects=True)
    assert "本地文件夹和代码没有被删除" in response.get_data(as_text=True)
    assert local_file.exists()
    with app.app_context():
        assert db.session.get(Project, project_id) is None
        assert TestRunModel.query.filter_by(sprint_id=sprint_id).count() == 0
        assert FileOperation.query.filter_by(project_id=project_id).count() == 0
