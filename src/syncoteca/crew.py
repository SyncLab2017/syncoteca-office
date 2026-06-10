import json
import os
import litellm
from pathlib import Path
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from dotenv import load_dotenv

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "data" / "knowledge"


def _load_knowledge(agent_name: str) -> str:
    json_path = KNOWLEDGE_DIR / f"{agent_name}.json"
    md_path = KNOWLEDGE_DIR / f"{agent_name}.md"

    entries = []
    if json_path.exists():
        try:
            entries = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    elif md_path.exists():
        content = md_path.read_text(encoding="utf-8").strip()
        if content:
            entries = [{"ts": "—", "text": content}]

    if not entries:
        return ""

    lines = [f"[{e.get('ts','—')}] {e.get('text','')}" for e in entries]
    return "\n\n---\n## АКТУАЛЬНЫЕ ЗНАНИЯ (загружено руководителем):\n" + "\n\n".join(lines)

litellm.num_retries = 8
litellm.retry_after = 30
litellm.retryable_error_types = ["overloaded_error", "rate_limit_error", "service_unavailable_error"]

from .tools import (
    SearchRightsHolderTool,
    EmailDraftTool,
    DocumentTool,
    DatabaseTool,
    RoyaltyCalculatorTool,
    MetadataTool,
    SupabaseTool,
    AsanaSearchTool,
)

load_dotenv(override=True)

CONFIG_DIR = Path(__file__).parent / "config"


def _make_step_callback(agent_mem_name: str):
    """Returns a step_callback that emits office events."""
    def callback(step_output):
        try:
            from . import events as ev
            msg = str(step_output)[:200] if step_output else "..."
            ev.emit(agent_mem_name, "step", msg, status="working")
        except Exception:
            pass
    return callback


def make_llm(model: str = "anthropic/claude-haiku-4-5-20251001") -> LLM:
    return LLM(
        model=model,
        api_key=os.environ["ANTHROPIC_API_KEY"],
        temperature=0.3,
        max_retries=3,
    )


def make_sonnet() -> LLM:
    return make_llm("anthropic/claude-sonnet-4-6")


