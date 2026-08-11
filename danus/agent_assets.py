"""Resolve Codex worker/verifier assets from a checkout or installed package."""

from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "agents"
_PACKAGED_ROOT = Path(__file__).resolve().parent / "_agent_assets"
_CHECKOUT_ROOT = _SOURCE_ROOT.parent
_ROLES = {"worker", "verifier"}


def _resolve(relative: Path) -> Path:
    roots = (
        (_SOURCE_ROOT, _PACKAGED_ROOT)
        if (_CHECKOUT_ROOT / "pyproject.toml").is_file()
        and (_CHECKOUT_ROOT / "AGENTS.md").is_file()
        else (_PACKAGED_ROOT,)
    )
    for root in roots:
        candidate = root / relative
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Danus agent asset is missing: {relative.as_posix()}")


def contract(role: str) -> Path:
    if role not in _ROLES:
        raise ValueError(f"unsupported agent asset role: {role}")
    return _resolve(Path("contracts") / f"{role}.md")


def skills(role: str) -> Path:
    if role == "verifier":
        role = "verify"
    elif role != "worker":
        raise ValueError(f"unsupported agent asset role: {role}")
    return _resolve(Path("skills") / role)
