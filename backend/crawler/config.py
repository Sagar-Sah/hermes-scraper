from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("ATLAS_DATA_DIR", "./data")).resolve()
    )
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    api_token: str = field(default_factory=lambda: os.getenv("ATLAS_API_TOKEN", ""))
    product_concurrency: int = field(
        default_factory=lambda: int(os.getenv("ATLAS_PRODUCT_CONCURRENCY", "100"))
    )
    browser_product_concurrency: int = field(
        default_factory=lambda: int(os.getenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", "4"))
    )
    requests_per_second: float = field(
        default_factory=lambda: float(os.getenv("ATLAS_SOURCE_RPS", "2"))
    )
    source_concurrency: int = field(
        default_factory=lambda: int(os.getenv("ATLAS_SOURCE_CONCURRENCY", "2"))
    )
    cache_seconds: int = field(
        default_factory=lambda: int(os.getenv("ATLAS_CACHE_SECONDS", "86400"))
    )
    persist_browser_sessions: bool = field(
        default_factory=lambda: os.getenv(
            "ATLAS_BROWSER_PERSIST_SESSION", "true"
        ).lower()
        not in {"false", "0", "no"}
    )
    max_response_bytes: int = 32 * 1024 * 1024
    s3_bucket: str = field(default_factory=lambda: os.getenv("ATLAS_S3_BUCKET", ""))
    s3_endpoint: str | None = field(
        default_factory=lambda: os.getenv("ATLAS_S3_ENDPOINT") or None
    )

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = "sqlite:///" + str(self.data_dir / "atlas.db")
        self.product_concurrency = max(1, min(self.product_concurrency, 256))
        self.browser_product_concurrency = max(
            1, min(self.browser_product_concurrency, 256)
        )

    def execution_capacity(self):
        """Startup limits for extraction workers, separate from source pacing."""
        return {
            "product_task_limit": self.product_concurrency,
            "browser_page_limit": self.browser_product_concurrency,
            "effective_browser_page_limit": min(self.product_concurrency, self.browser_product_concurrency),
            "maximum_configured_limit": 256,
            "browser_setting": "ATLAS_BROWSER_PRODUCT_CONCURRENCY",
            "restart_required": True,
            "scope": "per_instance",
        }


settings = Settings()
