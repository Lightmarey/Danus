from pathlib import Path
from shutil import copy2, copytree, rmtree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        source = root / ".agents" / "skills"
        target = Path(self.build_lib) / "danus" / "_authoring_assets"
        rmtree(target, ignore_errors=True)
        human = target / "human-summary"
        human.mkdir(parents=True, exist_ok=True)
        for name in (
            "REPORT_WRITER_PROMPT.md", "md2html.js", "package.json",
            "package-lock.json",
        ):
            copy2(source / "human-summary" / name, human)
        paper = target / "write-paper"
        for name in ("boilerplate", "roles", "style", "templates"):
            copytree(source / "write-paper" / name, paper / name, dirs_exist_ok=True)

        agent_target = Path(self.build_lib) / "danus" / "_agent_assets"
        rmtree(agent_target, ignore_errors=True)
        contracts = agent_target / "contracts"
        contracts.mkdir(parents=True)
        for name in ("worker.md", "verifier.md"):
            copy2(root / "agents" / "contracts" / name, contracts)
        for role, script in (
            ("worker", "check_conformance.py"),
            ("verify", "test_verification_schema.py"),
        ):
            source_role = root / "agents" / "skills" / role
            target_role = agent_target / "skills" / role
            target_role.mkdir(parents=True)
            copy2(source_role / script, target_role)
            for skill in source_role.iterdir():
                if not skill.is_dir() or skill.name == "__pycache__":
                    continue
                (target_role / skill.name / "agents").mkdir(parents=True)
                copy2(skill / "SKILL.md", target_role / skill.name / "SKILL.md")
                copy2(
                    skill / "agents" / "openai.yaml",
                    target_role / skill.name / "agents" / "openai.yaml",
                )


setup(cmdclass={"build_py": BuildPy})
