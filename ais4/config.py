from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIS4_", env_file=".env", extra="ignore")

    db_admin_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ais4"
    db_host: str = "localhost"
    db_porta: int = 5432
    db_nome: str = "ais4"
    senha_app: dict[str, str] = {}

    artefatos_dir: str = "artefatos"
    usuarios_arquivo: str = "config/usuarios.toml"

    max_iteracoes_autocritica: int = 3
    max_tentativas_etapa: int = 3

    # PROVISÓRIO: pontos em aberto da especificação
    promocao_min_projetos: int = 3  # PROVISÓRIO
    promocao_nota_min: float = 4.0  # PROVISÓRIO
    limiar_duplicidade: float = 0.85  # PROVISÓRIO
    orcamento_padrao_usd: float = 50  # PROVISÓRIO
    orcamento_padrao_tokens: int = 20_000_000  # PROVISÓRIO
    max_projetos_em_execucao: int = 1  # PROVISÓRIO

    modelo_padrao: str = "claude-opus-5-5"
    modelo_por_papel: dict[str, str] = {}
    effort_por_papel: dict[str, str] = {}

    embedding_modelo: str = "voyage-multilingual-2"
    voyage_api_key: str = ""


@lru_cache
def obter_config() -> Config:
    return Config()
