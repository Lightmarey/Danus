from pathlib import Path
from shutil import copy2, copytree, rmtree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / ".agents" / "skills"
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


setup(cmdclass={"build_py": BuildPy})
