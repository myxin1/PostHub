from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("ADMIN", "USER", name="userrole"), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("plan", sa.String(length=64), nullable=False),
        sa.Column("limits_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "integrations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.Enum("WORDPRESS", "FACEBOOK", "INSTAGRAM", "GEMINI", name="integrationtype"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.Enum("CONNECTED", "EXPIRED", "ERROR", name="integrationstatus"), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_integrations_user_id", "integrations", ["user_id"], unique=False)

    op.create_table(
        "automation_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("schedule_config_json", sa.JSON(), nullable=False),
        sa.Column("anti_block_config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_automation_profiles_user_id", "automation_profiles", ["user_id"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("automation_profiles.id"), nullable=False),
        sa.Column("type", sa.Enum("URL", "RSS", name="sourcetype"), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sources_profile_id", "sources", ["profile_id"], unique=False)

    op.create_table(
        "ai_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("destination", sa.Enum("WORDPRESS", "FACEBOOK", "INSTAGRAM", name="actiondestination"), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ai_actions_user_id", "ai_actions", ["user_id"], unique=False)

    op.create_table(
        "collected_contents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("automation_profiles.id"), nullable=False),
        sa.Column("source_id", sa.String(length=36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("raw_html", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("lead_image_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_content_fingerprint"),
    )
    op.create_index("ix_collected_contents_user_id", "collected_contents", ["user_id"], unique=False)
    op.create_index("ix_collected_contents_profile_id", "collected_contents", ["profile_id"], unique=False)
    op.create_index("ix_collected_contents_source_id", "collected_contents", ["source_id"], unique=False)
    op.create_index("ix_collected_contents_fingerprint", "collected_contents", ["fingerprint"], unique=False)

    op.create_table(
        "posts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("automation_profiles.id"), nullable=False),
        sa.Column("collected_content_id", sa.String(length=36), sa.ForeignKey("collected_contents.id"), nullable=False),
        sa.Column("status", sa.Enum("pending", "processing", "completed", "failed", name="poststatus"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("wp_post_id", sa.Integer(), nullable=True),
        sa.Column("wp_url", sa.Text(), nullable=True),
        sa.Column("outputs_json", sa.JSON(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("categories_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_posts_user_id", "posts", ["user_id"], unique=False)
    op.create_index("ix_posts_profile_id", "posts", ["profile_id"], unique=False)
    op.create_index("ix_posts_collected_content_id", "posts", ["collected_content_id"], unique=False)
    op.create_index("ix_posts_status", "posts", ["status"], unique=False)
    op.create_index("ix_posts_scheduled_for", "posts", ["scheduled_for"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("automation_profiles.id"), nullable=True),
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "succeeded", "failed", name="jobstatus"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"], unique=False)
    op.create_index("ix_jobs_post_id", "jobs", ["post_id"], unique=False)
    op.create_index("ix_jobs_type", "jobs", ["type"], unique=False)
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)
    op.create_index("ix_jobs_run_at", "jobs", ["run_at"], unique=False)
    op.create_index("ix_jobs_locked_at", "jobs", ["locked_at"], unique=False)

    op.create_table(
        "job_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("automation_profiles.id"), nullable=True),
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_job_logs_user_id", "job_logs", ["user_id"], unique=False)
    op.create_index("ix_job_logs_post_id", "job_logs", ["post_id"], unique=False)
    op.create_index("ix_job_logs_stage", "job_logs", ["stage"], unique=False)
    op.create_index("ix_job_logs_status", "job_logs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("job_logs")
    op.drop_table("jobs")
    op.drop_table("posts")
    op.drop_table("collected_contents")
    op.drop_table("ai_actions")
    op.drop_table("sources")
    op.drop_table("automation_profiles")
    op.drop_table("integrations")
    op.drop_table("user_settings")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS userrole")
    op.execute("DROP TYPE IF EXISTS integrationtype")
    op.execute("DROP TYPE IF EXISTS integrationstatus")
    op.execute("DROP TYPE IF EXISTS sourcetype")
    op.execute("DROP TYPE IF EXISTS actiondestination")
    op.execute("DROP TYPE IF EXISTS poststatus")
    op.execute("DROP TYPE IF EXISTS jobstatus")

