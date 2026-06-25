"""Company config — one object per tenant, holds all paths for that account."""
from dataclasses import dataclass
from pathlib import Path
import json

_BASE = Path(__file__).parent.parent


@dataclass
class Company:
    slug: str
    display_name: str
    _config_dir: Path
    _data_dir: Path

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def config_dir(self):            return self._config_dir
    @property
    def data_dir(self):              return self._data_dir
    @property
    def session_state_file(self):    return self.config_dir / "session_state.json"
    @property
    def credentials_file(self):      return self.config_dir / "credentials.json"
    @property
    def settings_file(self):         return self.config_dir / "settings.json"
    @property
    def message_templates_file(self):return self.config_dir / "message_templates.json"
    @property
    def job_descriptions_file(self): return self.config_dir / "job_descriptions.json"
    @property
    def candidates_dir(self):        return self.data_dir / "candidates"
    @property
    def resumes_dir(self):           return self.data_dir / "resumes"
    @property
    def conversations_dir(self):     return self.data_dir / "conversations"
    @property
    def screenshots_dir(self):       return self.data_dir / "screenshots"
    @property
    def status_counts_file(self):    return self.data_dir / "job_status_counts.json"
    @property
    def candidates_index_file(self): return self.data_dir / "candidates_index.json"
    @property
    def last_run_file(self):         return self.data_dir / "last_run.json"
    @property
    def audit_log_file(self):        return self.data_dir / "audit_log.json"

    def bootstrap(self):
        for d in [self.config_dir, self.candidates_dir, self.resumes_dir,
                  self.conversations_dir, self.screenshots_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> dict:
        try:
            if self.settings_file.exists():
                return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def load_credentials(self) -> dict:
        try:
            if self.credentials_file.exists():
                return json.loads(self.credentials_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}


RMS = Company(
    slug="rms",
    display_name="Reliable Medical Services",
    _config_dir=_BASE / "config",
    _data_dir=_BASE / "data" / "rms",
)

# Speech Masters gets its own subdirectories
SM = Company(
    slug="sm",
    display_name="Speech Masters",
    _config_dir=_BASE / "config" / "sm",
    _data_dir=_BASE / "data" / "sm",
)

COMPANIES: dict[str, Company] = {"rms": RMS, "sm": SM}