@CrewBase
class SyncotecaCrew:
    """Синкотека — Multi-Agent Music Sync Licensing Office."""

    agents_config = str(CONFIG_DIR / "agents.yaml")
    tasks_config = str(CONFIG_DIR / "tasks.yaml")

    # --- Tools (shared instances) ---
    _search = SearchRightsHolderTool()
    _email = EmailDraftTool()
    _doc = DocumentTool()
    _db = DatabaseTool()
    _royalty = RoyaltyCalculatorTool()
    _metadata = MetadataTool()
    _synclab = SupabaseTool()
    _asana_search = AsanaSearchTool()

    # --- Agents ---

    @agent
    def coordinator(self) -> Agent:
        return Agent(
            config=self.agents_config["coordinator"],
            llm=make_llm(),
            verbose=True,
            allow_delegation=True,
        )

    @agent
    def license_manager(self) -> Agent:
        cfg = dict(self.agents_config["license_manager"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("ekaterina")
        return Agent(
            config=cfg,
            llm=make_sonnet(),
            tools=[self._synclab, self._search, self._email, self._db, self._asana_search],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("ekaterina"),
        )

    @agent
    def content_manager(self) -> Agent:
        cfg = dict(self.agents_config["content_manager"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("sasha")
        return Agent(
            config=cfg,
            llm=make_llm(),
            tools=[self._synclab, self._db, self._metadata],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("sasha"),
        )

    @agent
    def accountant(self) -> Agent:
        cfg = dict(self.agents_config["accountant"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("marina")
        return Agent(
            config=cfg,
            llm=make_sonnet(),
            tools=[self._synclab, self._royalty, self._doc, self._db],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("marina"),
        )

    @agent
    def lawyer(self) -> Agent:
        cfg = dict(self.agents_config["lawyer"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("ksusha")
        return Agent(
            config=cfg,
            llm=make_llm(),
            tools=[self._synclab, self._doc, self._search, self._db],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("ksusha"),
        )

    @agent
    def biz_dev(self) -> Agent:
        cfg = dict(self.agents_config["biz_dev"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("biz_dev")
        return Agent(
            config=cfg,
            llm=make_llm(),
            tools=[self._synclab, self._search, self._email, self._db],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("biz_dev"),
        )

    @agent
    def developer(self) -> Agent:
        cfg = dict(self.agents_config["developer"])
        cfg["backstory"] = cfg.get("backstory", "") + _load_knowledge("developer")
        return Agent(
            config=cfg,
            llm=make_llm(),
            tools=[self._synclab, self._db, self._search],
            memory=True,
            verbose=True,
            step_callback=_make_step_callback("developer"),
        )

    # --- Tasks ---

    @task
    def coordinate_request(self) -> Task:
        return Task(
            config=self.tasks_config["coordinate_request"],
            agent=self.coordinator(),
        )

    @task
    def find_rights_holders(self) -> Task:
        return Task(
            config=self.tasks_config["find_rights_holders"],
            agent=self.license_manager(),
        )

    @task
    def draft_licensing_email(self) -> Task:
        return Task(
            config=self.tasks_config["draft_licensing_email"],
            agent=self.license_manager(),
        )

    @task
    def process_track_metadata(self) -> Task:
        return Task(
            config=self.tasks_config["process_track_metadata"],
            agent=self.content_manager(),
        )

    @task
    def calculate_royalties(self) -> Task:
        return Task(
            config=self.tasks_config["calculate_royalties"],
            agent=self.accountant(),
        )

    @task
    def review_contract(self) -> Task:
        return Task(
            config=self.tasks_config["review_contract"],
            agent=self.lawyer(),
        )

    @task
    def draft_license_agreement(self) -> Task:
        return Task(
            config=self.tasks_config["draft_license_agreement"],
            agent=self.lawyer(),
        )

    @task
    def pitch_to_supervisor(self) -> Task:
        return Task(
            config=self.tasks_config["pitch_to_supervisor"],
            agent=self.biz_dev(),
        )

    @task
    def design_database_schema(self) -> Task:
        return Task(
            config=self.tasks_config["design_database_schema"],
            agent=self.developer(),
        )

    # --- Crew presets ---

    @crew
    def licensing_crew(self) -> Crew:
        """Full licensing pipeline: find rights → draft email → review contract → calculate royalties."""
        return Crew(
            agents=[
                self.coordinator(),
                self.license_manager(),
                self.lawyer(),
                self.accountant(),
            ],
            tasks=[
                self.coordinate_request(),
                self.find_rights_holders(),
                self.draft_licensing_email(),
                self.review_contract(),
                self.calculate_royalties(),
            ],
            process=Process.sequential,
            verbose=True,
        )

    @crew
    def content_crew(self) -> Crew:
        """Content pipeline: process metadata → store in database."""
        return Crew(
            agents=[self.content_manager(), self.developer()],
            tasks=[self.process_track_metadata(), self.design_database_schema()],
            process=Process.sequential,
            verbose=True,
        )

    @crew
    def biz_dev_crew(self) -> Crew:
        """BD pipeline: research supervisor → write pitch."""
        return Crew(
            agents=[self.coordinator(), self.biz_dev()],
            tasks=[self.coordinate_request(), self.pitch_to_supervisor()],
            process=Process.sequential,
            verbose=True,
        )

    @crew
    def full_crew(self) -> Crew:
        """All agents, sequential — covers full licensing pipeline."""
        return Crew(
            agents=[
                self.license_manager(),
                self.lawyer(),
                self.accountant(),
                self.biz_dev(),
            ],
            tasks=[
                self.find_rights_holders(),
                self.review_contract(),
                self.calculate_royalties(),
                self.draft_licensing_email(),
            ],
            process=Process.sequential,
            max_rpm=10,
            verbose=True,
        )
