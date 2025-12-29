from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # OpenAI API Settings
    OPENAI_API_KEY: str | None = None # Allow key to be optional to enable server startup
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    OPENAI_MODEL_CHEAT_CHECK: str = "qwen3-235b-a22b"
    
    # Image Generation Settings (optional)
    IMAGE_GEN_MODEL: str | None = None  # 图片生成模型，如 "gpt-image-1" 或其他兼容模型
    IMAGE_GEN_BASE_URL: str | None = None  # 图片生成API地址，为空则使用 OPENAI_BASE_URL
    IMAGE_GEN_API_KEY: str | None = None  # 图片生成API密钥，为空则使用 OPENAI_API_KEY
    IMAGE_GEN_IDLE_SECONDS: int = 10  # 状态静止多少秒后触发图片生成

    # JWT Settings for OAuth2
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 600

    # Database URL
    DATABASE_URL: str = "sqlite:///./veloera.db"

    # Linux.do OAuth Settings
    LINUXDO_CLIENT_ID: str | None = None
    LINUXDO_CLIENT_SECRET: str | None = None
    LINUXDO_SCOPE: str = "read"

    # Server Settings
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    UVICORN_RELOAD: bool = True

    # Point to the .env file in the 'backend' directory relative to the project root
    model_config = SettingsConfigDict(env_file="backend/.env")

# Create a single instance of the settings
settings = Settings()