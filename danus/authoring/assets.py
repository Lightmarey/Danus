from pathlib import Path


def skill_dir(name: str) -> Path:
    repo_asset = Path(__file__).resolve().parents[2] / ".agents" / "skills" / name
    if repo_asset.is_dir():
        return repo_asset
    return Path(__file__).resolve().parents[1] / "_authoring_assets" / name
