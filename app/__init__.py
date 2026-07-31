import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import inspect, text

from .models import Project, Sprint, db

logger = logging.getLogger(__name__)

DEFAULT_SECRET_KEY = "dev-only-change-before-deploy"


def _warn_about_default_secret_key():
    logger.warning(
        "\n" + "=" * 72 + "\n"
        "[安全警告] 正在使用默认 SECRET_KEY。\n"
        "该密钥用于会话签名和 API Key 加密，生产环境必须替换。\n"
        "请在 .env 文件中配置：SECRET_KEY=你的随机长字符串\n"
        + "=" * 72
    )


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    secret_key = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
    if secret_key == DEFAULT_SECRET_KEY and not (test_config or {}).get("TESTING"):
        _warn_about_default_secret_key()
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///agilesprint.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PORT=int(os.getenv("PORT", "5000")),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)
        project_columns = {column["name"] for column in inspector.get_columns("project")}
        if "attached_file_path" not in project_columns:
            db.session.execute(text("ALTER TABLE project ADD COLUMN attached_file_path VARCHAR(500) DEFAULT ''"))
        sprint_columns = {column["name"] for column in inspector.get_columns("sprint")}
        if "last_error" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN last_error TEXT DEFAULT ''"))
        if "last_failed_stage" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN last_failed_stage VARCHAR(80) DEFAULT ''"))
        if "task_type" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN task_type VARCHAR(32) DEFAULT 'development'"))
        if "team_config" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN team_config TEXT DEFAULT ''"))
        if "assigned_team_id" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN assigned_team_id INTEGER"))
        if "stop_requested" not in sprint_columns:
            db.session.execute(text("ALTER TABLE sprint ADD COLUMN stop_requested BOOLEAN DEFAULT 0"))
        db.session.commit()
        from .services import ensure_seed_data
        ensure_seed_data()

    from .routes import web
    app.register_blueprint(web)

    @app.context_processor
    def navigation_context():
        """Small, always-available task list for the desktop chat sidebar."""
        return {
            "sidebar_sprints": Sprint.query.order_by(Sprint.updated_at.desc()).limit(18).all(),
            "sidebar_projects": Project.query.order_by(Project.updated_at.desc()).limit(8).all(),
        }

    return app
