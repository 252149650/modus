"""Public facade for Modus' ownership-aware project scaffolding.

Import consumers through this facade; catalog validation, write ownership and
platform projection keep their own implementation modules and lifetimes.
"""

from .catalog import (
    TemplateCatalog,
    TemplateCatalogError,
    TemplateFormatError,
    TemplateNotFoundError,
)
from .project import (
    ProjectScaffolder,
    ScaffoldRequest,
    ScaffoldStateError,
    load_commands,
    load_skills,
    scaffold_project,
    templates_dir,
)
from .writer import AssetOwnership, ScaffoldAsset, ScaffoldWriteError, ScaffoldWriter

__all__ = [
    "AssetOwnership",
    "ProjectScaffolder",
    "ScaffoldAsset",
    "ScaffoldRequest",
    "ScaffoldStateError",
    "ScaffoldWriteError",
    "ScaffoldWriter",
    "TemplateCatalog",
    "TemplateCatalogError",
    "TemplateFormatError",
    "TemplateNotFoundError",
    "load_commands",
    "load_skills",
    "scaffold_project",
    "templates_dir",
]
