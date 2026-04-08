from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = "dev-secret-key"
    db_path: str = "./data/glassops.db"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    model_config = {"env_prefix": "GLASSOPS_"}


settings = Settings()
