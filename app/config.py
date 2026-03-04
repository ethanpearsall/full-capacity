import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "change-me-in-production")
    APP_ENV: str = os.getenv("APP_ENV", "development")
    APP_URL: str = os.getenv("APP_URL", "http://localhost:8000")
    STORAGE_BUCKET: str = os.getenv("STORAGE_BUCKET", "documents")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    EMAIL_WEBHOOK_SECRET: str = os.getenv("EMAIL_WEBHOOK_SECRET", "")
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
    NYLAS_API_KEY: str = os.getenv("NYLAS_API_KEY", "")
    NYLAS_API_URI: str = os.getenv("NYLAS_API_URI", "https://api.us.nylas.com")
    NYLAS_CLIENT_ID: str = os.getenv("NYLAS_CLIENT_ID", "")
    NYLAS_CLIENT_SECRET: str = os.getenv("NYLAS_CLIENT_SECRET", "")
    NYLAS_WEBHOOK_SECRET: str = os.getenv("NYLAS_WEBHOOK_SECRET", "")
    NYLAS_CALLBACK_URI: str = os.getenv("NYLAS_CALLBACK_URI", "")


settings = Settings()
