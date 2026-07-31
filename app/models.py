from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AdminUser(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(80), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Project(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default="")
    workspace_path = db.Column(db.String(500), default="")
    attached_file_path = db.Column(db.String(500), default="")
    status = db.Column(db.String(32), default="draft", nullable=False)
    sprints = db.relationship("Sprint", backref="project", cascade="all, delete-orphan", lazy=True)
    members = db.relationship("TeamMember", backref="project", cascade="all, delete-orphan", lazy=True)


class Sprint(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    goal = db.Column(db.Text, default="")
    status = db.Column(db.String(32), default="draft", nullable=False)
    mode = db.Column(db.String(16), default="manual", nullable=False)
    repair_round = db.Column(db.Integer, default=0, nullable=False)
    task_type = db.Column(db.String(32), default="development", nullable=False)
    assigned_team_id = db.Column(db.Integer, db.ForeignKey("agent_team.id"), nullable=True)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)
    team_config = db.Column(db.Text, default="")
    last_error = db.Column(db.Text, default="")
    last_failed_stage = db.Column(db.String(80), default="")
    tasks = db.relationship("WorkItem", backref="sprint", cascade="all, delete-orphan", lazy=True)
    messages = db.relationship("AgentMessage", backref="sprint", cascade="all, delete-orphan", lazy=True)


class TeamMember(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(32), nullable=False)
    skills = db.Column(db.String(255), default="")
    workload = db.Column(db.Integer, default=0, nullable=False)
    is_agent = db.Column(db.Boolean, default=True, nullable=False)


class AgentTeam(TimestampMixin, db.Model):
    """A reusable AI team that can be selected for any new task."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")
    members = db.relationship("AgentTeamMember", backref="team", cascade="all, delete-orphan", lazy=True)
    assigned_sprints = db.relationship("Sprint", backref="assigned_team", lazy=True)


class AgentTeamMember(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("agent_team.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    skills = db.Column(db.String(255), default="")
    workload = db.Column(db.Integer, default=0, nullable=False)


class WorkItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sprint_id = db.Column(db.Integer, db.ForeignKey("sprint.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(32), default="todo", nullable=False)
    priority = db.Column(db.String(16), default="medium", nullable=False)
    assignee = db.Column(db.String(80), default="")
    estimate = db.Column(db.String(32), default="")
    return_reason = db.Column(db.Text, default="")


class AgentMessage(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sprint_id = db.Column(db.Integer, db.ForeignKey("sprint.id"), nullable=False)
    sender = db.Column(db.String(80), nullable=False)
    sender_role = db.Column(db.String(32), nullable=False)
    receiver = db.Column(db.String(80), default="团队")
    stage = db.Column(db.String(32), nullable=False)
    summary = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    decision = db.Column(db.String(100), default="")


class ProviderConfig(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), unique=True, nullable=False)
    base_url = db.Column(db.String(300), default="")
    model_name = db.Column(db.String(120), default="")
    encrypted_key = db.Column(db.Text, default="")
    enabled = db.Column(db.Boolean, default=False, nullable=False)


class FileOperation(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    requested_by = db.Column(db.String(80), default="开发 Agent", nullable=False)
    action = db.Column(db.String(32), nullable=False)
    target_path = db.Column(db.String(800), nullable=False)
    content = db.Column(db.Text, default="")
    risk_level = db.Column(db.String(16), default="medium", nullable=False)
    status = db.Column(db.String(16), default="pending", nullable=False)
    result = db.Column(db.Text, default="")


class TestRun(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sprint_id = db.Column(db.Integer, db.ForeignKey("sprint.id"), nullable=False)
    command = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    output = db.Column(db.Text, default="")
    repair_round = db.Column(db.Integer, default=0, nullable=False)


class ModelUsage(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), nullable=False)
    model_name = db.Column(db.String(120), default="")
    success = db.Column(db.Boolean, default=False, nullable=False)
    attempts = db.Column(db.Integer, default=1, nullable=False)
    duration_ms = db.Column(db.Integer, default=0, nullable=False)
    prompt_tokens = db.Column(db.Integer, default=0, nullable=False)
    completion_tokens = db.Column(db.Integer, default=0, nullable=False)
    total_tokens = db.Column(db.Integer, default=0, nullable=False)
    error_message = db.Column(db.String(500), default="")
